from __future__ import annotations

import builtins
from pathlib import Path

from streamlit.testing.v1 import AppTest


PAGE = Path(__file__).resolve().parents[1] / "pages" / "4_MFI_Drafter.py"


class FakeUpload:
    name = "mfi.csv"
    type = "text/csv"

    @staticmethod
    def getvalue():
        return b"csv-data"


class FakeMFIBackend:
    def __init__(
        self,
        *,
        upload=None,
        validation=None,
        result=None,
        generation_enabled=True,
        status=None,
    ):
        self.upload = upload
        self.result = result
        self.status = status
        self.validation = validation or {
            "valid": True,
            "missing_columns": [],
            "missing_metadata_fields": [],
            "errors": [],
        }
        self.requests = []
        self.runs = []
        self.generation_enabled = generation_enabled

    def request_json(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if path == "/mfi-drafter/info":
            return {
                "generation_enabled": self.generation_enabled,
                "release_control": {
                    "analysis_version": (
                        "2" if self.generation_enabled else "1"
                    ),
                    "enabled": self.generation_enabled,
                    "configuration_status": "configured",
                    "service_name": "mfi-drafter",
                    "deployment_revision": "test",
                },
            }
        if path.startswith("/mfi-drafter/status/"):
            return self.status
        assert path == "/mfi-drafter/validate-csv"
        return self.validation

    def run_async_and_poll(self, **kwargs):
        self.runs.append(kwargs)
        if self.result is not None:
            return ("mfi-run-1", {"status": "completed"}, self.result)
        return (
            "mfi-run-1",
            {"status": "completed"},
            {
                "run_id": "mfi-run-1",
                "country": "South Sudan",
                "national_mfi": 6.2,
                "llm_calls": 1,
                "report_blocks": [],
            },
        )


def _element(elements, label):
    return next(element for element in elements if element.label == label)


def _app(monkeypatch, backend, run_id=None):
    monkeypatch.setattr(builtins, "_mfi_drafter_test_backend", backend, raising=False)
    page_path = str(PAGE).replace("\\", "\\\\")
    source = f'''
import builtins
import sys
import types
import streamlit as st

if {run_id!r}:
    st.query_params["mfi_run"] = {run_id!r}
backend = builtins._mfi_drafter_test_backend
if backend.upload is not None:
    st.file_uploader = lambda *args, **kwargs: backend.upload

shared = types.ModuleType("streamlit_shared")
shared.apply_wfp_theme = lambda: None
shared.render_wfp_sidebar_logo = lambda: None
shared.render_onboarding_sidebar_button = lambda **kwargs: None
shared.render_instructions_sidebar_button = lambda **kwargs: None
shared.render_bug_report_sidebar_link = lambda **kwargs: None
shared.render_bug_report_header_link = lambda **kwargs: None
shared.render_report_delivery = lambda **kwargs: kwargs["render_technical_details"]()
shared.render_report_blocks = lambda *args, **kwargs: None
shared.render_mfi_raw_table_downloads = lambda *args, **kwargs: None
shared.render_llm_diagnostics = lambda *args, **kwargs: None
shared.request_json = backend.request_json
shared.request_bytes = lambda *args, **kwargs: (b"draft", {{}})
shared.run_async_and_poll = backend.run_async_and_poll
shared.safe_show_error = lambda error: st.error(str(error))
sys.modules["streamlit_shared"] = shared

page_path = r"{page_path}"
with open(page_path, encoding="utf-8") as page_file:
    exec(compile(page_file.read(), page_path, "exec"))
'''
    return AppTest.from_string(source).run(timeout=20)


def test_mfi_form_removes_override_controls(monkeypatch):
    app = _app(monkeypatch, FakeMFIBackend())

    assert not app.exception
    assert _element(app.button, "Generate report")
    assert not [field for field in app.text_input if "Override" in field.label]
    assert not [box for box in app.checkbox if "Override" in box.label]
    assert not [field for field in app.date_input if "Override" in field.label]


def test_phase4_disabled_deployment_disables_generation_but_keeps_page(
    monkeypatch,
):
    backend = FakeMFIBackend(
        upload=FakeUpload(),
        generation_enabled=False,
    )
    app = _app(monkeypatch, backend)

    assert not app.exception
    button = _element(app.button, "Generate report")
    assert button.disabled is True
    assert any(
        "not enabled in this deployment" in warning.value
        for warning in app.warning
    )
    assert backend.runs == []


def test_invalid_mfi_metadata_opens_dialog_and_does_not_start_run(monkeypatch):
    backend = FakeMFIBackend(
        upload=FakeUpload(),
        validation={
            "valid": False,
            "missing_columns": [],
            "missing_metadata_fields": ["StartDate", "EndDate"],
            "errors": ["Missing or invalid required collection metadata: StartDate, EndDate"],
        },
    )
    app = _app(monkeypatch, backend)

    app = _element(app.button, "Generate report").click().run(timeout=20)

    assert not app.exception
    assert backend.requests
    assert backend.runs == []
    assert any("StartDate, EndDate" in error.value for error in app.error)
    assert any("corrected final processed MFI CSV" in info.value for info in app.info)


def test_valid_mfi_metadata_starts_async_generation_without_overrides(monkeypatch):
    backend = FakeMFIBackend(upload=FakeUpload())
    app = _app(monkeypatch, backend)

    app = _element(app.button, "Generate report").click().run(timeout=20)

    assert not app.exception
    assert len(backend.runs) == 1
    assert backend.runs[0]["start_data"] == {}
    assert "country_override" not in backend.runs[0]["start_data"]
    assert "data_collection_start_override" not in backend.runs[0]["start_data"]
    assert "data_collection_end_override" not in backend.runs[0]["start_data"]


def test_methodology_warning_is_prominent_in_result_view(monkeypatch):
    backend = FakeMFIBackend(
        upload=FakeUpload(),
        result={
            "run_id": "mfi-run-1",
            "country": "South Sudan",
            "national_mfi": 6.2,
            "llm_calls": 1,
            "report_blocks": [],
            "methodology_warnings": [
                {
                    "code": "mfir_records_excluded",
                    "message": "Excluded six MFIr-only records.",
                }
            ],
            "excluded_market_records": [
                {"market_name": "Hinche"},
                {"market_name": "Jacmel"},
            ],
        },
    )
    app = _app(monkeypatch, backend)

    app = _element(app.button, "Generate report").click().run(timeout=20)

    assert not app.exception
    assert any(
        "Excluded six MFIr-only records" in warning.value for warning in app.warning
    )
    assert any(
        "Excluded MFIr-only records: Hinche, Jacmel" in caption.value
        for caption in app.caption
    )


def test_mean_priorities_limitations_and_review_outcome_are_visible(monkeypatch):
    backend = FakeMFIBackend(
        upload=FakeUpload(),
        result={
            "run_id": "mfi-run-1",
            "workflow_revision": "mfi-light-v1",
            "country": "South Sudan",
            "mean_mfi_across_assessed_markets": 6.234,
            "llm_calls": 6,
            "report_blocks": [],
            "review_status": "completed",
            "review_reports": {
                "dimensions": {"needs_revision": True, "review_markdown": "Tighten the Price section."},
                "markets": {"needs_revision": False, "review_markdown": "No changes needed."},
            },
            "assessment_profile": {
                "assessed_market_count": 12,
                "priority_dimension_names": ["Service", "Infrastructure"],
                "limitations": [
                    {"message": "Regional coverage is incomplete."}
                ],
            },
            "generation_diagnostics": {"phases": [], "progress_pct": 100},
        },
    )
    app = _app(monkeypatch, backend)

    app = _element(app.button, "Generate report").click().run(timeout=20)

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Mean MFI across assessed markets"] == "6.23/10"
    assert metrics["Narrative QA"] == "Completed"
    assert metrics["Dimensions review"] == "Corrected"
    assert metrics["Markets review"] == "No changes requested"
    assert metrics["Model calls"] == "6"
    assert any(
        "Priority dimensions: Service, Infrastructure" in info.value
        for info in app.info
    )
    assert any(
        "Regional coverage is incomplete" in warning.value
        for warning in app.warning
    )


def test_r7_context_status_is_informational_or_warning_in_technical_details(monkeypatch):
    neutral_backend = FakeMFIBackend(
        upload=FakeUpload(),
        result={
            "run_id": "mfi-run-neutral",
            "country": "South Sudan",
            "report_blocks": [],
            "context_status": {
                "status": "no_results",
                "limitation_code": None,
            },
        },
    )
    neutral = _app(monkeypatch, neutral_backend)
    neutral = _element(neutral.button, "Generate report").click().run(timeout=20)
    assert any(
        "Context evidence status: no results" in item.value for item in neutral.info
    )

    failure_backend = FakeMFIBackend(
        upload=FakeUpload(),
        result={
            "run_id": "mfi-run-failure",
            "country": "South Sudan",
            "report_blocks": [],
            "context_status": {
                "status": "retrieval_failed",
                "limitation_code": "context_retrieval_unavailable",
            },
        },
    )
    failed = _app(monkeypatch, failure_backend)
    failed = _element(failed.button, "Generate report").click().run(timeout=20)
    assert any(
        "context_retrieval_unavailable" in item.value for item in failed.warning
    )

def test_failed_run_shows_phase_progress_and_offers_no_recovery_actions(monkeypatch):
    backend = FakeMFIBackend(status={
        "run_id": "mfi-run-7",
        "status": "failed",
        "current_node": "review_markets",
        "progress_pct": 0,
        "error": "Attempts exhausted for review_markets; generate the report again",
        "metadata": {"generation_diagnostics": {"progress_pct": 45, "phases": [
            {"node": "draft_markets", "status": "succeeded", "reused": False},
            {"node": "review_markets", "status": "failed", "reused": False},
        ]}},
    })
    app = _app(monkeypatch, backend, run_id="mfi-run-7")

    assert not app.exception
    assert any("Attempts exhausted" in warning.value for warning in app.warning)
    assert any("Generate it again" in caption.value for caption in app.caption)
    assert not [button for button in app.button
                if button.label in {"Resume", "Prepare incomplete draft", "Prepare analytical download"}]
    paths = [path for _, path, _ in backend.requests]
    assert "/mfi-drafter/status/mfi-run-7" in paths
    assert not [path for path in paths if any(part in path for part in ("/resume/", "/draft/", "/analysis/"))]
