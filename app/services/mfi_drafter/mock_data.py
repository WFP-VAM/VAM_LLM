"""Explicitly synthetic MFI data for demonstrations and tests; never a DataBridge observation."""
from __future__ import annotations

import logging
import random
from typing import Any, Dict, List

import numpy as np

from .methodology import (
    ANALYSIS_SCHEMA_VERSION,
    DRIVERS_BY_DIMENSION,
    METHODOLOGY_VERSION,
    SUBSECTIONS_BY_DIMENSION,
)
from .schemas import MFI_DIMENSIONS, MFIMetric

logger = logging.getLogger(__name__)


def generate_mock_mfi_data(
    country: str,
    markets: List[str],
    data_collection_start: str,
    data_collection_end: str
) -> Dict[str, Any]:
    """Generate explicitly synthetic data in the canonical typed evidence shape."""
    logger.info(f"[MOCK] Generating MFI data for {country} ({len(markets)} markets)")

    # Mock admin mapping (API-like enrichment)
    admin0 = country
    admin1_pool = [f"{country} - Admin1 {i + 1}" for i in range(min(3, max(1, len(markets))))]
    market_admin_map: Dict[str, Dict[str, str]] = {}
    for i, market in enumerate(markets):
        admin1 = admin1_pool[i % len(admin1_pool)]
        admin2 = f"{admin1} - Admin2 {(i % 2) + 1}"
        market_admin_map[market] = {"admin0": admin0, "admin1": admin1, "admin2": admin2}

    markets_data = []
    for market in markets:
        admin_info = market_admin_map.get(market, {"admin0": country, "admin1": "Unknown", "admin2": "Unknown"})
        region = admin_info["admin1"]
        dimension_scores = {}
        subsections: Dict[str, List[Dict[str, Any]]] = {
            dimension: [] for dimension in MFI_DIMENSIONS
        }
        drivers: Dict[str, List[Dict[str, Any]]] = {
            dimension: [] for dimension in MFI_DIMENSIONS
        }

        for dim in MFI_DIMENSIONS:
            base_score = random.uniform(4.5, 9.5)
            dimension_scores[dim] = round(base_score, 1)

            quality_maximum = 8.0
            quality_measure = random.uniform(0.0, quality_maximum)
            for definition in SUBSECTIONS_BY_DIMENSION[dim]:
                if definition.metric_id == "quality.maximum":
                    raw_value = quality_maximum
                elif definition.metric_id == "quality.measure":
                    raw_value = quality_measure
                else:
                    raw_value = random.uniform(definition.raw_min, definition.raw_max)
                normalized = definition.normalize(
                    raw_value,
                    dynamic_max=quality_maximum
                    if definition.metric_id == "quality.measure"
                    else None,
                )
                subsections[dim].append(
                    MFIMetric(
                        metric_id=definition.metric_id,
                        dimension=definition.dimension,
                        display_name=definition.display_name,
                        variable_name=definition.variable_name,
                        source_level_id=definition.source_level_id,
                        source_level_name=definition.source_level_name,
                        role=definition.role,
                        raw_value=raw_value,
                        raw_min=definition.raw_min,
                        raw_max=definition.raw_max,
                        normalized_value=normalized,
                        orientation=definition.orientation,
                        unit=definition.unit,
                        evidence_scope=definition.evidence_scope,
                        observed_raw_values=[raw_value],
                        market_coverage=len(markets),
                        market_coverage_total=len(markets),
                        missing_count=0,
                        applicability_status="available",
                        validation_status="valid",
                        methodology_note=(
                            "Synthetic mock evidence for workflow demonstration only; "
                            "not a DataBridge observation."
                        ),
                        product_group=definition.product_group,
                        question_group=definition.question_group,
                        item_name=definition.item_name,
                        severity_weight=definition.severity_weight,
                    ).model_dump()
                )
            for definition in DRIVERS_BY_DIMENSION[dim]:
                raw_value = random.uniform(definition.raw_min, definition.raw_max)
                normalized = definition.normalize(raw_value)
                drivers[dim].append(
                    MFIMetric(
                        metric_id=definition.metric_id,
                        dimension=definition.dimension,
                        display_name=definition.display_name,
                        variable_name=definition.variable_name,
                        source_level_id=definition.source_level_id,
                        source_level_name=definition.source_level_name,
                        role=definition.role,
                        raw_value=raw_value,
                        raw_min=definition.raw_min,
                        raw_max=definition.raw_max,
                        normalized_value=normalized,
                        orientation=definition.orientation,
                        unit=definition.unit,
                        evidence_scope=definition.evidence_scope,
                        observed_raw_values=[raw_value],
                        market_coverage=len(markets),
                        market_coverage_total=len(markets),
                        missing_count=0,
                        applicability_status="available",
                        validation_status="valid",
                        methodology_note=(
                            "Synthetic mock evidence for workflow demonstration only; "
                            "not a DataBridge observation."
                        ),
                        product_group=definition.product_group,
                        question_group=definition.question_group,
                        item_name=definition.item_name,
                        severity_weight=definition.severity_weight,
                    ).model_dump()
                )

        overall_mfi = round(np.mean(list(dimension_scores.values())), 1)
        markets_data.append({
            "market_name": market,
            "admin0": admin_info["admin0"],
            "admin1": admin_info["admin1"],
            "admin2": admin_info["admin2"],
            "region": region,
            "overall_mfi": overall_mfi,
            "dimension_scores": dimension_scores,
            "subsections": subsections,
            "drivers": drivers,
            "traders_surveyed": random.randint(15, 30)
        })

    regions = sorted({m["region"] for m in markets_data})

    survey_metadata = {
        "country": country,
        "collection_period": f"{data_collection_start} to {data_collection_end}",
        "total_traders": sum(m["traders_surveyed"] for m in markets_data),
        "total_markets": len(markets_data),
        "regions_covered": regions
    }

    return {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "score_authority": "synthetic_mock",
        "excluded_market_records": [],
        "methodology_warnings": [],
        "warnings": [],
        "markets_data": markets_data,
        "metric_summaries": _summarize_mock_evidence(markets_data),
        "survey_metadata": survey_metadata
    }


