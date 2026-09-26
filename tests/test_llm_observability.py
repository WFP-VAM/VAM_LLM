"""The shared LLM client and tracer: stable failure codes, sanitized records, retries, payload capture and logs."""
import gzip
import hashlib
import json
import logging
import threading
from dataclasses import replace
from io import StringIO
from pathlib import Path

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.shared.llm import (
    FilePart,
    LLMCallError,
    LLMClient,
    LLMRequest,
    LLMResponse,
    ModelProfile,
    Tracer,
    parse_json_object,
)
from app.shared.llm import tracing
from app.shared.llm.vertex import VertexProvider, reply_from

PROFILE = ModelProfile(service="test-service", model="gemini-test", location="global",
                       temperature=0.0, timeout_seconds=90.0)
USAGE = {"prompt_tokens": 11, "candidate_tokens": 7, "thought_tokens": 3, "total_tokens": 21}


class FakeProvider:
    """Replies in order (text, or an exception to raise); the last reply repeats. Records every request."""

    def __init__(self, *replies, finish_reason="STOP"):
        self.replies = list(replies)
        self.finish_reason = finish_reason
        self.requests = []

    def generate(self, _profile, request):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return LLMResponse(text=reply, finish_reason=self.finish_reason, response_id="response-1", usage=dict(USAGE))


def make_client(provider, *, run_id="run_public", live=None, audit=None, profile=PROFILE):
    tracer = Tracer(service=profile.service, run_id=run_id, live=live, audit=audit)
    return LLMClient(profile, tracer=tracer, provider=provider, sleep=lambda _seconds: None)


def make_request(prompt="prompt", **fields):
    return LLMRequest(operation="test.operation.v1", node="test_node", parts=[prompt], **fields)


def test_reply_keeps_the_answer_without_thoughts_and_reads_usage():
    response = types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text="reasoning", thought=True), types.Part(text="answer")]),
            finish_reason=types.FinishReason.MAX_TOKENS)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=11, candidates_token_count=7, thoughts_token_count=3, total_token_count=21),
        response_id="r-1", model_version="gemini-test-001")
    reply = reply_from(response)
    assert (reply.text, reply.finish_reason, reply.response_id, reply.model_version) == (
        "answer", "MAX_TOKENS", "r-1", "gemini-test-001")
    assert reply.usage == USAGE
    blocked = reply_from(types.GenerateContentResponse())
    assert (blocked.text, blocked.finish_reason) == ("", "BLOCKED")


def test_parse_json_object_accepts_fences_and_rejects_non_objects():
    assert parse_json_object('```json\n{"answer": 1}\n```') == {"answer": 1}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")
    with pytest.raises(json.JSONDecodeError):
        parse_json_object('{"broken": }')


def test_successful_json_call_records_sanitized_metadata(monkeypatch):
    monkeypatch.delenv("LLM_TRACE_PAYLOADS", raising=False)
    snapshots = []
    client = make_client(FakeProvider('{"value": 4}'), run_id="mfi_public", live=snapshots.append)

    result = client.generate(make_request("private prompt", artifact_type="dimension", artifact_id="Price"),
                             parse=parse_json_object, validate=lambda payload, _response: payload["value"])

    assert result.value == 4
    diagnostic = client.tracer.snapshot()
    assert diagnostic["run_id"] == "mfi_public"
    assert diagnostic["succeeded_calls"] == 1
    call = diagnostic["calls"][0]
    assert call["sequence"] == 1 and call["attempt"] == 1
    assert call["prompt_sha256"] == hashlib.sha256(b"private prompt").hexdigest()
    assert call["response_sha256"] == hashlib.sha256(b'{"value": 4}').hexdigest()
    assert call["token_usage"] == USAGE
    public_json = json.dumps(diagnostic)
    assert "private prompt" not in public_json
    assert '{"value": 4}' not in public_json
    assert snapshots[0]["status"] == "running"
    assert snapshots[-1]["status"] == "completed"


