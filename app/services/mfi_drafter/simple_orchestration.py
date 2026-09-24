"""Pure contracts for the simplified live MFI narrative orchestration.

The application owns all artifact and claim identities.  Models draft prose and
return semantic findings, but they never provide authoritative routing metadata.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List, Mapping, Sequence

from .claim_identity import normalized_slug
from .light_evidence import MARKET_PROMPT_PROJECTION_VERSION, build_market_prompt_projection
from .methodology import (
    DISPLAY_DIMENSIONS,
    NARRATIVE_PROHIBITIONS,
    NARRATIVE_PROMPT_CONSTRAINTS,
)
from .narrative import (
    NARRATIVE_DENSITY_POLICY,
    compact_catalog,
    executive_catalog_ids,
    market_catalog_ids,
    parse_dimension_narrative,
    parse_market_narrative,
)
from .qa_pipeline import (
    apply_field_patch,
    build_sequential_correction_tasks,
    correction_field_patch_contract,
    project_correction_transport,
    validate_field_patch_payload,
)
from .schemas import MFINarrativeQAFlag


NARRATIVE_ORCHESTRATION_VERSION = "mfi-narrative-simple-v1"
SEMANTIC_REVIEW_CONTRACT_VERSION = "mfi-semantic-review-v2"
SEMANTIC_REVIEW_MAX_CHARACTERS = 400_000
MAX_MARKETS_PER_DRAFT_BATCH = 5
MARKET_DRAFT_MAX_PROMPT_CHARACTERS = 160_000
CONSOLIDATED_CORRECTION_MAX_PROMPT_CHARACTERS = 600_000

DIMENSION_DRAFT_OPERATION = "mfi.dimension_batch_drafting.v1"
MARKET_DRAFT_OPERATION = "mfi.market_batch_drafting.v2"
EXECUTIVE_DRAFT_OPERATION = "mfi.executive_drafting.v3"
CONSOLIDATED_CORRECTION_OPERATION = "mfi.consolidated_correction.v2"
CORRECTED_CLAIM_VERIFICATION_OPERATION = (
    "mfi.corrected_claim_verification.v2"
)
SEMANTIC_REVIEW_OPERATIONS = {
    "overview": "mfi.semantic_review.overview.v2",
    "dimensions": "mfi.semantic_review.dimensions.v2",
    "markets": "mfi.semantic_review.markets.v2",
}

# Semantic review is intentionally narrow.  These are the two application domains the
# reviewer is allowed to report; prose style and policy preferences are outside its job.
SEMANTIC_REVIEW_ISSUE_TYPES = {
    "data_mismatch",
    "context_interpretation_problem",
}

_DIMENSION_FIELDS = (
    "summary",
    "key_findings",
    "subdimension_analysis",
    "geographic_patterns",
    "data_limitations",
    "recommendations",
)
_MARKET_FIELDS = (
    "priority_issues",
    "recommended_interventions",
    "limitations",
)
_EXECUTIVE_FIELDS = (
    "motivation",
    "key_findings",
    "recommendations",
    "limitations",
    "scope_statement",
)
_ARTIFACT_ORDER = {
    "context": 0,
    "dimension": 1,
    "market": 2,
    "executive_summary": 3,
}
_FIELD_ORDER = {
    "summary": 0,
    "motivation": 0,
    "key_findings": 1,
    "priority_issues": 1,
    "subdimension_analysis": 2,
    "geographic_patterns": 3,
    "data_limitations": 4,
    "limitations": 4,
    "recommendations": 5,
    "recommended_interventions": 5,
    "scope_statement": 6,
    "text": 0,
}


def _serialized(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _normal_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def ordered_dimension_profiles(
    assessment_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    profiles = {
        str(item.get("dimension")): dict(item)
        for item in assessment_profile.get("dimensions", []) or []
        if isinstance(item, Mapping) and item.get("dimension")
    }
    order = [name for name in DISPLAY_DIMENSIONS if name in profiles]
    order.extend(
        sorted(
            (name for name in profiles if name not in order),
            key=lambda value: (_normal_name(value), value),
        )
    )
    return [profiles[name] for name in order]


def build_dimension_draft_batches(
    assessment_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Return one batch per priority dimension and one non-priority batch."""
    profiles = ordered_dimension_profiles(assessment_profile)
    if assessment_profile.get("workflow_revision") == "mfi-reliable-v1":
        return [{"batch_id": f"dimension-{normalized_slug(p['dimension'])}", "batch_kind": "complete_dimension", "artifact_ids": [p["dimension"]], "profiles": [p]} for p in profiles]
    priorities = [item for item in profiles if bool(item.get("is_priority"))]
    non_priorities = [item for item in profiles if not bool(item.get("is_priority"))]
    batches: List[Dict[str, Any]] = []
    for profile in priorities:
        dimension = str(profile["dimension"])
        batches.append(
            {
                "batch_id": f"dimension-priority-{normalized_slug(dimension)}",
                "batch_kind": "priority_dimension",
                "artifact_ids": [dimension],
                "profiles": [profile],
            }
        )
    if non_priorities:
        batches.append(
            {
                "batch_id": "dimension-non-priority",
                "batch_kind": "non_priority_dimensions",
                "artifact_ids": [str(item["dimension"]) for item in non_priorities],
                "profiles": non_priorities,
            }
        )
    return batches


