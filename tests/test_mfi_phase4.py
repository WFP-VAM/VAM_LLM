from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.shared.llm_observability import LLMCallError

from app.services.mfi_drafter import graph, router
from app.services.mfi_drafter.errors import MFIGenerationBlockedError
from app.services.mfi_drafter.features import (
    MFIAnalysisVersionDisabled,
    MFI_DRAFTER_ANALYSIS_VERSION_ENV,
    mfi_release_control,
    require_mfi_analysis_v2,
)
from app.services.mfi_drafter.release_validation import (
    LEGACY_REFERENCE_COMMIT,
    MFIHumanApproval,
    MFIPilotRunEvidence,
    MFIRegressionCaseConfig,
    MFIRegressionCaseEvidence,
    MFIReleaseArtifact,
    MFIReleaseEvidenceManifest,
    MFIReleaseRequirements,
    MFIReleaseValidationConfig,
    MFIReleaseValidationCheck,
    evaluate_release_readiness,
    run_compare,
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


def test_direct_runner_stops_before_graph_or_llm(monkeypatch):
    invoked = False

    def fail_if_called(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("graph must not be built")

    monkeypatch.delenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, raising=False)
    monkeypatch.setattr(graph, "build_graph", fail_if_called)

    with pytest.raises(MFIAnalysisVersionDisabled):
        graph.run_mfi_report_generation(
            country="Testland",
            data_collection_start="2026-01-01",
            data_collection_end="2026-01-31",
            markets=["Central"],
        )

    assert invoked is False


def test_direct_runner_uses_supplied_immutable_snapshot(monkeypatch, caplog):
    class FakeAgent:
        @staticmethod
        def invoke(state, config=None):
            assert config == {"recursion_limit": 100}
            return {
                **state,
                "qa_review": {"status": "passed"},
                "generation_diagnostics": state["generation_diagnostics"],
            }

    control = mfi_release_control(
        {
            MFI_DRAFTER_ANALYSIS_VERSION_ENV: "2",
            "K_REVISION": "candidate-7",
        }
    )
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "invalid-after-submit")
    monkeypatch.setattr(graph, "build_graph", lambda on_step=None: FakeAgent())

    with caplog.at_level("INFO"):
        result = graph.run_mfi_report_generation(
            country="Testland",
            data_collection_start="2026-01-01",
            data_collection_end="2026-01-31",
            markets=["Central"],
            release_control=control,
        )

    assert result["release_control"]["analysis_version"] == "2"
    assert result["release_control"]["deployment_revision"] == "candidate-7"
    assert "generation started" in caplog.text
    assert "generation completed" in caplog.text
    completed = next(
        record
        for record in caplog.records
        if getattr(record, "mfi_event", None) == "generation_completed"
    )
    assert completed.mfi_context_status == "not_attempted"
    assert completed.mfi_context_limitation_code is None


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


def _synthetic_analyzed_state():
    data = graph.generate_mock_mfi_data(
        "Testland",
        ["Central", "North", "South"],
        "2026-01-01",
        "2026-01-31",
    )
    state = graph.create_initial_state(
        country="Testland",
        data_collection_start="2026-01-01",
        data_collection_end="2026-01-31",
        markets=["Central", "North", "South"],
    )
    state.update(graph.node_mfi_data_agent({**state, "csv_data": data, "use_csv_data": True}))
    state.update(graph.node_mfi_analysis(state))
    return state


def test_drafting_transport_failure_interrupts_enabled_stage(monkeypatch):
    state = _synthetic_analyzed_state()

    class FailingModel:
        @staticmethod
        def invoke(messages):
            raise RuntimeError("offline")

    monkeypatch.setattr(graph, "get_model", lambda: FailingModel())
    with pytest.raises(LLMCallError) as caught:
        graph.node_dimension_drafter(state)
    assert caught.value.failure_code == "llm_transport_error"
    assert caught.value.node == "dimension_drafter"


def test_final_qa_blocks_unresolved_material_findings():
    state = graph.create_initial_state(
        country="Testland",
        data_collection_start="2026-01-01",
        data_collection_end="2026-01-31",
        markets=["Central"],
    )
    flag = {
        "flag_id": "high-1",
        "source": "deterministic",
        "code": "invalid",
        "severity": "high",
        "artifact_type": "global",
        "artifact_id": None,
        "field_name": None,
        "claim_id": None,
        "message": "Invalid",
        "recommendation": "",
        "metric_ids": [],
        "document_ids": [],
        "expected_value": None,
        "actual_value": None,
        "repairable": False,
    }
    state["deterministic_flags"] = [flag]

    with pytest.raises(MFIGenerationBlockedError) as caught:
        graph.node_finalize_qa(state)
    assert caught.value.code == "mfi_narrative_qa_unresolved"
    assert caught.value.status_code == 502