@pytest.mark.parametrize(
    ("reply", "expected_code", "expected_stage"),
    [
        (RuntimeError("provider token=secret"), "llm_transport_error", "transport"),
        ("", "llm_empty_or_unreadable_response", "response_extraction"),
        ("not json", "llm_invalid_json", "json_parse"),
        ('{"wrong": true}', "llm_response_contract_error", "contract_validation"),
    ],
)
def test_failures_raise_a_typed_error_and_keep_sanitized_diagnostics(reply, expected_code, expected_stage):
    client = make_client(FakeProvider(reply))

    with pytest.raises(LLMCallError) as caught:
        client.generate(make_request(), parse=parse_json_object, validate=lambda payload, _response: payload["required"])

    assert caught.value.failure_code == expected_code
    assert caught.value.stage == expected_stage
    diagnostic = client.tracer.snapshot()
    assert diagnostic["status"] == "failed"
    assert diagnostic["failed_calls"] == 1
    call = diagnostic["calls"][0]
    assert call["failure_code"] == expected_code
    assert call["failure_stage"] == expected_stage
    assert "secret" not in str(call.get("error_message"))
    assert diagnostic["contract_failed_calls"] == (0 if expected_stage == "transport" else 1)


def test_invalid_concatenated_json_records_content_free_structure():
    raw = '{"flags": []}\n{"flags": []}'
    client = make_client(FakeProvider(raw), run_id="json-shape")

    with pytest.raises(LLMCallError) as caught:
        client.generate(make_request("private prompt"), parse=parse_json_object)

    assert caught.value.failure_code == "llm_invalid_json"
    assert caught.value.raw_text == raw
    assert "raw_text" not in caught.value.to_public_dict()
    call = client.tracer.snapshot()["calls"][0]
    assert call["json_root_value_count"] == 2
    assert call["json_trailing_character_count"] > 0
    assert call["json_error_line"] == 2
    assert call["json_error_column"] == 1
    public_json = json.dumps(client.tracer.snapshot())
    assert raw not in public_json
    assert '"flags"' not in public_json


def test_public_error_names_the_call_and_its_artifact_only():
    error = LLMCallError(failure_code="llm_transport_error", call_id="llm-1", node="red_team",
                         operation="market_monitor.red_team_review.v1", stage="transport", raw_text="private",
                         artifact_type="global", artifact_id="qa_review")
    assert error.to_public_dict() == {
        "code": "llm_call_failed", "failure_code": "llm_transport_error", "call_id": "llm-1", "node": "red_team",
        "operation": "market_monitor.red_team_review.v1", "stage": "transport",
        "artifact_type": "global", "artifact_id": "qa_review"}
    assert "private" not in str(error)


def test_recovered_call_is_successful_at_run_level_but_remains_auditable():
    client = make_client(FakeProvider('{"flags": []}{"flags": []}'), run_id="recovered")
    with pytest.raises(LLMCallError) as caught:
        client.generate(make_request(), parse=parse_json_object)
    client.tracer.mark_recovered(caught.value.call_id, disposition="recovered_by_repair")

    diagnostic = client.tracer.snapshot()
    assert diagnostic["status"] == "completed"
    assert (diagnostic["succeeded_calls"], diagnostic["recovered_calls"]) == (1, 1)
    assert (diagnostic["failed_calls"], diagnostic["contract_failed_calls"]) == (0, 0)
    assert diagnostic["calls"][0]["status"] == "recovered"
    assert diagnostic["calls"][0]["failure_code"] == "llm_invalid_json"


def test_transient_errors_are_retried_and_the_failed_attempt_recovered():
    busy = genai_errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})
    provider = FakeProvider(busy, "done")
    client = make_client(provider, profile=replace(PROFILE, attempts=2))

    assert client.generate(make_request()).value == "done"

    diagnostic = client.tracer.snapshot()
    assert diagnostic["status"] == "completed" and diagnostic["total_calls"] == 2
    first, second = diagnostic["calls"]
    assert (first["status"], first["disposition"], first["transient"]) == ("recovered", "recovered_by_retry", True)
    assert (second["attempt"], second["retry_of"], second["status"]) == (2, first["call_id"], "succeeded")


