"""Lossless relevant projections of the existing analytical engine; no new calculations."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from functools import cmp_to_key
from typing import Any, Dict, List, Mapping

from .methodology import METRIC_DEFINITIONS_BY_ID, SCORE_VALIDATION_ABS_TOLERANCE

MARKET_PROMPT_PROJECTION_VERSION = "mfi-market-prompt-v1"

_LINEAGE ={"ledger_metric_ids", "source_metric_ids", "member_ids", "metric_ledger",
            "workflow_revision", "satisfied_by", "contributing_metric_ids"}


def compact(value):
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if k not in _LINEAGE}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def table(rows, columns):
    return {"columns": columns, "rows": [[compact(row.get(c)) for c in columns] for row in rows]}


def selected_markets(profile):
    return sorted((m for m in profile["markets"] if m["is_priority_market"]),
                  key=lambda m: m["selection_order"])


def section_specs(profile, family):
    if family == "dimensions":
        return [{"section_id": d["dimension"], "title": d["dimension"]} for d in profile["dimensions"]]
    return [{"section_id": m.get("market_key") or m["market_name"], "title": m["market_name"]}
            for m in selected_markets(profile)]


def source_map(documents):
    docs = sorted(documents, key=lambda d: (str(d.get("doc_id", "")), str(d.get("url", ""))))
    seen, result = set(), {}
    for doc in docs:
        key = doc.get("url") or doc.get("doc_id")
        if not key or key in seen or not doc.get("content"):
            continue
        seen.add(key)
        result[f"S{len(result)+1}"] = {k: doc.get(k) for k in ("doc_id", "title", "source", "date", "url", "content")}
    return result


def metric_table(rows):
    prepared = []
    for row in rows:
        definition = METRIC_DEFINITIONS_BY_ID.get(row["metric_id"])
        prepared.append({**row, "parent_subsection_id": definition.parent_subsection_id if definition else None})
    return table(prepared, ["metric_id", "display_name", "role", "parent_subsection_id", "unit", "orientation",
        "evidence_scope", "mean_raw_value", "mean_normalized_value", "unfavorable_rate", "coverage",
        "availability", "product_group", "question_group", "item_name", "item_relevant", "relevance_reasons", "permitted_subject_phrase"])


def evidence(state, family, ids):
    profile = state["assessment_profile"]
    common = {"country": state["country"], "period": [state["data_collection_start"], state["data_collection_end"]],
        "methodology": state["methodology_version"], "survey_metadata": state["survey_metadata"],
        "overall": compact(profile["overall_statistics"]), "priority_dimensions": profile["priority_dimension_names"],
        "limitations": compact(profile["limitations"]), "sources": state.get("sources", {}),
        "context_limitation": state.get("context_limitation"),
        "market_scores": {"columns": ["market_key", "market_name", "region", "overall_mfi"] + [d["dimension"] for d in profile["dimensions"]],
            "rows": [[m.get("market_key"), m["market_name"], m.get("region"), m["overall_mfi"]] +
                     [next(x["score"] for x in m["dimension_profile"] if x["dimension"] == d["dimension"]) for d in profile["dimensions"]]
                     for m in profile["markets"]]}}
    if family == "dimensions":
        common["dimensions"] = [{"dimension": d["dimension"], "statistics": compact(d["statistics"]),
            "priority": d["is_priority"], "rank_among_dimensions": d["profile_rank"],
            "regions": compact(d["regional_summaries"]), "facts": compact(d["analytical_facts"]),
            "components_and_drivers": metric_table([*d["subsections"], *d["drivers"]])}
            for d in profile["dimensions"] if d["dimension"] in ids]
    elif family == "markets":
        selected =[m for m in selected_markets(profile) if (m.get("market_key") or m["market_name"]) in ids]
        common["selected_markets"] = table(selected, ["market_key", "market_name", "score_rank", "selection_order", "selection_reasons"])
        rows, definitions, ranks = [], {}, []
        for market in selected:
            projection = build_market_prompt_projection(profile, market)
            ranks.extend({"market_key": market["market_key"], "dimension": d["dimension"],
                "rank_within_market": d["rank"], "is_weak": d["is_weak"]} for d in market["dimension_profile"])
            for ledger_id in projection["selected_ledger_metric_ids"]:
                entry = profile["metric_ledger"][ledger_id]
                if entry["statistic"] not in {"market_explanatory_normalized_value", "market_explanatory_raw_value", "derived_market_unfavorable_rate"}:
                    continue  # Stored scores and ranks have their own single tables.
                metric_id = next((s for s in entry.get("source_metric_ids", []) if s in METRIC_DEFINITIONS_BY_ID), None)
                if not metric_id:  # Scores already appear once in the comparator matrix.
                    continue
                definition = METRIC_DEFINITIONS_BY_ID[metric_id]
                definitions[metric_id] = {"metric_id": metric_id, "name": definition.display_name,
                    "dimension": definition.dimension, "role": definition.role, "parent_subsection_id": definition.parent_subsection_id}
                rows.append({"market_key": market["market_key"], "metric_id": metric_id, "value": entry["value"],
                    "unit": entry["unit"], "statistic": entry["statistic"], "coverage": entry.get("coverage"),
                    "population_basis": entry.get("population_basis"), "subject": entry.get("permitted_subject_phrase")})
        common["within_market_dimension_ranks"] = table(ranks, ["market_key", "dimension", "rank_within_market", "is_weak"])
        common["indicator_definitions"] = table(list(definitions.values()), ["metric_id", "name", "dimension", "role", "parent_subsection_id"])
        common["local_evidence"] = table(rows, ["market_key", "metric_id", "value", "unit", "statistic", "coverage", "population_basis", "subject"])
    else:
        common["dimension_summary"] = table(profile["dimensions"], ["dimension", "statistics", "profile_rank", "is_priority"])
        common["selected_market_summary"] = table(selected_markets(profile), ["market_key", "market_name", "region", "overall_mfi", "score_rank", "selection_order"])
    return deepcopy(common)


def _market_local_evidence(
    assessment_profile: Mapping[str, Any],
    *,
    market_name: str,
    dimension: str,
) -> Dict[str, Dict[str, str]]:
    """Index one market/dimension ledger by methodology metric and statistic."""
    result: Dict[str, Dict[str, str]] = defaultdict(dict)
    ledger = assessment_profile.get("metric_ledger") or {}
    if not isinstance(ledger, Mapping):
        return {}
    for ledger_id, raw_entry in ledger.items():
        if not isinstance(raw_entry, Mapping):
            continue
        if (
            str(raw_entry.get("market_name") or "") != market_name
            or str(raw_entry.get("dimension") or "") != dimension
        ):
            continue
        source_ids = [
            str(item) for item in raw_entry.get("source_metric_ids", []) or [] if item
        ]
        if not source_ids or source_ids[0] not in METRIC_DEFINITIONS_BY_ID:
            continue
        statistic = str(raw_entry.get("statistic") or "")
        if statistic:
            result[source_ids[0]][statistic] = str(ledger_id)
    return dict(result)


def _primary_local_ledger_id(
    statistics: Mapping[str, str],
    *,
    role: str,
) -> str | None:
    if role in {"category_driver", "question_driver", "item_driver"}:
        return statistics.get("derived_market_unfavorable_rate")
    return statistics.get("market_explanatory_normalized_value") or statistics.get(
        "market_explanatory_raw_value"
    )


def _market_projection_entry(
    *,
    source_metric_id: str,
    ledger_id: str,
) -> Dict[str, Any]:
    definition = METRIC_DEFINITIONS_BY_ID[source_metric_id]
    return {
        "source_metric_id": source_metric_id,
        "ledger_metric_id": ledger_id,
        "role": definition.role,
        "parent_subsection_id": definition.parent_subsection_id,
        "product_group": definition.product_group,
        "question_group": definition.question_group,
        "item_name": definition.item_name,
    }


def _compare_metric_value(
    left: float,
    right: float,
    *,
    descending: bool = False,
) -> int:
    """Compare analytical values while preserving methodology-level ties."""
    delta = float(left) - float(right)
    if abs(delta) <= SCORE_VALIDATION_ABS_TOLERANCE:
        return 0
    result = -1 if delta < 0 else 1
    return -result if descending else result


def _compare_subsection_candidate(
    left: tuple[float, int, str, str],
    right: tuple[float, int, str, str],
) -> int:
    value_order = _compare_metric_value(left[0], right[0])
    if value_order:
        return value_order
    return (left[2] > right[2]) - (left[2] < right[2])


def _compare_driver_candidate(
    left: tuple[float, int, str, str],
    right: tuple[float, int, str, str],
) -> int:
    value_order = _compare_metric_value(left[0], right[0], descending=True)
    if value_order:
        return value_order
    if left[1] != right[1]:
        return -1 if left[1] > right[1] else 1
    return (left[2] > right[2]) - (left[2] < right[2])


def _compare_item_candidate(
    left: tuple[float, str, str],
    right: tuple[float, str, str],
) -> int:
    value_order = _compare_metric_value(left[0], right[0], descending=True)
    if value_order:
        return value_order
    return (left[1] > right[1]) - (left[1] < right[1])


def build_market_prompt_projection(
    assessment_profile: Mapping[str, Any],
    market_profile: Mapping[str, Any],
) -> Dict[str, Any]:
    """Project bounded, market-scoped evidence without changing Phase 2 data."""
    market_name = str(market_profile.get("market_name") or "")
    ledger = assessment_profile.get("metric_ledger") or {}
    if not market_name or not isinstance(ledger, Mapping):
        raise ValueError("Market prompt projection requires a market and metric ledger")

    relevant_items_by_dimension = {
        str(dimension.get("dimension")): {
            str(metric.get("metric_id"))
            for metric in dimension.get("drivers", []) or []
            if isinstance(metric, Mapping)
            and metric.get("metric_id")
            and bool(metric.get("item_relevant"))
        }
        for dimension in assessment_profile.get("dimensions", []) or []
        if isinstance(dimension, Mapping) and dimension.get("dimension")
    }
    selected_ids: List[str] = [
        str(item) for item in market_profile.get("ledger_metric_ids", []) or [] if item
    ]
    weak_projections: List[Dict[str, Any]] = []
    selection_counts = {"subsections": 0, "drivers": 0, "relevant_items": 0}

    for weak in market_profile.get("weak_dimensions", []) or []:
        if not isinstance(weak, Mapping) or not weak.get("dimension"):
            continue
        dimension = str(weak["dimension"])
        weak_ledger_ids = [
            str(item) for item in weak.get("ledger_metric_ids", []) or [] if item
        ]
        selected_ids.extend(weak_ledger_ids)
        by_source = _market_local_evidence(
            assessment_profile,
            market_name=market_name,
            dimension=dimension,
        )
        subsection_candidates: List[tuple[float, int, str, str]] = []
        driver_candidates: List[tuple[float, int, str, str]] = []
        item_candidates: List[tuple[float, str, str]] = []
        for source_metric_id, statistics in by_source.items():
            definition = METRIC_DEFINITIONS_BY_ID[source_metric_id]
            ledger_id = _primary_local_ledger_id(
                statistics,
                role=definition.role,
            )
            entry = ledger.get(ledger_id) if ledger_id else None
            if not ledger_id or not isinstance(entry, Mapping):
                continue
            value = entry.get("value")
            if not isinstance(value, (int, float)):
                continue
            if dimension == "Food Quality":
                is_subsection = definition.role == "dimension_validation_component"
            else:
                is_subsection = definition.role == "official_subsection"
            if is_subsection:
                quality_order = (
                    0
                    if source_metric_id == "quality.measure"
                    else 1
                    if source_metric_id == "quality.maximum"
                    else 2
                )
                subsection_candidates.append(
                    (float(value), quality_order, source_metric_id, ledger_id)
                )
            elif definition.role in {"category_driver", "question_driver"}:
                driver_candidates.append(
                    (
                        float(value),
                        int(definition.severity_weight or 0),
                        source_metric_id,
                        ledger_id,
                    )
                )
            elif (
                definition.role == "item_driver"
                and source_metric_id in relevant_items_by_dimension.get(dimension, set())
            ):
                item_candidates.append(
                    (float(value), source_metric_id, ledger_id)
                )

        if dimension == "Food Quality":
            subsection_candidates.sort(key=lambda item: (item[1], item[2]))
        else:
            subsection_candidates.sort(key=cmp_to_key(_compare_subsection_candidate))
        driver_candidates.sort(key=cmp_to_key(_compare_driver_candidate))
        item_candidates.sort(key=cmp_to_key(_compare_item_candidate))
        subsections = [
            _market_projection_entry(source_metric_id=item[2], ledger_id=item[3])
            for item in subsection_candidates[:2]
        ]
        drivers = [
            _market_projection_entry(source_metric_id=item[2], ledger_id=item[3])
            for item in driver_candidates[:4]
        ]
        relevant_items = [
            _market_projection_entry(source_metric_id=item[1], ledger_id=item[2])
            for item in item_candidates[:3]
        ]
        for entry in [*subsections, *drivers, *relevant_items]:
            selected_ids.append(str(entry["ledger_metric_id"]))
        selection_counts["subsections"] += len(subsections)
        selection_counts["drivers"] += len(drivers)
        selection_counts["relevant_items"] += len(relevant_items)
        weak_projections.append(
            {
                "dimension": dimension,
                "score": weak.get("score"),
                "rank": weak.get("rank"),
                "selection_order": weak.get("selection_order"),
                "ledger_metric_ids": weak_ledger_ids,
                "official_subsections": subsections,
                "explanatory_drivers": drivers,
                "relevant_items": relevant_items,
            }
        )

    return {
        "projection_version": MARKET_PROMPT_PROJECTION_VERSION,
        "market_name": market_name,
        "market_key": market_profile.get("market_key"),
        "region": market_profile.get("region"),
        "overall_mfi": market_profile.get("overall_mfi"),
        "score_rank": market_profile.get("score_rank"),
        "selection_order": market_profile.get("selection_order"),
        "ledger_metric_ids": [
            str(item) for item in market_profile.get("ledger_metric_ids", []) or [] if item
        ],
        "weak_dimensions": weak_projections,
        "selected_ledger_metric_ids": list(dict.fromkeys(selected_ids)),
        "selection_counts": selection_counts,
    }