def test_public_schema_marks_phase4_aliases_deprecated():
    properties = GenerateMFIReportOutput.model_json_schema()["properties"]

    assert properties["national_mfi"]["deprecated"] is True
    assert properties["risk_distribution"]["deprecated"] is True
    assert properties["dimension_scores"]["deprecated"] is True
    assert "release_control" in properties
    assert "generation_diagnostics" in properties


def _passed_check(check_id: str) -> MFIReleaseValidationCheck:
    return MFIReleaseValidationCheck(
        check_id=check_id,
        status="passed",
        blocking=True,
        message="passed",
    )


def test_release_validation_cases_are_generic_and_unique():
    config = MFIReleaseValidationConfig(
        cases=[
            MFIRegressionCaseConfig(
                case_id="country-example-17",
                label="Local assessment example",
                source_csv="private/example.csv",
                expected_included_market_count=12,
                expected_priority_dimensions=["Service"],
            )
        ],
        required_real_pilot_count=2,
    )

    assert config.cases[0].source_csv == "private/example.csv"
    assert config.required_real_pilot_count == 2

    with pytest.raises(ValueError, match="unique"):
        MFIReleaseValidationConfig(
            cases=[config.cases[0], config.cases[0]]
        )


def test_release_readiness_refuses_empty_regression_configuration():
    manifest = MFIReleaseEvidenceManifest(
        release_id="empty",
        mode="regression",
        generated_at="2026-07-30T12:00:00+00:00",
        candidate_revision="candidate",
    )

    evaluate_release_readiness(manifest)

    assert manifest.release_ready is False
    assert "regression:no_required_cases_configured" in manifest.blockers


def _ready_manifest() -> MFIReleaseEvidenceManifest:
    artifacts = {
        "case-alpha": MFIReleaseArtifact(
            artifact_id="case-alpha.docx",
            path="case-alpha.docx",
            sha256="a" * 64,
            byte_count=10,
            kind="docx",
        ),
        "case-beta": MFIReleaseArtifact(
            artifact_id="case-beta.docx",
            path="case-beta.docx",
            sha256="b" * 64,
            byte_count=10,
            kind="docx",
        ),
    }
    regression_cases = [
        MFIRegressionCaseEvidence(
            case_id=case_id,
            label=case_id.title(),
            source_sha256=("c" if case_id == "case-alpha" else "d") * 64,
            checks=[_passed_check(f"{case_id}.checks")],
            artifacts=[artifacts[case_id]],
            methodology_warning_codes=(
                [] if case_id == "case-alpha" else ["records_excluded"]
            ),
            limitation_codes=[
                "assessment_scope_not_representative",
                "item_trader_denominator_unavailable",
            ],
        )
        for case_id in ("case-alpha", "case-beta")
    ]
    approvals = [
        MFIHumanApproval(
            case_id=case_id,
            reviewer="reviewer",
            reviewed_at="2026-07-30T12:00:00+00:00",
            analytical_status="approved",
            visual_status="approved",
            warning_dispositions={
                code: "accepted"
                for code in (
                    regression_cases[index].methodology_warning_codes
                    + regression_cases[index].limitation_codes
                )
            },
            artifact_sha256={
                f"{case_id}.docx": artifacts[case_id].sha256
            },
        )
        for index, case_id in enumerate(("case-alpha", "case-beta"))
    ]

    def pilot(run_id, kind, case_id=None):
        return MFIPilotRunEvidence(
            run_id=run_id,
            kind=kind,
            case_id=case_id,
            completed_async=True,
            analysis_version="2",
            llm_calls=5,
            docx_exported=True,
            preview_rendered=True,
            score_integrity_passed=True,
            fallback_used=False,
            human_status="approved",
        )

    return MFIReleaseEvidenceManifest(
        release_id="candidate",
        mode="compare",
        generated_at="2026-07-30T12:00:00+00:00",
        candidate_revision="candidate-commit",
        baseline_revision=LEGACY_REFERENCE_COMMIT,
        validation_config_sha256="e" * 64,
        requirements=MFIReleaseRequirements(
            required_case_ids=["case-alpha", "case-beta"],
            required_live_pilot_case_ids=["case-alpha", "case-beta"],
            required_real_pilot_count=3,
        ),
        regression_cases=regression_cases,
        comparison_checks=[_passed_check("comparison")],
        approvals=approvals,
        pilot_runs=[
            pilot("alpha-live", "regression_case", "case-alpha"),
            pilot("beta-live", "regression_case", "case-beta"),
            pilot("real-1", "real"),
            pilot("real-2", "real"),
            pilot("real-3", "real"),
        ],
    )