@pytest.mark.parametrize("link,disposition", [("retry_of", "recovered_by_retry"), ("repair_of", "recovered_by_repair")])
def test_a_later_call_linked_to_a_failed_one_is_its_next_attempt_and_recovers_it(link, disposition):
    # Drafters that run their own attempts (MFI) link each one to the call it retries or repairs.
    client = make_client(FakeProvider("{", '{"ok": true}'))
    with pytest.raises(LLMCallError) as caught:
        client.generate(make_request(), parse=parse_json_object)
    client.generate(make_request("second", **{link: caught.value.call_id}), parse=parse_json_object)

    diagnostic = client.tracer.snapshot()
    first, second = diagnostic["calls"]
    assert (first["status"], first["disposition"]) == ("recovered", disposition)
    assert (second["attempt"], second[link], second["status"]) == (2, first["call_id"], "succeeded")
    assert (diagnostic["status"], diagnostic["failed_calls"], diagnostic["recovered_calls"]) == ("completed", 0, 1)


@pytest.mark.parametrize("error", [
    genai_errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}),
    genai_errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}),
])
def test_permission_and_argument_errors_are_not_retried(error):
    provider = FakeProvider(error, "never reached")
    client = make_client(provider, profile=replace(PROFILE, attempts=3))
    with pytest.raises(LLMCallError) as caught:
        client.generate(make_request())
    assert (caught.value.stage, caught.value.transient, len(provider.requests)) == ("transport", False, 1)


def test_truncated_reply_fails_only_when_the_request_says_so():
    client = make_client(FakeProvider("cut mid-sen", finish_reason="MAX_TOKENS"))
    assert client.generate(make_request()).value == "cut mid-sen"
    with pytest.raises(LLMCallError) as caught:
        client.generate(make_request(fail_on_truncation=True))
    assert (caught.value.failure_code, caught.value.stage) == ("llm_response_truncated", "response_extraction")


class Audit:
    """Records the hooks it receives; `fail_at` names the one that raises."""

    def __init__(self, fail_at=None):
        self.fail_at, self.events = fail_at, []

    def _hook(self, name, record):
        self.events.append((name, record.status))
        if self.fail_at == name:
            raise OSError(f"{name} store down")

    def requested(self, record, request):
        self._hook("requested", record)

    def responded(self, record, response):
        self._hook("responded", record)

    def validated(self, record):
        self._hook("validated", record)

    def failed(self, record):
        self._hook("failed", record)


@pytest.mark.parametrize("fail_at,code", [("requested", "llm_request_persistence_error"),
                                          ("responded", "llm_response_persistence_error"),
                                          ("validated", "llm_response_persistence_error")])
def test_audit_failures_fail_the_call_with_the_original_error(fail_at, code):
    provider = FakeProvider("reply")
    audit = Audit(fail_at)
    client = make_client(provider, audit=audit)
    with pytest.raises(OSError):
        client.generate(make_request())
    call = client.tracer.snapshot()["calls"][0]
    assert (call["status"], call["failure_code"]) == ("failed", code)
    assert len(provider.requests) == (0 if fail_at == "requested" else 1)
    assert audit.events[-1][0] == fail_at  # an audit that failed is not asked to record the failure


def test_audit_follows_each_attempt_and_its_errors_never_hide_the_call_failure():
    busy = genai_errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})
    audit = Audit()
    client = make_client(FakeProvider(busy, "done"), audit=audit, profile=replace(PROFILE, attempts=2))
    client.generate(make_request())
    assert audit.events == [("requested", "started"), ("failed", "failed"), ("requested", "started"),
                            ("responded", "started"), ("validated", "started")]
    audit = Audit("failed")
    client = make_client(FakeProvider("not json"), audit=audit)
    with pytest.raises(LLMCallError) as caught:
        client.generate(make_request(), parse=parse_json_object)
    assert caught.value.failure_code == "llm_invalid_json" and audit.events[-1] == ("failed", "failed")


