"""The Streamlit dispatcher serves Market Monitor in the report language, as the API does."""
import io
import json

from docx import Document
from fastapi.testclient import TestClient

import main
from app.services.market_monitor.i18n import t
from app.shared.runs import report_runs
from app.streamlit_backend import dispatcher


def test_dispatcher_info_matches_the_api():
    api = TestClient(main.app).get("/market-monitor/info").json()
    local = json.loads(dispatcher.dispatch_request("GET", "/market-monitor/info").content)
    assert local == api
    assert "language" in [item["name"] for item in local["inputs"]]


def test_dispatcher_async_run_records_the_language_and_translates_live_titles(monkeypatch, immediate_launch):

    def fake_run_report_generation(*, country, time_period, on_step, **_kwargs):
        on_step("data_agent", {"databridges_rows": [{"Country": country, "Commodity": "Maize", "Price": 1.0}]})
        on_step("news_retrieval", {"retriever_traces": [], "seerist_documents": [], "reliefweb_documents": []})
        return {"country": country, "time_period": time_period, "report_draft_sections": {}, "warnings": []}

    monkeypatch.setattr(dispatcher, "run_report_generation", fake_run_report_generation)
    response = dispatcher._market_monitor_generate_async(json_body={
        "country": "South Sudan", "time_period": "2025-01", "language": "fr",
        "commodity_list": ["Maize"], "admin1_list": [], "currency_code": "SSP",
        "enabled_modules": [], "use_mock_data": True,
    })

    run = report_runs.get_run(response.json()["run_id"])
    assert run.status == "completed"
    assert run.metadata["language"] == "fr"
    assert run.metadata["language_source"] == "explicit"
    live = run.metadata["live_outputs"]
    for section, key in (("databridges", "live.price_data.title"), ("seerist", "live.seerist.title"),
                         ("reliefweb", "live.reliefweb.title")):
        assert live[section]["title"] == t("fr", key)
    assert t("fr", "live.price_data.title") != t("en", "live.price_data.title")


def test_dispatcher_docx_export_uses_the_report_language(monkeypatch):
    report_runs.create_run("run_es", service="market-monitor")
    report_runs.set_run_completed("run_es", result={
        "country": "Colombia", "time_period": "2025-01", "language": "es",
        "report_draft_sections": {"HIGHLIGHTS": "Texto."}, "visualizations": {},
        "document_references": [{"doc_id": "D1", "source": "ReliefWeb", "date": "2025-01-10",
                                 "title": "Informe", "url": "https://example.org/informe"}],
    })

    response = dispatcher.dispatch_request("POST", "/market-monitor/export-docx/run_es", json_body={})

    assert response.status_code == 200
    headings = [p.text for p in Document(io.BytesIO(response.content)).paragraphs]
    assert t("es", "section.REFERENCES") in headings
    assert t("es", "section.REFERENCES") != t("en", "section.REFERENCES")
