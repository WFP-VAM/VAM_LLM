"""Vertex wire contracts, separate from MFI application tests."""
import json
from dataclasses import replace
import httpx
import pytest
from app.shared.llm.vertex_schema import compile_schema, SchemaConfigurationError
from app.services.mfi_drafter.contracts import response_schema
from app.services.mfi_drafter.prompts import instructions
from test_mfi_light_runtime import MFI_PROFILE, ledger, runtime, answer, invoke


def test_requests_on_the_wire_keep_the_mfi_contract(ledger, vertex_wire):
    from app.shared.llm.vertex import VertexProvider
    sent, replies = vertex_wire
    replies += [httpx.Response(200, json={"totalTokens": 120}), httpx.Response(200, json={"candidates": [
        {"content": {"role": "model", "parts": [{"text": json.dumps(answer("A", "B"))}]}, "finishReason": "STOP"}]})]
    model = runtime(ledger, VertexProvider(), replace(MFI_PROFILE, project="offline-test"))
    assert len(invoke(model)["sections"]) == 2
    count, draft = sent
    model_path = "/projects/offline-test/locations/global/publishers/google/models/gemini-3.1-pro-preview"
    assert (model_path + ":countTokens") in count["url"] and (model_path + ":generateContent") in draft["url"]
    assert (count["timeout"], draft["timeout"]) == (30.0, 600.0)
    config = draft["body"]["generationConfig"]
    assert {k: v for k, v in config.items() if k != "responseSchema"} == {
        "temperature": 1.0, "candidateCount": 1, "maxOutputTokens": 65536, "responseMimeType": "application/json"}
    # Vertex's default alphabetical order, which the drafts have always followed, is now explicit.
    # google-genai keeps the snake_case spelling of this nested field; Vertex accepts both.
    schema = config["responseSchema"]
    assert schema.get("propertyOrdering", schema.get("property_ordering")) == ["notes", "sections"]
    assert draft["body"]["contents"] == count["body"]["contents"]
    assert count["body"]["generationConfig"]["responseSchema"] == config["responseSchema"]
    assert "systemInstruction" not in draft["body"]
    assert "evidence overrides" in instructions("correct_dimensions")


def test_schema_compilation_never_returns_shared_mutable_dictionary():
    from app.services.mfi_drafter.contracts import SectionsResponse
    provider_schema = lambda model: compile_schema(model.model_json_schema(), MFI_PROFILE)
    first = provider_schema(SectionsResponse)
    first["properties"]["sections"].clear()
    assert provider_schema(SectionsResponse)["properties"]["sections"]["type"] == "array"


def test_provider_schemas_pin_the_alphabetical_property_order():
    for review, order in ((False, ["notes", "sections"]), (True, ["needs_revision", "review_markdown"])):
        assert compile_schema(response_schema(review), MFI_PROFILE)["propertyOrdering"] == order
    assert compile_schema(response_schema(), MFI_PROFILE)["properties"]["sections"]["items"]["propertyOrdering"] == ["section_id", "text_markdown"]


@pytest.mark.parametrize("schema", [{"oneOf": [{"type": "string"}, {"type": "number"}]}, {"type": "strin"}])
def test_unknown_provider_schema_construct_is_configuration_error(schema):
    with pytest.raises(SchemaConfigurationError):
        compile_schema(schema, MFI_PROFILE)