def test_a_run_can_keep_payloads_out_of_the_trace_bucket(monkeypatch):
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "gs://private-bucket")
    monkeypatch.setattr(tracing, "_persist_payload", lambda **_kwargs: pytest.fail("payload persisted"))
    tracer = Tracer(service="seasonal-outlook", run_id="audited", capture_payloads=False)
    LLMClient(PROFILE, tracer=tracer, provider=FakeProvider("reply")).generate(make_request())
    diagnostic = tracer.snapshot()
    assert diagnostic["payload_capture_enabled"] is False
    assert diagnostic["calls"][0]["payload_persistence_status"] == "disabled"


def test_started_snapshot_is_visible_before_a_blocking_provider_returns():
    entered, release, snapshots = threading.Event(), threading.Event(), []

    class BlockingProvider:
        def generate(self, _profile, _request):
            entered.set()
            assert release.wait(timeout=2)
            return LLMResponse(text="ready")

    client = make_client(BlockingProvider(), live=snapshots.append)
    thread = threading.Thread(target=lambda: client.generate(make_request()))
    thread.start()
    assert entered.wait(timeout=2)
    assert snapshots[0]["status"] == "running"
    assert snapshots[0]["current_call_id"] == snapshots[0]["active_call_ids"][0]
    release.set()
    thread.join(timeout=2)
    assert client.tracer.snapshot()["status"] == "completed"


def test_payload_capture_uses_deterministic_private_gzip_path(monkeypatch):
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "gs://private-bucket/operator-prefix")
    stored = {}

    def fake_persist(**kwargs):
        stored.update(kwargs)
        stored["gzip"] = gzip.compress(json.dumps(kwargs["payload"]).encode("utf-8"))
        return ("gs://private-bucket/operator-prefix/llm-traces/v1/"
                f"{kwargs['service']}/{kwargs['run_id']}/{kwargs['sequence']:04d}-{kwargs['call_id']}.json.gz")

    monkeypatch.setattr(tracing, "_persist_payload", fake_persist)
    client = make_client(FakeProvider("response body"), run_id="run-private")
    client.generate(make_request("prompt body"))

    assert client.tracer.snapshot()["calls"][0]["payload_persistence_status"] == "stored"
    payload = json.loads(gzip.decompress(stored["gzip"]))
    assert payload["request"]["parts"] == ["prompt body"]
    assert payload["response"]["text"] == "response body"
    assert payload["diagnostic"]["status"] == "succeeded"


def test_payload_storage_failure_does_not_change_a_valid_result(monkeypatch):
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "gs://private-bucket")
    monkeypatch.setattr(tracing, "_persist_payload",
                        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("storage unavailable")))
    client = make_client(FakeProvider("valid prose"), run_id="storage")
    assert client.generate(make_request()).value == "valid prose"
    diagnostic = client.tracer.snapshot()
    assert diagnostic["succeeded_calls"] == 1
    assert diagnostic["payload_persistence_failures"] == 1


def test_observability_configuration_fails_closed_for_invalid_payload_uri(monkeypatch):
    monkeypatch.setenv("LLM_TRACE_PAYLOADS", "true")
    monkeypatch.setenv("LLM_TRACE_GCS_URI", "https://public.example/traces")
    config = tracing.observability_config()
    assert config.payload_capture_enabled is True
    assert config.payload_storage_configured is False
    assert config.configuration_status == "invalid"


def test_structured_logs_never_include_prompt_or_response_bodies():
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    tracing._TRACE_LOGGER.addHandler(handler)
    try:
        make_client(FakeProvider("TOP SECRET RESPONSE"), run_id="log").generate(make_request("TOP SECRET PROMPT"))
    finally:
        tracing._TRACE_LOGGER.removeHandler(handler)
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert lines
    log_text = "\n".join(lines)
    assert "TOP SECRET PROMPT" not in log_text
    assert "TOP SECRET RESPONSE" not in log_text
    for line in lines:
        json.loads(line)


