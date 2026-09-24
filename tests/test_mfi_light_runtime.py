import json
from types import SimpleNamespace
import pytest
from langchain_core.messages import HumanMessage
from app.services.mfi_drafter import light_service, light_runtime
from app.services.mfi_drafter.light_contracts import response_schema, inspect_sections, instructions


class Responses:
    def __init__(self, values):
        self.values, self.calls, self.counts = iter(values), [], 0

    def count(self, messages, schema, timeout):
        self.counts += 1
        return 100

    def generate(self, messages, schema, timeout):
        self.calls.append(json.loads(messages[0].content.split("\nREQUEST:\n")[1]))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            value, finish = value
        else:
            finish = "STOP"
        return SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value), response_metadata={"finish_reason": finish})


def answer(*ids, text="Supported prose."):
    return {"sections": [{"section_id": sid, "text_markdown": text} for sid in ids], "notes": []}


def test_active_prompts_are_isolated_from_legacy_recommendation_policy(monkeypatch):
    from app.services.mfi_drafter import light_contracts, methodology
    nodes = [n for n in light_contracts.NODES
             if n.startswith(("draft_", "review_", "correct_")) or n == "executive_summary"]
    before = {node: instructions(node) for node in nodes}
    monkeypatch.setattr(methodology, "NARRATIVE_PROHIBITIONS", ("Legacy-only policy change.",))
    assert {node: instructions(node) for node in nodes} == before


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(light_runtime.time, "sleep", lambda _: None)
    return light_runtime.RunLedger("response")


@pytest.mark.parametrize("policy_name", ["RECOMMENDATION_POLICY", "ANALYSIS_POLICY", "STYLE_POLICY"])
def test_shared_policy_change_is_recorded_in_the_effective_contract(monkeypatch, policy_name):
    from app.services.mfi_drafter import light_contracts
    old_contract = light_service.effective_contract()
    monkeypatch.setattr(light_contracts, policy_name,
                        getattr(light_contracts, policy_name) + "\nRevised shared guidance.")
    new_contract = light_service.effective_contract()
    assert new_contract["schema_hashes"] == old_contract["schema_hashes"]
    assert len(new_contract["prompt_hashes"]) == 7
    assert all(value != old_contract["prompt_hashes"][node]
               for node, value in new_contract["prompt_hashes"].items())


def invoke(ledger, client):
    return light_runtime.ModelRuntime(ledger, client).invoke("draft_dimensions", "dimensions", {"EVIDENCE": {"sources": {}}, "requested_sections": ["A", "B"]}, ["A", "B"])


@pytest.mark.parametrize("defect", ["missing", "blank", "duplicate", "bad_citation", "metadata"])
def test_repairs_only_invalid_sections(ledger, defect):
    first = answer("A", "B")
    first["sections"][0]["text_markdown"] = "Original valid prose stays."
    if defect == "missing": first["sections"].pop()
    if defect == "blank": first["sections"][1]["text_markdown"] = " "
    if defect == "duplicate": first["sections"].append(first["sections"][1])
    if defect == "bad_citation": first["sections"][1]["text_markdown"] = "Unsupported [S99]."
    if defect == "metadata": first["sections"][1]["claim_id"] = "forbidden"
    client = Responses([first, answer("B", text="Repaired prose.")])
    output = invoke(ledger, client)
    assert client.calls[1]["requested_sections"] == ["B"]
    assert output["sections"][0]["text_markdown"] == "Original valid prose stays."
    assert len(client.calls) == 2
    assert len(next(iter(ledger.read()["light_work"].values()))["attempts"]) == 2


def test_syntax_then_schema_error_has_no_third_attempt(ledger):
    client = Responses(["{", answer("A")])
    with pytest.raises(light_runtime.InvalidResponse): invoke(ledger, client)
    assert len(client.calls) == 2
    work = next(iter(ledger.read()["light_work"].values()))
    assert work["status"] == "failed" and [a["status"] for a in work["attempts"]] == ["failed", "failed"]


@pytest.mark.parametrize("first", [(answer("A", "B"), "MAX_TOKENS"), "{", [1, 2]])
def test_truncation_and_unreadable_response_bounded(ledger, first):
    client = Responses([first, answer("B") if isinstance(first, tuple) else answer("A", "B")])
    assert len(invoke(ledger, client)["sections"]) == 2
    assert len(client.calls) == 2


def test_access_error_is_not_a_format_retry(ledger):
    from google.api_core.exceptions import PermissionDenied
    client = Responses([PermissionDenied("test model not enabled")])
    with pytest.raises(PermissionDenied): invoke(ledger, client)
    assert len(client.calls) == 1


def test_diagnostics_describe_calls_without_response_text(ledger):
    client = Responses([answer("A", "B", text="Confidential drafted prose.")])
    assert len(invoke(ledger, client)["sections"]) == 2
    diagnostics = light_runtime.public_diagnostics(ledger.read())
    call = diagnostics["llm_diagnostics"]["calls"][0]
    assert call["status"] == "succeeded" and call["finish_reason"] == "STOP"
    assert diagnostics["llm_diagnostics"]["status"] == "running"
    assert "Confidential drafted prose" not in json.dumps(diagnostics)


def test_token_counts_are_cached_within_a_run(ledger):
    client = Responses([answer("A", "B"), answer("A", "B")])
    runtime = light_runtime.ModelRuntime(ledger, client)
    package = {"EVIDENCE": {"sources": {}}, "requested_sections": ["A", "B"]}
    runtime.invoke("draft_dimensions", "first", package, ["A", "B"])
    runtime.invoke("draft_dimensions", "second", package, ["A", "B"])
    assert client.counts == 1 and ledger.read()["light_token_count_requests"] == 1


def test_provider_configuration_schema_count_and_cached_client_are_isolated(monkeypatch):
    from google.auth.credentials import AnonymousCredentials
    from langchain_google_vertexai import ChatVertexAI
    from app.services.mfi_drafter.light_contracts import MODEL
    model = ChatVertexAI(model_name=MODEL, project="offline-test", location="global", temperature=1.0,
        timeout=600, max_retries=0, max_output_tokens=65536, credentials=AnonymousCredentials())
    messages = [HumanMessage(content="Offline configuration test")]
    for review in (False, True):
        schema = response_schema(review)
        request = model._prepare_request_gemini(messages, response_mime_type="application/json", response_schema=schema)
        assert request.model.endswith("/"+MODEL)
        assert request.generation_config.max_output_tokens == 65536
        assert request.generation_config.temperature == 1.0
        assert request.generation_config.response_mime_type == "application/json"
        assert request.generation_config.response_schema.properties
        from google.cloud.aiplatform_v1beta1.types import CountTokensRequest
        count = CountTokensRequest(model=request.model, endpoint=request.model, contents=request.contents,
            system_instruction=request.system_instruction, generation_config=request.generation_config)
        assert count.generation_config.response_schema == request.generation_config.response_schema
    assert model.response_schema is None
    assert "evidence overrides" in instructions("correct_dimensions")


def test_unknown_identifiers_and_scalar_notes_are_not_silently_accepted():
    payload = answer("A", "unknown")
    payload["notes"] = None
    valid, issues = inspect_sections(payload, ["A", "B"], {})
    assert set(valid) == {"A"} and len(issues) == 3
