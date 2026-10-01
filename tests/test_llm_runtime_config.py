from __future__ import annotations

import pytest

from app.shared.llm import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMRuntimeConfigurationError,
    Tracer,
    llm_runtime_config,
    llm_runtime_status,
    market_monitor_profile,
)
from app.shared.llm import tracing
from app.services.mfi_drafter import router


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    for name in (
        "LLM_MODEL",
        "VERTEX_LOCATION",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
        "LLM_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_llm_runtime_defaults_and_overrides(monkeypatch) -> None:
    defaults = llm_runtime_config()
    assert defaults.default_timeout_seconds == 90.0
    assert defaults.max_retries == 2

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    configured = llm_runtime_config()
    assert configured.default_timeout_seconds == 120.0
    assert configured.max_retries == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_TIMEOUT_SECONDS", "zero"),
        ("LLM_TIMEOUT_SECONDS", "NaN"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "601"),
        ("LLM_MAX_RETRIES", "-1"),
        ("LLM_MAX_RETRIES", "11"),
        ("LLM_MAX_OUTPUT_TOKENS", "0"),
    ],
)
def test_invalid_llm_runtime_configuration_fails_closed(monkeypatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(LLMRuntimeConfigurationError) as caught:
        llm_runtime_config()
    assert caught.value.code == "llm_runtime_configuration_invalid"
    assert caught.value.field == name
    status = llm_runtime_status().model_dump()
    assert status["configuration_status"] == "invalid"
    assert status["error_code"] == "llm_runtime_configuration_invalid"


def test_market_monitor_profile_keeps_its_settings(monkeypatch) -> None:
    profile = market_monitor_profile()
    assert (profile.model, profile.location, profile.temperature, profile.timeout_seconds) == (
        "gemini-2.5-pro", "us-central1", 0.0, 90.0)
    assert (profile.candidate_count, profile.max_output_tokens, profile.attempts) == (1, None, 2)

    # LLM_MAX_RETRIES has always counted attempts (LangChain's reading): 0 and 1 both mean a single attempt.
    for retries, attempts in (("0", 1), ("1", 1), ("3", 3)):
        monkeypatch.setenv("LLM_MAX_RETRIES", retries)
        assert market_monitor_profile().attempts == attempts
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    assert market_monitor_profile().max_output_tokens == 4096


def test_light_mfi_profile_is_independent_of_legacy_model_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("MFI_DRAFTER_ANALYSIS_VERSION", "2")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "invalid")
    assert router._require_enabled_release_control().enabled
    assert router.light_runtime_status()["timeout"] == 600
    assert router.light_runtime_status()["sdk_retries"] == 0
    assert llm_runtime_status().configuration_status == "invalid"


def test_runtime_status_exposes_only_sanitized_settings(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "225")
    assert llm_runtime_status().model_dump() == {
        "configuration_status": "configured",
        "default_timeout_seconds": 225.0,
        "max_retries": 2,
        "error_code": None,
        "error_field": None,
    }


def test_call_can_complete_after_sixty_seconds_with_a_longer_deadline(monkeypatch) -> None:
    ticks = iter([100.0, 161.0])
    monkeypatch.setattr(tracing.time, "perf_counter", lambda: next(ticks))

    class Provider:
        def generate(self, _profile, _request):
            return LLMResponse(outcome="completed", text='{"flags": []}')

    profile = market_monitor_profile()
    client = LLMClient(profile, tracer=Tracer(service="market-monitor", run_id="long-review"), provider=Provider())
    result = client.generate_json(prompt="review", node="red_team", operation="market_monitor.red_team_review.v1",
                                  validator=lambda payload: payload["flags"])
    assert result.value == []
    call = client.tracer.snapshot()["calls"][0]
    assert call["duration_ms"] == 61000
    assert call["configured_timeout_seconds"] == 90.0

    ticks = iter([100.0, 281.0])
    request = LLMRequest(operation="o", node="n", parts=["review"], timeout_seconds=180)
    client.generate(request)
    assert client.tracer.snapshot()["calls"][1]["configured_timeout_seconds"] == 180.0
