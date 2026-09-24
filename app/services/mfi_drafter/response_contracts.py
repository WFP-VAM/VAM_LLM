"""Shared model-facing contracts. Country and artifact identities are invocation data."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import schemas
from .light_contracts import ContractConfigurationError, compile_provider_schema, expanded_schema, provider_schema
from .reliable_contracts import SourcePassage, fingerprint

RESPONSE_CONTRACT_VERSION = "mfi-responses-v1"


class Transport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Claim(schemas.MFIClaimPatchValue):
    model_config = ConfigDict(extra="forbid", strict=True)


class Subsection(Transport):
    name: str = Field(min_length=1)
    subsection_metric_id: str | None = None
    score_0_10: float | None = None
    interpretation: Claim
    driver_metric_ids: list[str] = Field(default_factory=list)


class DimensionResponse(Transport):
    summary: Claim
    key_findings: list[Claim]
    subdimension_analysis: list[Subsection]
    geographic_patterns: list[Claim]
    data_limitations: list[Claim]
    recommendations: list[Claim]


class MarketNarrative(Transport):
    priority_issues: list[Claim]
    recommended_interventions: list[Claim]
    limitations: list[Claim]


class MarketRow(Transport):
    market_name: str
    narrative: MarketNarrative


class MarketResponse(Transport):
    markets: list[MarketRow]


class ExecutiveResponse(Transport):
    motivation: Claim | None
    key_findings: list[Claim]
    recommendations: list[Claim]
    limitations: list[Claim]


class ContextStatement(Transport):
    text: str = Field(min_length=1)
    classification: Literal["corroborating", "potentially_explanatory", "unrelated"]
    document_ids: list[str] = Field(min_length=1)
    source_passages: list[SourcePassage] = Field(min_length=1)


class ContextResponse(Transport):
    statements: list[ContextStatement]


class ReviewFinding(Transport):
    claim_id: str
    issue_type: Literal["data_mismatch", "context_interpretation_problem"]
    severity: Literal["high", "medium"]
    message: str
    recommendation: str
    fact_ids: list[str] = Field(default_factory=list)
    claimed_value: float | None = None
    expected_value: float | None = None


class ReviewResponse(Transport):
    flags: list[ReviewFinding]


@dataclass(frozen=True)
class ResponseContract:
    kind: str
    model: type[BaseModel]
    version: str = RESPONSE_CONTRACT_VERSION

    @property
    def schema_hash(self):
        return fingerprint(self.model.model_json_schema())


CONTRACTS = {kind: ResponseContract(kind, model) for kind, model in {
    "context": ContextResponse, "dimension": DimensionResponse, "market": MarketResponse,
    "executive": ExecutiveResponse, "review": ReviewResponse,
    "patch_context_text": schemas.MFIContextTextFieldPatch,
    "patch_context_classification": schemas.MFIContextClassificationFieldPatch,
    "patch_document_references": schemas.MFIContextDocumentsFieldPatch,
    "patch_claim": schemas.MFIClaimFieldPatch,
    "patch_claim_list": schemas.MFIClaimListFieldPatch,
    "patch_subsections": schemas.MFISubdimensionFieldPatch,
    "patch_context_withdrawal": schemas.MFIContextWithdrawalPatch,
}.items()}


def examples(kind):
    claim = {"text":"Observed evidence.", "segments":[], "metric_ids":[], "document_ids":[],
             "scope":"assessment", "polarity":"descriptive"}
    subsection = {"name":"Supported component", "interpretation":claim, "driver_metric_ids":[]}
    samples = {
        "context": {"statements":[{"text":"Source-supported context.", "classification":"potentially_explanatory",
                     "document_ids":["supplied-id"], "source_passages":[{"document_id":"supplied-id", "text":"Exact supplied passage."}]}]},
        "dimension": {"summary":claim, "key_findings":[], "subdimension_analysis":[], "geographic_patterns":[], "data_limitations":[], "recommendations":[]},
        "market": {"markets":[{"market_name":"Requested market label", "narrative":{"priority_issues":[], "recommended_interventions":[], "limitations":[]}}]},
        "executive": {"motivation":claim, "key_findings":[], "recommendations":[], "limitations":[]},
        "review": {"flags":[]},
        "patch_context_text":{"replacement":"Source-supported text."},
        "patch_context_classification":{"replacement":"unrelated"},
        "patch_document_references":{"replacement":["supplied-id"]},
        "patch_claim":{"replacement":claim}, "patch_claim_list":{"replacement":[claim]},
        "patch_subsections":{"replacement":[subsection]}, "patch_context_withdrawal":{"replacement":True},
    }
    sample = deepcopy(samples[kind])
    CONTRACTS[kind].model.model_validate(sample, strict=True)
    return sample


def instructions(contract):
    from .packages import serialized
    return ("\nRESPONSE_CONTRACT " + contract.version + ": " + contract.kind + "\n"
            "Use only the permitted types and enum values in RESPONSE_SCHEMA. Citation fields are arrays. "
            "Do not supply application-owned claim IDs, statement IDs, revisions or validation metadata. "
            "The example illustrates structure only; use the supplied assessment and source evidence.\n"
            "RESPONSE_SCHEMA: " + serialized(provider_schema(contract.model)) + "\n"
            "VALIDATED_EXAMPLE: " + serialized(examples(contract.kind)) + "\n")


def inspect_response(contract, payload, context=None):
    issues = []
    try:
        contract.model.model_validate(payload, strict=True)
    except ValidationError as exc:
        for error in exc.errors(include_url=False, include_input=False):
            path = [p for p in error["loc"] if isinstance(p, (str, int))]
            issues.append({"path":path, "code":error["type"], "message":error["msg"],
                           "expected":error.get("ctx", {}).get("expected", error["msg"]), "repairable":True})
    context = context or {}
    if contract.kind == "review" and isinstance(payload, dict) and isinstance(payload.get("flags"), list) and "claim_ids" in context:
        for index, finding in enumerate(payload["flags"]):
            if isinstance(finding, dict) and isinstance(finding.get("claim_id"), str) and finding["claim_id"] not in context["claim_ids"]:
                issues.append({"path":["flags",index,"claim_id"], "code":"unknown_claim",
                               "message":"Review findings must identify a supplied candidate claim", "repairable":True})
    if contract.kind == "market" and isinstance(payload, dict) and isinstance(payload.get("markets"), list):
        names = [row.get("market_name") if isinstance(row, dict) else None for row in payload["markets"]]
        expected = context.get("market_names")
        if expected is not None:
            for index in range(max(len(names),len(expected))):
                if index >= len(expected):
                    issues.append({"path":["markets",index], "code":"unexpected_artifact", "message":"Remove this unrequested extra market row", "repairable":True})
                elif index >= len(names):
                    issues.append({"path":["markets",index], "code":"missing_artifact", "message":f"Supply the missing market row for {expected[index]}", "repairable":True})
                elif isinstance(names[index],str) and names[index] != expected[index]:
                    issues.append({"path":["markets",index,"market_name"], "code":"artifact_identity_mismatch",
                                   "message":f"This position requires market identity {expected[index]}", "repairable":True})
    if contract.kind == "context" and isinstance(payload, dict) and isinstance(payload.get("statements"), list):
        documents = context.get("documents")
        for index, statement in enumerate(payload["statements"]):
            try:
                value = ContextStatement.model_validate(statement, strict=True)
            except ValidationError:
                continue
            if documents is not None:
                if any(key not in documents for key in value.document_ids):
                    issues.append({"path":["statements",index,"document_ids"], "code":"unknown_document",
                                   "message":"Cite only supplied document IDs", "repairable":True})
                valid = all(p.document_id in value.document_ids and p.document_id in documents
                            and p.text.strip() and p.text in documents[p.document_id] for p in value.source_passages)
                if not valid or set(value.document_ids) - {p.document_id for p in value.source_passages}:
                    issues.append({"path":["statements",index,"source_passages"], "code":"invalid_source_passage",
                                   "message":"Every cited document requires an exact supplied supporting passage", "repairable":True})
    return [issue for issue in issues if not any(other["path"] != issue["path"] and
            issue["path"][:len(other["path"])] == other["path"] for other in issues)]


def contract_manifest():
    return {kind:{"version":c.version, "schema_hash":c.schema_hash} for kind,c in CONTRACTS.items()}


def bound_request(kind, messages):
    from langchain_core.messages import HumanMessage
    contract = CONTRACTS[kind]
    bound = [HumanMessage(content=instructions(contract) + str(messages[0].content)), *messages[1:]] if messages else [HumanMessage(content=instructions(contract))]
    return bound, provider_schema(contract.model)


def request_package(kind, messages):
    from app.shared.llm_observability import serialize_messages
    bound, schema = bound_request(kind, messages)
    return {"messages":serialize_messages(bound), "response_schema":schema}
