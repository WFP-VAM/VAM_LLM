"""One-cycle API compatibility aliases for canonical typed MFI evidence.

Internal graph, prompt, report, and UI code must consume ``subsections`` and
``drivers``.  This module is intentionally called only while constructing an
external API response.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping, Optional

from .methodology import DISPLAY_DIMENSIONS
from .schemas import get_risk_level


def canonical_and_legacy_response_fields(
    result: dict[str, Any],
) -> dict[str, Any]:
    """Centralize canonical analysis output and one-cycle response aliases.

    The graph normally supplies ``assessment_profile``.  Building it here as a
    fallback keeps direct/in-process serialization paths deterministic without
    allowing any legacy alias to feed analysis.
    """
    profile = result.get("assessment_profile")
    if not isinstance(profile, dict):
        from .analysis import build_assessment_profile

        profile = build_assessment_profile(
            result.get("markets_data", []) or [],
            result.get("metric_summaries", {}) or {},
            result,
        ).model_dump()
    canonical_mean = float(profile["mean_mfi_across_assessed_markets"])

    serialized_markets = with_legacy_sub_score_aliases(
        result.get("markets_data", []) or []
    )
    risk_distribution: dict[str, int] = {}
    for market in serialized_markets:
        if not isinstance(market, dict):
            continue
        risk = str(market.get("risk_level") or "Unknown")
        risk_distribution[risk] = risk_distribution.get(risk, 0) + 1
    dimension_scores = _legacy_dimension_scores(profile)
    narrative_aliases = legacy_narrative_aliases(
        dimension_narratives=result.get("dimension_narratives", {}) or {},
        market_narratives=result.get("market_narratives", {}) or {},
        executive_narrative=result.get("executive_summary_narrative", {}) or {},
    )
    market_score_distribution = deepcopy(
        result.get("market_score_distribution") or []
    )
    if not market_score_distribution:
        market_score_distribution = [
            {
                "market_name": market.get("market_name"),
                "overall_mfi": market.get("overall_mfi"),
                "score_rank": market.get("score_rank"),
                "selection_order": market.get("selection_order"),
                "is_priority_market": market.get("is_priority_market", False),
            }
            for market in profile.get("markets", [])
            if isinstance(market, dict)
        ]
    country_context = "\n".join(
        str(statement.get("text"))
        for statement in result.get("context_evidence", []) or []
        if isinstance(statement, dict)
        and statement.get("text")
        and statement.get("classification") != "unrelated"
    ) or None

    return {
        "mean_mfi_across_assessed_markets": canonical_mean,
        "assessment_profile": deepcopy(profile),
        "market_score_distribution": market_score_distribution,
        "national_mfi": round(canonical_mean, 1),
        "risk_distribution": risk_distribution,
        "markets_data": serialized_markets,
        "dimension_scores": dimension_scores,
        "country_context": country_context,
        **narrative_aliases,
    }


def with_legacy_sub_score_aliases(
    markets_data: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return detached market payloads with the deprecated alias populated."""
    serialized = deepcopy(list(markets_data))
    for market in serialized:
        score = market.get("overall_mfi")
        market["risk_level"] = (
            get_risk_level(float(score)) if score is not None else "Unknown"
        )
        market["sub_scores"] = _legacy_alias_for_market(market)
    return serialized