def ordered_priority_market_profiles(
    assessment_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    profiles = {
        str(item.get("market_name")): dict(item)
        for item in assessment_profile.get("markets", []) or []
        if isinstance(item, Mapping) and item.get("market_name")
    }
    ordered: List[Dict[str, Any]] = []
    for name in assessment_profile.get("priority_market_names", []) or []:
        if str(name) in profiles:
            ordered.append(profiles[str(name)])
    return ordered


def build_market_draft_batches(
    assessment_profile: Mapping[str, Any],
    *,
    maximum_batch_size: int = MAX_MARKETS_PER_DRAFT_BATCH,
) -> List[Dict[str, Any]]:
    if maximum_batch_size < 1:
        raise ValueError("maximum_batch_size must be positive")
    profiles = ordered_priority_market_profiles(assessment_profile)
    batches: List[Dict[str, Any]] = []
    for offset in range(0, len(profiles), maximum_batch_size):
        group = profiles[offset : offset + maximum_batch_size]
        sequence = len(batches) + 1
        batches.append(
            {
                "batch_id": f"market-draft-{sequence:02d}",
                "batch_kind": "selected_markets",
                "artifact_ids": [str(item["market_name"]) for item in group],
                "profiles": group,
            }
        )
    return batches


class MarketDraftPromptContractError(ValueError):
    """Raised before Vertex when one market cannot fit the prompt contract."""

    def __init__(
        self,
        *,
        market_name: str,
        character_count: int,
        target_characters: int,
    ) -> None:
        self.market_name = market_name
        self.character_count = int(character_count)
        self.target_characters = int(target_characters)
        super().__init__(
            f"Market {market_name!r} requires {character_count} prompt characters; "
            f"the limit is {target_characters}."
        )


def compose_market_draft_prompt(batch: Mapping[str, Any]) -> str:
    """Return the exact prompt measured by the budget planner and sent to Vertex."""
    return f"""Draft targeted MFI narratives for the requested markets.

Use English only and valid JSON only. The prompt contains only the markets'
weak dimensions and selected matching market-scoped evidence. Every number must
exactly match a `formatted_value` in CLAIM_CATALOG and cite the associated
`metric_id`. Do not calculate or infer values. Every claim must declare
metric_ids, document_ids, scope, and polarity. Recommendations must cite
evidence used by a priority issue. A limitation is optional and may be included
only when it is specific to this market and cites market-scoped evidence. Do not
repeat a generic assessment limitation. Coverage and representation are added
deterministically in evidence notes; do not introduce coverage numbers in prose.

PROHIBITIONS:
{json.dumps(list(NARRATIVE_PROHIBITIONS))}

REQUESTED_MARKETS_IN_REQUIRED_ORDER:
{json.dumps(batch['artifact_ids'])}

MARKET_PROFILES:
{json.dumps(batch['profiles'])}

CLAIM_CATALOG:
{json.dumps(batch['claim_catalog'])}

CONSTRAINTS:
{json.dumps(list(NARRATIVE_PROMPT_CONSTRAINTS))}

R8 CLAIM CEILINGS (maximums, not quotas; order by analytical importance):
- {NARRATIVE_DENSITY_POLICY.market_priority_issues} priority issues;
- {NARRATIVE_DENSITY_POLICY.market_recommendations} linked recommendations;
- {NARRATIVE_DENSITY_POLICY.market_limitations} market-specific limitation.

Return:
{{
  "markets": [
    {{
      "market_name": "exact requested market name",
      "narrative": {{
        "priority_issues": [CLAIM],
        "recommended_interventions": [CLAIM],
        "limitations": [CLAIM]
      }}
    }}
  ]
}}
where CLAIM is:
{{
  "text": "...",
  "claim_kind": "finding|recommendation|limitation",
  "metric_ids": ["ledger ids"],
  "document_ids": [],
  "scope": "market",
  "polarity": "favorable|unfavorable|neutral|descriptive"
}}
"""


def _market_prompt_batch(
    projections: Sequence[Mapping[str, Any]],
    *,
    catalog: Mapping[str, Mapping[str, Any]],
    sequence: int,
) -> Dict[str, Any]:
    names = [str(item["market_name"]) for item in projections]
    selected_ids = list(
        dict.fromkeys(
            str(metric_id)
            for projection in projections
            for metric_id in projection.get("selected_ledger_metric_ids", []) or []
            if metric_id
        )
    )
    prompt_catalog = compact_catalog(catalog, selected_ids)
    counts = {
        key: sum(
            int((projection.get("selection_counts") or {}).get(key, 0) or 0)
            for projection in projections
        )
        for key in ("subsections", "drivers", "relevant_items")
    }
    batch: Dict[str, Any] = {
        "batch_id": f"market-draft-{_short_hash('|'.join(str(p.get('market_key') or p['market_name']) for p in projections))}",
        "batch_kind": "selected_markets",
        "sequence": sequence,
        "artifact_ids": names,
        "profiles": [deepcopy(dict(item)) for item in projections],
        "claim_catalog": prompt_catalog,
        "catalog_entry_count": len(prompt_catalog),
        "selection_counts": counts,
        "projection_version": MARKET_PROMPT_PROJECTION_VERSION,
    }
    prompt = compose_market_draft_prompt(batch)
    batch["prompt"] = prompt
    batch["prompt_character_count"] = len(prompt)
    return batch


def build_budgeted_market_draft_batches(
    assessment_profile: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
    *,
    maximum_batch_size: int = MAX_MARKETS_PER_DRAFT_BATCH,
    maximum_prompt_characters: int = MARKET_DRAFT_MAX_PROMPT_CHARACTERS,
) -> List[Dict[str, Any]]:
    """Pack ordered markets under both the artifact and exact prompt budgets."""
    if maximum_batch_size < 1:
        raise ValueError("maximum_batch_size must be positive")
    if maximum_prompt_characters < 1:
        raise ValueError("maximum_prompt_characters must be positive")
    if assessment_profile.get("workflow_revision"):
        maximum_prompt_characters = min(maximum_prompt_characters, 140_000)
    def within_budget(batch):
        if int(batch["prompt_character_count"]) > maximum_prompt_characters:
            return False
        if assessment_profile.get("workflow_revision"):
            from langchain_core.messages import HumanMessage
            from .response_contracts import request_package
            from .packages import serialized
            batch["outgoing_character_count"] = len(serialized(request_package("market", [HumanMessage(content=batch["prompt"])])))
            return batch["outgoing_character_count"] <= 160_000
        return True
    projections = [
        build_market_prompt_projection(assessment_profile, profile)
        for profile in ordered_priority_market_profiles(assessment_profile)
    ]
    batches: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for projection in projections:
        candidate = _market_prompt_batch(
            [*pending, projection],
            catalog=catalog,
            sequence=len(batches) + 1,
        )
        if (
            len(pending) < maximum_batch_size
            and within_budget(candidate)
        ):
            pending.append(projection)
            continue
        if pending:
            batches.append(
                _market_prompt_batch(
                    pending,
                    catalog=catalog,
                    sequence=len(batches) + 1,
                )
            )
            pending = []
        single = _market_prompt_batch(
            [projection],
            catalog=catalog,
            sequence=len(batches) + 1,
        )
        if not within_budget(single):
            raise MarketDraftPromptContractError(
                market_name=str(projection["market_name"]),
                character_count=int(single["prompt_character_count"]),
                target_characters=maximum_prompt_characters,
            )
        pending = [projection]
    if pending:
        batches.append(
            _market_prompt_batch(
                pending,
                catalog=catalog,
                sequence=len(batches) + 1,
            )
        )
    return batches


def validate_dimension_draft_batch(
    payload: Any,
    *,
    batch: Mapping[str, Any],
    assessment_profile: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    expected = [str(item) for item in batch.get("artifact_ids", [])]
    if (
        isinstance(payload, Mapping)
        and not isinstance(payload.get("dimensions"), list)
        and len(expected) == 1
        and "summary" in payload
    ):
        payload = {
            "dimensions": [
                {"dimension": expected[0], "narrative": dict(payload)}
            ]
        }
    if not isinstance(payload, Mapping) or not isinstance(payload.get("dimensions"), list):
        raise ValueError("Dimension batch must contain a dimensions array")
    rows = payload["dimensions"]
    actual = [
        str(item.get("dimension") or "") if isinstance(item, Mapping) else ""
        for item in rows
    ]
    if actual != expected:
        raise ValueError("Dimension batch identities or ordering are incomplete")
    profiles = {
        str(item["dimension"]): item for item in batch.get("profiles", []) or []
    }
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(
            row.get("narrative"), Mapping
        ):
            raise ValueError("Each dimension batch row requires a narrative object")
        dimension = str(row["dimension"])
        result[dimension] = parse_dimension_narrative(
            row["narrative"],
            dimension_profile=profiles[dimension],
            assessment_profile=assessment_profile,
            strict=True,
        )
    return result


def validate_market_draft_batch(
    payload: Any,
    *,
    batch: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    expected = [str(item) for item in batch.get("artifact_ids", [])]
    if (
        isinstance(payload, Mapping)
        and not isinstance(payload.get("markets"), list)
        and len(expected) == 1
        and "priority_issues" in payload
    ):
        payload = {
            "markets": [
                {"market_name": expected[0], "narrative": dict(payload)}
            ]
        }
    if not isinstance(payload, Mapping) or not isinstance(payload.get("markets"), list):
        raise ValueError("Market batch must contain a markets array")
    rows = payload["markets"]
    actual = [
        str(item.get("market_name") or "") if isinstance(item, Mapping) else ""
        for item in rows
    ]
    if actual != expected:
        raise ValueError("Market batch identities or ordering are incomplete")
    profiles = {
        str(item["market_name"]): item for item in batch.get("profiles", []) or []
    }
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(
            row.get("narrative"), Mapping
        ):
            raise ValueError("Each market batch row requires a narrative object")
        market = str(row["market_name"])
        from .facts import render_payload
        result[market] = parse_market_narrative(
            render_payload(row["narrative"], batch.get("claim_catalog", {})),
            market_profile=profiles[market],
            strict=True,
        )
        if profiles[market].get("market_key"):
            result[market]["market_key"] = profiles[market]["market_key"]
    return result


def dimension_batch_catalog(
    catalog: Mapping[str, Mapping[str, Any]],
    batch: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    profiles = dimension_batch_prompt_profiles(batch)
    return compact_catalog(catalog, _catalog_ids_in_value(profiles, catalog))


def _catalog_ids_in_value(
    value: Any,
    catalog: Mapping[str, Mapping[str, Any]],
) -> List[str]:
    ids: List[str] = []

    def collect(nested: Any) -> None:
        if isinstance(nested, Mapping):
            for item in nested.values():
                collect(item)
        elif isinstance(nested, list):
            for item in nested:
                collect(item)
        elif isinstance(nested, str) and nested in catalog:
            ids.append(nested)

    collect(value)
    return list(dict.fromkeys(ids))


def _claim_reference_ids(value: Any, key: str) -> List[str]:
    ids: List[str] = []

    def collect(nested: Any) -> None:
        if isinstance(nested, Mapping):
            references = nested.get(key)
            if isinstance(references, list):
                ids.extend(str(item) for item in references if item)
            for item in nested.values():
                collect(item)
        elif isinstance(nested, list):
            for item in nested:
                collect(item)

    collect(value)
    return list(dict.fromkeys(ids))


def _compact_ranked_metric(metric: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: deepcopy(metric.get(key))
        for key in (
            "metric_id",
            "display_name",
            "role",
            "mean_raw_value",
            "mean_normalized_value",
            "unit",
            "orientation",
            "evidence_scope",
            "coverage",
            "availability",
            "unfavorable_rate",
            "weakness_rank",
            "group_rank",
            "product_group",
            "question_group",
            "item_name",
            "item_relevant",
            "relevance_reasons",
            "matching_category_metric_id",
            "ledger_metric_ids",
        )
        if metric.get(key) is not None
    }


def dimension_prompt_profile(profile: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep only evidence that the bounded R8 prose can actually consume."""
    priority = bool(profile.get("is_priority"))
    if profile.get("workflow_revision") == "mfi-reliable-v1":
        result = deepcopy(dict(profile))
        result["subsections"] = [_compact_ranked_metric(m) for m in profile.get("subsections", [])]
        result["drivers"] = [_compact_ranked_metric(m) for m in profile.get("drivers", []) if m.get("role") != "item_driver" or m.get("item_relevant")]
        # Match the complete regional mean/denominator table used in drafting.
        # Other referenced regional statistics are authorized explicitly from
        # the affected claims/findings by the correction package builder.
        result["regional_summaries"] = [
            {"region": region["region"], "statistics": {
                "mean": region["statistics"]["mean"],
                "denominator": region["statistics"]["denominator"]},
             "ledger_metric_ids": [key for key in region.get("ledger_metric_ids", []) if key.endswith(".mean")]}
            for region in profile.get("regional_summaries", [])
        ]
        localized = result.get("localized_patterns") or {}
        localized["markets_where_dimension_is_weakest_within_market"] = localized.pop("markets_where_lowest", [])
        return result
    result: Dict[str, Any] = {
        key: deepcopy(profile.get(key))
        for key in (
            "dimension",
            "statistics",
            "profile_rank",
            "selection_order",
            "is_priority",
            "priority_reasons",
            "localized_patterns",
            "ledger_metric_ids",
        )
        if profile.get(key) is not None
    }
    localized = dict(result.get("localized_patterns") or {})
    if not priority:
        localized.pop("ordered_markets", None)
    result["localized_patterns"] = localized
    if not priority:
        result["subsections"] = []
        result["drivers"] = []
        result["regional_summaries"] = []
        return result
    subsections = [
        _compact_ranked_metric(item)
        for item in list(profile.get("subsections", []) or [])[:2]
        if isinstance(item, Mapping)
    ]
    drivers = [
        item
        for item in profile.get("drivers", []) or []
        if isinstance(item, Mapping)
    ]
    fixed = [item for item in drivers if str(item.get("role")) != "item_driver"][:4]
    relevant_items = [
        item
        for item in drivers
        if str(item.get("role")) == "item_driver" and bool(item.get("item_relevant"))
    ][:12]
    result["subsections"] = subsections
    result["drivers"] = [
        _compact_ranked_metric(item) for item in [*fixed, *relevant_items]
    ]
    result["regional_summaries"] = deepcopy(
        list(profile.get("regional_summaries", []) or [])
    )
    return result


def dimension_batch_prompt_profiles(
    batch: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    return [
        dimension_prompt_profile(profile)
        for profile in batch.get("profiles", []) or []
        if isinstance(profile, Mapping)
    ]


def market_batch_catalog(
    catalog: Mapping[str, Mapping[str, Any]],
    batch: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    ids: List[str] = []
    for profile in batch.get("profiles", []) or []:
        ids.extend(market_catalog_ids(profile, catalog))
    return compact_catalog(catalog, list(dict.fromkeys(ids)))


def _add_claim(
    rows: List[Dict[str, Any]],
    claim: Mapping[str, Any],
    *,
    artifact_type: str,
    artifact_id: str,
    field_name: str,
    position: int,
) -> None:
    if not claim.get("claim_id"):
        return
    rows.append(
        {
            "claim_id": str(claim["claim_id"]),
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "field_name": field_name,
            "position": position,
            "text": str(claim.get("text") or ""),
            "segments": deepcopy(claim.get("segments", [])),
            "fact_ids": list(claim.get("fact_ids", [])),
            "source_passages": deepcopy(claim.get("source_passages", [])),
            "scope": claim.get("scope"),
            "polarity": claim.get("polarity"),
            "metric_ids": [str(item) for item in claim.get("metric_ids", []) or []],
            "document_ids": [
                str(item) for item in claim.get("document_ids", []) or []
            ],
        }
    )


def _artifact_claim_rows(
    narrative: Mapping[str, Any],
    *,
    artifact_type: str,
    artifact_id: str,
    fields: Sequence[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for field_name in fields:
        value = narrative.get(field_name)
        if isinstance(value, Mapping):
            _add_claim(
                rows,
                value,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                field_name=field_name,
                position=1,
            )
        elif isinstance(value, list):
            for position, claim in enumerate(value, start=1):
                if isinstance(claim, Mapping):
                    if field_name == "subdimension_analysis":
                        interpretation = claim.get("interpretation")
                        if isinstance(interpretation, Mapping):
                            _add_claim(
                                rows,
                                interpretation,
                                artifact_type=artifact_type,
                                artifact_id=artifact_id,
                                field_name=field_name,
                                position=position,
                            )
                    else:
                        _add_claim(
                            rows,
                            claim,
                            artifact_type=artifact_type,
                            artifact_id=artifact_id,
                            field_name=field_name,
                            position=position,
                        )
    return rows


def canonical_claim_rows(
    *,
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    context_evidence: Sequence[Mapping[str, Any]],
    assessment_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for profile in ordered_dimension_profiles(assessment_profile):
        dimension = str(profile["dimension"])
        narrative = dimension_narratives.get(dimension)
        if isinstance(narrative, Mapping):
            rows.extend(
                _artifact_claim_rows(
                    narrative,
                    artifact_type="dimension",
                    artifact_id=dimension,
                    fields=_DIMENSION_FIELDS,
                )
            )
    for profile in ordered_priority_market_profiles(assessment_profile):
        market = str(profile["market_name"])
        narrative = market_narratives.get(market)
        if isinstance(narrative, Mapping):
            rows.extend(
                _artifact_claim_rows(
                    narrative,
                    artifact_type="market",
                    artifact_id=market,
                    fields=_MARKET_FIELDS,
                )
            )
    if isinstance(executive_narrative, Mapping):
        rows.extend(
            _artifact_claim_rows(
                executive_narrative,
                artifact_type="executive_summary",
                artifact_id="executive_summary",
                fields=_EXECUTIVE_FIELDS,
            )
        )
    for position, statement in enumerate(context_evidence, start=1):
        if not isinstance(statement, Mapping) or not statement.get("statement_id"):
            continue
        rows.append(
            {
                "claim_id": str(statement["statement_id"]),
                "artifact_type": "context",
                "artifact_id": str(statement["statement_id"]),
                "field_name": "text",
                "position": position,
                "text": str(statement.get("text") or ""),
                "scope": "context",
                "polarity": "descriptive",
                "metric_ids": [],
                "document_ids": [
                    str(item) for item in statement.get("document_ids", []) or []
                ],
            }
        )
    claim_ids = [row["claim_id"] for row in rows]
    if not all(claim_ids) or len(claim_ids) != len(set(claim_ids)):
        raise ValueError("Review input requires globally unique canonical claim IDs")
    return rows


def _review_evidence(
    catalog: Mapping[str, Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    cited = sorted(
        {
            str(metric_id)
            for claim in claims
            for metric_id in claim.get("metric_ids", []) or []
            if metric_id
        }
    )
    return {
        metric_id: {
            key: catalog[metric_id].get(key)
            for key in (
                "label",
                "numeric_value",
                "formatted_value",
                "allowed_renderings",
                "statistic",
                "unit",
                "orientation",
                "scope",
                "coverage_label",
                "dimension",
                "market_name",
                "region",
                "aggregation_method",
                "population_basis",
                "pooled_denominator_available",
                "representation_basis",
                "representation_kind",
                "representation_complete",
                "representation_required",
                "source_metric_ids",
            )
            if catalog[metric_id].get(key) is not None
        }
        for metric_id in cited
        if metric_id in catalog
    }


def _review_documents(
    documents: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    *,
    additional_document_ids: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    cited = {
        str(document_id)
        for claim in claims
        for document_id in claim.get("document_ids", []) or []
        if document_id
    }
    cited.update(str(item) for item in additional_document_ids if item)
    projected = []
    for item in documents:
        if not isinstance(item, Mapping):
            continue
        document_id = str(item.get("doc_id") or "")
        if not document_id or document_id not in cited:
            continue
        projected.append(
            {
                "document_id": document_id,
                "source": item.get("source"),
                "title": item.get("title"),
                "date": item.get("date"),
                "content_excerpt": str(item.get("content") or "")[:800],
            }
        )
    return sorted(projected, key=lambda item: item["document_id"])


def _accepted_context_for_review(
    context_evidence: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Project only accepted, source-backed run context into semantic review."""
    available_document_ids = {
        str(item.get("doc_id"))
        for item in documents
        if isinstance(item, Mapping) and item.get("doc_id")
    }
    accepted: List[Dict[str, Any]] = []
    for statement in context_evidence:
        if not isinstance(statement, Mapping):
            continue
        classification = str(statement.get("classification") or "")
        if classification not in {"corroborating", "potentially_explanatory"}:
            continue
        document_ids = [
            str(item)
            for item in statement.get("document_ids", []) or []
            if str(item) in available_document_ids
        ]
        if not document_ids:
            continue
        accepted.append(
            {
                "statement_id": str(statement.get("statement_id") or ""),
                "text": str(statement.get("text") or ""),
                "classification": classification,
                "document_ids": document_ids,
            }
        )
    return accepted


def _report_review_context(
    assessment_profile: Mapping[str, Any],
    report_context: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    supplied = dict(report_context or {})
    return {
        key: value
        for key, value in {
            "country": supplied.get("country"),
            "data_collection_start": supplied.get("data_collection_start"),
            "data_collection_end": supplied.get("data_collection_end"),
            "methodology_version": assessment_profile.get("methodology_version"),
            "score_authority": assessment_profile.get("score_authority"),
            "assessed_market_count": assessment_profile.get("assessed_market_count"),
            "mean_mfi_across_assessed_markets": assessment_profile.get(
                "mean_mfi_across_assessed_markets"
            ),
            "priority_dimension_names": assessment_profile.get(
                "priority_dimension_names", []
            ),
            "priority_market_names": assessment_profile.get(
                "priority_market_names", []
            ),
            "limitations": assessment_profile.get("limitations", []),
        }.items()
        if value is not None
    }


def _dimension_review_facts(
    assessment_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    for profile in ordered_dimension_profiles(assessment_profile):
        localized = profile.get("localized_patterns") or {}
        facts.append(
            {
                key: deepcopy(value)
                for key, value in {
                    "dimension": profile.get("dimension"),
                    "statistics": profile.get("statistics"),
                    "coverage": profile.get("coverage"),
                    "regional_summaries": profile.get("regional_summaries", []),
                    "analytical_facts": profile.get("analytical_facts", {}),
                    "profile_rank": profile.get("profile_rank"),
                    "selection_order": profile.get("selection_order"),
                    "is_priority": profile.get("is_priority"),
                    "priority_reasons": profile.get("priority_reasons", []),
                    "localized_patterns": {
                        key: deepcopy(localized.get(key))
                        for key in (
                            "regions_where_bottom_one",
                            "regions_where_bottom_two",
                            "markets_where_lowest",
                            "ordered_markets",
                            "score_range",
                            "iqr",
                        )
                        if localized.get(key) is not None
                    },
                }.items()
                if value is not None
            }
        )
    return facts


def _market_review_facts(
    assessment_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    return [
        {
            key: deepcopy(value)
            for key, value in {
                "market_name": profile.get("market_name"),
                "region": profile.get("region"),
                "overall_mfi": profile.get("overall_mfi"),
                "score_rank": profile.get("score_rank"),
                "selection_order": profile.get("selection_order"),
                "weak_dimensions": [
                    {
                        key: deepcopy(item.get(key))
                        for key in (
                            "dimension",
                            "score",
                            "rank",
                            "selection_order",
                            "is_weak",
                        )
                        if item.get(key) is not None
                    }
                    for item in profile.get("weak_dimensions", []) or []
                    if isinstance(item, Mapping)
                ],
            }.items()
            if value is not None
        }
        for profile in ordered_priority_market_profiles(assessment_profile)
    ]


def build_semantic_review_packages(
    *,
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    context_evidence: Sequence[Mapping[str, Any]],
    assessment_profile: Mapping[str, Any],
    claim_catalog: Mapping[str, Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    deterministic_flags: Sequence[Mapping[str, Any]],
    report_context: Mapping[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    rows = canonical_claim_rows(
        dimension_narratives=dimension_narratives,
        market_narratives=market_narratives,
        executive_narrative=executive_narrative,
        context_evidence=context_evidence,
        assessment_profile=assessment_profile,
    )
    by_section = {
        "overview": [
            row
            for row in rows
            if row["artifact_type"] in {"context", "executive_summary"}
        ],
        "dimensions": [row for row in rows if row["artifact_type"] == "dimension"],
        "markets": [row for row in rows if row["artifact_type"] == "market"],
    }
    accepted_context = _accepted_context_for_review(context_evidence, documents)
    context_document_ids = [
        document_id
        for statement in accepted_context
        for document_id in statement["document_ids"]
    ]
    general_context = _report_review_context(assessment_profile, report_context)
    packages: List[Dict[str, Any]] = []
    for sequence, section in enumerate(("overview", "dimensions", "markets"), start=1):
        claims = by_section[section]
        if section == "overview":
            facts: Any = {
                "assessed_market_count": assessment_profile.get(
                    "assessed_market_count"
                ),
                "mean_mfi_across_assessed_markets": assessment_profile.get(
                    "mean_mfi_across_assessed_markets"
                ),
                "priority_dimension_names": assessment_profile.get(
                    "priority_dimension_names", []
                ),
                "priority_market_names": assessment_profile.get(
                    "priority_market_names", []
                ),
                "limitations": assessment_profile.get("limitations", []),
            }
        elif section == "dimensions":
            facts = _dimension_review_facts(assessment_profile)
        else:
            facts = _market_review_facts(assessment_profile)
        package = {
            "contract_version": SEMANTIC_REVIEW_CONTRACT_VERSION,
            "section": section,
            "report_context": general_context,
            "claims": claims,
            "deterministic_facts": facts,
            "evidence_by_metric_id": _review_evidence(claim_catalog, claims),
            "accepted_context": accepted_context,
            "cited_documents": _review_documents(
                documents,
                claims,
                additional_document_ids=context_document_ids,
            ),
        }
        character_count = len(_serialized(package))
        packages.append(
            {
                "review_id": f"semantic-review-{section}",
                "section": section,
                "sequence": sequence,
                "claim_ids": [row["claim_id"] for row in claims],
                "character_count": character_count,
                "package": package,
            }
        )
    return packages


def validate_semantic_review_response(
    payload: Any,
    *,
    review: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Validate the deliberately narrow evidence-consistency review contract."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("flags"), list):
        raise ValueError("Semantic review response requires a flags array")
    claim_rows = {
        str(row["claim_id"]): row
        for row in review.get("package", {}).get("claims", []) or []
        if isinstance(row, Mapping) and row.get("claim_id")
    }
    result: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(payload["flags"]):
        if not isinstance(raw, Mapping):
            raise ValueError(f"flags[{index}] must be an object")
        issue_type = str(raw.get("issue_type") or "").strip()
        severity = str(raw.get("severity") or "").strip().lower()
        message = str(raw.get("message") or "").strip()
        claim_id = str(raw.get("claim_id") or "").strip() or None
        # A model may still volunteer style or policy advice despite the narrow prompt.
        # Such rows are outside this review's authority and are ignored rather than
        # promoted into correction work or allowed to block delivery.
        if issue_type not in SEMANTIC_REVIEW_ISSUE_TYPES:
            continue
        if severity not in {"high", "medium"} or not message:
            raise ValueError(f"flags[{index}] is missing required semantic fields")
        if claim_id is not None and claim_id not in claim_rows:
            raise ValueError("Semantic review references a claim outside its section")
        row = claim_rows.get(claim_id or "")
        if review.get("package", {}).get("contract_version") == "mfi-reliable-review-v1":
            from .review import reconcile_numerical_finding
            if row is None:
                raise ValueError("Reliable semantic findings must identify the affected claim")
            if not reconcile_numerical_finding(raw, review, row):
                continue
        artifact_type = str(row["artifact_type"]) if row else "global"
        artifact_id = str(row["artifact_id"]) if row else None
        field_name = str(row["field_name"]) if row else None
        code = f"semantic_{issue_type}"
        normalized_message = " ".join(message.casefold().split())
        key = (code, claim_id or "global", normalized_message)
        if key in seen:
            continue
        seen.add(key)
        identity = "|".join(
            [
                str(review.get("section")),
                code,
                claim_id or "global",
                normalized_message,
            ]
        )
        result.append(
            MFINarrativeQAFlag(
                flag_id=f"semantic-{normalized_slug(code)}-{_short_hash(identity)}",
                source="red_team",
                code=code,
                severity=severity,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                field_name=field_name,
                claim_id=claim_id,
                message=message,
                recommendation=str(raw.get("recommendation") or "").strip(),
                metric_ids=list(row.get("metric_ids", [])) if row else [],
                document_ids=list(row.get("document_ids", [])) if row else [],
                repairable=row is not None,
                review_batch_id=str(review.get("review_id")),
                review_batch_ids=[str(review.get("review_id"))],
            ).model_dump()
        )
    return result


def material_local_flags(
    flags: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    return [
        dict(flag)
        for flag in flags
        if str(flag.get("severity")) in {"high", "medium"}
        and bool(flag.get("repairable", True))
        and str(flag.get("artifact_type") or "") != "global"
        and flag.get("artifact_id")
        and flag.get("field_name")
    ]


def unresolved_high_flags(
    flags: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    return [dict(flag) for flag in flags if str(flag.get("severity")) == "high"]


def build_consolidated_correction_targets(
    flags: Sequence[Mapping[str, Any]],
    *,
    assessment_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    return build_sequential_correction_tasks(
        material_local_flags(flags),
        attempt_number=1,
        assessment_profile=assessment_profile,
    )


def validate_consolidated_correction_response(
    payload: Any,
    *,
    targets: Sequence[Mapping[str, Any]],
    ignored_metadata_fields: List[str] | None = None,
) -> Dict[str, Any]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("patches"), list):
        raise ValueError("Consolidated correction requires a patches array")
    expected = {str(target["task_id"]): target for target in targets}
    patches = payload["patches"]
    actual_ids: List[str] = []
    result: Dict[str, Any] = {}
    for index, patch in enumerate(patches):
        if not isinstance(patch, Mapping):
            raise ValueError(f"patches[{index}] must be an object")
        target_id = str(patch.get("target_id") or "")
        if not target_id or target_id not in expected:
            raise ValueError("Correction response contains an unknown target_id")
        actual_ids.append(target_id)
        result[target_id] = validate_field_patch_payload(
            {"replacement": patch.get("replacement")},
            task=expected[target_id],
            ignored_metadata_fields=ignored_metadata_fields,
        )
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise ValueError("Every correction target must appear exactly once")
    return result


def consolidated_correction_prompt_payload(
    *,
    targets: Sequence[Mapping[str, Any]],
    flags: Sequence[Mapping[str, Any]],
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    context_evidence: Sequence[Mapping[str, Any]],
    assessment_profile: Mapping[str, Any],
    claim_catalog: Mapping[str, Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    flags_by_id = {
        str(flag.get("flag_id")): flag
        for flag in flags
        if isinstance(flag, Mapping) and flag.get("flag_id")
    }
    document_by_id = {
        str(item.get("doc_id")): item
        for item in documents
        if isinstance(item, Mapping) and item.get("doc_id")
    }
    entries: List[Dict[str, Any]] = []
    authorized_metric_ids: List[str] = []
    authorized_document_ids: List[str] = []
    artifact_contexts: Dict[str, Any] = {}
    artifact_authorizations: Dict[str, Dict[str, List[str]]] = {}
    for target in targets:
        artifact_type = str(target["artifact_type"])
        artifact_id = str(target["artifact_id"])
        field_name = str(target["field_name"])
        if artifact_type == "dimension":
            artifact = dimension_narratives[artifact_id]
            profile = next(
                item
                for item in assessment_profile.get("dimensions", []) or []
                if str(item.get("dimension")) == artifact_id
            )
            target_metric_ids = _catalog_ids_in_value(
                dimension_prompt_profile(profile), claim_catalog
            )
        elif artifact_type == "market":
            artifact = market_narratives[artifact_id]
            profile = next(
                item
                for item in assessment_profile.get("markets", []) or []
                if str(item.get("market_name")) == artifact_id
            )
            target_metric_ids = list(
                build_market_prompt_projection(
                    assessment_profile, profile
                ).get("selected_ledger_metric_ids", [])
                or []
            )
        elif artifact_type == "executive_summary":
            artifact = executive_narrative
            target_metric_ids = executive_catalog_ids(assessment_profile)
        else:
            artifact = next(
                item
                for item in context_evidence
                if str(item.get("statement_id")) == artifact_id
            )
            target_metric_ids = []
        target_flags = [
            flags_by_id[flag_id]
            for flag_id in target.get("flag_ids", []) or []
            if flag_id in flags_by_id
        ]
        cited_document_ids = {
            str(document_id)
            for flag in target_flags
            for document_id in flag.get("document_ids", []) or []
        }
        target_metric_ids = list(
            dict.fromkeys(
                [
                    *(str(item) for item in target_metric_ids if item),
                    *_claim_reference_ids(artifact, "metric_ids"),
                    *(
                        str(metric_id)
                        for flag in target_flags
                        for metric_id in flag.get("metric_ids", []) or []
                        if metric_id
                    ),
                ]
            )
        )
        cited_document_ids.update(_claim_reference_ids(artifact, "document_ids"))
        authorized_metric_ids.extend(target_metric_ids)
        authorized_document_ids.extend(sorted(cited_document_ids))
        context_id = f"{artifact_type}:{artifact_id}"
        artifact_contexts.setdefault(
            context_id,
            project_correction_transport(artifact),
        )
        authorization = artifact_authorizations.setdefault(
            context_id,
            {"metric_ids": [], "document_ids": []},
        )
        authorization["metric_ids"] = list(
            dict.fromkeys([*authorization["metric_ids"], *target_metric_ids])
        )
        authorization["document_ids"] = sorted(
            set([*authorization["document_ids"], *cited_document_ids])
        )
        entries.append(
            {
                "target_id": str(target["task_id"]),
                "artifact_type": artifact_type,
                "artifact_id": artifact_id,
                "field_name": field_name,
                "findings": [
                    {
                        key: flag.get(key)
                        for key in (
                            "flag_id",
                            "code",
                            "severity",
                            "claim_id",
                            "message",
                            "recommendation",
                        )
                    }
                    for flag in target_flags
                ],
                "current_field": project_correction_transport(
                    artifact.get(field_name)
                ),
                "artifact_context_id": context_id,
                "patch_contract": correction_field_patch_contract(
                    task=target,
                    artifact=artifact,
                    assessment_profile=assessment_profile,
                ),
            }
        )
    unique_metric_ids = list(dict.fromkeys(authorized_metric_ids))
    unique_document_ids = sorted(set(authorized_document_ids))
    return {
        "contract_version": "mfi-consolidated-correction-v2",
        "targets": entries,
        "artifact_contexts": artifact_contexts,
        "artifact_authorizations": artifact_authorizations,
        "authorized_claim_catalog": compact_catalog(
            claim_catalog, unique_metric_ids
        ),
        "authorized_documents": [
            {
                "document_id": document_id,
                "source": document_by_id[document_id].get("source"),
                "date": document_by_id[document_id].get("date"),
                "title": document_by_id[document_id].get("title"),
                "content_excerpt": str(
                    document_by_id[document_id].get("content") or ""
                )[:800],
            }
            for document_id in unique_document_ids
            if document_id in document_by_id
        ],
        "prohibitions": list(NARRATIVE_PROHIBITIONS),
    }


def _iter_patch_claims(value):
    if isinstance(value, dict):
        if "text" in value:
            yield value
        else:
            for child in value.values():
                yield from _iter_patch_claims(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_patch_claims(child)


def apply_consolidated_patches(
    *,
    targets: Sequence[Mapping[str, Any]],
    replacements: Mapping[str, Any],
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    context_evidence: Sequence[Mapping[str, Any]],
    assessment_profile: Mapping[str, Any],
) -> Dict[str, Any]:
    state = {
        "dimension_narratives": deepcopy(dict(dimension_narratives)),
        "market_narratives": deepcopy(dict(market_narratives)),
        "executive_summary_narrative": deepcopy(dict(executive_narrative)),
        "context_evidence": deepcopy(list(context_evidence)),
    }
    for target in targets:
        if target.get("expected_field_hash"):
            from .reliable_contracts import fingerprint
            kind, identifier = target["artifact_type"], target.get("artifact_id")
            if kind == "dimension":
                artifact = state["dimension_narratives"][identifier]
            elif kind == "market":
                artifact = state["market_narratives"][identifier]
            elif kind == "context":
                artifact = next(item for item in state["context_evidence"] if item["statement_id"] == identifier)
            else:
                artifact = state["executive_summary_narrative"]
            if fingerprint(project_correction_transport(artifact.get(target["field_name"]))) != target["expected_field_hash"]:
                raise ValueError("Correction target field revision is stale")
        previous_revision = max((int(claim.get("revision", 1)) for claim in _iter_patch_claims(artifact.get(target["field_name"])))) if target.get("expected_field_hash") and list(_iter_patch_claims(artifact.get(target["field_name"]))) else 1
        state = apply_field_patch(
            task=target,
            replacement=replacements[str(target["task_id"])],
            dimension_narratives=state["dimension_narratives"],
            market_narratives=state["market_narratives"],
            executive_narrative=state["executive_summary_narrative"],
            context_evidence=state["context_evidence"],
            assessment_profile=assessment_profile,
        )
        if target.get("expected_field_hash") and kind != "context":
            updated = state["dimension_narratives"][identifier] if kind == "dimension" else state["market_narratives"][identifier] if kind == "market" else state["executive_summary_narrative"]
            for claim in _iter_patch_claims(updated.get(target["field_name"])):
                claim["revision"] = previous_revision + 1
    return state


def target_keys(targets: Sequence[Mapping[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (
            str(target.get("artifact_type")),
            str(target.get("artifact_id")),
            str(target.get("field_name")),
        )
        for target in targets
    }


def flags_outside_targets(
    flags: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    keys = target_keys(targets)
    return [
        dict(flag)
        for flag in flags
        if (
            str(flag.get("artifact_type")),
            str(flag.get("artifact_id")),
            str(flag.get("field_name")),
        )
        not in keys
    ]


def build_corrected_claim_verification_package(
    *,
    targets: Sequence[Mapping[str, Any]],
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
    context_evidence: Sequence[Mapping[str, Any]],
    assessment_profile: Mapping[str, Any],
    claim_catalog: Mapping[str, Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    report_context: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    keys = target_keys(targets)
    claims = [
        row
        for row in canonical_claim_rows(
            dimension_narratives=dimension_narratives,
            market_narratives=market_narratives,
            executive_narrative=executive_narrative,
            context_evidence=context_evidence,
            assessment_profile=assessment_profile,
        )
        if (
            row["artifact_type"],
            row["artifact_id"],
            row["field_name"],
        )
        in keys
    ]
    accepted_context = _accepted_context_for_review(context_evidence, documents)
    context_document_ids = [
        document_id
        for statement in accepted_context
        for document_id in statement["document_ids"]
    ]
    package = {
        "contract_version": SEMANTIC_REVIEW_CONTRACT_VERSION,
        "section": "corrected_claims",
        "report_context": _report_review_context(
            assessment_profile, report_context
        ),
        "claims": claims,
        "evidence_by_metric_id": _review_evidence(claim_catalog, claims),
        "accepted_context": accepted_context,
        "cited_documents": _review_documents(
            documents,
            claims,
            additional_document_ids=context_document_ids,
        ),
    }
    return {
        "review_id": "corrected-claim-verification",
        "section": "corrected_claims",
        "sequence": 4,
        "claim_ids": [row["claim_id"] for row in claims],
        "character_count": len(_serialized(package)),
        "package": package,
    }
