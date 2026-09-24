import gzip
import hashlib
import json
import logging
from io import StringIO
import threading
from pathlib import Path

import pytest

from app.shared import llm_observability as observability
from app.shared.llm_observability import (
    LLMCallError,
    LLMTraceSession,
    extract_response_text,
    parse_json_object,
)


class FakeResponse:
    def __init__(self, content, *, response_id="response-1"):
        self.content = content
        self.id = response_id
        self.response_metadata = {"finish_reason": "STOP"}
        self.usage_metadata = {
            "input_tokens": 11,
            "output_tokens": 7,
            "thoughts_token_count": 3,
            "total_tokens": 21,
        }


class FakeModel:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def test_extract_response_text_supports_strings_and_langchain_blocks():
    assert extract_response_text(FakeResponse(" hello "))[:2] == ("hello", "string")
    text, shape, raw = extract_response_text(
        FakeResponse(
            [
                {"type": "text", "text": "first"},
                {"type": "output_text", "text": "second"},
            ]
        )
    )
    assert text == "first\nsecond"
    assert shape == "content_blocks"
    assert raw[0]["text"] == "first"


@pytest.mark.parametrize("content", ["", "   ", [], [{"type": "image"}], 42])
def test_extract_response_text_rejects_empty_or_unsupported_content(content):
    with pytest.raises((TypeError, ValueError)):
        extract_response_text(FakeResponse(content))


def test_parse_json_object_accepts_fences_and_rejects_non_objects():
    assert parse_json_object('```json\n{"answer": 1}\n```') == {"answer": 1}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")
    with pytest.raises(json.JSONDecodeError):
        parse_json_object('{"broken": }')


