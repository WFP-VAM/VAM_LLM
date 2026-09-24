from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.mfi_drafter import router
from app.services.mfi_drafter.features import (
    MFIAnalysisVersionDisabled,
    MFI_DRAFTER_ANALYSIS_VERSION_ENV,
    mfi_release_control,
    require_mfi_analysis_v2,
)
from app.services.mfi_drafter.schemas import GenerateMFIReportOutput
from app.streamlit_backend import dispatcher


@pytest.mark.parametrize(
    ("environment", "version", "enabled", "status"),
    [
        ({}, "1", False, "default_disabled"),
        ({MFI_DRAFTER_ANALYSIS_VERSION_ENV: ""}, "1", False, "default_disabled"),
        ({MFI_DRAFTER_ANALYSIS_VERSION_ENV: "1"}, "1", False, "configured"),
        ({MFI_DRAFTER_ANALYSIS_VERSION_ENV: " 2 "}, "2", True, "configured"),
        ({MFI_DRAFTER_ANALYSIS_VERSION_ENV: "true"}, "true", False, "invalid"),
        ({MFI_DRAFTER_ANALYSIS_VERSION_ENV: "3"}, "3", False, "invalid"),
    ],
)
def test_release_control_is_strict_and_fail_closed(
    environment, version, enabled, status
):
    control = mfi_release_control(environment)

    assert control.analysis_version == version
    assert control.enabled is enabled
    assert control.configuration_status == status


def test_release_control_captures_cloud_run_revision():
    control = mfi_release_control(
        {
            MFI_DRAFTER_ANALYSIS_VERSION_ENV: "2",
            "K_SERVICE": "mfi-pilot",
            "K_REVISION": "mfi-pilot-00007",
        }
    )

    assert control.service_name == "mfi-pilot"
    assert control.deployment_revision == "mfi-pilot-00007"


def test_disabled_control_has_stable_error_contract():
    control = mfi_release_control({})

    with pytest.raises(MFIAnalysisVersionDisabled) as raised:
        require_mfi_analysis_v2(control)

    assert raised.value.status_code == 503
    assert raised.value.to_dict()["code"] == "mfi_drafter_analysis_v2_disabled"
    assert raised.value.to_dict()["release_control"]["enabled"] is False


@pytest.fixture
def fastapi_client():
    app = FastAPI()
    app.include_router(router.router, prefix="/mfi-drafter")
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        (
            "/mfi-drafter/generate-from-csv",
            {"files": {"file": ("mfi.csv", b"invalid", "text/csv")}},
        ),
        (
            "/mfi-drafter/generate-from-csv-async",
            {"files": {"file": ("mfi.csv", b"invalid", "text/csv")}},
        ),
    ],
)
def test_fastapi_generation_paths_return_stable_503(
    monkeypatch, fastapi_client, path, kwargs
):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("generation work must not start")

    monkeypatch.delenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, raising=False)
    monkeypatch.setattr(router, "run_mfi_report_generation", forbidden)
    monkeypatch.setattr(router, "create_run", forbidden)

    response = fastapi_client.post(path, **kwargs)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "mfi_drafter_analysis_v2_disabled"
    )
    assert called is False


def test_fastapi_validation_info_health_and_artifacts_remain_available(
    monkeypatch, fastapi_client
):
    monkeypatch.delenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, raising=False)
    monkeypatch.setattr(
        router,
        "get_run_artifact",
        lambda run_id, artifact_id: SimpleNamespace(
            file_name="existing.txt",
            mime_type="text/plain",
            content=b"existing",
        ),
    )

    validation = fastapi_client.post(
        "/mfi-drafter/validate-csv",
        files={"file": ("mfi.csv", b"invalid", "text/csv")},
    )
    info = fastapi_client.get("/mfi-drafter/info")
    health = fastapi_client.get("/mfi-drafter/health")
    artifact = fastapi_client.get(
        "/mfi-drafter/artifacts/existing-run/report"
    )

    assert validation.status_code == 200
    assert info.json()["generation_enabled"] is False
    assert info.json()["release_control"]["analysis_version"] == "1"
    assert health.status_code == 200
    assert health.json()["generation_enabled"] is False
    assert artifact.status_code == 200
    assert artifact.content == b"existing"


@pytest.mark.parametrize(
    ("path", "json_body", "files"),
    [
        (
            "/mfi-drafter/generate-from-csv",
            None,
            {"file": ("mfi.csv", b"invalid", "text/csv")},
        ),
        (
            "/mfi-drafter/generate-from-csv-async",
            None,
            {"file": ("mfi.csv", b"invalid", "text/csv")},
        ),
    ],
)
def test_dispatcher_generation_paths_return_stable_503(
    monkeypatch, path, json_body, files
):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("generation work must not start")

    monkeypatch.delenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, raising=False)
    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", forbidden)
    monkeypatch.setattr(dispatcher, "create_run", forbidden)

    response = dispatcher.dispatch_request(
        "POST",
        path,
        json_body=json_body,
        files=files,
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "mfi_drafter_analysis_v2_disabled"
    )
    assert called is False


def test_dispatcher_async_run_retains_submission_snapshot(monkeypatch):
    targets = []
    captured = {}

    class DeferredThread:
        def __init__(self, *, target, daemon):
            targets.append(target)

        def start(self):
            return None

    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "2")
    monkeypatch.setenv("K_REVISION", "pilot-revision")
    monkeypatch.setattr(dispatcher.threading, "Thread", DeferredThread)
    monkeypatch.setattr(dispatcher, "create_run", lambda run_id: None)
    monkeypatch.setattr(dispatcher, "update_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher,
        "set_run_completed",
        lambda *args, **kwargs: None,
    )

    def fake_run(*args, release_control, **kwargs):
        captured["control"] = release_control.model_dump()
        return {"warnings": []}

    monkeypatch.setattr(dispatcher, "run_mfi_report_generation", fake_run)
    monkeypatch.setattr(dispatcher, "_extract_file",
                        lambda *args: SimpleNamespace(filename="mfi.csv", content=b"csv"))
    monkeypatch.setattr(dispatcher, "load_mfi_from_csv", lambda **kwargs: {
        "country": "Testland", "data_collection_start": "2026-01-01", "data_collection_end": "2026-01-31",
        "markets": ["Central"], "survey_metadata": {}})

    response = dispatcher._mfi_drafter_generate_from_csv_async(data={}, files={}, params={})
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "invalid")
    targets[0]()

    assert response.status_code == 200
    assert captured["control"]["analysis_version"] == "2"
    assert captured["control"]["deployment_revision"] == "pilot-revision"


def test_public_schema_marks_phase4_aliases_deprecated():
    properties = GenerateMFIReportOutput.model_json_schema()["properties"]

    assert properties["national_mfi"]["deprecated"] is True
    assert properties["risk_distribution"]["deprecated"] is True
    assert properties["dimension_scores"]["deprecated"] is True
    assert "release_control" in properties
    assert "generation_diagnostics" in properties
