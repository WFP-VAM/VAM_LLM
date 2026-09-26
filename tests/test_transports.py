"""One implementation per endpoint: the pages' in-process calls and HTTP clients get the same answers."""
import io
import json
import logging

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.api import app
from app.services.mfi_drafter import router as mfi_router
from app.services.mfi_drafter.synthetic_fixtures import SyntheticSpec, build_csv_bytes
from app.shared.llm import LLMCallError
from app.shared.runs import report_runs
from app.streamlit_backend.dispatcher import dispatch_request

HTTP = TestClient(app, raise_server_exceptions=False)


def both(method, path, **kwargs):
    """The same request over HTTP and through the pages' in-process dispatcher."""
    http = HTTP.request(method, path, **kwargs)
    local = dispatch_request(method, path, json_body=kwargs.get("json"), files=kwargs.get("files"),
                             data=kwargs.get("data"), params=kwargs.get("params"))
    return http, local


@pytest.mark.parametrize("path", ["/", "/health", "/market-monitor/info", "/market-monitor/health",
                                  "/mfi-drafter/info", "/mfi-drafter/health", "/mfi-drafter/dimensions",
                                  "/mfi-drafter/sample-markets", "/seasonal-outlook/info",
                                  "/market-monitor/status/no-such-run", "/mfi-drafter/result/no-such-run",
                                  "/market-monitor/no-such-endpoint"])
def test_read_only_endpoints_answer_the_same_over_both_transports(path):
    http, local = both("GET", path)
    assert http.status_code == local.status_code
    assert http.json() == local.json()


def test_invalid_input_is_refused_the_same_way():
    http, local = both("POST", "/market-monitor/generate-async", json={"country": 5})
    assert http.status_code == local.status_code == 422
    assert http.json() == local.json()


def test_uploads_and_downloads_pass_through_unchanged(monkeypatch):
    csv = build_csv_bytes(SyntheticSpec(market_count=1, region_count=1))
    http, local = both("POST", "/mfi-drafter/validate-csv", files={"file": ("mfi.csv", csv, "text/csv")})
    assert http.status_code == local.status_code == 200 and http.json() == local.json()

    report_runs.create_run("export-run", service="market-monitor")
    report_runs.set_run_completed("export-run", result={"country": "Testland", "time_period": "2025-01", "language": "en",
                                                        "report_draft_sections": {"HIGHLIGHTS": "Text."}, "visualizations": {}})
    http, local = both("POST", "/market-monitor/export-docx/export-run", json={})
    assert http.status_code == local.status_code == 200
    assert local.headers["Content-Type"] == http.headers["content-type"]
    texts = [[p.text for p in Document(io.BytesIO(reply.content)).paragraphs] for reply in (http, local)]
    assert texts[0] == texts[1] and texts[0]


def test_form_fields_are_sent_as_text_and_unset_fields_are_left_out(monkeypatch):
    received = {}

    def capture(**kwargs):
        received.update(kwargs)
        raise ValueError("stop here")
    monkeypatch.setenv("MFI_DRAFTER_ANALYSIS_VERSION", "2")
    monkeypatch.setattr(mfi_router, "load_mfi_from_csv", capture)
    reply = dispatch_request("POST", "/mfi-drafter/generate-from-csv", files={"file": ("mfi.csv", b"x", "text/csv")},
                             data={"country_override": "Testland", "data_collection_start_override": None})
    assert reply.status_code == 400
    assert (received["country_override"], received["start_date_override"]) == ("Testland", None)


@pytest.mark.parametrize("error,status", [
    (ValueError("a renderer failed"), 500),
    (LLMCallError(failure_code="llm_transport_error", call_id="llm-0001-x", node="draft_markets",
                  operation="mfi.light.draft_markets.v1", stage="transport"), 502),
])
def test_a_drafting_failure_is_not_reported_as_a_csv_error(monkeypatch, error, status):
    monkeypatch.setenv("MFI_DRAFTER_ANALYSIS_VERSION", "2")
    monkeypatch.setattr(mfi_router, "load_mfi_from_csv", lambda **kwargs: {
        "country": "Testland", "data_collection_start": "2026-01-01", "data_collection_end": "2026-01-31",
        "markets": ["Central"], "survey_metadata": {}})

    def fail(**kwargs):
        raise error
    monkeypatch.setattr(mfi_router, "run_mfi_report_generation", fail)
    reply = dispatch_request("POST", "/mfi-drafter/generate-from-csv", files={"file": ("mfi.csv", b"x", "text/csv")})
    assert reply.status_code == status
    detail = reply.json()["detail"]
    assert (detail["failure_code"] == "llm_transport_error") if status == 502 else detail == "a renderer failed"


def test_an_unhandled_error_becomes_a_500_with_its_message(monkeypatch):
    def broken():
        raise RuntimeError("service info broke")
    monkeypatch.setattr(mfi_router, "service_info", broken)
    reply = dispatch_request("GET", "/mfi-drafter/info")
    assert reply.status_code == 500 and "service info broke" in reply.json()["detail"]


def test_in_process_calls_do_not_fill_the_log(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        dispatch_request("GET", "/health")
    assert not [record for record in caplog.records if record.name == "httpx"]
