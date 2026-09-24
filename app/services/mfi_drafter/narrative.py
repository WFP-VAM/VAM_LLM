"""Deterministic narrative contracts, claim catalog, validation, and fallbacks.

LLMs may draft prose, but this module owns which values may be stated, how they
are displayed, whether citations resolve, and which content requires repair.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic import ValidationError

from .claim_identity import (
    canonical_claim_index,
    canonicalize_narrative_identities,
    context_statement_id,
    dimension_claim_id,
    executive_claim_id,
    market_claim_id,
    subdimension_claim_id,
)
from .evidence_notes import compose_evidence_note
from .methodology import (
    DIMENSION_REVIEW_GUIDANCE,
    DISPLAY_DIMENSIONS,
    METRIC_DEFINITIONS_BY_ID,
)
from .wording import (
    NEUTRAL_SCOPE_STATEMENT,
    affordability_claims,
    causal_claims,
    has_approved_aggregation_wording,
    modality_conclusions,
    pooled_population_phrases,
    residual_markup,
    sanitize_claim_text,
    withdrawn_text,
)
from .schemas import (
    MFIClaimCatalogEntry,
    MFIClaimValidationResult,
    MFIContextEvidenceStatement,
    MFICoverageSummary,
    MFICorrectionAttemptRecord,
    MFICorrectionTarget,
    MFIDimensionNarrative,
    MFIExecutiveNarrative,
    MFIMarketNarrative,
    MFINarrativeClaim,
    MFINarrativeQAFlag,
    MFIQAReview,
    MFISubdimensionNarrative,
)
from .visualization import format_market_coverage

_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_MATERIAL_SEVERITIES = {"high", "medium"}
DELIVERABLE_UNVERIFIED_FIGURE_CODES = frozenset(
    {
        "numeric_value_mismatch",
        "uncited_numeric_value",
        "unit_mismatch",
    }
)


@dataclass(frozen=True)
class MFINarrativeDensityPolicy:
    """Canonical R8 claim-count ceilings applied before validation."""

    non_priority_findings: int = 1
    non_priority_geographic_patterns: int = 0
    non_priority_limitations: int = 1
    non_priority_recommendations: int = 1
    priority_findings: int = 3
    priority_subdimensions: int = 2
    priority_geographic_patterns: int = 2
    priority_limitations: int = 1
    priority_recommendations: int = 3
    market_priority_issues: int = 3
    market_recommendations: int = 3
    market_limitations: int = 1
    executive_recommendations: int = 3
    executive_limitations: int = 3


NARRATIVE_DENSITY_POLICY = MFINarrativeDensityPolicy()
_CLAIM_FIELDS = (
    "summary",
    "key_findings",
    "geographic_patterns",
    "data_limitations",
    "recommendations",
    "priority_issues",
    "recommended_interventions",
    "scope_statement",
    "motivation",
    "limitations",
)


def build_claim_catalog(
    assessment_profile: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Materialize a closed, display-ready catalog from the metric ledger."""
    ledger = assessment_profile.get("metric_ledger")
    if not isinstance(ledger, Mapping):
        return {}
    catalog: dict[str, dict[str, Any]] = {}
    for metric_id in sorted(str(key) for key in ledger):
        raw = ledger.get(metric_id)
        if not isinstance(raw, Mapping) or raw.get("value") is None:
            continue
        value = float(raw["value"])
        unit = str(raw.get("unit") or "")
        statistic = str(raw.get("statistic") or "")
        formatted = _format_catalog_value(value, unit, statistic)
        coverage = raw.get("coverage")
        coverage_label = _coverage_label(coverage)
        source_metric_ids = [
            str(item) for item in raw.get("source_metric_ids", []) if item
        ]
        (
            representation_kind,
            representation_complete,
            representation_required,
        ) = _claim_representation(
            statistic=statistic,
            coverage=coverage,
            representation_basis=str(
                raw.get("representation_basis") or "all_assessed_markets"
            ),
            source_metric_ids=source_metric_ids,
        )
        scope = _canonical_scope(
            str(raw.get("evidence_scope") or ""),
            market_name=raw.get("market_name"),
            region=raw.get("region"),
        )
        entry = MFIClaimCatalogEntry(
            metric_id=metric_id,
            label=str(raw.get("label") or metric_id),
            numeric_value=value,
            formatted_value=formatted,
            allowed_renderings=_allowed_renderings(value, unit, statistic),
            statistic=statistic,
            unit=unit,
            orientation=str(raw.get("orientation") or "descriptive"),
            scope=scope,
            dimension=_optional_text(raw.get("dimension")),
            market_name=_optional_text(raw.get("market_name")),
            region=_optional_text(raw.get("region")),
            coverage_label=coverage_label,
            source_metric_ids=source_metric_ids,
            aggregation_method=raw.get("aggregation_method")
            or "unweighted_market_mean",
            population_basis=raw.get("population_basis") or "descriptive",
            pooled_denominator_available=bool(
                raw.get("pooled_denominator_available")
            ),
            representation_basis=raw.get("representation_basis")
            or "all_assessed_markets",
            permitted_subject_phrase=str(raw.get("permitted_subject_phrase") or ""),
            # A single canonical scope cannot express that one value legitimately backs
            # more than one kind of claim. The list is seeded with the canonical scope so
            # behaviour is unchanged; later phases widen it and move the validator onto it.
            permitted_claim_scopes=[scope],
            represented_market_count=_optional_count(
                coverage, "available_market_count"
            ),
            assessed_market_count=_optional_count(
                coverage, "total_assessed_market_count"
            ),
            representation_kind=representation_kind,
            representation_complete=representation_complete,
            representation_required=representation_required,
        )
        catalog[metric_id] = entry.model_dump()
    from .facts import catalog_facts
    catalog_facts(assessment_profile, catalog)
    return catalog


def _optional_count(coverage: Any, key: str) -> Optional[int]:
    """Return one coverage count as an integer, so claims need not parse the label."""
    if not isinstance(coverage, Mapping):
        return None
    value = coverage.get(key)
    return int(value) if isinstance(value, (int, float)) else None


def _claim_representation(
    *,
    statistic: str,
    coverage: Any,
    representation_basis: str,
    source_metric_ids: Sequence[str],
) -> tuple[str, bool, bool]:
    """Classify disclosure needs without changing the underlying ledger entry."""
    available = _optional_count(coverage, "available_market_count")
    total = _optional_count(coverage, "total_assessed_market_count")
    if available is None or total is None:
        return "none", False, False
    definitions = [
        METRIC_DEFINITIONS_BY_ID[metric_id]
        for metric_id in source_metric_ids
        if metric_id in METRIC_DEFINITIONS_BY_ID
    ]
    if any(definition.role == "item_driver" for definition in definitions):
        kind = "item"
    elif representation_basis == "applicable_assessed_markets" or any(
        definition.applicability_rule == "quality_applicability"
        for definition in definitions
    ):
        kind = "applicability"
    else:
        kind = "fixed_metric"
    complete = available == total and int(coverage.get("missing_count") or 0) == 0
    normalized_statistic = statistic.casefold()
    explicit_representation_value = (
        "coverage" in normalized_statistic
        or normalized_statistic in {"count", "denominator", "numerator"}
        or normalized_statistic.endswith("_count")
    )
    required = (
        kind in {"item", "applicability"}
        or not complete
        or explicit_representation_value
    )
    return kind, complete, required