def test_successful_json_call_records_sanitized_metadata(monkeypatch):
    monkeypatch.delenv("LLM_TRACE_PAYLOADS", raising=False)
    snapshots = []
    session = LLMTraceSession(service="mfi-drafter", run_id="mfi_public", sink=snapshots.append)
    model = FakeModel(FakeResponse('{"value": 4}'))
    messages = [{"role": "user", "content": "private prompt"}]

    result = session.invoke_json(
        model=model,
        messages=messages,
        node="dimension_drafter",
        operation="mfi.dimension_drafting.v2",
        artifact_type="dimension",
        artifact_id="Price",
        validator=lambda payload: payload["value"],
    )

    assert result.value == 4
    diagnostic = session.snapshot()
    assert diagnostic["run_id"] == "mfi_public"
    assert diagnostic["succeeded_calls"] == 1
    assert diagnostic["calls"][0]["sequence"] == 1
    assert diagnostic["calls"][0]["prompt_sha256"] == hashlib.sha256(
        json.dumps(messages[0], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert diagnostic["calls"][0]["response_sha256"] == hashlib.sha256(
        b'{"value": 4}'
    ).hexdigest()
    assert diagnostic["calls"][0]["token_usage"] == {
        "prompt_tokens": 11,
        "candidate_tokens": 7,
        "thought_tokens": 3,
        "total_tokens": 21,
    }
    public_json = json.dumps(diagnostic)
    assert "private prompt" not in public_json
    assert '{"value": 4}' not in public_json
    assert snapshots[0]["status"] == "running"
    assert snapshots[-1]["status"] == "completed"


@pytest.mark.parametrize(
    ("response", "error", "expected_code", "expected_stage"),
    [
        (None, RuntimeError("provider token=secret"), "llm_transport_error", "transport"),
        (FakeResponse(""), None, "llm_empty_or_unreadable_response", "response_extraction"),
        (FakeResponse("not json"), None, "llm_invalid_json", "json_parse"),
        (FakeResponse('{"wrong": true}'), None, "llm_response_contract_error", "contract_validation"),
    ],
)
def test_json_failures_raise_typed_error_and_preserve_diagnostics(
    response, error, expected_code, expected_stage
):
    session = LLMTraceSession(service="market-monitor", run_id="run_public")
    model = FakeModel(response, error=error)

    with pytest.raises(LLMCallError) as caught:
        session.invoke_json(
            model=model,
            messages=[{"role": "user", "content": "prompt"}],
            node="trend_analyst",
            operation="market_monitor.trend_analysis.v1",
            validator=lambda payload: payload["required"],
        )

    assert caught.value.failure_code == expected_code
    assert caught.value.stage == expected_stage
    diagnostic = session.snapshot()
    assert diagnostic["status"] == "failed"
    assert diagnostic["failed_calls"] == 1
    call = diagnostic["calls"][0]
    assert call["failure_code"] == expected_code
    assert call["failure_stage"] == expected_stage
    assert "secret" not in str(call.get("error_message"))
    assert diagnostic["contract_failed_calls"] == (0 if expected_stage == "transport" else 1)


def test_invalid_concatenated_json_records_content_free_structure():
    raw = '{"flags": []}\n{"flags": []}'
    session = LLMTraceSession(service="mfi-drafter", run_id="mfi-json-shape")

    with pytest.raises(LLMCallError) as caught:
        session.invoke_json(
            model=FakeModel(FakeResponse(raw)),
            messages=[{"role": "user", "content": "private prompt"}],
            node="red_team",
            operation="mfi.red_team_review.v6",
            validator=lambda payload: payload,
        )

    assert caught.value.failure_code == "llm_invalid_json"
    assert caught.value.raw_text == raw
    assert "raw_text" not in caught.value.to_public_dict()
    diagnostic = session.snapshot()
    call = diagnostic["calls"][0]
    assert call["json_root_value_count"] == 2
    assert call["json_trailing_character_count"] > 0
    assert call["json_error_line"] == 2
    assert call["json_error_column"] == 1
    public_json = json.dumps(diagnostic)
    assert raw not in public_json
    assert '"flags"' not in public_json


def test_recovered_call_is_successful_at_run_level_but_remains_auditable():
    session = LLMTraceSession(service="mfi-drafter", run_id="mfi-recovered")
    with pytest.raises(LLMCallError) as caught:
        session.invoke_json(
            model=FakeModel(FakeResponse('{"flags": []}{"flags": []}')),
            messages=[{"role": "user", "content": "prompt"}],
            node="red_team",
            operation="mfi.red_team_review.v6",
            validator=lambda payload: payload,
        )
    session.mark_recovered(caught.value.call_id)

    diagnostic = session.snapshot()
    assert diagnostic["status"] == "completed"
    assert diagnostic["succeeded_calls"] == 1
    assert diagnostic["recovered_calls"] == 1
    assert diagnostic["failed_calls"] == 0
    assert diagnostic["contract_failed_calls"] == 0
    assert diagnostic["calls"][0]["status"] == "recovered"
    assert diagnostic["calls"][0]["failure_code"] == "llm_invalid_json"


def test_started_snapshot_is_visible_before_blocking_model_returns():
    entered = threading.Event()
    release = threading.Event()
    snapshots = []

    class BlockingModel:
        def invoke(self, _messages):
            entered.set()
            assert release.wait(timeout=2)
            return FakeResponse("ready")

    session = LLMTraceSession(service="mfi-drafter", run_id="mfi_block", sink=snapshots.append)
    thread = threading.Thread(
        target=lambda: session.invoke_text(
            model=BlockingModel(),
            messages=[{"role": "user", "content": "prompt"}],
            node="red_team",
            operation="mfi.red_team_review.v6",
        )
    )
    thread.start()
    assert entered.wait(timeout=2)
    assert snapshots[0]["status"] == "running"
    assert snapshots[0]["current_call_id"]
    release.set()
    thread.join(timeout=2)
    assert session.snapshot()["status"] == "completed"


def test_payload_capture_uses_deterministic_private_gzip_path(monkeypatch):
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "gs://private-bucket/operator-prefix")
    stored = {}

    def fake_persist(**kwargs):
        stored.update(kwargs)
        stored["gzip"] = gzip.compress(json.dumps(kwargs["payload"]).encode("utf-8"))
        return (
            "gs://private-bucket/operator-prefix/llm-traces/v1/"
            f"{kwargs['service']}/{kwargs['run_id']}/{kwargs['sequence']:04d}-{kwargs['call_id']}.json.gz"
        )

    monkeypatch.setattr(observability, "_persist_payload", fake_persist)
    session = LLMTraceSession(service="market-monitor", run_id="run-private")
    session.invoke_text(
        model=FakeModel(FakeResponse("response body")),
        messages=[{"role": "user", "content": "prompt body"}],
        node="module_orchestrator",
        operation="market_monitor.exchange_rate_module.v1",
    )

    diagnostic = session.snapshot()
    assert diagnostic["calls"][0]["payload_persistence_status"] == "stored"
    payload = json.loads(gzip.decompress(stored["gzip"]))
    assert payload["request"]["messages"][0]["content"] == "prompt body"
    assert payload["response"]["normalized_text"] == "response body"
    assert payload["diagnostic"]["status"] == "succeeded"


def test_payload_storage_failure_does_not_change_valid_llm_result(monkeypatch):
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "gs://private-bucket")
    monkeypatch.setattr(
        observability,
        "_persist_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )
    session = LLMTraceSession(service="mfi-drafter", run_id="mfi-storage")
    result = session.invoke_text(
        model=FakeModel(FakeResponse("valid prose")),
        messages=[{"role": "user", "content": "prompt"}],
        node="dimension_drafter",
        operation="mfi.dimension_drafting.v2",
    )
    assert result.value == "valid prose"
    diagnostic = session.snapshot()
    assert diagnostic["succeeded_calls"] == 1
    assert diagnostic["payload_persistence_failures"] == 1


def test_observability_configuration_fails_closed_for_invalid_payload_uri(monkeypatch):
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "https://public.example/traces")
    config = observability.observability_config()
    assert config.payload_capture_enabled is True
    assert config.payload_storage_configured is False
    assert config.configuration_status == "invalid"


def test_structured_logs_never_include_prompt_or_response_bodies():
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    observability._TRACE_LOGGER.addHandler(handler)
    try:
        session = LLMTraceSession(service="mfi-drafter", run_id="mfi-log")
        session.invoke_text(
            model=FakeModel(FakeResponse("TOP SECRET RESPONSE")),
            messages=[{"role": "user", "content": "TOP SECRET PROMPT"}],
            node="dimension_drafter",
            operation="mfi.dimension_drafting.v2",
        )
    finally:
        observability._TRACE_LOGGER.removeHandler(handler)
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert lines
    log_text = "\n".join(lines)
    assert "TOP SECRET PROMPT" not in log_text
    assert "TOP SECRET RESPONSE" not in log_text
    for line in lines:
        json.loads(line)


def test_report_workflows_have_no_direct_model_invocations():
    root = Path(__file__).resolve().parents[1]
    # The only permitted invoke: the compiled Market Monitor graph, and the MFI model runtime.
    for relative, permitted in (
        ("app/services/mfi_drafter/light_graph.py", "runtime.invoke("),
        ("app/services/market_monitor/graph.py", "agent.invoke("),
    ):
        source = (root / relative).read_text(encoding="utf-8")
        direct_invocations = [
            line.strip()
            for line in source.splitlines()
            if ".invoke(" in line and permitted not in line
        ]
        assert direct_invocations == []