def test_release_ready_requires_all_automated_human_and_pilot_gates():
    manifest = evaluate_release_readiness(_ready_manifest())

    assert manifest.release_ready is True
    assert manifest.blockers == []

    fallback_manifest = copy.deepcopy(manifest)
    fallback_manifest.pilot_runs[-1].fallback_used = True
    evaluate_release_readiness(fallback_manifest)

    assert fallback_manifest.release_ready is False
    assert "pilot:real_assessments" in fallback_manifest.blockers


def test_release_ready_rejects_unpinned_baseline_and_missing_dispositions():
    manifest = _ready_manifest()
    manifest.baseline_revision = "different"
    manifest.approvals[1].warning_dispositions = {}

    evaluate_release_readiness(manifest)

    assert manifest.release_ready is False
    assert "baseline_revision" in manifest.blockers
    assert "approval_evidence:case-beta" in manifest.blockers


def test_optional_regression_case_does_not_become_a_release_dependency():
    manifest = _ready_manifest()
    manifest.regression_cases.append(
        MFIRegressionCaseEvidence(
            case_id="exploratory-case",
            label="Exploratory local fixture",
            source_sha256="f" * 64,
            checks=[
                MFIReleaseValidationCheck(
                    check_id="exploratory.failure",
                    status="failed",
                    blocking=True,
                    message="Optional evidence failed",
                )
            ],
        )
    )

    evaluate_release_readiness(manifest)

    assert manifest.release_ready is True
    assert manifest.blockers == []


def test_release_ready_requires_validation_configuration_hash():
    manifest = _ready_manifest()
    manifest.validation_config_sha256 = None

    evaluate_release_readiness(manifest)

    assert "validation_config" in manifest.blockers


def test_compare_is_deterministic_and_never_executes_legacy(tmp_path):
    legacy_result = tmp_path / "legacy.json"
    current_result = tmp_path / "current.json"
    legacy_docx = tmp_path / "legacy.docx"
    current_docx = tmp_path / "current.docx"
    legacy_preview = tmp_path / "legacy.png"
    current_preview = tmp_path / "current.png"
    regression_manifest = tmp_path / "regression.json"
    legacy_result.write_text(
        json.dumps({"report_blocks": [{"type": "heading", "text": "Old"}]}),
        encoding="utf-8",
    )
    current_result.write_text(
        json.dumps(
            {
                "analysis_schema_version": "2.0",
                "narrative_schema_version": "2.0",
                "assessment_profile": {
                    "priority_dimension_names": ["Service"]
                },
                "report_blocks": [
                    {"type": "heading", "text": "Assessment profile"}
                ],
                "dimension_narratives": {"Service": {}},
                "national_mfi": 5.0,
                "risk_distribution": {},
                "dimension_scores": [],
                "markets_data": [{"sub_scores": {}}],
            }
        ),
        encoding="utf-8",
    )
    legacy_docx.write_bytes(b"PKlegacy")
    current_docx.write_bytes(b"PKcurrent")
    legacy_preview.write_bytes(b"\x89PNGlegacy")
    current_preview.write_bytes(b"\x89PNGcurrent")
    regression = _ready_manifest()
    regression.approvals = []
    regression.pilot_runs = []
    regression.release_ready = False
    regression.blockers = []
    regression_manifest.write_text(
        regression.model_dump_json(indent=2),
        encoding="utf-8",
    )

    first = run_compare(
        legacy_result_path=legacy_result,
        current_result_path=current_result,
        legacy_docx_path=legacy_docx,
        current_docx_path=current_docx,
        legacy_preview_path=legacy_preview,
        current_preview_path=current_preview,
        regression_manifest_path=regression_manifest,
        output_directory=tmp_path / "first",
        release_id="comparison",
        candidate_revision="candidate",
    )
    second = run_compare(
        legacy_result_path=legacy_result,
        current_result_path=current_result,
        legacy_docx_path=legacy_docx,
        current_docx_path=current_docx,
        legacy_preview_path=legacy_preview,
        current_preview_path=current_preview,
        regression_manifest_path=regression_manifest,
        output_directory=tmp_path / "second",
        release_id="comparison",
        candidate_revision="candidate",
    )

    assert [check.model_dump() for check in first.comparison_checks] == [
        check.model_dump() for check in second.comparison_checks
    ]
    assert (tmp_path / "first" / "comparison.html").read_bytes() == (
        tmp_path / "second" / "comparison.html"
    ).read_bytes()
    assert first.release_ready is False
    assert "approval:case-alpha" in first.blockers