def _summarize_mock_evidence(
    markets_data: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Summarize synthetic typed metrics with the same deterministic market mean."""
    summaries: Dict[str, List[Dict[str, Any]]] = {
        dimension: [] for dimension in MFI_DIMENSIONS
    }
    for dimension in MFI_DIMENSIONS:
        by_metric: Dict[str, List[Dict[str, Any]]] = {}
        for market in markets_data:
            for group_name in ("subsections", "drivers"):
                for metric in market[group_name].get(dimension, []):
                    by_metric.setdefault(metric["metric_id"], []).append(metric)
        for metric_id, metrics in sorted(by_metric.items()):
            raw_values = [float(metric["raw_value"]) for metric in metrics]
            normalized = [
                float(metric["normalized_value"])
                for metric in metrics
                if metric.get("normalized_value") is not None
            ]
            reference = metrics[0]
            summaries[dimension].append(
                {
                    "metric_id": metric_id,
                    "dimension": dimension,
                    "display_name": reference["display_name"],
                    "role": reference["role"],
                    "mean_raw_value": sum(raw_values) / len(raw_values),
                    "mean_normalized_value": (
                        sum(normalized) / len(normalized) if normalized else None
                    ),
                    "aggregation_numerator": sum(raw_values),
                    "aggregation_denominator": len(raw_values),
                    "available_market_count": len(raw_values),
                    "total_assessed_market_count": len(markets_data),
                    "missing_count": len(markets_data) - len(raw_values),
                    "unit": reference["unit"],
                    "orientation": reference["orientation"],
                    "evidence_scope": reference["evidence_scope"],
                    "contributing_metric_ids": [metric_id],
                    "methodology_note": reference["methodology_note"],
                }
            )
    return summaries
