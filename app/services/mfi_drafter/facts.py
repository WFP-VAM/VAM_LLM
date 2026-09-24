"""Deterministic comparison facts and subject-bound narrative rendering."""
from __future__ import annotations

from copy import deepcopy

from .identity import context_token, normalized_slug
from .methodology import SCORE_VALIDATION_ABS_TOLERANCE
from .reliable_contracts import AnalyticalFact, CoverageRequirement, WORKFLOW_REVISION


def enrich_profile(profile: dict, markets: list[dict], metadata: dict) -> dict:
    if metadata.get("workflow_revision") != WORKFLOW_REVISION:
        return profile
    result = deepcopy(profile)
    result["workflow_revision"] = WORKFLOW_REVISION
    identities = (metadata.get("validated_assessment") or {}).get("market_identities", {})
    result["market_identities"] = identities
    by_name = {m["market_name"]: m for m in markets}
    for market in result["markets"]:
        market["market_key"] = by_name[market["market_name"]].get("market_key")
    facts = {}
    coverage = []
    for dimension in result["dimensions"]:
        name = dimension["dimension"]
        slug = normalized_slug(name)
        population = sorted(markets, key=lambda m: m.get("market_key", m["market_name"]))
        median = dimension["statistics"]["median"]
        thresholds = [("at_or_below_median", "le", median)] + [(f"below_{n}", "lt", float(n)) for n in range(1, 11)]
        score_ids = [f"market.{context_token(m['market_name'])}.dimension.{slug}.stored" for m in population]
        for label, operator, threshold in thresholds:
            selected = [m for m in population if (m["dimension_scores"][name] <= threshold + SCORE_VALIDATION_ABS_TOLERANCE if operator == "le" else m["dimension_scores"][name] < threshold - SCORE_VALIDATION_ABS_TOLERANCE)]
            fact_id = f"fact.assessment.{slug}.{label}"
            phrase = f"at or below the median of {threshold:.2f}/10" if operator == "le" else f"below {threshold:g}/10"
            facts[fact_id] = AnalyticalFact(fact_id=fact_id, dimension=name, subject=name, population="included_assessed_markets", statistic="count", value=len(selected), unit="count", operator=operator, threshold=threshold, numerator=len(selected), denominator=len(population), member_ids=[m.get("market_key", m["market_name"]) for m in selected], source_metric_ids=score_ids, rendered_text=f"{len(selected)} of {len(population)} assessed markets scored {phrase} for {name}.").model_dump()
        regions = dimension.get("regional_summaries", [])
        for label, func in (("minimum", min), ("maximum", max)):
            if not regions:
                continue
            value = func(r["statistics"]["mean"] for r in regions)
            ties = sorted(r["region"] for r in regions if abs(r["statistics"]["mean"] - value) <= SCORE_VALIDATION_ABS_TOLERANCE)
            fact_id = f"fact.region.{slug}.{label}"
            sources = [mid for r in regions for mid in r.get("ledger_metric_ids", []) if mid.endswith(".mean")]
            facts[fact_id] = AnalyticalFact(fact_id=fact_id, dimension=name, subject=", ".join(ties), population="included_assessed_regions", statistic=label, value=value, unit="score", operator=label, denominator=len(regions), member_ids=ties, source_metric_ids=sources, rendered_text=f"{', '.join(ties)} had the {'lowest' if label == 'minimum' else 'highest'} unweighted regional mean {name} score ({value:.2f}/10).").model_dump()
        requirements = [("local_extremes", score_ids), ("distribution", dimension.get("ledger_metric_ids", [])), ("geography", [mid for r in regions for mid in r.get("ledger_metric_ids", [])])]
        requirements += [(m["metric_id"], m.get("ledger_metric_ids", [])) for m in [*dimension.get("subsections", []), *dimension.get("drivers", [])]]
        for kind, ids in requirements:
            coverage.append(CoverageRequirement(requirement_id=f"coverage.{slug}.{kind}", dimension=name, kind=kind, metric_ids=ids).model_dump())
        dimension["analytical_facts"] = {k:v for k,v in facts.items() if v["dimension"] == name}
        dimension["workflow_revision"] = WORKFLOW_REVISION
    # Register comparisons in the existing ledger. The fact registry adds rendering
    # and provenance; it never calculates a competing value.
    for key, fact in facts.items():
        regional = fact["population"] == "included_assessed_regions"
        result["metric_ledger"][key] = {
            "ledger_id": key, "label": fact["rendered_text"], "value": fact["value"],
            "statistic": fact["statistic"], "unit": fact["unit"], "orientation": "descriptive",
            "evidence_scope": "region" if regional else "included_assessed_markets",
            "dimension": fact["dimension"], "region": fact["subject"] if regional else None,
            "source_metric_ids": fact["source_metric_ids"],
            "aggregation_method": "count" if fact["unit"] == "count" else "unweighted_market_mean",
            "population_basis": "market_level", "representation_basis": "all_assessed_markets",
            "permitted_subject_phrase": "the number of assessed markets meeting the stated condition" if not regional else "the regional mean across assessed markets",
        }
    result["analytical_facts"] = facts
    result["coverage_manifest"] = coverage
    # Rewrite references once at the analysis boundary; names remain display data.
    replacements = {f"market.{context_token(m['market_name'])}.": f"market.{m['market_key']}." for m in markets if m.get("market_key")}
    def rewrite(value):
        if isinstance(value, dict):
            return {rewrite(k):rewrite(v) for k,v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, str):
            for old, new in replacements.items():
                if value.startswith(old):
                    return new + value[len(old):]
        return value
    return rewrite(result)