def compact_catalog(
    catalog: Mapping[str, Mapping[str, Any]],
    metric_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Return the prompt-safe subset without exact internal numeric values."""
    result: list[dict[str, Any]] = []
    for metric_id in dict.fromkeys(str(item) for item in metric_ids):
        entry = catalog.get(metric_id)
        if not isinstance(entry, Mapping):
            continue
        result.append(
            {
                "metric_id": metric_id,
                "label": entry.get("label"),
                "formatted_value": entry.get("formatted_value"),
                "unit": entry.get("unit"),
                "orientation": entry.get("orientation"),
                "scope": entry.get("scope"),
                "coverage": entry.get("coverage_label"),
                "dimension": entry.get("dimension"),
                "market_name": entry.get("market_name"),
                "region": entry.get("region"),
                # The correct way to describe what this value measures. Supplied so the
                # wording does not have to be inferred from the label; the classification
                # enums behind it stay internal, since validation reads the full catalog.
                "permitted_subject_phrase": entry.get("permitted_subject_phrase"),
                **({"fact": {key: value for key, value in entry["fact"].items() if key not in {"source_metric_ids", "member_ids"}}} if entry.get("fact") else {}),
            }
        )
    return result


def dimension_catalog_ids(dimension_profile: Mapping[str, Any]) -> list[str]:
    """Select deterministic dimension evidence suitable for narrative drafting."""
    ids: list[str] = [
        str(item) for item in dimension_profile.get("ledger_metric_ids", []) if item
    ]
    for regional in dimension_profile.get("regional_summaries", []) or []:
        if isinstance(regional, Mapping):
            ids.extend(
                str(item) for item in regional.get("ledger_metric_ids", []) if item
            )
    for metric in dimension_profile.get("subsections", []) or []:
        if isinstance(metric, Mapping):
            ids.extend(_preferred_metric_ledger_ids(metric))
    for metric in dimension_profile.get("drivers", []) or []:
        if not isinstance(metric, Mapping):
            continue
        role = str(metric.get("role") or "")
        if role == "item_driver" and not bool(metric.get("item_relevant")):
            continue
        if metric.get("weakness_rank") is None and role != "item_driver":
            continue
        ids.extend(_preferred_metric_ledger_ids(metric))
    localized = dimension_profile.get("localized_patterns")
    if isinstance(localized, Mapping):
        for market in localized.get("ordered_markets", []) or []:
            if isinstance(market, Mapping) and market.get("ledger_metric_id"):
                ids.append(str(market["ledger_metric_id"]))
    return list(dict.fromkeys(ids))


def market_catalog_ids(
    market_profile: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Select official and explanatory evidence for one priority market."""
    market_name = str(market_profile.get("market_name") or "")
    weak_dimensions = {
        str(item.get("dimension"))
        for item in market_profile.get("weak_dimensions", []) or []
        if isinstance(item, Mapping) and item.get("dimension")
    }
    ids: list[str] = [
        str(item) for item in market_profile.get("ledger_metric_ids", []) if item
    ]
    for item in market_profile.get("weak_dimensions", []) or []:
        if isinstance(item, Mapping):
            ids.extend(str(value) for value in item.get("ledger_metric_ids", []) if value)
    for metric_id, entry in catalog.items():
        if (
            entry.get("market_name") == market_name
            and entry.get("dimension") in weak_dimensions
            and (
                metric_id.endswith(".normalized")
                or metric_id.endswith(".unfavorable_rate")
                or metric_id.endswith(".coverage")
            )
        ):
            ids.append(metric_id)
    return list(dict.fromkeys(ids))


def executive_catalog_ids(
    assessment_profile: Mapping[str, Any],
) -> list[str]:
    """Select profile-level evidence for the executive-summary prompt."""
    ids = [
        "assessment.mfi.mean",
        "assessment.mfi.median",
        "assessment.mfi.minimum",
        "assessment.mfi.maximum",
        "assessment.mfi.q1",
        "assessment.mfi.q3",
        "assessment.mfi.iqr",
        "assessment.mfi.range",
        "assessment.mfi.denominator",
        "assessment.mfi.coverage",
    ]
    priority = set(assessment_profile.get("priority_dimension_names", []) or [])
    for dimension in assessment_profile.get("dimensions", []) or []:
        if not isinstance(dimension, Mapping):
            continue
        if dimension.get("dimension") not in priority:
            continue
        if assessment_profile.get("workflow_revision"):
            ids.extend(dimension.get("ledger_metric_ids", []))
            ids.extend(dimension.get("analytical_facts", {}).keys())
        else:
            ids.extend(dimension_catalog_ids(dimension))
    return list(dict.fromkeys(ids))


def parse_context_evidence(
    payload: Any,
    *,
    documents: Sequence[Mapping[str, Any]],
    expected_statement_ids: Optional[Sequence[str]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate LLM context classifications and retain only known documents."""
    known = {
        str(document.get("doc_id"))
        for document in documents
        if isinstance(document, Mapping) and document.get("doc_id")
    }
    raw_statements = (
        payload.get("statements", [])
        if isinstance(payload, Mapping)
        else []
    )
    statements: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_statements or []):
        if not isinstance(raw, Mapping):
            continue
        text = sanitize_claim_text(raw.get("text"))
        if not text:
            continue
        classification = str(raw.get("classification") or "unrelated")
        if classification not in {
            "corroborating",
            "potentially_explanatory",
            "unrelated",
        }:
            classification = "unrelated"
        supplied_ids = [str(item) for item in raw.get("document_ids", []) if item]
        invalid = [item for item in supplied_ids if item not in known]
        valid_ids = [item for item in supplied_ids if item in known]
        statement_id = (
            str(expected_statement_ids[index])
            if expected_statement_ids is not None
            and index < len(expected_statement_ids)
            else context_statement_id(index + 1)
        )
        flags: list[str] = []
        if invalid or not valid_ids:
            flags.append("invalid_document_id")
            warnings.append(
                _flag(
                    source="deterministic",
                    code="invalid_document_id",
                    severity="medium",
                    artifact_type="context",
                    artifact_id=statement_id,
                    field_name="document_ids",
                    message=(
                        "Context statement cites an unknown document or has no "
                        "valid source document."
                    ),
                    document_ids=invalid,
                )
            )
        statement = MFIContextEvidenceStatement(
            statement_id=statement_id,
            text=text,
            source_passages=[dict(p) for p in raw.get("source_passages", []) if isinstance(p, Mapping)],
            passage_binding_required=bool(raw.get("passage_binding_required")),
            classification=classification,
            document_ids=valid_ids,
            validation_status="unverified" if flags else "verified",
            validation_flags=flags,
        )
        statements.append(statement.model_dump())
    return statements, warnings


def _validate_claim_payload(payload: Any, *, field: str) -> None:
    """Validate the transport shape before tolerant canonical normalization."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field} must be a claim object")
    if not str(payload.get("text") or "").strip():
        raise ValueError(f"{field}.text is required")
    for key in ("metric_ids", "document_ids"):
        if key not in payload or not isinstance(payload.get(key), list):
            raise ValueError(f"{field}.{key} must be a list")
    for key in ("scope", "polarity"):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"{field}.{key} is required")
    # Validate the entire claim before list normalization, which is tolerant
    # only for historical/non-strict callers. Preserve the exact field path.
    try:
        _claim_from_payload(payload, claim_id="validation", claim_kind="finding", scope="assessment")
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _validate_claim_list_payload(payload: Any, *, field: str) -> None:
    if not isinstance(payload, list):
        raise ValueError(f"{field} must be a list")
    for index, item in enumerate(payload):
        _validate_claim_payload(item, field=f"{field}[{index}]")


def _require_payload_fields(payload: Mapping[str, Any], fields: Sequence[str]) -> None:
    missing = [field for field in fields if field not in payload]
    if missing:
        raise ValueError(
            "Narrative response is missing required field(s): "
            + ", ".join(missing)
        )


def parse_dimension_narrative(
    payload: Any,
    *,
    dimension_profile: Mapping[str, Any],
    assessment_profile: Mapping[str, Any],
    strict: bool = False,
) -> dict[str, Any]:
    """Normalize one LLM dimension payload into the canonical schema."""
    dimension = str(dimension_profile["dimension"])
    is_priority = bool(dimension_profile.get("is_priority"))
    reliable = assessment_profile.get("workflow_revision") == "mfi-reliable-v1"
    if reliable:
        from .facts import render_payload
        payload = render_payload(payload, build_claim_catalog(assessment_profile))
    if not isinstance(payload, Mapping):
        if strict:
            raise ValueError("Dimension narrative response must be an object")
        return fallback_dimension_narrative(
            dimension_profile,
            assessment_profile=assessment_profile,
        )
    try:
        if strict:
            _require_payload_fields(
                payload,
                (
                    "summary",
                    "key_findings",
                    "subdimension_analysis",
                    "geographic_patterns",
                    "data_limitations",
                    "recommendations",
                ),
            )
            _validate_claim_payload(payload.get("summary"), field="summary")
            for field in (
                "key_findings",
                "geographic_patterns",
                "data_limitations",
                "recommendations",
            ):
                _validate_claim_list_payload(payload.get(field, []), field=field)
            raw_subdimensions_for_validation = payload.get(
                "subdimension_analysis", []
            )
            if not isinstance(raw_subdimensions_for_validation, list):
                raise ValueError("subdimension_analysis must be a list")
            for index, raw in enumerate(raw_subdimensions_for_validation):
                if not isinstance(raw, Mapping):
                    raise ValueError(
                        f"subdimension_analysis[{index}] must be an object"
                    )
                _validate_claim_payload(
                    raw.get("interpretation"),
                    field=f"subdimension_analysis[{index}].interpretation",
                )
                if not isinstance(raw.get("driver_metric_ids", []), list):
                    raise ValueError(
                        f"subdimension_analysis[{index}].driver_metric_ids must be a list"
                    )
        finding_limit = (
            NARRATIVE_DENSITY_POLICY.priority_findings
            if is_priority
            else NARRATIVE_DENSITY_POLICY.non_priority_findings
        )
        geography_limit = (
            NARRATIVE_DENSITY_POLICY.priority_geographic_patterns
            if is_priority
            else NARRATIVE_DENSITY_POLICY.non_priority_geographic_patterns
        )
        limitation_limit = (
            NARRATIVE_DENSITY_POLICY.priority_limitations
            if is_priority
            else NARRATIVE_DENSITY_POLICY.non_priority_limitations
        )
        recommendation_limit = (
            NARRATIVE_DENSITY_POLICY.priority_recommendations
            if is_priority
            else NARRATIVE_DENSITY_POLICY.non_priority_recommendations
        )
        if reliable:
            finding_limit = geography_limit = limitation_limit = None
        summary = _claim_from_payload(
            payload.get("summary"),
            claim_id=dimension_claim_id(dimension, "summary", 1),
            claim_kind="summary",
            scope="assessment",
        )
        key_findings = _claim_list(
            payload.get("key_findings"),
            prefix=f"dimension.{_slug(dimension)}.finding",
            claim_kind="finding",
            default_scope="assessment",
            max_items=finding_limit,
        )
        geographic = _claim_list(
            payload.get("geographic_patterns"),
            prefix=f"dimension.{_slug(dimension)}.geography",
            claim_kind="geographic_pattern",
            default_scope="region",
            max_items=geography_limit,
        )
        limitations = _claim_list(
            payload.get("data_limitations"),
            prefix=f"dimension.{_slug(dimension)}.limitation",
            claim_kind="limitation",
            default_scope="assessment",
            max_items=limitation_limit,
        )
        recommendations = _claim_list(
            payload.get("recommendations"),
            prefix=f"dimension.{_slug(dimension)}.recommendation",
            claim_kind="recommendation",
            default_scope="assessment",
            max_items=recommendation_limit,
        )
        subdimensions: list[MFISubdimensionNarrative] = []
        raw_subdimensions = (
            (payload.get("subdimension_analysis", []) or [])
            if is_priority or reliable
            else []
        )
        for index, raw in enumerate(
            raw_subdimensions if reliable else raw_subdimensions[: NARRATIVE_DENSITY_POLICY.priority_subdimensions]
        ):
            if not isinstance(raw, Mapping):
                continue
            interpretation = _claim_from_payload(
                raw.get("interpretation"),
                claim_id=(
                    subdimension_claim_id(dimension, index + 1)
                ),
                claim_kind="finding",
                scope="assessment",
            )
            score = raw.get("score_0_10")
            subdimensions.append(
                MFISubdimensionNarrative(
                    name=str(raw.get("name") or "").strip()
                    or f"{dimension} evidence",
                    subsection_metric_id=_optional_text(
                        raw.get("subsection_metric_id")
                    ),
                    score_0_10=float(score) if score is not None else None,
                    interpretation=interpretation,
                    driver_metric_ids=[
                        str(item)
                        for item in raw.get("driver_metric_ids", [])
                        if item
                    ],
                )
            )
        narrative = MFIDimensionNarrative(
            dimension=dimension,
            is_priority=is_priority,
            summary=summary,
            key_findings=key_findings,
            subdimension_analysis=subdimensions,
            geographic_patterns=geographic,
            data_limitations=limitations,
            recommendations=recommendations,
        )
        return narrative.model_dump()
    except (TypeError, ValueError, ValidationError):
        if strict:
            raise
        return fallback_dimension_narrative(
            dimension_profile,
            assessment_profile=assessment_profile,
        )


def parse_market_narrative(
    payload: Any,
    *,
    market_profile: Mapping[str, Any],
    strict: bool = False,
) -> dict[str, Any]:
    market_name = str(market_profile["market_name"])
    if not isinstance(payload, Mapping):
        if strict:
            raise ValueError("Market narrative response must be an object")
        return fallback_market_narrative(market_profile)
    try:
        if strict:
            _require_payload_fields(
                payload,
                (
                    "priority_issues",
                    "recommended_interventions",
                    "limitations",
                ),
            )
            for field in (
                "priority_issues",
                "recommended_interventions",
                "limitations",
            ):
                _validate_claim_list_payload(payload.get(field, []), field=field)
        issues = _claim_list(
            payload.get("priority_issues"),
            prefix=market_claim_id(market_name, "issue", 1).rsplit(".", 1)[0],
            claim_kind="finding",
            default_scope="market",
            max_items=NARRATIVE_DENSITY_POLICY.market_priority_issues,
        )
        interventions = _claim_list(
            payload.get("recommended_interventions"),
            prefix=market_claim_id(market_name, "intervention", 1).rsplit(".", 1)[0],
            claim_kind="recommendation",
            default_scope="market",
            max_items=NARRATIVE_DENSITY_POLICY.market_recommendations,
        )
        limitations = _claim_list(
            payload.get("limitations"),
            prefix=market_claim_id(market_name, "limitation", 1).rsplit(".", 1)[0],
            claim_kind="limitation",
            default_scope="market",
            max_items=NARRATIVE_DENSITY_POLICY.market_limitations,
        )
        limitations = [claim for claim in limitations if claim.metric_ids]
        # The prompt no longer requests a modality consideration, and any model that
        # supplies one anyway is answering a question the assessment cannot answer. The
        # scope caveat is stated once in the executive summary and the methodology note
        # instead of once per market.
        return MFIMarketNarrative(
            market_name=market_name,
            region=_optional_text(market_profile.get("region")),
            overall_mfi=float(market_profile["overall_mfi"]),
            score_rank=int(market_profile["score_rank"]),
            weak_dimensions=[
                str(item["dimension"])
                for item in market_profile.get("weak_dimensions", [])
                if isinstance(item, Mapping) and item.get("dimension")
            ],
            priority_issues=issues,
            recommended_interventions=interventions,
            limitations=limitations,
            modality_consideration=None,
        ).model_dump()
    except (TypeError, ValueError, ValidationError):
        if strict:
            raise
        return fallback_market_narrative(market_profile)


def parse_executive_narrative(
    payload: Any,
    *,
    assessment_profile: Mapping[str, Any],
    strict: bool = False,
) -> dict[str, Any]:
    if assessment_profile.get("workflow_revision"):
        from .facts import render_payload
        payload = render_payload(payload, build_claim_catalog(assessment_profile))
    if not isinstance(payload, Mapping):
        if strict:
            raise ValueError("Executive narrative response must be an object")
        return fallback_executive_narrative(assessment_profile)
    try:
        if strict:
            _require_payload_fields(
                payload,
                ("motivation", "key_findings", "recommendations", "limitations"),
            )
            if payload.get("motivation") is not None:
                _validate_claim_payload(payload.get("motivation"), field="motivation")
            for field in ("key_findings", "recommendations", "limitations"):
                _validate_claim_list_payload(payload.get(field, []), field=field)
        motivation = (
            _claim_from_payload(
                payload.get("motivation"),
                claim_id=executive_claim_id("motivation", 1),
                claim_kind="summary",
                scope="assessment",
            )
            if payload.get("motivation")
            else None
        )
        return MFIExecutiveNarrative(
            motivation=motivation,
            key_findings=_claim_list(
                payload.get("key_findings"),
                prefix="executive.finding",
                claim_kind="finding",
                default_scope="assessment",
                max_items=len(
                    assessment_profile.get("priority_dimension_names", []) or []
                ),
            ),
            recommendations=_claim_list(
                payload.get("recommendations"),
                prefix="executive.recommendation",
                claim_kind="recommendation",
                default_scope="assessment",
                max_items=NARRATIVE_DENSITY_POLICY.executive_recommendations,
            ),
            limitations=_claim_list(
                payload.get("limitations"),
                prefix="executive.limitation",
                claim_kind="limitation",
                default_scope="assessment",
                max_items=NARRATIVE_DENSITY_POLICY.executive_limitations,
            ),
            # Deterministic regardless of what the model returned, so the report always
            # states its own scope exactly once.
            scope_statement=_scope_statement_claim(),
        ).model_dump()
    except (TypeError, ValueError, ValidationError):
        if strict:
            raise
        return fallback_executive_narrative(assessment_profile)


def fallback_dimension_narrative(
    dimension_profile: Mapping[str, Any],
    *,
    assessment_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a fully deterministic, cited dimension narrative."""
    dimension = str(dimension_profile["dimension"])
    mean_id = _find_id(
        dimension_profile.get("ledger_metric_ids", []), suffix=".mean"
    )
    rank_id = _find_id(
        dimension_profile.get("ledger_metric_ids", []), suffix=".rank"
    )
    metric_ids = [item for item in (mean_id, rank_id) if item]
    summary = MFINarrativeClaim(
        claim_id=dimension_claim_id(dimension, "summary", 1),
        text=(
            f"The average {dimension} score across assessed markets and its "
            "relative position are reported in the evidence note below."
        ),
        claim_kind="summary",
        metric_ids=metric_ids,
        scope="assessment",
        polarity="neutral",
    )
    findings: list[MFINarrativeClaim] = []
    statistics = dimension_profile.get("statistics") or {}
    range_ids = [
        item
        for item in dimension_profile.get("ledger_metric_ids", [])
        if str(item).endswith((".minimum", ".maximum", ".iqr"))
    ]
    findings.append(
        MFINarrativeClaim(
            claim_id=dimension_claim_id(dimension, "finding", 1),
            text=(
                f"{dimension} varied across the included assessed markets; the "
                "minimum, maximum, and interquartile spread are reported below."
            ),
            claim_kind="finding",
            metric_ids=[str(item) for item in range_ids],
            scope="assessment",
            polarity="descriptive",
        )
    )
    subdimensions: list[MFISubdimensionNarrative] = []
    if bool(dimension_profile.get("is_priority")):
        ranked_drivers = [
            item
            for item in dimension_profile.get("drivers", []) or []
            if isinstance(item, Mapping)
            and item.get("weakness_rank") is not None
            and (
                item.get("role") != "item_driver"
                or bool(item.get("item_relevant"))
            )
        ]
        ranked_drivers.sort(
            key=lambda item: (
                int(item.get("weakness_rank") or 10**9),
                -int(item.get("severity_weight") or 0),
                str(item.get("metric_id") or ""),
            )
        )
        selected_drivers = [
            item for item in ranked_drivers if item.get("role") != "item_driver"
        ][:3]
        relevant_item = next(
            (
                item
                for item in ranked_drivers
                if item.get("role") == "item_driver"
                and bool(item.get("item_relevant"))
            ),
            None,
        )
        if relevant_item is not None:
            selected_drivers.append(relevant_item)
        for driver in ranked_drivers:
            if driver not in selected_drivers and len(selected_drivers) < 4:
                selected_drivers.append(driver)
        driver_ids: list[str] = []
        for driver in selected_drivers:
            preferred = _preferred_metric_ledger_ids(driver)
            driver_id = _find_id(preferred, suffix=".unfavorable_rate")
            if driver_id is None and preferred:
                driver_id = preferred[0]
            if driver_id and driver_id not in driver_ids:
                driver_ids.append(driver_id)
            if len(driver_ids) == 4:
                break
        candidates = (
            dimension_profile.get("drivers", [])
            if dimension == "Food Quality"
            else dimension_profile.get("subsections", [])
        )
        candidates = [
            item
            for item in candidates or []
            if isinstance(item, Mapping) and item.get("weakness_rank") is not None
        ]
        candidates.sort(
            key=lambda item: (
                int(item.get("weakness_rank") or 10**9),
                str(item.get("metric_id") or ""),
            )
        )
        for index, metric in enumerate(candidates[:2]):
            cited = _preferred_metric_ledger_ids(metric)
            subdimensions.append(
                MFISubdimensionNarrative(
                    name=str(metric.get("display_name") or metric.get("metric_id")),
                    subsection_metric_id=_find_id(
                        cited,
                        suffix=(
                            ".unfavorable_rate"
                            if dimension == "Food Quality"
                            else ".mean_normalized"
                        ),
                    )
                    or (cited[0] if cited else None),
                    score_0_10=(
                        round(float(metric["mean_normalized_value"]), 2)
                        if metric.get("mean_normalized_value") is not None
                        and dimension != "Food Quality"
                        else None
                    ),
                    interpretation=MFINarrativeClaim(
                        claim_id=subdimension_claim_id(dimension, index + 1),
                        text=(
                            f"This evidence is among the weakest available "
                            f"{dimension} components and should guide deeper review."
                        ),
                        claim_kind="finding",
                        metric_ids=list(dict.fromkeys([*cited, *driver_ids])),
                        scope="assessment",
                        polarity="unfavorable",
                    ),
                    driver_metric_ids=driver_ids if index == 0 else [],
                )
            )
    geographic: list[MFINarrativeClaim] = []
    localized = dimension_profile.get("localized_patterns") or {}
    lowest = list(localized.get("markets_where_lowest", []) or [])
    if lowest and bool(dimension_profile.get("is_priority")):
        market_ids = [
            str(item.get("ledger_metric_id"))
            for item in localized.get("ordered_markets", []) or []
            if isinstance(item, Mapping)
            and item.get("name") in set(lowest)
            and item.get("ledger_metric_id")
        ]
        geographic.append(
            MFINarrativeClaim(
                claim_id=dimension_claim_id(dimension, "geography", 1),
                text=(
                    f"{dimension} was the lowest-scoring dimension in one or more "
                    "assessed markets listed in the deterministic profile."
                ),
                claim_kind="geographic_pattern",
                metric_ids=market_ids,
                scope="market",
                polarity="unfavorable",
            )
        )
    elif bool(dimension_profile.get("is_priority")):
        ordered_markets = [
            item
            for item in localized.get("ordered_markets", []) or []
            if isinstance(item, Mapping) and item.get("ledger_metric_id")
        ]
        if ordered_markets:
            geographic.append(
                MFINarrativeClaim(
                    claim_id=dimension_claim_id(dimension, "geography", 1),
                    text=(
                        f"The lowest assessed-market {dimension} observation is "
                        "identified in the localized evidence below."
                    ),
                    claim_kind="geographic_pattern",
                    metric_ids=[str(ordered_markets[0]["ledger_metric_id"])],
                    scope="market",
                    polarity="unfavorable",
                )
            )
    limitations: list[MFINarrativeClaim] = []
    for index, limitation in enumerate(
        item
        for item in assessment_profile.get("limitations", []) or []
        if isinstance(item, Mapping)
        and item.get("dimension") in {None, dimension}
    ):
        limitations.append(
            MFINarrativeClaim(
                claim_id=dimension_claim_id(dimension, "limitation", index + 1),
                text=str(limitation.get("message") or ""),
                claim_kind="limitation",
                metric_ids=_limitation_claim_ids(
                    limitation, assessment_profile
                ),
                scope="assessment",
                polarity="descriptive",
            )
        )
    recommendation_ids = (
        subdimensions[0].interpretation.metric_ids
        if subdimensions
        else metric_ids
    )
    recommendation = MFINarrativeClaim(
        claim_id=dimension_claim_id(dimension, "recommendation", 1),
        text=DIMENSION_REVIEW_GUIDANCE[dimension],
        claim_kind="recommendation",
        metric_ids=recommendation_ids,
        scope="assessment",
        polarity="neutral",
    )
    narrative = MFIDimensionNarrative(
        dimension=dimension,
        is_priority=bool(dimension_profile.get("is_priority")),
        summary=summary,
        key_findings=findings,
        subdimension_analysis=subdimensions,
        geographic_patterns=geographic,
        data_limitations=limitations[
            : NARRATIVE_DENSITY_POLICY.priority_limitations
            if bool(dimension_profile.get("is_priority"))
            else NARRATIVE_DENSITY_POLICY.non_priority_limitations
        ],
        recommendations=[recommendation],
    )
    _ = statistics
    return narrative.model_dump()


def fallback_market_narrative(
    market_profile: Mapping[str, Any],
) -> dict[str, Any]:
    market_name = str(market_profile["market_name"])
    overall_id = _find_id(
        market_profile.get("ledger_metric_ids", []), suffix=".stored"
    )
    weak_dimensions = [
        item
        for item in market_profile.get("weak_dimensions", []) or []
        if isinstance(item, Mapping)
    ][: NARRATIVE_DENSITY_POLICY.market_priority_issues]
    issues = [
        MFINarrativeClaim(
            claim_id=market_claim_id(market_name, "issue", index + 1),
            text=(
                f"{item.get('dimension')} is among the market's lowest-scoring "
                "dimensions and warrants review."
            ),
            claim_kind="finding",
            metric_ids=[
                str(value) for value in item.get("ledger_metric_ids", []) if value
            ],
            scope="market",
            polarity="unfavorable",
        )
        for index, item in enumerate(weak_dimensions)
    ]
    linked = issues[0].metric_ids if issues else ([overall_id] if overall_id else [])
    return MFIMarketNarrative(
        market_name=market_name,
        region=_optional_text(market_profile.get("region")),
        overall_mfi=float(market_profile["overall_mfi"]),
        score_rank=int(market_profile["score_rank"]),
        weak_dimensions=[
            str(item.get("dimension")) for item in weak_dimensions
        ],
        priority_issues=issues,
        recommended_interventions=[
            MFINarrativeClaim(
                claim_id=market_claim_id(market_name, "intervention", 1),
                text=(
                    "Review the cited market-side weakness with local teams and "
                    "triangulate it with operational and feasibility evidence."
                ),
                claim_kind="recommendation",
                metric_ids=linked,
                scope="market",
                polarity="neutral",
            )
        ],
        limitations=[],
        # Deliberately absent: repeating one identical caveat in every market section is
        # the boilerplate the report already carries too much of, and a caveat that
        # appears only when drafting happens to fail is not a delivery contract.
        modality_consideration=None,
    ).model_dump()


def deduplicate_dimension_recommendations(
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    dimension_profiles: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Replace repeated recommendation boilerplate with cited dimension guidance.

    The first occurrence is retained. Later normalized duplicates receive the
    dimension-specific deterministic recommendation while preserving their stable claim
    ID. If that replacement is already present in the same output, the duplicate claim is
    dropped rather than adding more boilerplate.
    """
    result = deepcopy(dict(dimension_narratives))
    profiles = {
        str(profile.get("dimension")): profile
        for profile in dimension_profiles
        if isinstance(profile, Mapping) and profile.get("dimension")
    }
    seen: set[str] = set()
    for dimension in DISPLAY_DIMENSIONS:
        narrative = result.get(dimension)
        if not isinstance(narrative, dict):
            continue
        recommendations = narrative.get("recommendations") or []
        if not isinstance(recommendations, list):
            continue
        retained: list[dict[str, Any]] = []
        for recommendation in recommendations:
            if not isinstance(recommendation, dict):
                continue
            normalized = _normalized_narrative_text(recommendation.get("text"))
            if normalized and normalized not in seen:
                seen.add(normalized)
                retained.append(recommendation)
                continue
            profile = profiles.get(dimension)
            if not isinstance(profile, Mapping):
                continue
            fallback = fallback_dimension_narrative(
                profile,
                assessment_profile={"limitations": [], "metric_ledger": {}},
            )
            replacement = deepcopy((fallback.get("recommendations") or [None])[0])
            if not isinstance(replacement, dict):
                continue
            replacement["claim_id"] = recommendation.get("claim_id")
            replacement_normalized = _normalized_narrative_text(
                replacement.get("text")
            )
            if not replacement_normalized or replacement_normalized in seen:
                continue
            seen.add(replacement_normalized)
            retained.append(replacement)
        narrative["recommendations"] = retained
    return result


def _normalized_narrative_text(value: Any) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split()
    )


def fallback_executive_narrative(
    assessment_profile: Mapping[str, Any],
) -> dict[str, Any]:
    priority = list(assessment_profile.get("priority_dimension_names", []) or [])
    motivation = MFINarrativeClaim(
        claim_id=executive_claim_id("motivation", 1),
        text=(
            "This report summarizes functionality across the included assessed "
            "markets and identifies relative priorities for deeper analysis."
        ),
        claim_kind="summary",
        metric_ids=["assessment.mfi.mean", "assessment.mfi.denominator"],
        scope="assessment",
        polarity="neutral",
    )
    findings: list[MFINarrativeClaim] = []
    for index, dimension in enumerate(priority):
        dimension_profile = next(
            (
                item
                for item in assessment_profile.get("dimensions", []) or []
                if isinstance(item, Mapping)
                and item.get("dimension") == dimension
            ),
            {},
        )
        findings.append(
            MFINarrativeClaim(
                claim_id=executive_claim_id("finding", index + 1),
                text=(
                    f"{dimension} is a priority dimension for deeper analysis "
                    "under the relative assessment-profile rule."
                ),
                claim_kind="finding",
                metric_ids=[
                    str(item)
                    for item in dimension_profile.get("ledger_metric_ids", [])
                    if str(item).endswith((".mean", ".rank"))
                ],
                scope="assessment",
                polarity="unfavorable",
            )
        )
    limitations = [
        MFINarrativeClaim(
            claim_id=executive_claim_id("limitation", index + 1),
            text=str(item.get("message") or ""),
            claim_kind="limitation",
            metric_ids=_limitation_claim_ids(item, assessment_profile),
            scope="assessment",
            polarity="descriptive",
        )
        for index, item in enumerate(assessment_profile.get("limitations", []) or [])
        if isinstance(item, Mapping) and item.get("message")
    ]
    recommendation_ids = [
        metric_id
        for finding in findings[:2]
        for metric_id in finding.metric_ids
    ]
    return MFIExecutiveNarrative(
        motivation=motivation,
        key_findings=findings,
        recommendations=[
            MFINarrativeClaim(
                claim_id=executive_claim_id("recommendation", 1),
                text=(
                    "Prioritize deeper review of the cited dimensions and use "
                    "their subsection, driver, and geographic evidence to design "
                    "proportionate follow-up."
                ),
                claim_kind="recommendation",
                metric_ids=recommendation_ids,
                scope="assessment",
                polarity="neutral",
            )
        ],
        limitations=limitations[:3],
        scope_statement=_scope_statement_claim(),
    ).model_dump()


def _scope_statement_claim() -> MFINarrativeClaim:
    """Build the deterministic statement of what the assessment does not establish.

    It is emitted on every run, including LLM-drafted ones, so the report always states
    its own scope once. Because it names a modality and reaches a conclusion about scope,
    only the validator's methodological-negation rule keeps it legal — which makes the
    deterministic suites a live guard on that rule.
    """
    return MFINarrativeClaim(
        claim_id=executive_claim_id("scope_statement", 1),
        text=NEUTRAL_SCOPE_STATEMENT,
        claim_kind="limitation",
        metric_ids=[],
        scope="assessment",
        polarity="neutral",
    )


def _limitation_claim_ids(
    limitation: Mapping[str, Any],
    assessment_profile: Mapping[str, Any],
) -> list[str]:
    """Resolve public limitation context to deterministic ledger citations."""
    ledger = assessment_profile.get("metric_ledger") or {}
    code = str(limitation.get("code") or "")
    dimension = _optional_text(limitation.get("dimension"))
    candidates: list[str] = []
    if code == "unavailable_explanatory_evidence" and dimension:
        candidates.append(
            "assessment.limitation.unavailable_evidence."
            f"{_slug(dimension)}.count"
        )
    elif code == "incomplete_regional_coverage":
        candidates.append("assessment.limitation.missing_region.count")
    elif code == "mfir_records_excluded":
        candidates.append("assessment.excluded_market_records.count")
    candidates.extend(
        str(metric_id)
        for metric_id in limitation.get("metric_ids", []) or []
        if metric_id
    )
    return [
        metric_id
        for metric_id in dict.fromkeys(candidates)
        if metric_id in ledger
    ]


def validate_structured_narratives(
    *,
    context_evidence: Sequence[Mapping[str, Any]],
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    claim_catalog: Mapping[str, Mapping[str, Any]],
    assessment_profile: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Validate, mark, and return detached narrative artifacts."""
    context_copy = deepcopy(list(context_evidence))
    dimension_copy, market_copy, executive_copy = apply_narrative_density_policy(
        dimension_narratives=dimension_narratives,
        market_narratives=market_narratives,
        executive_narrative=executive_narrative,
        assessment_profile=assessment_profile,
    )
    (
        dimension_copy,
        market_copy,
        executive_copy,
        context_copy,
    ) = canonicalize_narrative_identities(
        dimension_narratives=dimension_copy,
        market_narratives=market_copy,
        executive_narrative=executive_copy,
        context_evidence=context_copy,
    )
    known_documents = {
        str(item.get("doc_id")): item
        for item in documents
        if isinstance(item, Mapping) and item.get("doc_id")
    }
    relevant_items = _relevant_source_metric_ids(assessment_profile)
    priority_dimensions = set(
        assessment_profile.get("priority_dimension_names", []) or []
    )
    flags: list[dict[str, Any]] = []
    claims = list(
        _iter_claims(
            dimension_copy,
            market_copy,
            executive_copy,
        )
    )

    seen_claim_ids: set[str] = set()
    for location, claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        claim_flags: list[dict[str, Any]] = []
        if not claim_id or claim_id in seen_claim_ids:
            claim_flags.append(
                _flag(
                    code="duplicate_or_missing_claim_id",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id or None,
                    message="Narrative claims require a unique stable claim ID.",
                )
            )
        seen_claim_ids.add(claim_id)
        cited_ids = [
            str(item) for item in claim.get("metric_ids", []) if item
        ]
        cited_entries: list[Mapping[str, Any]] = []
        for metric_id in cited_ids:
            entry = claim_catalog.get(metric_id)
            if not isinstance(entry, Mapping):
                claim_flags.append(
                    _flag(
                        code="invalid_metric_id",
                        severity="high",
                        artifact_type=location["artifact_type"],
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message=f"Claim cites unknown metric ID: {metric_id}.",
                        metric_ids=[metric_id],
                    )
                )
            else:
                cited_entries.append(entry)
        for document_id in claim.get("document_ids", []) or []:
            if str(document_id) not in known_documents:
                claim_flags.append(
                    _flag(
                        code="invalid_document_id",
                        severity="high",
                        artifact_type=location["artifact_type"],
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message=f"Claim cites unknown document ID: {document_id}.",
                        document_ids=[str(document_id)],
                    )
                )
        if location["artifact_type"] == "dimension":
            wrong_context = [
                str(entry.get("metric_id"))
                for entry in cited_entries
                if entry.get("dimension")
                and entry.get("dimension") != location.get("artifact_id")
            ]
            if wrong_context:
                claim_flags.append(
                    _flag(
                        code="citation_context_mismatch",
                        severity="high",
                        artifact_type="dimension",
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message="Dimension claim cites evidence from another dimension.",
                        metric_ids=wrong_context,
                    )
                )
        if location["artifact_type"] == "market":
            wrong_context = [
                str(entry.get("metric_id"))
                for entry in cited_entries
                if entry.get("market_name")
                and entry.get("market_name") != location.get("artifact_id")
            ]
            if wrong_context:
                claim_flags.append(
                    _flag(
                        code="citation_context_mismatch",
                        severity="high",
                        artifact_type="market",
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message="Market claim cites evidence from another market.",
                        metric_ids=wrong_context,
                    )
                )
        text = str(claim.get("text") or "")
        numbers = _numeric_tokens(text)
        if numbers and not cited_entries and not claim.get("document_ids"):
            claim_flags.append(
                _flag(
                    code="uncited_numeric_value",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message="Quantitative claim has no metric or document citation.",
                    actual_value=", ".join(token for token, _value, _percent in numbers),
                )
            )
        for token, numeric, is_percent in numbers:
            if _numeric_token_is_authorized(
                numeric,
                is_percent,
                cited_entries,
                [
                    known_documents[str(item)]
                    for item in claim.get("document_ids", []) or []
                    if str(item) in known_documents
                ],
            ):
                continue
            claim_flags.append(
                _flag(
                    code="numeric_value_mismatch",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message=(
                        f"Numeric value {token} is not authorized by the cited "
                        "deterministic evidence."
                    ),
                    metric_ids=cited_ids,
                    actual_value=token,
                )
            )
        if any(percent for _token, _numeric, percent in numbers) and not any(
            str(entry.get("unit")) == "proportion" for entry in cited_entries
        ):
            claim_flags.append(
                _flag(
                    code="unit_mismatch",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message="Percentage claim does not cite a proportion metric.",
                    metric_ids=cited_ids,
                    actual_value=", ".join(
                        token for token, _numeric, percent in numbers if percent
                    ),
                )
            )
        declared_scope = str(claim.get("scope") or "assessment")
        entry_scopes = {str(entry.get("scope")) for entry in cited_entries}
        if entry_scopes and declared_scope not in entry_scopes:
            claim_flags.append(
                _flag(
                    code="scope_mismatch",
                    severity="medium",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message=(
                        f"Claim declares {declared_scope} scope but cites "
                        f"{', '.join(sorted(entry_scopes))} evidence."
                    ),
                    metric_ids=cited_ids,
                )
            )
        declared_polarity = str(claim.get("polarity") or "neutral")
        if _polarity_mismatch(declared_polarity, cited_entries):
            claim_flags.append(
                _flag(
                    code="polarity_mismatch",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message="Claim polarity conflicts with the cited metric orientation.",
                    metric_ids=cited_ids,
                )
            )
        for entry in cited_entries:
            if (
                claim.get("claim_kind") == "limitation"
                or str(entry.get("statistic")) == "count"
            ):
                continue
            source_ids = set(entry.get("source_metric_ids", []) or [])
            item_sources = {item for item in source_ids if ".item." in str(item)}
            if item_sources and not item_sources <= relevant_items:
                claim_flags.append(
                    _flag(
                        code="insufficient_item_coverage",
                        severity="high",
                        artifact_type=location["artifact_type"],
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message=(
                            "Item-level claim cites evidence that did not pass "
                            "the Phase 2 relevance and coverage rule."
                        ),
                        metric_ids=[str(entry.get("metric_id"))],
                    )
                )
        claim_flags.extend(_terminology_flags(text, location, claim_id, cited_ids))
        claim_flags.extend(_causality_flags(text, location, claim_id))
        claim_flags.extend(_markup_flags(text, location, claim_id))
        claim_flags.extend(
            _population_wording_flags(text, location, claim_id, claim, cited_entries)
        )
        claim_flags = _deduplicate_flags(claim_flags)
        claim["validation_flags"] = [
            str(item["code"]) for item in claim_flags
        ]
        claim["validation_status"] = (
            "unverified"
            if any(item["severity"] in _MATERIAL_SEVERITIES for item in claim_flags)
            else "verified"
        )
        flags.extend(claim_flags)

    flags.extend(
        _priority_contract_flags(
            dimension_copy,
            priority_dimensions=priority_dimensions,
        )
    )
    flags.extend(
        _narrative_structure_flags(
            dimension_copy,
            market_copy,
            assessment_profile=assessment_profile,
            claim_catalog=claim_catalog,
        )
    )
    flags.extend(_recommendation_linkage_flags(dimension_copy, market_copy, executive_copy))
    flags.extend(_duplicate_dimension_recommendation_flags(dimension_copy))
    flags.extend(_context_contract_flags(context_copy, known_documents))

    material_claim_ids = {
        str(flag.get("claim_id"))
        for flag in flags
        if flag.get("claim_id") and flag.get("severity") in _MATERIAL_SEVERITIES
    }
    for _location, claim in _iter_claims(
        dimension_copy,
        market_copy,
        executive_copy,
    ):
        if str(claim.get("claim_id")) in material_claim_ids:
            claim["validation_status"] = "unverified"
    material = [flag for flag in flags if flag["severity"] in _MATERIAL_SEVERITIES]
    low = [flag for flag in flags if flag["severity"] == "low"]
    validation = MFIClaimValidationResult(
        status=(
            "failed"
            if material
            else "passed_with_warnings"
            if low
            else "passed"
        ),
        validated_claim_count=len(claims),
        verified_claim_count=sum(
            claim.get("validation_status") == "verified"
            for _location, claim in _iter_claims(
                dimension_copy,
                market_copy,
                executive_copy,
            )
        ),
        unverified_claim_count=sum(
            claim.get("validation_status") == "unverified"
            for _location, claim in _iter_claims(
                dimension_copy,
                market_copy,
                executive_copy,
            )
        ),
        flags=[MFINarrativeQAFlag.model_validate(flag) for flag in flags],
    ).model_dump()
    return (
        validation,
        dimension_copy,
        market_copy,
        executive_copy,
        context_copy,
        {"flags": flags},
    )


def validate_evidence_bound_narratives(
    *,
    context_evidence: Sequence[Mapping[str, Any]],
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    claim_catalog: Mapping[str, Mapping[str, Any]],
    assessment_profile: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Validate only application contracts, citations, and numeric claims.

    Interpretive judgements (scope, polarity, causality, terminology, ranking
    language, recommendation quality, and repetition) belong to the semantic
    review.  This validator deliberately avoids turning prose heuristics into
    deterministic delivery blockers.
    """
    context_copy = deepcopy(list(context_evidence))
    dimension_copy, market_copy, executive_copy = apply_narrative_density_policy(
        dimension_narratives=dimension_narratives,
        market_narratives=market_narratives,
        executive_narrative=executive_narrative,
        assessment_profile=assessment_profile,
    )
    (
        dimension_copy,
        market_copy,
        executive_copy,
        context_copy,
    ) = canonicalize_narrative_identities(
        dimension_narratives=dimension_copy,
        market_narratives=market_copy,
        executive_narrative=executive_copy,
        context_evidence=context_copy,
    )
    known_documents = {
        str(item.get("doc_id")): item
        for item in documents
        if isinstance(item, Mapping) and item.get("doc_id")
    }
    flags: list[dict[str, Any]] = []
    claims = list(_iter_claims(dimension_copy, market_copy, executive_copy))
    seen_claim_ids: set[str] = set()
    for location, claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        claim_flags: list[dict[str, Any]] = []
        if not claim_id or claim_id in seen_claim_ids:
            claim_flags.append(
                _flag(
                    code="duplicate_or_missing_claim_id",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id or None,
                    message="Narrative claims require a unique canonical claim ID.",
                    repairable=False,
                )
            )
        seen_claim_ids.add(claim_id)
        cited_ids = [str(item) for item in claim.get("metric_ids", []) if item]
        cited_entries: list[Mapping[str, Any]] = []
        for metric_id in cited_ids:
            entry = claim_catalog.get(metric_id)
            if not isinstance(entry, Mapping):
                claim_flags.append(
                    _flag(
                        code="invalid_metric_id",
                        severity="high",
                        artifact_type=location["artifact_type"],
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message=f"Claim cites unknown metric ID: {metric_id}.",
                        metric_ids=[metric_id],
                    )
                )
            else:
                cited_entries.append(entry)
        valid_documents: list[Mapping[str, Any]] = []
        for document_id in claim.get("document_ids", []) or []:
            document = known_documents.get(str(document_id))
            if document is None:
                claim_flags.append(
                    _flag(
                        code="invalid_document_id",
                        severity="high",
                        artifact_type=location["artifact_type"],
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message=f"Claim cites unknown document ID: {document_id}.",
                        document_ids=[str(document_id)],
                    )
                )
            else:
                valid_documents.append(document)
        if location["artifact_type"] == "dimension":
            wrong = [
                str(entry.get("metric_id"))
                for entry in cited_entries
                if entry.get("dimension")
                and entry.get("dimension") != location.get("artifact_id")
            ]
            if wrong:
                claim_flags.append(
                    _flag(
                        code="citation_context_mismatch",
                        severity="high",
                        artifact_type="dimension",
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message="Dimension claim cites evidence from another dimension.",
                        metric_ids=wrong,
                    )
                )
        if location["artifact_type"] == "market":
            wrong = [
                str(entry.get("metric_id"))
                for entry in cited_entries
                if entry.get("market_name")
                and entry.get("market_name") != location.get("artifact_id")
            ]
            if wrong:
                claim_flags.append(
                    _flag(
                        code="citation_context_mismatch",
                        severity="high",
                        artifact_type="market",
                        artifact_id=location.get("artifact_id"),
                        field_name=location.get("field_name"),
                        claim_id=claim_id,
                        message="Market claim cites evidence from another market.",
                        metric_ids=wrong,
                    )
                )
        if assessment_profile.get("workflow_revision") == "mfi-reliable-v1":
            from .facts import subject_binding_problems, subsection_binding_problems
            for problem in [*subject_binding_problems(str(claim.get("text") or ""), cited_entries), *subsection_binding_problems(claim, claim_catalog)]:
                claim_flags.append(_flag(code="citation_context_mismatch", severity="high", artifact_type=location["artifact_type"], artifact_id=location.get("artifact_id"), field_name=location.get("field_name"), claim_id=claim_id, message=problem, metric_ids=cited_ids))
            passages = []
            for passage in claim.get("source_passages", []):
                document = known_documents.get(str(passage.get("document_id") or passage.get("doc_id")))
                excerpt = str(passage.get("text") or "")
                if document and str(passage.get("document_id") or passage.get("doc_id")) in {str(item) for item in claim.get("document_ids", [])} and excerpt and excerpt in str(document.get("content") or ""):
                    passages.append({"content": excerpt})
                else:
                    claim_flags.append(_flag(code="invalid_source_passage", severity="high", artifact_type=location["artifact_type"], artifact_id=location.get("artifact_id"), field_name=location.get("field_name"), claim_id=claim_id, message="The cited passage is not present in the original document."))
            valid_documents = passages
        numbers = _numeric_tokens(str(claim.get("text") or ""))
        if numbers and not cited_ids and not claim.get("document_ids"):
            claim_flags.append(
                _flag(
                    code="uncited_numeric_value",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message="Quantitative claim has no metric or document citation.",
                    actual_value=", ".join(token for token, _value, _percent in numbers),
                )
            )
        for token, numeric, is_percent in numbers:
            if _numeric_token_is_authorized(
                numeric,
                is_percent,
                cited_entries,
                valid_documents,
            ):
                continue
            claim_flags.append(
                _flag(
                    code="numeric_value_mismatch",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message=(
                        f"Numeric value {token} is not authorized by the cited "
                        "evidence."
                    ),
                    metric_ids=cited_ids,
                    actual_value=token,
                )
            )
        if any(percent for _token, _numeric, percent in numbers) and not any(
            str(entry.get("unit")) == "proportion" for entry in cited_entries
        ) and not valid_documents:
            claim_flags.append(
                _flag(
                    code="unit_mismatch",
                    severity="high",
                    artifact_type=location["artifact_type"],
                    artifact_id=location.get("artifact_id"),
                    field_name=location.get("field_name"),
                    claim_id=claim_id,
                    message="Percentage claim does not cite proportion evidence.",
                    metric_ids=cited_ids,
                    actual_value=", ".join(
                        token for token, _numeric, percent in numbers if percent
                    ),
                )
            )
        claim_flags = _deduplicate_flags(claim_flags)
        claim["validation_flags"] = [str(item["code"]) for item in claim_flags]
        claim["validation_flag_ids"] = [
            str(item["flag_id"]) for item in claim_flags
        ]
        claim["validation_status"] = "unverified" if claim_flags else "verified"
        flags.extend(claim_flags)

    for statement in context_copy:
        if not isinstance(statement, Mapping):
            continue
        invalid = [
            str(document_id)
            for document_id in statement.get("document_ids", []) or []
            if str(document_id) not in known_documents
        ]
        if invalid:
            flags.append(
                _flag(
                    code="invalid_context_document_id",
                    severity="high",
                    artifact_type="context",
                    artifact_id=str(statement.get("statement_id") or "") or None,
                    field_name="text",
                    claim_id=str(statement.get("statement_id") or "") or None,
                    message="Context statement cites an unknown document.",
                    document_ids=invalid,
                )
            )

    flags = _deduplicate_flags(flags)
    material_claim_ids = {
        str(flag.get("claim_id"))
        for flag in flags
        if flag.get("claim_id") and flag.get("severity") in _MATERIAL_SEVERITIES
    }
    for _location, claim in _iter_claims(dimension_copy, market_copy, executive_copy):
        if str(claim.get("claim_id")) in material_claim_ids:
            claim["validation_status"] = "unverified"
    material = [flag for flag in flags if flag["severity"] in _MATERIAL_SEVERITIES]
    low = [flag for flag in flags if flag["severity"] == "low"]
    validation = MFIClaimValidationResult(
        status=(
            "failed"
            if material
            else "passed_with_warnings"
            if low
            else "passed"
        ),
        validated_claim_count=len(claims),
        verified_claim_count=sum(
            claim.get("validation_status") == "verified"
            for _location, claim in _iter_claims(
                dimension_copy, market_copy, executive_copy
            )
        ),
        unverified_claim_count=sum(
            claim.get("validation_status") == "unverified"
            for _location, claim in _iter_claims(
                dimension_copy, market_copy, executive_copy
            )
        ),
        flags=[MFINarrativeQAFlag.model_validate(flag) for flag in flags],
    ).model_dump()
    return (
        validation,
        dimension_copy,
        market_copy,
        executive_copy,
        context_copy,
        {"flags": flags},
    )


def apply_narrative_density_policy(
    *,
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    assessment_profile: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Bound every canonical artifact, including rehydrated correction state."""
    dimensions = deepcopy(dict(dimension_narratives))
    for narrative in dimensions.values():
        if not isinstance(narrative, dict):
            continue
        if assessment_profile.get("workflow_revision") == "mfi-reliable-v1":
            continue
        priority = bool(narrative.get("is_priority"))
        narrative["key_findings"] = list(narrative.get("key_findings") or [])[
            : (
                NARRATIVE_DENSITY_POLICY.priority_findings
                if priority
                else NARRATIVE_DENSITY_POLICY.non_priority_findings
            )
        ]
        narrative["subdimension_analysis"] = (
            list(narrative.get("subdimension_analysis") or [])[
                : NARRATIVE_DENSITY_POLICY.priority_subdimensions
            ]
            if priority
            else []
        )
        narrative["geographic_patterns"] = list(
            narrative.get("geographic_patterns") or []
        )[
            : (
                NARRATIVE_DENSITY_POLICY.priority_geographic_patterns
                if priority
                else NARRATIVE_DENSITY_POLICY.non_priority_geographic_patterns
            )
        ]
        narrative["data_limitations"] = list(
            narrative.get("data_limitations") or []
        )[
            : (
                NARRATIVE_DENSITY_POLICY.priority_limitations
                if priority
                else NARRATIVE_DENSITY_POLICY.non_priority_limitations
            )
        ]
        narrative["recommendations"] = list(
            narrative.get("recommendations") or []
        )[
            : (
                NARRATIVE_DENSITY_POLICY.priority_recommendations
                if priority
                else NARRATIVE_DENSITY_POLICY.non_priority_recommendations
            )
        ]
    markets = deepcopy(dict(market_narratives))
    for narrative in markets.values():
        if not isinstance(narrative, dict):
            continue
        narrative["priority_issues"] = list(
            narrative.get("priority_issues") or []
        )[: NARRATIVE_DENSITY_POLICY.market_priority_issues]
        narrative["recommended_interventions"] = list(
            narrative.get("recommended_interventions") or []
        )[: NARRATIVE_DENSITY_POLICY.market_recommendations]
        narrative["limitations"] = [
            claim
            for claim in list(narrative.get("limitations") or [])
            if isinstance(claim, Mapping) and claim.get("metric_ids")
        ][: NARRATIVE_DENSITY_POLICY.market_limitations]
        narrative["modality_consideration"] = None

    executive = deepcopy(dict(executive_narrative))
    priority_count = len(
        assessment_profile.get("priority_dimension_names", []) or []
    )
    executive["key_findings"] = list(
        executive.get("key_findings") or []
    )[:priority_count]
    executive["recommendations"] = list(
        executive.get("recommendations") or []
    )[: NARRATIVE_DENSITY_POLICY.executive_recommendations]
    executive["limitations"] = list(executive.get("limitations") or [])[
        : NARRATIVE_DENSITY_POLICY.executive_limitations
    ]
    return dimensions, markets, executive


def _duplicate_dimension_recommendation_flags(
    dimension_narratives: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Flag later cross-dimension boilerplate instead of replacing live prose."""
    ordered = [name for name in DISPLAY_DIMENSIONS if name in dimension_narratives]
    ordered.extend(
        sorted(
            (str(name) for name in dimension_narratives if str(name) not in ordered),
            key=lambda value: (value.casefold(), value),
        )
    )
    first_by_text: dict[str, tuple[str, str]] = {}
    flags: list[dict[str, Any]] = []
    for dimension in ordered:
        narrative = dimension_narratives.get(dimension)
        if not isinstance(narrative, Mapping):
            continue
        for recommendation in narrative.get("recommendations", []) or []:
            if not isinstance(recommendation, Mapping):
                continue
            normalized = _normalized_narrative_text(recommendation.get("text"))
            if not normalized:
                continue
            claim_id = str(recommendation.get("claim_id") or "")
            first = first_by_text.get(normalized)
            if first is None:
                first_by_text[normalized] = (dimension, claim_id)
                continue
            flags.append(
                _flag(
                    code="duplicate_dimension_recommendation",
                    severity="medium",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    field_name="recommendations",
                    claim_id=claim_id or None,
                    message=(
                        "Recommendation duplicates wording already used for "
                        f"{first[0]}; provide dimension-specific review guidance."
                    ),
                    recommendation=(
                        "Rewrite this recommendation using the cited evidence for "
                        f"{dimension} while preserving its scope and linkage."
                    ),
                    repairable=True,
                )
            )
    return flags


def normalize_red_team_flags(payload: Any) -> list[dict[str, Any]]:
    raw_flags = payload.get("flags", []) if isinstance(payload, Mapping) else []
    flags: list[dict[str, Any]] = []
    for raw in raw_flags or []:
        if not isinstance(raw, Mapping):
            continue
        artifact_type = str(raw.get("artifact_type") or "global")
        if artifact_type not in {
            "context",
            "dimension",
            "market",
            "executive_summary",
            "global",
        }:
            artifact_type = "global"
        severity = str(raw.get("severity") or "medium").lower()
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        code = str(raw.get("code") or raw.get("issue_type") or "semantic_error")
        metric_ids = [str(item) for item in raw.get("metric_ids", []) if item]
        document_ids = [str(item) for item in raw.get("document_ids", []) if item]
        identity = "|".join(
            [
                code,
                artifact_type,
                str(raw.get("artifact_id") or ""),
                str(raw.get("field_name") or ""),
                str(raw.get("claim_id") or ""),
            ]
        )
        flags.append(
            MFINarrativeQAFlag(
                flag_id=f"red-team-{_slug(code)}-{_short_hash(identity)}",
                source="red_team",
                code=code,
                severity=severity,
                artifact_type=artifact_type,
                artifact_id=_optional_text(raw.get("artifact_id")),
                field_name=_optional_text(raw.get("field_name")),
                claim_id=_optional_text(raw.get("claim_id")),
                message=str(
                    raw.get("message")
                    or raw.get("details")
                    or "Red-Team review identified a narrative issue."
                ),
                recommendation=str(raw.get("recommendation") or ""),
                metric_ids=metric_ids,
                document_ids=document_ids,
                repairable=bool(raw.get("repairable", True)),
            ).model_dump()
        )
    return flags


def build_correction_targets(
    flags: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, Optional[str], Optional[str]], dict[str, Any]] = {}
    for flag in flags:
        if (
            str(flag.get("severity")) not in _MATERIAL_SEVERITIES
            or not bool(flag.get("repairable", True))
        ):
            continue
        key = (
            str(flag.get("artifact_type") or "global"),
            _optional_text(flag.get("artifact_id")),
            _optional_text(flag.get("field_name")),
        )
        target = grouped.setdefault(
            key,
            {
                "artifact_type": key[0],
                "artifact_id": key[1],
                "field_name": key[2],
                "claim_ids": [],
                "flag_ids": [],
            },
        )
        if flag.get("claim_id"):
            target["claim_ids"].append(str(flag["claim_id"]))
        if flag.get("flag_id"):
            target["flag_ids"].append(str(flag["flag_id"]))
    return [
        MFICorrectionTarget.model_validate(
            {
                **target,
                "claim_ids": sorted(set(target["claim_ids"])),
                "flag_ids": sorted(set(target["flag_ids"])),
            }
        ).model_dump()
        for _key, target in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][0],
                item[0][1] or "",
                item[0][2] or "",
            ),
        )
    ]