def _legacy_dimension_scores(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate old dimension aggregations solely at response serialization."""
    rows: list[dict[str, Any]] = []
    for dimension in profile.get("dimensions", []) or []:
        if not isinstance(dimension, dict):
            continue
        localized = dimension.get("localized_patterns") or {}
        rows.append(
            {
                "dimension": dimension.get("dimension"),
                "national_score": (dimension.get("statistics") or {}).get("mean"),
                "regional_scores": {
                    str(summary.get("region")): (
                        summary.get("statistics") or {}
                    ).get("mean")
                    for summary in dimension.get("regional_summaries", []) or []
                    if isinstance(summary, dict) and summary.get("region")
                },
                "market_scores": {
                    str(item.get("name")): item.get("value")
                    for item in localized.get("ordered_markets", []) or []
                    if isinstance(item, dict) and item.get("name")
                },
            }
        )
    return rows


def _legacy_alias_for_market(market: dict[str, Any]) -> dict[str, dict[str, Any]]:
    subsections = _flatten_groups(market.get("subsections"))
    drivers = _flatten_groups(market.get("drivers"))

    return {
        "Assortment": {
            "breadth": _score(subsections.get("assortment.breadth")),
            "depth": _score(subsections.get("assortment.depth")),
        },
        "Availability": {
            "scarce_cereals_pct": _complement(
                _raw(drivers.get("availability.scarcity.category.cereal_food"))
            ),
            "runout_cereals_pct": _complement(
                _raw(drivers.get("availability.runout.category.cereal_food"))
            ),
        },
        "Price": {
            "increase_cereals_pct": _complement(
                _raw(drivers.get("price.increase.category.cereal_food"))
            ),
            "unstable_cereals_pct": _complement(
                _raw(drivers.get("price.stability.category.cereal_food"))
            ),
        },
        "Resilience": {
            "low_density_pct": _complement(
                _mean_raw_with_prefix(drivers, "resilience.vulnerability.density.")
            ),
            "high_complexity_pct": _complement(
                _mean_raw_with_prefix(drivers, "resilience.vulnerability.complexity.")
            ),
            "high_criticality_pct": _complement(
                _mean_raw_with_prefix(drivers, "resilience.vulnerability.criticality.")
            ),
        },
        "Competition": {
            "less_than_five_competitors": _below_midpoint(
                subsections.get("competition.concentration")
            ),
            "monopoly_risk": _below_midpoint(
                subsections.get("competition.monopoly")
            ),
        },
        "Infrastructure": {
            "condition_good": _majority(
                drivers.get("infrastructure.condition.good")
            ),
            "condition_medium": _majority(
                drivers.get("infrastructure.condition.medium")
            ),
            "condition_poor": _majority(
                drivers.get("infrastructure.condition.poor")
            ),
        },
        "Service": {
            "checkout_score": _mean_score_with_prefix(
                drivers, "service.checkout."
            ),
            "shopping_experience_score": _mean_score_with_prefix(
                drivers, "service.shopping."
            ),
        },
        "Food Quality": {
            "quality_standards_met_pct": _mean_raw_with_prefix(
                drivers, "quality.condition."
            ),
            "quality_problems_pct": _complement(
                _mean_raw_with_prefix(drivers, "quality.condition.")
            ),
        },
        "Access & Protection": {
            "access_issues_pct": _score_complement(
                subsections.get("access_protection.access")
            ),
            "protection_issues_pct": _score_complement(
                subsections.get("access_protection.protection")
            ),
        },
    }


def _flatten_groups(value: Any) -> dict[str, dict[str, Any]]:
    flattened: dict[str, dict[str, Any]] = {}
    if not isinstance(value, dict):
        return flattened
    for metrics in value.values():
        if not isinstance(metrics, list):
            continue
        for metric in metrics:
            if isinstance(metric, dict) and metric.get("metric_id"):
                flattened[str(metric["metric_id"])] = metric
    return flattened


def _usable(metric: Optional[dict[str, Any]]) -> bool:
    return bool(
        metric
        and metric.get("applicability_status") == "available"
        and metric.get("validation_status") == "valid"
    )


def _raw(metric: Optional[dict[str, Any]]) -> Optional[float]:
    if not _usable(metric) or metric.get("raw_value") is None:
        return None
    return float(metric["raw_value"])


def _score(metric: Optional[dict[str, Any]]) -> Optional[float]:
    if not _usable(metric) or metric.get("normalized_value") is None:
        return None
    return float(metric["normalized_value"])


def _complement(value: Optional[float]) -> Optional[float]:
    return None if value is None else 1.0 - value


def _score_complement(metric: Optional[dict[str, Any]]) -> Optional[float]:
    value = _score(metric)
    return None if value is None else (10.0 - value) / 10.0


def _matching_values(
    metrics: dict[str, dict[str, Any]], prefix: str, field: str
) -> list[float]:
    values: list[float] = []
    for metric_id, metric in metrics.items():
        if (
            metric_id.startswith(prefix)
            and _usable(metric)
            and metric.get(field) is not None
        ):
            values.append(float(metric[field]))
    return values


def _mean_raw_with_prefix(
    metrics: dict[str, dict[str, Any]], prefix: str
) -> Optional[float]:
    values = _matching_values(metrics, prefix, "raw_value")
    return sum(values) / len(values) if values else None


def _mean_score_with_prefix(
    metrics: dict[str, dict[str, Any]], prefix: str
) -> Optional[float]:
    values = _matching_values(metrics, prefix, "normalized_value")
    return sum(values) / len(values) if values else None


def _below_midpoint(metric: Optional[dict[str, Any]]) -> Optional[int]:
    value = _raw(metric)
    return None if value is None else int(value < 3.0)


def _majority(metric: Optional[dict[str, Any]]) -> Optional[int]:
    value = _raw(metric)
    return None if value is None else int(value > 0.5)


def legacy_narrative_aliases(
    *,
    dimension_narratives: Mapping[str, Mapping[str, Any]],
    market_narratives: Mapping[str, Mapping[str, Any]],
    executive_narrative: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one-cycle narrative aliases from canonical structures."""
    dimension_findings: dict[str, dict[str, str]] = {}
    for dimension in DISPLAY_DIMENSIONS:
        narrative = dimension_narratives.get(dimension)
        if not isinstance(narrative, Mapping):
            continue
        findings = [
            str(item.get("text") or "")
            for item in narrative.get("key_findings", []) or []
            if isinstance(item, Mapping) and item.get("text")
        ]
        summary = narrative.get("summary")
        summary_text = (
            str(summary.get("text") or "")
            if isinstance(summary, Mapping)
            else ""
        )
        recommendations = [
            str(item.get("text") or "")
            for item in narrative.get("recommendations", []) or []
            if isinstance(item, Mapping) and item.get("text")
        ]
        dimension_findings[dimension] = {
            "key_findings": "\n".join(f"- {item}" for item in findings),
            "score_interpretation": summary_text,
            "recommendations": "\n".join(
                f"- {item}" for item in recommendations
            ),
        }
    market_recommendations: dict[str, dict[str, Any]] = {}
    for market_name, narrative in market_narratives.items():
        if not isinstance(narrative, Mapping):
            continue
        modality = narrative.get("modality_consideration")
        market_recommendations[str(market_name)] = {
            "region": narrative.get("region"),
            "mfi_score": narrative.get("overall_mfi"),
            "weak_dimensions": list(narrative.get("weak_dimensions", []) or []),
            "priority_issues": [
                str(item.get("text") or "")
                for item in narrative.get("priority_issues", []) or []
                if isinstance(item, Mapping) and item.get("text")
            ],
            "recommended_interventions": [
                str(item.get("text") or "")
                for item in narrative.get("recommended_interventions", []) or []
                if isinstance(item, Mapping) and item.get("text")
            ],
            "modality_considerations": (
                str(modality.get("text") or "")
                if isinstance(modality, Mapping)
                else ""
            ),
        }
    executive_parts: list[str] = []
    motivation = executive_narrative.get("motivation")
    if isinstance(motivation, Mapping) and motivation.get("text"):
        executive_parts.extend(["**MOTIVATION**", str(motivation["text"]), ""])
    findings = executive_narrative.get("key_findings", []) or []
    if findings:
        executive_parts.append("**KEY FINDINGS**")
        executive_parts.extend(
            f"- {item.get('text')}"
            for item in findings
            if isinstance(item, Mapping) and item.get("text")
        )
        executive_parts.append("")
    recommendations = executive_narrative.get("recommendations", []) or []
    if recommendations:
        executive_parts.append("**RECOMMENDATIONS**")
        executive_parts.extend(
            f"- {item.get('text')}"
            for item in recommendations
            if isinstance(item, Mapping) and item.get("text")
        )
    return {
        "executive_summary": "\n".join(executive_parts).strip(),
        "dimension_findings": dimension_findings,
        "market_recommendations": market_recommendations,
    }