def test_vertex_provider_sends_the_profile_settings_and_nothing_else(vertex_wire):
    calls, _ = vertex_wire
    profile = replace(PROFILE, project="proj", candidate_count=1)
    reply = VertexProvider().generate(profile, make_request("hello"))

    assert "/projects/proj/locations/global/publishers/google/models/gemini-test:generateContent" in calls[0]["url"]
    assert calls[0]["body"] == {"contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                                "generationConfig": {"temperature": 0.0, "candidateCount": 1}}
    assert calls[0]["timeout"] == 90.0
    assert (reply.text, reply.finish_reason, reply.usage["total_tokens"]) == ("ok", "STOP", 4)


def test_vertex_provider_sends_system_files_schema_thinking_and_headers(vertex_wire):
    calls, _ = vertex_wire
    profile = replace(PROFILE, project="proj", temperature=1.0, thinking_level="HIGH", include_thoughts=False,
                      media_resolution="MEDIA_RESOLUTION_HIGH", headers=(("X-Vertex-AI-LLM-Request-Type", "shared"),))
    request = LLMRequest(operation="o", node="n", system="rules", parts=["payload", FilePart("gs://bucket/map", "image/png")],
                         response_schema={"type": "OBJECT", "properties": {"a": {"type": "STRING"}}}, json_output=True,
                         max_output_tokens=32768, timeout_seconds=1200)
    VertexProvider().generate(profile, request)

    body = calls[0]["body"]
    assert body["systemInstruction"]["parts"] == [{"text": "rules"}]
    # google-genai keeps the spelling of some nested fields; Vertex accepts both.
    assert body["contents"][0]["parts"][1]["fileData"] in ({"fileUri": "gs://bucket/map", "mimeType": "image/png"},
                                                         {"file_uri": "gs://bucket/map", "mime_type": "image/png"})
    config = body["generationConfig"]
    assert (config["responseMimeType"], config["maxOutputTokens"], config["mediaResolution"]) == (
        "application/json", 32768, "MEDIA_RESOLUTION_HIGH")
    assert config["thinkingConfig"] in ({"thinkingLevel": "HIGH", "includeThoughts": False},
                                        {"thinking_level": "HIGH", "include_thoughts": False})
    assert calls[0]["headers"]["X-Vertex-AI-LLM-Request-Type"] == "shared"
    assert calls[0]["timeout"] == 1200.0


def test_vertex_provider_leaves_retries_to_the_client(vertex_wire):
    calls, replies = vertex_wire
    replies.append(httpx.Response(503, json={"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}))
    with pytest.raises(genai_errors.ServerError):
        VertexProvider().generate(replace(PROFILE, project="proj"), make_request())
    assert len(calls) == 1


def test_vertex_provider_reuses_one_sdk_client_per_project_location_and_headers(monkeypatch):
    created = []

    class FakeSdkClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(genai, "Client", FakeSdkClient)
    provider = VertexProvider()
    first = provider._client(replace(PROFILE, project="proj"))
    assert provider._client(replace(PROFILE, project="proj", timeout_seconds=600)) is first
    assert provider._client(replace(PROFILE, project="other")) is not first
    assert len(created) == 2
    assert created[0]["http_options"].retry_options.attempts == 1


# Model SDKs may be used only by the shared LLM package.
_SDK_IMPORTS = ("google.genai", "from google import genai", "langchain_google_vertexai", "import vertexai",
                "from vertexai", "google.cloud.aiplatform")


def test_only_the_shared_client_talks_to_the_model_sdk():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "app").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("app/shared/llm/"):
            continue
        source = path.read_text(encoding="utf-8")
        offenders += [f"{relative}: {name}" for name in _SDK_IMPORTS if name in source]
    assert offenders == []


def test_report_workflows_have_no_direct_model_invocations():
    root = Path(__file__).resolve().parents[1]
    # The only permitted invoke: the compiled Market Monitor graph, and the MFI model runtime.
    mfi = ["app/services/mfi_drafter/graph.py", "app/services/mfi_drafter/sections.py",
           *sorted(p.relative_to(root).as_posix() for p in (root / "app/services/mfi_drafter/nodes").glob("*.py"))]
    for relative, permitted in (
        *((path, "runtime.invoke(") for path in mfi),
        ("app/services/market_monitor/graph.py", "agent.invoke("),
    ):
        source = (root / relative).read_text(encoding="utf-8")
        direct_invocations = [
            line.strip() for line in source.splitlines() if ".invoke(" in line and permitted not in line
        ]
        assert direct_invocations == []
