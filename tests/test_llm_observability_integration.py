import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.shared import async_runs
from app.shared.llm_observability import LLMCallError
from app.services.market_monitor import router as market_router
from app.services.mfi_drafter import router as mfi_router
from app.services.mfi_drafter.features import MFI_DRAFTER_ANALYSIS_VERSION_ENV
from app.streamlit_backend import dispatcher


class ImmediateThread:
    def __init__(self, *, target, daemon):
        self.target = target

    def start(self):
        self.target()


MFI_CSV = {"country": "Testland", "data_collection_start": "2026-01-01", "data_collection_end": "2026-01-31",
           "markets": ["Central"], "survey_metadata": {"collection_period": "2026-01-01 to 2026-01-31"}}


@pytest.fixture(autouse=True)
def reset_memory_runs(monkeypatch):
    monkeypatch.setattr(async_runs, "_BACKEND", "memory")
    async_runs._RUNS.clear()
    async_runs._RUN_ARTIFACTS.clear()
    monkeypatch.setattr(
        dispatcher, "threading", SimpleNamespace(Thread=ImmediateThread)
    )


@pytest.fixture
def mfi_csv_upload(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")
    monkeypatch.setattr(dispatcher, "_extract_file", lambda *args: SimpleNamespace(filename="mfi.csv", content=b"csv"))
    monkeypatch.setattr(dispatcher, "load_mfi_from_csv", lambda **kwargs: dict(MFI_CSV))
    monkeypatch.setattr(mfi_router, "load_mfi_from_csv", lambda **kwargs: dict(MFI_CSV))


def _trace(service, run_id, status, total_calls=0):
    return {
        "trace_schema_version": "1.0",
        "service": service,
        "run_id": run_id,
        "status": status,
        "current_call_id": None,
        "total_calls": total_calls,
        "succeeded_calls": 0,
        "failed_calls": 0,
        "contract_failed_calls": 0,
        "payload_capture_enabled": False,
        "payload_storage_configured": False,
        "payload_persistence_failures": 0,
        "calls": [],
    }


def test_mfi_dispatcher_uses_public_run_id_for_graph_and_live_trace(monkeypatch, mfi_csv_upload):
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(_trace("mfi-drafter", run_id, "running", total_calls=1))
        return {"run_id": run_id, "warnings": [], "llm_diagnostics": {}}

    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", fake_generation)
    response = dispatcher._mfi_drafter_generate_from_csv_async(data={}, files={}, params={})
    public_run_id = response.json()["run_id"]
    run = async_runs.get_run(public_run_id)

    assert captured["run_id"] == public_run_id
    assert run is not None and run.status == "completed"
    assert run.metadata["llm_diagnostics"]["run_id"] == public_run_id


def test_market_dispatcher_uses_public_run_id_for_graph(monkeypatch):
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, country, time_period, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(_trace("market-monitor", run_id, "not_started"))
        return {
            "run_id": run_id,
            "country": country,
            "time_period": time_period,
            "warnings": [],
            "visualizations": {},
            "report_draft_sections": {},
        }

    monkeypatch.setattr(dispatcher, "run_report_generation", fake_generation)
    response = dispatcher._market_monitor_generate_async(
        json_body={
            "country": "Testland",
            "time_period": "2026-01",
            "use_mock_data": True,
        }
    )
    public_run_id = response.json()["run_id"]
    run = async_runs.get_run(public_run_id)

    assert captured["run_id"] == public_run_id
    assert run is not None and run.status == "completed"
    assert run.metadata["llm_diagnostics"]["run_id"] == public_run_id


def test_fastapi_market_monitor_synchronous_path_maps_llm_failures_to_502(monkeypatch):
    def fail(**_kwargs):
        raise LLMCallError(
            failure_code="llm_response_contract_error",
            call_id="llm-0001-api",
            node="narrative_drafter",
            operation="market_monitor.narrative_drafting.v1",
            stage="contract_validation",
        )

    monkeypatch.setattr(market_router, "run_report_generation", fail)
    market_app = FastAPI()
    market_app.include_router(market_router.router)
    market_response = TestClient(market_app).post(
        "/generate",
        json={
            "country": "Testland",
            "time_period": "2026-01",
            "use_mock_data": True,
        },
    )
    assert market_response.status_code == 502
    assert market_response.json()["detail"]["code"] == "llm_call_failed"


def test_fastapi_async_mfi_public_and_graph_run_ids_match(monkeypatch, mfi_csv_upload):
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(_trace("mfi-drafter", run_id, "not_started"))
        return {"run_id": run_id, "warnings": [], "llm_diagnostics": {}}

    monkeypatch.setattr(mfi_router, "run_mfi_report_generation", fake_generation)
    app = FastAPI()
    app.include_router(mfi_router.router)
    response = TestClient(app).post(
        "/generate-from-csv-async",
        files={"file": ("mfi.csv", b"csv", "text/csv")},
    )
    public_run_id = response.json()["run_id"]
    assert response.status_code == 200
    assert captured["run_id"] == public_run_id
    run = async_runs.get_run(public_run_id)
    assert run is not None and run.status == "completed"


def test_fastapi_async_market_public_and_graph_run_ids_match(monkeypatch):
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, country, time_period, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(_trace("market-monitor", run_id, "not_started"))
        return {
            "run_id": run_id,
            "country": country,
            "time_period": time_period,
            "warnings": [],
            "visualizations": {},
            "report_draft_sections": {},
        }

    monkeypatch.setattr(market_router, "run_report_generation", fake_generation)
    app = FastAPI()
    app.include_router(market_router.router)
    response = TestClient(app).post(
        "/generate-async",
        json={
            "country": "Testland",
            "time_period": "2026-01",
            "use_mock_data": True,
        },
    )
    public_run_id = response.json()["run_id"]
    assert response.status_code == 200
    assert captured["run_id"] == public_run_id
    run = async_runs.get_run(public_run_id)
    assert run is not None and run.status == "completed"


def test_info_and_health_expose_sanitized_configuration(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "gs://private-secret-bucket/traces")
    for path in (
        "/mfi-drafter/info",
        "/mfi-drafter/health",
        "/market-monitor/info",
        "/market-monitor/health",
    ):
        payload = dispatcher.dispatch_request("GET", path).json()
        config = payload["llm_observability"]
        assert config["payload_capture_enabled"] is True
        assert config["payload_storage_configured"] is True
        assert config["retention_days"] == 30
        assert "private-secret-bucket" not in json.dumps(payload)
