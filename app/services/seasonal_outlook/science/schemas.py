"""Minimal local contracts. No provider-specific types or automatic interpretation."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Signal(Record):
    evidence_id: str
    geography: str
    signal: str
    magnitude_as_read: str | None
    legend_class: str | None
    location_in_figure: str
    confidence: Literal["high", "medium", "low"]
    limitations: list[str]


class MapReading(Record):
    figure_id: str
    title_as_read: str
    product_kind: Literal["observed", "forecast", "mixed", "other", "unknown"]
    variable: str
    metric: str
    units: str | None
    reference_baseline: str | None
    period_as_read: str
    valid_start: str | None
    valid_end: str | None
    issue_date: str | None
    geographic_coverage: str
    legend_as_read: str
    readability: Literal["readable", "partial", "unreadable"]
    signals: list[Signal]
    limitations: list[str]


class EvidenceBundle(Record):
    case_id: str
    maps: list[MapReading]


class Claim(Record):
    claim_id: str
    text: str
    geography: str
    period: str
    intensity_as_supported: str | None
    claim_kind: Literal["observation", "forecast", "interpretation"]
    season_ids: list[str]
    evidence_ids: list[str] = Field(min_length=1)
    rule_ids: list[str]
    certainty: Literal["observed", "likely", "possible", "uncertain"]


class Paragraph(Record):
    text: str
    claim_ids: list[str] = Field(min_length=1)


class Priority(Record):
    geography: str
    reason: str
    evidence_ids: list[str] = Field(min_length=1)


class RegionalAnalysis(Record):
    case_id: str
    headline: str
    headline_claim_ids: list[str]
    priorities: list[Priority]
    claims: list[Claim]
    paragraphs: list[Paragraph]
    missing_context: list[str]
    intentional_omissions: list[str]
    limitations: list[str]


class StudyRule(Record):
    rule_id: str
    layer: Literal["extraction", "analysis", "writing"]
    title: str
    instruction_en: str
    applicability: str
    required_inputs: list[str]
    examples: list[str]
    counterexamples: list[str]
    limitations: list[str]
    origin: Literal["user_decision", "semantic_constraint", "corpus_pattern", "candidate"]


class ClaimAssessment(Record):
    claim_id: str
    verdict: Literal["supported", "partial", "unsupported", "contradicted", "not_assessable"]
    severity: Literal["none", "minor", "major", "critical"]
    reference_ids: list[str]
    rationale: str


class ReferenceAssessment(Record):
    reference_id: str
    eligibility: Literal["map_supported", "needs_context", "intentional_exclusion", "unverifiable", "contradicted_reference"]
    coverage: Literal["covered", "partial", "omitted", "not_applicable"]
    output_claim_ids: list[str]
    rationale: str


class Evaluation(Record):
    run_id: str
    case_id: str
    arm: Literal["control", "rules"]
    reviewer: str
    source_images_checked: bool
    claims: list[ClaimAssessment]
    references: list[ReferenceAssessment]
    metadata_errors: list[str]
    priority_alignment: Literal["aligned", "partial", "misaligned", "not_assessable"]
    headline_alignment: Literal["aligned", "partial", "misaligned", "not_assessable"]
    uncertainty_alignment: Literal["aligned", "partial", "misaligned", "not_assessable"]
    important_omissions: list[str]
    notes: list[str]


def audit_links(evidence: EvidenceBundle, analysis: RegionalAnalysis, case: dict,
                rule_ids: set[str] | None = None) -> list[str]:
    errors = []
    if evidence.case_id != case["case_id"] or analysis.case_id != case["case_id"]:
        errors.append("case_id_mismatch")
    figure_ids = [m.figure_id for m in evidence.maps]
    if len(figure_ids) != len(set(figure_ids)) or set(figure_ids) != set(case["figure_ids"]):
        errors.append("figure_inventory_mismatch")
    signals = {s.evidence_id: (m, s) for m in evidence.maps for s in m.signals}
    if len(signals) != sum(len(m.signals) for m in evidence.maps):
        errors.append("duplicate_evidence_id")
    claim_ids = [c.claim_id for c in analysis.claims]
    if len(claim_ids) != len(set(claim_ids)):
        errors.append("duplicate_claim_id")
    if any(cid not in claim_ids for cid in analysis.headline_claim_ids):
        errors.append("headline_unknown_claim")
    if analysis.claims and not analysis.headline_claim_ids:
        errors.append("headline_unlinked")
    modes = {s["season_id"]: s["mode"] for s in case["calendar"]}
    for claim in analysis.claims:
        if not claim.season_ids or any(s not in modes for s in claim.season_ids):
            errors.append(f"invalid_season:{claim.claim_id}")
        for eid in claim.evidence_ids:
            if eid not in signals:
                errors.append(f"unknown_evidence:{claim.claim_id}:{eid}")
                continue
            kind = signals[eid][0].product_kind
            if claim.claim_kind == "observation" and kind in ("forecast", "mixed"):
                errors.append(f"observation_uses_future_product:{claim.claim_id}")
            if signals[eid][0].readability == "unreadable":
                errors.append(f"claim_uses_unreadable_map:{claim.claim_id}")
            # Per-cycle constraints also apply inside multi-season regional sections.
            for sid in claim.season_ids:
                mode = modes.get(sid)
                if mode == "excluded":
                    errors.append(f"excluded_season:{claim.claim_id}:{sid}")
                if mode == "past_only" and kind in ("forecast", "mixed"):
                    errors.append(f"postseason_forecast:{claim.claim_id}:{sid}")
                if mode == "past_only" and claim.claim_kind == "forecast":
                    errors.append(f"postseason_forecast_claim:{claim.claim_id}:{sid}")
                if mode == "future_only" and claim.claim_kind == "observation":
                    errors.append(f"preseason_retrospective:{claim.claim_id}:{sid}")
        if rule_ids is not None and any(r not in rule_ids for r in claim.rule_ids):
            errors.append(f"unknown_rule:{claim.claim_id}")
    used = set()
    for paragraph in analysis.paragraphs:
        used.update(paragraph.claim_ids)
        if any(c not in claim_ids for c in paragraph.claim_ids):
            errors.append("paragraph_unknown_claim")
    if set(claim_ids) != used:
        errors.append("unwritten_claim")
    for priority in analysis.priorities:
        if any(e not in signals for e in priority.evidence_ids):
            errors.append("priority_unknown_evidence")
    return sorted(set(errors))
