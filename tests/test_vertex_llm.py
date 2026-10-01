"""Real Google SDK serialization over a simulated HTTP transport."""
import json
from dataclasses import replace
import httpx
import pytest
from google import genai
from google.genai import types
from app.shared.llm import FilePart, LLMRequest, ModelProfile, ProviderError
from app.shared.llm.vertex import VertexProvider, reply_from
from test_llm_observability import make_request, USAGE
PROFILE = ModelProfile(service="test-service", model="gemini-test", location="global",
                       temperature=0.0, timeout_seconds=90.0)

class ImageResolver:
    def model_uri(self, reference):
        assert reference['namespace'] == 'images'
        return 'gs://bucket/' + reference['key']


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
    assert (blocked.text, blocked.finish_reason, blocked.outcome) == ("", None, "unknown")


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
    request = LLMRequest(operation="o", node="n", system="rules", parts=["payload", FilePart(dict(namespace="images", key="map", sha256="0"*64, size=1, mime="image/png"))],
                         response_schema={"type": "object", "properties": {"a": {"type": "string"}}}, json_output=True,
                         max_output_tokens=32768, timeout_seconds=1200)
    VertexProvider(store=ImageResolver()).generate(profile, request)

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
    with pytest.raises(ProviderError):
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
