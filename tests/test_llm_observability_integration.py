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
from app.services.mfi_drafter.errors import MFIGenerationBlockedError
from app.streamlit_backend import dispatcher


class ImmediateThread:
    def __init__(self, *, target, daemon):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture(autouse=True)
def reset_memory_runs(monkeypatch):
    monkeypatch.setattr(async_runs, "_BACKEND", "memory")
    async_runs._RUNS.clear()
    async_runs._RUN_ARTIFACTS.clear()
    monkeypatch.setattr(
        dispatcher, "threading", SimpleNamespace(Thread=ImmediateThread)
    )


def test_mfi_dispatcher_uses_public_run_id_for_graph_and_live_trace(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(
            {
                "trace_schema_version": "1.0",
                "service": "mfi-drafter",
                "run_id": run_id,
                "status": "running",
                "current_call_id": "llm-0001-test",
                "total_calls": 1,
                "succeeded_calls": 0,
                "failed_calls": 0,
                "contract_failed_calls": 0,
                "payload_capture_enabled": False,
                "payload_storage_configured": False,
                "payload_persistence_failures": 0,
                "calls": [],
            }
        )
        return {"run_id": run_id, "warnings": [], "llm_diagnostics": {}}

    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", fake_generation)
    response = dispatcher._mfi_drafter_generate_async(
        json_body={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        }
    )
    public_run_id = response.json()["run_id"]
    run = async_runs.get_run(public_run_id)

    assert captured["run_id"] == public_run_id
    assert run is not None and run.status == "completed"
    assert run.metadata["llm_diagnostics"]["run_id"] == public_run_id


def test_market_dispatcher_uses_public_run_id_for_graph(monkeypatch):
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, country, time_period, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(
            {
                "trace_schema_version": "1.0",
                "service": "market-monitor",
                "run_id": run_id,
                "status": "not_started",
                "current_call_id": None,
                "total_calls": 0,
                "succeeded_calls": 0,
                "failed_calls": 0,
                "contract_failed_calls": 0,
                "payload_capture_enabled": False,
                "payload_storage_configured": False,
                "payload_persistence_failures": 0,
                "calls": [],
            }
        )
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


def test_async_llm_failure_stops_at_active_node_and_retains_trace(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def failed_generation(*, run_id, llm_trace_sink, **_kwargs):
        call_id = "llm-0001-failed"
        llm_trace_sink(
            {
                "trace_schema_version": "1.0",
                "service": "mfi-drafter",
                "run_id": run_id,
                "status": "failed",
                "current_call_id": None,
                "total_calls": 1,
                "succeeded_calls": 0,
                "failed_calls": 1,
                "contract_failed_calls": 1,
                "payload_capture_enabled": False,
                "payload_storage_configured": False,
                "payload_persistence_failures": 0,
                "calls": [],
            }
        )
        raise LLMCallError(
            failure_code="llm_invalid_json",
            call_id=call_id,
            node="dimension_drafter",
            operation="mfi.dimension_drafting.v2",
            stage="json_parse",
        )

    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", failed_generation)
    response = dispatcher._mfi_drafter_generate_async(
        json_body={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        }
    )
    run_id = response.json()["run_id"]
    run = async_runs.get_run(run_id)

    assert run is not None and run.status == "failed"
    assert run.progress_pct < 100
    assert run.current_node == "dimension_drafter"
    assert run.traceback is None
    error = json.loads(run.error)
    assert error == {
        "call_id": "llm-0001-failed",
        "code": "llm_call_failed",
        "failure_code": "llm_invalid_json",
        "node": "dimension_drafter",
        "operation": "mfi.dimension_drafting.v2",
        "stage": "json_parse",
    }
    assert run.metadata["llm_diagnostics"]["failed_calls"] == 1


def test_synchronous_dispatchers_map_llm_failures_to_502(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def fail(**_kwargs):
        raise LLMCallError(
            failure_code="llm_transport_error",
            call_id="llm-0001-sync",
            node="red_team",
            operation="mfi.red_team_review.v6",
            stage="transport",
        )

    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", fail)
    response = dispatcher.dispatch_request(
        "POST",
        "/mfi-drafter/generate",
        json_body={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_call_failed"


def test_dispatcher_red_team_failure_sets_generation_status_failed(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def failed_generation(*, run_id, llm_trace_sink, **_kwargs):
        llm_trace_sink(
            {
                "trace_schema_version": "1.0",
                "service": "mfi-drafter",
                "run_id": run_id,
                "status": "failed",
                "current_call_id": None,
                "total_calls": 27,
                "succeeded_calls": 26,
                "failed_calls": 1,
                "contract_failed_calls": 0,
                "payload_capture_enabled": False,
                "payload_storage_configured": False,
                "payload_persistence_failures": 0,
                "calls": [],
            }
        )
        raise LLMCallError(
            failure_code="llm_transport_error",
            call_id="llm-0027-timeout",
            node="red_team",
            operation="mfi.red_team_review.v6",
            stage="transport",
        )

    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", failed_generation)
    response = dispatcher._mfi_drafter_generate_async(
        json_body={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Dangbo"],
            "use_mock_data": True,
        }
    )
    run_id = response.json()["run_id"]
    run = async_runs.get_run(run_id)
    assert run is not None and run.status == "failed"
    assert run.current_node == "red_team"
    assert run.progress_pct < 100
    assert run.metadata["generation_diagnostics"]["red_team_status"] == "failed"


def test_fastapi_synchronous_paths_map_llm_failures_to_502(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def fail(**_kwargs):
        raise LLMCallError(
            failure_code="llm_response_contract_error",
            call_id="llm-0001-api",
            node="narrative_drafter",
            operation="market_monitor.narrative_drafting.v1",
            stage="contract_validation",
        )

    monkeypatch.setattr(mfi_router, "run_mfi_report_generation", fail)
    mfi_app = FastAPI()
    mfi_app.include_router(mfi_router.router)
    mfi_response = TestClient(mfi_app).post(
        "/generate",
        json={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        },
    )
    assert mfi_response.status_code == 502
    assert mfi_response.json()["detail"]["code"] == "llm_call_failed"

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


def test_fastapi_async_mfi_public_and_graph_run_ids_match(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(
            {
                "trace_schema_version": "1.0",
                "service": "mfi-drafter",
                "run_id": run_id,
                "status": "not_started",
                "current_call_id": None,
                "total_calls": 0,
                "succeeded_calls": 0,
                "failed_calls": 0,
                "contract_failed_calls": 0,
                "payload_capture_enabled": False,
                "payload_storage_configured": False,
                "payload_persistence_failures": 0,
                "calls": [],
            }
        )
        return {"run_id": run_id, "warnings": [], "llm_diagnostics": {}}

    monkeypatch.setattr(mfi_router, "run_mfi_report_generation", fake_generation)
    app = FastAPI()
    app.include_router(mfi_router.router)
    response = TestClient(app).post(
        "/generate-async",
        json={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        },
    )
    public_run_id = response.json()["run_id"]
    assert response.status_code == 200
    assert captured["run_id"] == public_run_id
    run = async_runs.get_run(public_run_id)
    assert run is not None and run.status == "completed"


def test_async_fail_closed_mfi_run_publishes_no_result_or_artifact(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def blocked_generation(*, run_id, **_kwargs):
        raise MFIGenerationBlockedError(
            "mfi_narrative_qa_unresolved",
            "Material narrative QA remains unresolved.",
            stage="targeted_correction",
            status_code=502,
            task_id="task-3",
            attempt=3,
        )

    monkeypatch.setattr(mfi_router, "run_mfi_report_generation", blocked_generation)
    app = FastAPI()
    app.include_router(mfi_router.router)
    response = TestClient(app).post(
        "/generate-async",
        json={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        },
    )
    assert response.status_code == 200
    blocked_run_id = response.json()["run_id"]
    run = async_runs.get_run(blocked_run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.progress_pct < 100
    assert run.result is None
    assert not any(
        key[0] == blocked_run_id for key in async_runs._RUN_ARTIFACTS
    )
    public_error = json.loads(run.error)
    assert public_error["code"] == "mfi_narrative_qa_unresolved"
    assert public_error["task_id"] == "task-3"


def test_dispatcher_async_fail_closed_mfi_run_publishes_no_result(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def blocked_generation(*, run_id, **_kwargs):
        raise MFIGenerationBlockedError(
            "mfi_report_delivery_contract_failed",
            "Delivery blocks are invalid.",
            stage="finalize_delivery",
            status_code=500,
        )

    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", blocked_generation)
    response = dispatcher._mfi_drafter_generate_async(
        json_body={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        }
    )
    run = async_runs.get_run(response.json()["run_id"])
    assert run is not None and run.status == "failed"
    assert run.progress_pct < 100
    assert run.result is None
    assert json.loads(run.error)["code"] == "mfi_report_delivery_contract_failed"
    assert run.metadata["generation_diagnostics"]["delivery_contract_status"] == "failed"


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("mfi_narrative_qa_unresolved", 502),
        ("mfi_claim_identity_contract_failed", 500),
        ("mfi_report_delivery_contract_failed", 500),
    ],
)
def test_fastapi_maps_typed_fail_closed_errors(monkeypatch, code, status_code):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def blocked(**_kwargs):
        raise MFIGenerationBlockedError(
            code,
            "Generation cannot be delivered.",
            stage=(
                "targeted_correction"
                if status_code == 502
                else "finalize_delivery"
            ),
            status_code=status_code,
        )

    monkeypatch.setattr(mfi_router, "run_mfi_report_generation", blocked)
    app = FastAPI()
    app.include_router(mfi_router.router)
    response = TestClient(app).post(
        "/generate",
        json={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Central"],
            "use_mock_data": True,
        },
    )
    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code


def test_fastapi_async_red_team_failure_sets_generation_status_failed(monkeypatch):
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")

    def failed_generation(*, run_id, llm_trace_sink, **_kwargs):
        llm_trace_sink(
            {
                "trace_schema_version": "1.0",
                "service": "mfi-drafter",
                "run_id": run_id,
                "status": "failed",
                "current_call_id": None,
                "total_calls": 27,
                "succeeded_calls": 26,
                "failed_calls": 1,
                "contract_failed_calls": 0,
                "payload_capture_enabled": False,
                "payload_storage_configured": False,
                "payload_persistence_failures": 0,
                "calls": [],
            }
        )
        raise LLMCallError(
            failure_code="llm_transport_error",
            call_id="llm-0027-timeout",
            node="red_team",
            operation="mfi.red_team_review.v6",
            stage="transport",
        )

    monkeypatch.setattr(mfi_router, "run_mfi_report_generation", failed_generation)
    app = FastAPI()
    app.include_router(mfi_router.router)
    response = TestClient(app).post(
        "/generate-async",
        json={
            "country": "Testland",
            "data_collection_start": "2026-01-01",
            "data_collection_end": "2026-01-31",
            "markets": ["Dangbo"],
            "use_mock_data": True,
        },
    )
    assert response.status_code == 200
    run = async_runs.get_run(response.json()["run_id"])
    assert run is not None and run.status == "failed"
    assert run.current_node == "red_team"
    assert run.metadata["generation_diagnostics"]["red_team_status"] == "failed"


def test_fastapi_async_market_public_and_graph_run_ids_match(monkeypatch):
    captured = {}

    def fake_generation(*, run_id, llm_trace_sink, country, time_period, **_kwargs):
        captured["run_id"] = run_id
        llm_trace_sink(
            {
                "trace_schema_version": "1.0",
                "service": "market-monitor",
                "run_id": run_id,
                "status": "not_started",
                "current_call_id": None,
                "total_calls": 0,
                "succeeded_calls": 0,
                "failed_calls": 0,
                "contract_failed_calls": 0,
                "payload_capture_enabled": False,
                "payload_storage_configured": False,
                "payload_persistence_failures": 0,
                "calls": [],
            }
        )
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