def _flag_numeric_values(
    flag: Mapping[str, Any], claim_text: str
) -> list[str]:
    """Return exact flagged numeric tokens that are still present in the claim."""
    flagged_tokens = [
        token
        for token, _value, _percent in _numeric_tokens(
            str(flag.get("actual_value") or "")
        )
    ]
    claim_tokens = {
        token for token, _value, _percent in _numeric_tokens(claim_text)
    }
    return list(
        dict.fromkeys(token for token in flagged_tokens if token in claim_tokens)
    )


def classify_final_qa_flags(
    flags: Sequence[Mapping[str, Any]],
    *,
    dimension_narratives: Mapping[str, Any],
    market_narratives: Mapping[str, Any],
    executive_narrative: Mapping[str, Any],
    correction_completed: bool,
    corrected_verification_completed: bool,
) -> dict[str, Any]:
    """Partition final high findings into deliverable figures and blockers.

    This is deliberately a final-delivery policy, not a relaxation of claim
    validation.  A figure can be retained only after the single correction and
    its verification have completed, and only when the application can map the
    exact numeric token to one canonical reader-facing claim.
    """
    unique_flags = _deduplicate_flags(flags)
    canonical_claim_index(
        dimension_narratives,
        market_narratives,
        executive_narrative,
    )
    claim_text_by_id = {
        str(claim.get("claim_id")): str(claim.get("text") or "")
        for _location, claim in _iter_claims(
            dimension_narratives,
            market_narratives,
            executive_narrative,
        )
        if claim.get("claim_id")
    }
    high_flags = [
        flag for flag in unique_flags if str(flag.get("severity")) == "high"
    ]
    candidate_ids: set[str] = set()
    values_by_flag_id: dict[str, list[str]] = {}
    if correction_completed and corrected_verification_completed:
        for flag in high_flags:
            flag_id = str(flag.get("flag_id") or "")
            claim_id = str(flag.get("claim_id") or "")
            if (
                str(flag.get("source")) != "deterministic"
                or str(flag.get("code"))
                not in DELIVERABLE_UNVERIFIED_FIGURE_CODES
                or not flag_id
                or not claim_id
                or claim_id not in claim_text_by_id
            ):
                continue
            numeric_values = _flag_numeric_values(
                flag, claim_text_by_id[claim_id]
            )
            if not numeric_values:
                continue
            candidate_ids.add(flag_id)
            values_by_flag_id[flag_id] = numeric_values

    initially_blocking = [
        flag
        for flag in high_flags
        if str(flag.get("flag_id") or "") not in candidate_ids
    ]
    blocked_claim_ids = {
        str(flag.get("claim_id"))
        for flag in initially_blocking
        if flag.get("claim_id")
    }
    claim_id_by_flag_id = {
        str(flag.get("flag_id") or ""): str(flag.get("claim_id") or "")
        for flag in high_flags
    }
    deliverable_ids = {
        flag_id
        for flag_id in candidate_ids
        if claim_id_by_flag_id.get(flag_id, "") not in blocked_claim_ids
    }

    annotated_flags: list[dict[str, Any]] = []
    deliverable_flags: list[dict[str, Any]] = []
    for raw_flag in unique_flags:
        flag = dict(raw_flag)
        flag_id = str(flag.get("flag_id") or "")
        severity = str(flag.get("severity") or "")
        if flag_id in deliverable_ids:
            flag["delivery_disposition"] = (
                "retained_unverified_figure_for_delivery"
            )
            deliverable_flags.append(flag)
        elif severity == "high":
            flag["delivery_disposition"] = "blocking"
        elif severity == "medium":
            flag["delivery_disposition"] = "retained_unverified_for_delivery"
        elif severity == "low":
            flag["delivery_disposition"] = "advisory"
        annotated_flags.append(
            MFINarrativeQAFlag.model_validate(flag).model_dump()
        )

    deliverable_flag_ids = {
        str(flag.get("flag_id") or "") for flag in deliverable_flags
    }
    figure_values = list(
        dict.fromkeys(
            value
            for flag in annotated_flags
            if str(flag.get("flag_id") or "") in deliverable_flag_ids
            for value in values_by_flag_id.get(str(flag.get("flag_id") or ""), [])
        )
    )
    return {
        "flags": annotated_flags,
        "deliverable_figure_flags": [
            flag
            for flag in annotated_flags
            if str(flag.get("flag_id") or "") in deliverable_flag_ids
        ],
        "blocking_high_flags": [
            flag
            for flag in annotated_flags
            if flag.get("delivery_disposition") == "blocking"
        ],
        "unverified_figure_flag_ids": sorted(deliverable_flag_ids),
        "unverified_figure_claim_ids": sorted(
            {
                str(flag.get("claim_id"))
                for flag in deliverable_flags
                if flag.get("claim_id")
            }
        ),
        "unverified_figure_values": figure_values,
    }


