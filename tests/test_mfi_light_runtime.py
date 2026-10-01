import json
from dataclasses import replace
import pytest
from app.shared.llm import ProviderError
from app.services.mfi_drafter import runtime as mfi_runtime, service as mfi_service
from app.services.mfi_drafter.contracts import response_schema, inspect_sections
from app.services.mfi_drafter.prompts import instructions
from app.shared.llm.profiles import mfi_profile
from app.shared.llm import LLMCallError, LLMClient, LLMResponse, Tracer

MFI_PROFILE = mfi_profile()


class Responses:
    """The model as the shared client sees it: replies in order, recording each request's package."""

    def __init__(self, values):
        self.values, self.calls, self.counts = iter(values), [], 0

    def count_tokens(self, profile, request):
        self.counts += 1
        return 100

    def generate(self, profile, request):
        self.calls.append(json.loads(request.parts[0].split("\nREQUEST:\n")[1]))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            value, finish = value
        else:
            finish = "STOP"
        return LLMResponse(text=value if isinstance(value, str) else json.dumps(value), outcome="truncated" if finish == "MAX_TOKENS" else "completed", finish_reason=finish)


def answer(*ids, text="Supported prose."):
    return {"sections": [{"section_id": sid, "text_markdown": text} for sid in ids], "notes": []}


def busy():
    return ProviderError("busy", transient=True)


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(mfi_runtime.time, "sleep", lambda _: None)
    return mfi_runtime.RunLedger("response")


def runtime(ledger, provider, profile=MFI_PROFILE):
    return mfi_runtime.ModelRuntime(ledger, LLMClient(profile, tracer=Tracer(service="mfi-drafter", run_id="response"),
                                                        provider=provider))


def invoke(model):
    return model.invoke("draft_dimensions", "dimensions", {"EVIDENCE": {"sources": {}}, "requested_sections": ["A", "B"]}, ["A", "B"])


def calls(model):
    return model.llm.tracer.snapshot()["calls"]


@pytest.mark.parametrize("policy_name", ["RECOMMENDATION_POLICY", "ANALYSIS_POLICY", "STYLE_POLICY"])
def test_shared_policy_change_is_recorded_in_the_effective_contract(monkeypatch, policy_name):
    from app.services.mfi_drafter import prompts as mfi_prompts
    old_contract = mfi_service.effective_contract()
    monkeypatch.setattr(mfi_prompts, policy_name,
                        getattr(mfi_prompts, policy_name) + "\nRevised shared guidance.")
    new_contract = mfi_service.effective_contract()
    assert new_contract["schema_hashes"] == old_contract["schema_hashes"]
    assert len(new_contract["prompt_hashes"]) == 7
    assert all(value != old_contract["prompt_hashes"][node]
               for node, value in new_contract["prompt_hashes"].items())


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
    model = runtime(ledger, client)
    output = invoke(model)
    assert client.calls[1]["requested_sections"] == ["B"]
    assert output["sections"][0]["text_markdown"] == "Original valid prose stays."
    assert len(client.calls) == 2
    failed, repair = calls(model)
    assert (failed["status"], failed["disposition"]) == ("recovered", "recovered_by_repair")
    assert (repair["repair_of"], repair["attempt"], repair["status"]) == (failed["call_id"], 2, "succeeded")
    assert failed["work_item"] == repair["work_item"] == next(iter(ledger.read()["light_work"]))


def test_syntax_then_schema_error_has_no_third_attempt(ledger):
    client = Responses(["{", answer("A")])
    model = runtime(ledger, client)
    with pytest.raises(LLMCallError) as caught: invoke(model)
    assert isinstance(caught.value.__cause__, mfi_runtime.InvalidResponse)
    assert len(client.calls) == 2
    assert client.calls[1]["response_errors"] == ["Model output is not valid JSON"]
    work = next(iter(ledger.read()["light_work"].values()))
    assert work["status"] == "failed"
    assert [(c["status"], c["failure_code"]) for c in calls(model)] == [
        ("failed", "llm_invalid_json"), ("failed", "llm_response_contract_error")]


