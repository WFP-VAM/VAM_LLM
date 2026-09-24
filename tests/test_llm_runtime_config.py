from __future__ import annotations

import pytest

from app.shared import llm
from app.shared import llm_observability as observability
from app.shared.llm_observability import LLMTraceSession
from app.services.mfi_drafter import router


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    for name in (
        "LLM_TIMEOUT_SECONDS",
        "MFI_MARKET_DRAFT_TIMEOUT_SECONDS",
        "MFI_RED_TEAM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
        "LLM_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    llm._model_instances.clear()
    llm._model_instance = None
    yield
    llm._model_instances.clear()
    llm._model_instance = None


def test_llm_runtime_defaults_and_overrides(monkeypatch) -> None:
    defaults = llm.llm_runtime_config()
    assert defaults.default_timeout_seconds == 90.0
    assert defaults.mfi_market_draft_timeout_seconds == 180.0
    assert defaults.mfi_red_team_timeout_seconds == 600.0
    assert defaults.max_retries == 2

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("MFI_MARKET_DRAFT_TIMEOUT_SECONDS", "210")
    monkeypatch.setenv("MFI_RED_TEAM_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    configured = llm.llm_runtime_config()
    assert configured.default_timeout_seconds == 120.0
    assert configured.mfi_market_draft_timeout_seconds == 210.0
    assert configured.mfi_red_team_timeout_seconds == 240.0
    assert configured.max_retries == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_TIMEOUT_SECONDS", "zero"),
        ("LLM_TIMEOUT_SECONDS", "NaN"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("MFI_MARKET_DRAFT_TIMEOUT_SECONDS", "601"),
        ("MFI_RED_TEAM_TIMEOUT_SECONDS", "601"),
        ("LLM_MAX_RETRIES", "-1"),
        ("LLM_MAX_RETRIES", "11"),
        ("LLM_MAX_OUTPUT_TOKENS", "0"),
    ],
)
def test_invalid_llm_runtime_configuration_fails_closed(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(llm.LLMRuntimeConfigurationError) as caught:
        llm.require_llm_runtime_config()
    assert caught.value.code == "llm_runtime_configuration_invalid"
    assert caught.value.field == name
    status = llm.llm_runtime_status().model_dump()
    assert status["configuration_status"] == "invalid"
    assert status["error_code"] == "llm_runtime_configuration_invalid"


def test_light_mfi_profile_is_independent_of_legacy_model_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("MFI_DRAFTER_ANALYSIS_VERSION", "2")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "invalid")
    assert router._require_enabled_release_control().enabled
    assert router.light_runtime_status()["timeout"] == 600
    assert router.light_runtime_status()["sdk_retries"] == 0
    assert llm.llm_runtime_status().configuration_status == "invalid"


def test_vertex_clients_are_cached_by_effective_settings(monkeypatch) -> None:
    created = []

    class FakeVertex:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(kwargs)

    monkeypatch.setattr(llm, "ChatVertexAI", FakeVertex)
    monkeypatch.setattr(llm, "_get_vertex_project_id", lambda: "project")
    default_a = llm.get_model()
    default_b = llm.get_model()
    red_team_a = llm.get_model(timeout_seconds=180, max_retries=2)
    red_team_b = llm.get_model(timeout_seconds=180, max_retries=2)
    assert default_a is default_b
    assert red_team_a is red_team_b
    assert default_a is not red_team_a
    assert [item["timeout"] for item in created] == [90.0, 180.0]


def test_runtime_status_exposes_sanitized_market_deadline(monkeypatch) -> None:
    monkeypatch.setenv("MFI_MARKET_DRAFT_TIMEOUT_SECONDS", "225")
    status = llm.llm_runtime_status().model_dump()
    assert status == {
        "configuration_status": "configured",
        "default_timeout_seconds": 90.0,
        "mfi_market_draft_timeout_seconds": 225.0,
        "mfi_red_team_timeout_seconds": 600.0,
        "max_retries": 2,
        "error_code": None,
        "error_field": None,
    }


def test_call_can_complete_after_sixty_seconds_with_red_team_deadline(
    monkeypatch,
) -> None:
    ticks = iter([100.0, 161.0])
    monkeypatch.setattr(observability.time, "perf_counter", lambda: next(ticks))

    class Model:
        def invoke(self, _messages):
            return type("Response", (), {"content": '{"flags": []}'})()

    session = LLMTraceSession(service="mfi-drafter", run_id="long-red-team")
    result = session.invoke_json(
        model=Model(),
        messages=[{"role": "user", "content": "review"}],
        node="red_team",
        operation="mfi.red_team_review.v6",
        validator=lambda payload: payload["flags"],
        timeout_seconds=180,
        max_retries=2,
    )
    assert result.value == []
    call = session.snapshot()["calls"][0]
    assert call["duration_ms"] == 61000
    assert call["configured_timeout_seconds"] == 180.0