def build_qa_review(
    deterministic_flags: Sequence[Mapping[str, Any]],
    red_team_flags: Sequence[Mapping[str, Any]],
    *,
    correction_attempts: int,
    correction_history: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    flags = _deduplicate_flags([*deterministic_flags, *red_team_flags])
    material = [flag for flag in flags if flag["severity"] in _MATERIAL_SEVERITIES]
    low = [flag for flag in flags if flag["severity"] == "low"]
    figure_flags = [
        flag
        for flag in flags
        if flag.get("delivery_disposition")
        == "retained_unverified_figure_for_delivery"
    ]
    blocking_high = any(
        flag.get("severity") == "high"
        and flag.get("delivery_disposition") == "blocking"
        for flag in flags
    )
    status = (
        "delivered_with_unverified_figures"
        if figure_flags and not blocking_high
        else "completed_with_warnings"
        if material
        else "passed_with_advisories"
        if low
        else "passed"
    )
    return MFIQAReview(
        status=status,
        correction_attempts=int(correction_attempts),
        correction_history=[
            MFICorrectionAttemptRecord.model_validate(record)
            for record in correction_history
        ],
        flags=[MFINarrativeQAFlag.model_validate(flag) for flag in flags],
        unverified_figure_flag_ids=sorted(
            str(flag.get("flag_id"))
            for flag in figure_flags
            if flag.get("flag_id")
        ),
        unverified_figure_claim_ids=sorted(
            {
                str(flag.get("claim_id"))
                for flag in figure_flags
                if flag.get("claim_id")
            }
        ),
        unverified_figure_values=list(
            dict.fromkeys(
                token
                for flag in figure_flags
                for token, _value, _percent in _numeric_tokens(
                    str(flag.get("actual_value") or "")
                )
            )
        ),
    ).model_dump()


def apply_final_qa_annotations(
    *,
    dimension_narratives: Mapping[str, Any],
    market_narratives: Mapping[str, Any],
    executive_narrative: Mapping[str, Any],
    context_evidence: Sequence[Mapping[str, Any]],
    flags: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Attach final QA outcomes without changing drafted narrative content.

    The simplified live pipeline permits unresolved medium findings at delivery.
    Those findings therefore have to be reflected on the canonical claim (and not
    merely in the final QA table), so the R4 report composer can display one
    adjacent warning and API consumers can see the claim is unverified.  Low
    findings remain advisory and do not change verification status.
    """

    dimensions = deepcopy(dict(dimension_narratives))
    markets = deepcopy(dict(market_narratives))
    executive = deepcopy(dict(executive_narrative))
    context = deepcopy(list(context_evidence))
    material_by_claim: dict[str, list[Mapping[str, Any]]] = {}
    for flag in flags:
        claim_id = _optional_text(flag.get("claim_id"))
        if claim_id and str(flag.get("severity")) in _MATERIAL_SEVERITIES:
            material_by_claim.setdefault(claim_id, []).append(flag)

    def annotate(claim: dict[str, Any], *, identity_key: str = "claim_id") -> None:
        matched = material_by_claim.get(str(claim.get(identity_key) or ""), [])
        if not matched:
            return
        claim["validation_status"] = "unverified"
        claim["validation_flags"] = list(
            dict.fromkeys(
                [str(item) for item in claim.get("validation_flags", []) or []]
                + [str(item.get("code")) for item in matched if item.get("code")]
            )
        )
        claim["validation_flag_ids"] = list(
            dict.fromkeys(
                [str(item) for item in claim.get("validation_flag_ids", []) or []]
                + [
                    str(item.get("flag_id"))
                    for item in matched
                    if item.get("flag_id")
                ]
            )
        )

    for _location, claim in _iter_claims(dimensions, markets, executive):
        annotate(claim)
    for statement in context:
        if isinstance(statement, dict):
            annotate(statement, identity_key="statement_id")
    return dimensions, markets, executive, context


def apply_unresolved_claim_policy(
    *,
    dimension_narratives: Mapping[str, Any],
    market_narratives: Mapping[str, Any],
    executive_narrative: Mapping[str, Any],
    flags: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Withdraw claim text that could not be validated, keeping the draft in diagnostics.

    Repair is attempted first and bounded; this runs only once the attempts are spent. A
    high-severity finding means the statement is not supported by the assessment, so
    delivering it with a caveat would still be delivering it. The text is replaced by
    deterministic wording that says what happened, and the rejected draft is preserved
    only in technical QA details.

    Medium findings are left in place deliberately: they are wording or completeness
    problems rather than unsupported statements, and the delivery policy shows them with a
    visible marker instead of withdrawing them.

    Severity alone decides, not repairability — a finding nothing can repair is precisely
    the one that most needs replacing. No model is involved.
    """
    high_by_claim: dict[str, list[Mapping[str, Any]]] = {}
    for flag in flags:
        if str(flag.get("severity")) != "high":
            continue
        claim_id = _optional_text(flag.get("claim_id"))
        if claim_id:
            high_by_claim.setdefault(claim_id, []).append(flag)

    records: list[dict[str, Any]] = []
    if not high_by_claim:
        return (
            deepcopy(dict(dimension_narratives)),
            deepcopy(dict(market_narratives)),
            deepcopy(dict(executive_narrative)),
            records,
        )

    dimensions = deepcopy(dict(dimension_narratives))
    markets = deepcopy(dict(market_narratives))
    executive = deepcopy(dict(executive_narrative))

    # Resolve by the canonical structural index, never by "first claim with this
    # string".  The identity contract makes this one-to-one; an internal breach is
    # intentionally allowed to propagate to the graph's global fallback policy.
    claim_index = canonical_claim_index(dimensions, markets, executive)

    for location, claim in _iter_claims(dimensions, markets, executive):
        claim_id = str(claim.get("claim_id") or "")
        matched = high_by_claim.get(claim_id)
        if not matched:
            continue
        canonical_location = claim_index.get(claim_id)
        if canonical_location is None:
            continue
        if (
            canonical_location.artifact_type != str(location.get("artifact_type"))
            or canonical_location.artifact_id != str(location.get("artifact_id"))
            or canonical_location.field_name != str(location.get("field_name"))
        ):
            continue
        replacement = withdrawn_text(claim.get("claim_kind"))
        records.append(
            {
                "claim_id": claim_id,
                "artifact_type": str(location.get("artifact_type") or ""),
                "artifact_id": _optional_text(location.get("artifact_id")),
                "field_name": _optional_text(location.get("field_name")),
                "claim_kind": str(claim.get("claim_kind") or ""),
                "rejected_text": str(claim.get("text") or ""),
                "replacement_text": replacement,
                "rejected_metric_ids": list(claim.get("metric_ids") or []),
                "rejected_document_ids": list(claim.get("document_ids") or []),
                "codes": sorted({str(flag.get("code")) for flag in matched}),
                "flag_ids": sorted({str(flag.get("flag_id")) for flag in matched}),
                "severity": "high",
                "disposition": "replaced_by_deterministic_fallback",
            }
        )
        claim["text"] = replacement
        claim["substituted"] = True
        # Citations are dropped from the claim because the evidence note would otherwise
        # print values beneath text that no longer states anything about them, and an
        # unresolvable citation is itself a common cause of withdrawal. The originals stay
        # in the record above.
        claim["metric_ids"] = []
        claim["document_ids"] = []

    return dimensions, markets, executive, records


def unmatched_high_claim_ids(
    flags: Sequence[Mapping[str, Any]],
    substitutions: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return high-severity claim ids that matched no claim in any artifact.

    A finding that points at nothing cannot be shown to a reader, so reporting that claims
    are marked would be a promise the report does not keep.
    """
    substituted = {str(record.get("claim_id")) for record in substitutions}
    return sorted(
        {
            claim_id
            for flag in flags
            if str(flag.get("severity")) == "high"
            and (claim_id := _optional_text(flag.get("claim_id")))
            and claim_id not in substituted
        }
    )


def material_repairable_flags(flags: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(flag)
        for flag in flags
        if str(flag.get("severity")) in _MATERIAL_SEVERITIES
        and bool(flag.get("repairable", True))
    ]


def evidence_note(
    claim: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
) -> str:
    """Compatibility wrapper around the canonical pure composer."""
    return compose_evidence_note(claim, catalog, documents)


def _claim_from_payload(
    payload: Any,
    *,
    claim_id: str,
    claim_kind: str,
    scope: str,
) -> MFINarrativeClaim:
    raw = payload if isinstance(payload, Mapping) else {"text": payload}
    # The single funnel for every drafted claim, so sanitizing here makes the stored text
    # canonical for the API, the preview, and the export alike rather than leaving each
    # renderer to clean up after the model.
    text = sanitize_claim_text(raw.get("text"))
    if not text:
        raise ValueError("Narrative claim text is required")
    return MFINarrativeClaim(
        claim_id=claim_id,
        segments=list(raw.get("segments") or []),
        fact_ids=list(raw.get("fact_ids") or []),
        source_passages=list(raw.get("source_passages") or []),
        text=text,
        claim_kind=claim_kind,
        metric_ids=[str(item) for item in raw.get("metric_ids", []) if item],
        document_ids=[str(item) for item in raw.get("document_ids", []) if item],
        scope=str(raw.get("scope") or scope),
        polarity=str(raw.get("polarity") or "neutral"),
    )


def _claim_list(
    payload: Any,
    *,
    prefix: str,
    claim_kind: str,
    default_scope: str,
    max_items: Optional[int] = None,
) -> list[MFINarrativeClaim]:
    values = payload if isinstance(payload, Sequence) and not isinstance(payload, str) else []
    if max_items is not None:
        values = values[: max(0, int(max_items))]
    claims: list[MFINarrativeClaim] = []
    for index, item in enumerate(values):
        try:
            claims.append(
                _claim_from_payload(
                    item,
                    claim_id=f"{prefix}.{index + 1}",
                    claim_kind=claim_kind,
                    scope=default_scope,
                )
            )
        except (TypeError, ValueError, ValidationError):
            continue
    return claims


def _format_catalog_value(value: float, unit: str, statistic: str) -> str:
    if unit in {"rank", "count"} or statistic in {
        "rank",
        "rank_lowest_first",
        "denominator",
    }:
        return str(int(round(value)))
    if unit == "proportion" or statistic == "coverage_ratio":
        return f"{value * 100.0:.1f}%"
    if unit == "score":
        if "raw" in statistic:
            return f"{value:.2f}"
        return f"{value:.2f}/10"
    return f"{value:.2f}"


def _allowed_renderings(value: float, unit: str, statistic: str) -> list[str]:
    formatted = _format_catalog_value(value, unit, statistic)
    values = [formatted]
    if formatted.endswith("/10"):
        values.append(formatted[:-3])
    return list(dict.fromkeys(values))


def _coverage_label(value: Any) -> Optional[str]:
    if not isinstance(value, Mapping):
        return None
    try:
        coverage = MFICoverageSummary.model_validate(value)
    except (TypeError, ValueError, ValidationError):
        return None
    return format_market_coverage(coverage, subject="assessed markets")


def _canonical_scope(
    evidence_scope: str,
    *,
    market_name: Any,
    region: Any,
) -> str:
    if market_name:
        return "market"
    if region:
        return "region"
    if "surveyed_trader" in evidence_scope:
        return "surveyed_traders"
    return "assessment"


def _preferred_metric_ledger_ids(metric: Mapping[str, Any]) -> list[str]:
    ids = [str(item) for item in metric.get("ledger_metric_ids", []) if item]
    preferred_suffixes = (
        ".mean_normalized",
        ".unfavorable_rate",
        ".coverage",
        ".weakness_rank",
    )
    preferred = [
        metric_id
        for suffix in preferred_suffixes
        for metric_id in ids
        if metric_id.endswith(suffix)
    ]
    return preferred or ids


def _iter_claims(
    dimension_narratives: Mapping[str, Any],
    market_narratives: Mapping[str, Any],
    executive_narrative: Mapping[str, Any],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for dimension, narrative in dimension_narratives.items():
        if not isinstance(narrative, Mapping):
            continue
        yield from _claims_in_artifact(
            narrative,
            artifact_type="dimension",
            artifact_id=str(dimension),
        )
    for market, narrative in market_narratives.items():
        if not isinstance(narrative, Mapping):
            continue
        yield from _claims_in_artifact(
            narrative,
            artifact_type="market",
            artifact_id=str(market),
        )
    if isinstance(executive_narrative, Mapping):
        yield from _claims_in_artifact(
            executive_narrative,
            artifact_type="executive_summary",
            artifact_id="executive_summary",
        )


def _claims_in_artifact(
    artifact: Mapping[str, Any],
    *,
    artifact_type: str,
    artifact_id: str,
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for field_name in _CLAIM_FIELDS:
        value = artifact.get(field_name)
        if isinstance(value, dict) and value.get("claim_id"):
            yield (
                {
                    "artifact_type": artifact_type,
                    "artifact_id": artifact_id,
                    "field_name": field_name,
                },
                value,
            )
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("claim_id"):
                    yield (
                        {
                            "artifact_type": artifact_type,
                            "artifact_id": artifact_id,
                            "field_name": field_name,
                        },
                        item,
                    )
    for subdimension in artifact.get("subdimension_analysis", []) or []:
        if not isinstance(subdimension, Mapping):
            continue
        interpretation = subdimension.get("interpretation")
        if isinstance(interpretation, dict) and interpretation.get("claim_id"):
            yield (
                {
                    "artifact_type": artifact_type,
                    "artifact_id": artifact_id,
                    "field_name": "subdimension_analysis",
                },
                interpretation,
            )


def _numeric_tokens(text: str) -> list[tuple[str, float, bool]]:
    cleaned = re.sub(r"/\s*10\b", "", str(text))
    tokens: list[tuple[str, float, bool]] = []
    for match in _NUMBER_RE.finditer(cleaned):
        token = match.group(0)
        is_percent = token.endswith("%")
        try:
            numeric = float((token[:-1] if is_percent else token).replace(",", ""))
        except ValueError:
            continue
        tokens.append((token, numeric, is_percent))
    units = {"zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,"eighteen":18,"nineteen":19,"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90}
    scales = {"hundred":100, "thousand":1000, "million":1000000, "billion":1000000000}
    words = "|".join([*units, *scales])
    pattern = r"\b((?:" + words + r")(?:[- ]+(?:(?:and|point)[- ]+)?(?:" + words + r"))*)\s+(?:(?:assessed|surveyed|included)\s+)?(?:markets?|traders?|regions?|dimensions?|percent(?:age points)?|per cent|points?|of)\b"
    for match in re.finditer(pattern, cleaned, re.I):
        token = match.group(1)
        parts = re.split(r"[- ]+", token.lower())
        total = current = 0
        decimal = None
        for word in parts:
            if word == "point":
                decimal = ""
            elif word == "and":
                continue
            elif decimal is not None and word in units and units[word] < 10:
                decimal += str(units[word])
            elif word in units:
                current += units[word]
            elif word == "hundred":
                current = (current or 1) * 100
            elif word in scales:
                total += (current or 1) * scales[word]
                current = 0
        numeric = total + current + (float("0." + decimal) if decimal else 0)
        tokens.append((token, float(numeric), bool(re.search(r"percent|per cent", match.group(0), re.I))))
    return tokens


def _numeric_token_is_authorized(
    numeric: float,
    is_percent: bool,
    entries: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
) -> bool:
    for entry in entries:
        renderings = entry.get("allowed_renderings") or [
            entry.get("formatted_value")
        ]
        for rendering in renderings:
            for _token, expected, expected_percent in _numeric_tokens(
                str(rendering or "")
            ):
                if is_percent == expected_percent and abs(numeric - expected) <= 5e-7:
                    return True
    for document in documents:
        haystack = " ".join(
            str(document.get(field) or "")
            for field in ("date", "title", "content")
        )
        for _token, document_numeric, document_percent in _numeric_tokens(haystack):
            if is_percent == document_percent and abs(numeric - document_numeric) <= 5e-7:
                return True
    return False


def _polarity_mismatch(
    declared: str,
    entries: Sequence[Mapping[str, Any]],
) -> bool:
    for entry in entries:
        orientation = str(entry.get("orientation") or "")
        unit = str(entry.get("unit") or "")
        statistic = str(entry.get("statistic") or "")
        if statistic == "coverage_ratio":
            continue
        if unit != "proportion":
            continue
        if orientation == "higher_is_worse" and declared == "favorable":
            return True
        if orientation == "descriptive" and declared in {"favorable", "unfavorable"}:
            return True
    return False


def _location_fields(location: Mapping[str, Any]) -> dict[str, Any]:
    """Carry a flag's artifact identity from the claim it came from.

    Correction targets are grouped by ``(artifact_type, artifact_id, field_name)``, so a
    flag that hardcodes an identity re-drafts the wrong artifact. The affordability and
    concept checks previously hardcoded ``dimension``/``Price``, which was harmless only
    while they were gated to that dimension.
    """
    return {
        "artifact_type": str(location["artifact_type"]),
        "artifact_id": _optional_text(location.get("artifact_id")),
        "field_name": _optional_text(location.get("field_name")),
    }


# Word-boundaried, longest form first so "very high risk" reports once rather than also
# matching the "high risk" substring inside itself.
_TERMINOLOGY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\bnational\s+mfi\b", "unsupported_national_terminology", "national MFI"),
    (r"\bnational\s+score\b", "unsupported_national_terminology", "national score"),
    (r"\bvery high risk\b", "unsupported_risk_terminology", "very high risk"),
    (
        r"(?<!very )\b(?:high|medium|low) risk\b",
        "unsupported_risk_terminology",
        "risk class",
    ),
    (r"\bcritical dimension\b", "unsupported_priority_terminology", "critical dimension"),
    (r"\bcritical market\b", "unsupported_priority_terminology", "critical market"),
)


def _terminology_flags(
    text: str,
    location: Mapping[str, Any],
    claim_id: str,
    metric_ids: Sequence[str],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for pattern, code, label in _TERMINOLOGY_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            flags.append(
                _flag(
                    code=code,
                    severity="high",
                    **_location_fields(location),
                    claim_id=claim_id,
                    message=f"Unsupported report terminology: {label}.",
                    metric_ids=metric_ids,
                )
            )

    for sentence in modality_conclusions(text):
        flags.append(
            _flag(
                code="unsupported_modality_conclusion",
                severity="high",
                **_location_fields(location),
                claim_id=claim_id,
                message=(
                    "MFI evidence cannot determine transfer modality. A conditional "
                    "form of the conclusion is still a conclusion."
                ),
                metric_ids=metric_ids,
                actual_value=sentence,
            )
        )

    # Ungated. The Price dimension is where affordability is most tempting, but the
    # inference is unsupported wherever it appears, and the observed report drew it in
    # market narratives that the dimension gate could never have seen.
    for sentence in affordability_claims(text):
        flags.append(
            _flag(
                code="unsupported_affordability_claim",
                severity="high",
                **_location_fields(location),
                claim_id=claim_id,
                message=(
                    "MFI evidence does not establish affordability, inflation, or "
                    "household purchasing power."
                ),
                metric_ids=metric_ids,
                actual_value=sentence,
            )
        )

    artifact_id = str(location.get("artifact_id") or "")
    if artifact_id == "Service" and re.search(
        r"\b(?:courtesy|consumer satisfaction)\b", text, re.IGNORECASE
    ):
        flags.append(
            _flag(
                code="unsupported_service_concept",
                severity="high",
                **_location_fields(location),
                claim_id=claim_id,
                message="Service evidence does not measure courtesy or consumer satisfaction.",
                metric_ids=metric_ids,
            )
        )
    if artifact_id == "Access & Protection" and re.search(
        r"\boperating hours\b", text, re.IGNORECASE
    ):
        flags.append(
            _flag(
                code="unsupported_access_concept",
                severity="high",
                **_location_fields(location),
                claim_id=claim_id,
                message="Access & Protection evidence does not measure operating hours.",
                metric_ids=metric_ids,
            )
        )
    return flags


def _causality_flags(
    text: str,
    location: Mapping[str, Any],
    claim_id: str,
) -> list[dict[str, Any]]:
    """Flag causal and predictive assertions in any artifact.

    Previously limited to context and executive claims, which left dimension and market
    prose — where the observed report actually asserted mechanisms — entirely unchecked.
    """
    return [
        _flag(
            code="unsupported_causal_claim",
            severity="high",
            **_location_fields(location),
            claim_id=claim_id,
            message=(
                "The assessment describes patterns and cannot establish causal or "
                "predictive effects."
            ),
            actual_value=sentence,
        )
        for sentence in causal_claims(text)
    ]


def _population_wording_flags(
    text: str,
    location: Mapping[str, Any],
    claim_id: str,
    claim: Mapping[str, Any],
    cited_entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Enforce the aggregation wording recorded by the previous phase.

    An unweighted mean of per-market rates has no respondent denominator — the processed
    assessment carries none — so describing it as a share of traders states something the
    data cannot support. A single market's trader proportion is the one case where that
    wording is correct, and it is distinguished by its aggregation method rather than by
    any property of the sentence.

    Limitation claims are exempt: the limitation explaining that item-specific trader
    denominators are unavailable is this rule's own justification.
    """
    if str(claim.get("claim_kind") or "") == "limitation":
        return []

    flags: list[dict[str, Any]] = []
    unweighted = [
        entry
        for entry in cited_entries
        if str(entry.get("aggregation_method")) == "unweighted_market_mean"
    ]

    pooled = [
        entry for entry in unweighted if not entry.get("pooled_denominator_available")
    ]
    phrases = pooled_population_phrases(text)
    if phrases and pooled:
        flags.append(
            _flag(
                code="unsupported_population_wording",
                severity="high",
                **_location_fields(location),
                claim_id=claim_id,
                message=(
                    "This value is an unweighted mean of market-level rates and has no "
                    "respondent denominator, so it cannot be stated as a share of "
                    "traders or responses."
                ),
                metric_ids=[str(entry.get("metric_id")) for entry in pooled],
                expected_value=str(pooled[0].get("permitted_subject_phrase") or ""),
                actual_value=phrases[0],
            )
        )

    trader_level = [
        entry
        for entry in unweighted
        if str(entry.get("population_basis")) == "trader_level_within_market"
        and str(entry.get("unit")) == "proportion"
    ]
    if (
        trader_level
        and any(token[2] for token in _numeric_tokens(text))
        and not has_approved_aggregation_wording(text)
    ):
        flags.append(
            _flag(
                code="unqualified_aggregation_wording",
                severity="medium",
                **_location_fields(location),
                claim_id=claim_id,
                message=(
                    "A percentage derived from an unweighted mean of market-level rates "
                    "must be qualified as unweighted or market-level."
                ),
                metric_ids=[str(entry.get("metric_id")) for entry in trader_level],
                expected_value=str(
                    trader_level[0].get("permitted_subject_phrase") or ""
                ),
            )
        )
    return flags


def _markup_flags(
    text: str,
    location: Mapping[str, Any],
    claim_id: str,
) -> list[dict[str, Any]]:
    """Flag Markdown delimiters that survived sanitization.

    The sanitizer removes delimiters at the parsing boundary, so anything still present
    means it aborted to protect a number, or the construct is one it does not handle.
    Either way the canonical text is not plain, and the report contract requires it to be.
    """
    found = residual_markup(text)
    if not found:
        return []
    return [
        _flag(
            code="unsupported_markup",
            severity="medium",
            **_location_fields(location),
            claim_id=claim_id,
            message=(
                "Claim text must be plain text. Unsupported markup remains: "
                f"{', '.join(found)}."
            ),
        )
    ]


def _priority_contract_flags(
    narratives: Mapping[str, Mapping[str, Any]],
    *,
    priority_dimensions: set[str],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for dimension in priority_dimensions:
        narrative = narratives.get(dimension)
        if not isinstance(narrative, Mapping):
            flags.append(
                _flag(
                    code="missing_priority_dimension_narrative",
                    severity="high",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    message="Priority dimension has no structured narrative.",
                )
            )
            continue
        if not narrative.get("subdimension_analysis"):
            flags.append(
                _flag(
                    code="missing_priority_drilldown",
                    severity="high",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    field_name="subdimension_analysis",
                    message="Priority dimension requires subsection or question-level analysis.",
                )
            )
        driver_ids = {
            str(metric_id)
            for item in narrative.get("subdimension_analysis", []) or []
            if isinstance(item, Mapping)
            for metric_id in item.get("driver_metric_ids", []) or []
        }
        if len(driver_ids) < 2:
            flags.append(
                _flag(
                    code="insufficient_priority_drivers",
                    severity="medium",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    field_name="subdimension_analysis",
                    message="Priority dimension should cite two to four explanatory drivers.",
                    metric_ids=sorted(driver_ids),
                )
            )
        elif len(driver_ids) > 4:
            flags.append(
                _flag(
                    code="excess_priority_drivers",
                    severity="medium",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    field_name="subdimension_analysis",
                    message="Priority dimension should cite no more than four explanatory drivers.",
                    metric_ids=sorted(driver_ids),
                )
            )
    return flags


def _narrative_structure_flags(
    dimensions: Mapping[str, Mapping[str, Any]],
    markets: Mapping[str, Mapping[str, Any]],
    *,
    assessment_profile: Mapping[str, Any],
    claim_catalog: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    dimension_profiles = {
        str(item.get("dimension")): item
        for item in assessment_profile.get("dimensions", []) or []
        if isinstance(item, Mapping) and item.get("dimension")
    }
    for dimension, profile in dimension_profiles.items():
        narrative = dimensions.get(dimension)
        if not isinstance(narrative, Mapping):
            flags.append(
                _flag(
                    code="missing_dimension_narrative",
                    severity="high",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    message="Every official MFI dimension requires one narrative section.",
                )
            )
            continue
        if bool(narrative.get("is_priority")) != bool(profile.get("is_priority")):
            flags.append(
                _flag(
                    code="incorrect_priority_status",
                    severity="high",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    field_name="is_priority",
                    message="Narrative priority status differs from the deterministic profile.",
                )
            )
        cited_ids = {
            str(metric_id)
            for _location, claim in _claims_in_artifact(
                narrative,
                artifact_type="dimension",
                artifact_id=dimension,
            )
            for metric_id in claim.get("metric_ids", []) or []
        }
        requirements = {
            "mean": any(metric_id.endswith(".mean") for metric_id in cited_ids),
            "rank": any(metric_id.endswith(".rank") for metric_id in cited_ids),
            "variation": any(
                metric_id.endswith((".minimum", ".maximum", ".iqr", ".range"))
                for metric_id in cited_ids
            ),
        }
        if not all(requirements.values()):
            flags.append(
                _flag(
                    code="incomplete_dimension_summary",
                    severity="high",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    field_name="summary",
                    message=(
                        "Dimension narrative must cite mean score, profile rank, "
                        "and variation evidence."
                    ),
                    metric_ids=sorted(cited_ids),
                )
            )
        for field_name in ("key_findings", "recommendations"):
            if not narrative.get(field_name):
                flags.append(
                    _flag(
                        code=f"missing_dimension_{field_name}",
                        severity="high",
                        artifact_type="dimension",
                        artifact_id=dimension,
                        field_name=field_name,
                        message=f"Dimension narrative requires {field_name.replace('_', ' ')}.",
                    )
                )
        if bool(profile.get("is_priority")) and not narrative.get(
            "geographic_patterns"
        ):
            flags.append(
                _flag(
                    code="missing_priority_geographic_pattern",
                    severity="medium",
                    artifact_type="dimension",
                    artifact_id=dimension,
                    field_name="geographic_patterns",
                    message="Priority dimension requires a localized pattern narrative.",
                )
            )
        subdimensions = [
            item
            for item in narrative.get("subdimension_analysis", []) or []
            if isinstance(item, Mapping)
        ]
        for subdimension in subdimensions:
            metric_id = _optional_text(subdimension.get("subsection_metric_id"))
            entry = claim_catalog.get(metric_id or "")
            if metric_id and not isinstance(entry, Mapping):
                flags.append(
                    _flag(
                        code="invalid_metric_id",
                        severity="high",
                        artifact_type="dimension",
                        artifact_id=dimension,
                        field_name="subdimension_analysis",
                        message=f"Subdimension cites unknown metric ID: {metric_id}.",
                        metric_ids=[metric_id],
                    )
                )
            score = subdimension.get("score_0_10")
            if score is not None and isinstance(entry, Mapping):
                allowed = {
                    value
                    for rendering in entry.get("allowed_renderings", []) or []
                    for _token, value, is_percent in _numeric_tokens(str(rendering))
                    if not is_percent
                }
                if allowed and not any(
                    abs(float(score) - value) <= 5e-7 for value in allowed
                ):
                    flags.append(
                        _flag(
                            code="subdimension_score_mismatch",
                            severity="high",
                            artifact_type="dimension",
                            artifact_id=dimension,
                            field_name="subdimension_analysis",
                            message="Subdimension score is not authorized by its cited metric.",
                            metric_ids=[metric_id] if metric_id else [],
                            actual_value=str(score),
                        )
                    )
            for driver_id in subdimension.get("driver_metric_ids", []) or []:
                if str(driver_id) not in claim_catalog:
                    flags.append(
                        _flag(
                            code="invalid_metric_id",
                            severity="high",
                            artifact_type="dimension",
                            artifact_id=dimension,
                            field_name="subdimension_analysis",
                            message=f"Driver cites unknown metric ID: {driver_id}.",
                            metric_ids=[str(driver_id)],
                        )
                    )
        if dimension == "Food Quality" and bool(profile.get("is_priority")):
            invalid_quality = [
                str(item.get("subsection_metric_id"))
                for item in subdimensions
                if item.get("subsection_metric_id")
                and not any(
                    "quality.condition." in str(source_id)
                    for source_id in (
                        claim_catalog.get(
                            str(item.get("subsection_metric_id")), {}
                        ).get("source_metric_ids", [])
                    )
                )
            ]
            if invalid_quality:
                flags.append(
                    _flag(
                        code="quality_requires_question_evidence",
                        severity="high",
                        artifact_type="dimension",
                        artifact_id=dimension,
                        field_name="subdimension_analysis",
                        message="Food Quality must drill down through applicable question drivers.",
                        metric_ids=invalid_quality,
                    )
                )

        relevant_source_ids = {
            str(metric.get("metric_id"))
            for metric in profile.get("drivers", []) or []
            if isinstance(metric, Mapping) and metric.get("item_relevant")
        }
        if relevant_source_ids and bool(profile.get("is_priority")):
            cited_source_ids = {
                str(source_id)
                for _location, claim in _claims_in_artifact(
                    narrative,
                    artifact_type="dimension",
                    artifact_id=dimension,
                )
                for metric_id in claim.get("metric_ids", []) or []
                for source_id in (
                    claim_catalog.get(str(metric_id), {}).get(
                        "source_metric_ids", []
                    )
                )
            }
            if not (relevant_source_ids & cited_source_ids):
                flags.append(
                    _flag(
                        code="missing_relevant_item_evidence",
                        severity="medium",
                        artifact_type="dimension",
                        artifact_id=dimension,
                        field_name="key_findings",
                        message="Priority narrative omits all Phase 2-relevant item evidence.",
                    )
                )

    priority_market_names = set(
        assessment_profile.get("priority_market_names", []) or []
    )
    for market_name in priority_market_names:
        market_narrative = markets.get(str(market_name))
        if not isinstance(market_narrative, Mapping):
            flags.append(
                _flag(
                    code="missing_priority_market_narrative",
                    severity="high",
                    artifact_type="market",
                    artifact_id=str(market_name),
                    message="Every selected market requires a structured narrative.",
                )
            )
        else:
            for field_name in ("priority_issues", "recommended_interventions"):
                if not market_narrative.get(field_name):
                    flags.append(
                        _flag(
                            code=f"missing_market_{field_name}",
                            severity="high",
                            artifact_type="market",
                            artifact_id=str(market_name),
                            field_name=field_name,
                            message=(
                                "Selected market narrative requires "
                                f"{field_name.replace('_', ' ')}."
                            ),
                        )
                    )
    for market_name in markets:
        if market_name not in priority_market_names:
            flags.append(
                _flag(
                    code="nonpriority_market_narrative",
                    severity="medium",
                    artifact_type="market",
                    artifact_id=str(market_name),
                    message="Market narrative is not part of the deterministic selected set.",
                )
            )
    return flags


def _recommendation_linkage_flags(
    dimensions: Mapping[str, Mapping[str, Any]],
    markets: Mapping[str, Mapping[str, Any]],
    executive: Mapping[str, Any],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    artifacts: list[tuple[str, str, Mapping[str, Any]]] = [
        *[
            ("dimension", str(key), value)
            for key, value in dimensions.items()
            if isinstance(value, Mapping)
        ],
        *[
            ("market", str(key), value)
            for key, value in markets.items()
            if isinstance(value, Mapping)
        ],
        ("executive_summary", "executive_summary", executive),
    ]
    for artifact_type, artifact_id, artifact in artifacts:
        fields = (
            ("recommendations", artifact.get("recommendations", [])),
            (
                "recommended_interventions",
                artifact.get("recommended_interventions", []),
            ),
        )
        for field_name, recommendations in fields:
            for recommendation in recommendations or []:
                if (
                    isinstance(recommendation, Mapping)
                    and not recommendation.get("metric_ids")
                ):
                    flags.append(
                        _flag(
                            code="recommendation_without_evidence",
                            severity="medium",
                            artifact_type=artifact_type,
                            artifact_id=artifact_id,
                            field_name=field_name,
                            claim_id=_optional_text(
                                recommendation.get("claim_id")
                            ),
                            message="Recommendation must link to supplied MFI evidence.",
                        )
                    )
    return flags


def _context_contract_flags(
    statements: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for statement in statements:
        statement_id = str(statement.get("statement_id") or "")
        if statement.get("withdrawn") or statement.get("classification") == "unrelated":
            continue
        if statement.get("passage_binding_required"):
            passages = []
            for passage in statement.get("source_passages", []):
                document = documents.get(passage.get("document_id"))
                excerpt = str(passage.get("text") or "")
                if document and excerpt and excerpt in str(document.get("content") or ""):
                    passages.append({"content": excerpt})
            if not passages or any(not _numeric_token_is_authorized(value, percent, [], passages) for _, value, percent in _numeric_tokens(str(statement.get("text") or ""))):
                flags.append(_flag(code="invalid_source_passage", severity="high", artifact_type="context",
                    artifact_id=statement_id, field_name="text", claim_id=statement_id,
                    message="Context claim is not supported by its exact cited source passage.", document_ids=statement.get("document_ids", [])))
        document_ids = [
            str(item) for item in statement.get("document_ids", []) if item
        ]
        if not document_ids or any(item not in documents for item in document_ids):
            flags.append(
                _flag(
                    code="invalid_document_id",
                    severity="medium",
                    artifact_type="context",
                    artifact_id=statement_id,
                    field_name="document_ids",
                    message="Context statement must cite known source documents.",
                    document_ids=document_ids,
                )
            )
        lowered = str(statement.get("text") or "").casefold()
        if any(
            term in lowered
            for term in (
                "caused",
                "led to",
                "resulted in",
                "because of",
                "drove the",
            )
        ):
            flags.append(
                _flag(
                    code="unsupported_causal_claim",
                    severity="high",
                    artifact_type="context",
                    artifact_id=statement_id,
                    field_name="text",
                    message="Context classification cannot establish causality.",
                    document_ids=document_ids,
                )
            )
    return flags


def _relevant_source_metric_ids(
    assessment_profile: Mapping[str, Any],
) -> set[str]:
    return {
        str(metric.get("metric_id"))
        for dimension in assessment_profile.get("dimensions", []) or []
        if isinstance(dimension, Mapping)
        for metric in dimension.get("drivers", []) or []
        if isinstance(metric, Mapping)
        and metric.get("role") == "item_driver"
        and metric.get("item_relevant")
        and metric.get("metric_id")
    }


def _flag(
    *,
    code: str,
    severity: str,
    artifact_type: str,
    message: str,
    source: str = "deterministic",
    artifact_id: Optional[str] = None,
    field_name: Optional[str] = None,
    claim_id: Optional[str] = None,
    recommendation: str = "",
    metric_ids: Optional[Sequence[str]] = None,
    document_ids: Optional[Sequence[str]] = None,
    expected_value: Optional[str] = None,
    actual_value: Optional[str] = None,
    repairable: bool = True,
) -> dict[str, Any]:
    identity = "|".join(
        str(item or "")
        for item in (
            source,
            code,
            severity,
            artifact_type,
            artifact_id,
            field_name,
            claim_id,
            message,
            recommendation,
            ",".join(str(item) for item in metric_ids or []),
            ",".join(str(item) for item in document_ids or []),
            expected_value,
            actual_value,
            repairable,
        )
    )
    return MFINarrativeQAFlag(
        flag_id=f"{source}-{code}-{_short_hash(identity)}",
        source=source,
        code=code,
        severity=severity,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        field_name=field_name,
        claim_id=claim_id,
        message=message,
        recommendation=recommendation,
        metric_ids=list(metric_ids or []),
        document_ids=list(document_ids or []),
        expected_value=expected_value,
        actual_value=actual_value,
        repairable=repairable,
    ).model_dump()


def _deduplicate_flags(
    flags: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for flag in flags:
        flag_id = str(flag.get("flag_id") or _short_hash(str(flag)))
        unique[flag_id] = dict(flag)
    return [unique[key] for key in sorted(unique)]


def _find_id(values: Iterable[Any], *, suffix: str) -> Optional[str]:
    return next(
        (str(item) for item in values if str(item).endswith(suffix)),
        None,
    )


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_") or "unnamed"


def _short_hash(value: str) -> str:
    return sha256(str(value).encode("utf-8")).hexdigest()[:8]