@pytest.mark.parametrize("first", [(answer("A", "B"), "MAX_TOKENS"), "{", [1, 2], ""])
def test_truncation_and_unreadable_response_bounded(ledger, first):
    client = Responses([first, answer("B") if isinstance(first, tuple) else answer("A", "B")])
    assert len(invoke(runtime(ledger, client))["sections"]) == 2
    assert len(client.calls) == 2


def test_transient_error_is_retried_once_with_the_same_request(ledger):
    client = Responses([busy(), answer("A", "B")])
    model = runtime(ledger, client)
    assert len(invoke(model)["sections"]) == 2
    assert client.calls[0] == client.calls[1]
    failed, retry = calls(model)
    assert (failed["status"], failed["disposition"], failed["transient"]) == ("recovered", "recovered_by_retry", True)
    assert (retry["retry_of"], retry["repair_of"], retry["attempt"]) == (failed["call_id"], None, 2)


def test_two_transient_errors_fail_the_work_without_a_third_attempt(ledger):
    client = Responses([busy(), busy(), answer("A", "B")])
    with pytest.raises(LLMCallError) as caught: invoke(runtime(ledger, client))
    assert (caught.value.failure_code, caught.value.transient) == ("llm_transport_error", True)
    assert len(client.calls) == 2


def test_access_error_is_not_a_format_retry(ledger):
    denied = ProviderError("test model not enabled", kind="authentication")
    client = Responses([denied])
    with pytest.raises(LLMCallError) as caught: invoke(runtime(ledger, client))
    assert (caught.value.stage, caught.value.transient) == ("transport", False)
    assert len(client.calls) == 1


def test_no_new_call_starts_once_another_step_has_failed(ledger):
    client = Responses([answer("A", "B")])
    ledger.change(lambda v: v.update(light_phases={"draft_markets": {"status": "failed"}}))
    with pytest.raises(mfi_runtime.Stopped): invoke(runtime(ledger, client))
    assert client.calls == [] and client.counts == 0


def test_a_repair_is_not_started_after_another_step_failed(ledger):
    class SiblingFailsMeanwhile(Responses):
        def generate(self, profile, request):
            reply = super().generate(profile, request)
            ledger.change(lambda v: v.update(light_phases={"draft_markets": {"status": "failed"}}))
            return reply
    client = SiblingFailsMeanwhile([answer("A"), answer("B")])
    with pytest.raises(mfi_runtime.Stopped): invoke(runtime(ledger, client))
    assert len(client.calls) == 1


def test_diagnostics_describe_calls_without_response_text(ledger):
    client = Responses([answer("A", "B", text="Confidential drafted prose.")])
    model = runtime(ledger, client)
    assert len(invoke(model)["sections"]) == 2
    diagnostics = mfi_runtime.public_diagnostics(ledger.read(), model.llm.tracer.snapshot())
    call = diagnostics["llm_diagnostics"]["calls"][0]
    assert call["status"] == "succeeded" and call["finish_reason"] == "STOP"
    assert diagnostics["llm_diagnostics"]["status"] == "running"
    assert diagnostics["model_attempt_total"] == diagnostics["batches"][0]["attempt_count"] == 1
    assert "Confidential drafted prose" not in json.dumps(diagnostics)


def test_token_counts_are_cached_within_a_run(ledger):
    client = Responses([answer("A", "B"), answer("A", "B")])
    model = runtime(ledger, client)
    package = {"EVIDENCE": {"sources": {}}, "requested_sections": ["A", "B"]}
    model.invoke("draft_dimensions", "first", package, ["A", "B"])
    model.invoke("draft_dimensions", "second", package, ["A", "B"])
    assert client.counts == 1 and ledger.read()["light_token_count_requests"] == 1




def test_unknown_identifiers_and_scalar_notes_are_not_silently_accepted():
    payload = answer("A", "unknown")
    payload["notes"] = None
    valid, issues = inspect_sections(payload, ["A", "B"], {})
    assert set(valid) == {"A"} and len(issues) == 3
