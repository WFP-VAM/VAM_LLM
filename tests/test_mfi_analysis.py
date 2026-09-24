from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.services.mfi_drafter.analysis import build_assessment_profile
from app.services.mfi_drafter.methodology import (
    DISPLAY_DIMENSIONS,
    METRIC_DEFINITIONS_BY_ID,
)
from app.services.mfi_drafter.schemas import MFIAnalysisConfig


def _market(
    name: str,
    *,
    overall: float = 5.0,
    scores: dict[str, float] | None = None,
    region: str | None = "Region A",
) -> dict:
    dimensions = {dimension: 5.0 for dimension in DISPLAY_DIMENSIONS}
    dimensions.update(scores or {})
    return {
        "market_name": name,
        "region": region or "",
        "overall_mfi": overall,
        "dimension_scores": dimensions,
        "subsections": {dimension: [] for dimension in DISPLAY_DIMENSIONS},
        "drivers": {dimension: [] for dimension in DISPLAY_DIMENSIONS},
        "risk_level": "Low Risk",
    }


def _summary(
    metric_id: str,
    *,
    raw: float | None,
    normalized: float | None = None,
    available: int,
    total: int,
) -> dict:
    definition = METRIC_DEFINITIONS_BY_ID[metric_id]
    return {
        "metric_id": metric_id,
        "dimension": definition.dimension,
        "display_name": definition.display_name,
        "role": definition.role,
        "mean_raw_value": raw,
        "mean_normalized_value": normalized,
        "aggregation_numerator": (
            raw * available if raw is not None and available else None
        ),
        "aggregation_denominator": available,
        "available_market_count": available,
        "total_assessed_market_count": total,
        "missing_count": total - available,
        "unit": definition.unit,
        "orientation": definition.orientation,
        "evidence_scope": definition.evidence_scope,
        "contributing_metric_ids": [metric_id] if available else [],
        "methodology_note": definition.methodology_note,
    }


def _grouped(*summaries: dict) -> dict[str, list[dict]]:
    result = {dimension: [] for dimension in DISPLAY_DIMENSIONS}
    for summary in summaries:
        result[summary["dimension"]].append(summary)
    return result


def _profile(
    markets: list[dict],
    summaries: dict[str, list[dict]] | None = None,
    *,
    config: MFIAnalysisConfig | None = None,
    excluded: int = 0,
):
    return build_assessment_profile(
        markets,
        summaries or _grouped(),
        {
            "score_authority": "databridge_level_1",
            "excluded_market_records": [
                {"market_name": f"Excluded {index}"} for index in range(excluded)
            ],
        },
        config,
    )


def test_statistics_use_unweighted_linear_quartiles_without_rounding():
    markets = [
        _market(f"Market {index}", overall=value)
        for index, value in enumerate((1.0, 2.0, 3.0, 4.0), start=1)
    ]

    result = _profile(markets)
    stats = result.overall_statistics

    assert result.mean_mfi_across_assessed_markets == 2.5
    assert stats.model_dump(exclude={"coverage"}) == {
        "mean": 2.5,
        "median": 2.5,
        "minimum": 1.0,
        "maximum": 4.0,
        "q1": 1.75,
        "q3": 3.25,
        "iqr": 1.5,
        "score_range": 3.0,
        "numerator": 10.0,
        "denominator": 4,
    }
    assert stats.coverage.coverage_ratio == 1.0


def test_priority_dimensions_are_relative_and_record_both_reasons():
    dimension_values = {
        dimension: value
        for dimension, value in zip(
            DISPLAY_DIMENSIONS,
            (1.0, 2.0, 3.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0),
        )
    }

    result = _profile([_market("Only market", scores=dimension_values)])

    assert result.priority_dimension_names == list(DISPLAY_DIMENSIONS[:3])
    by_name = {profile.dimension: profile for profile in result.dimensions}
    assert by_name["Assortment"].priority_reasons == [
        "bottom_rank",
        "below_profile_mean",
    ]
    assert by_name["Availability"].profile_rank == 2
    assert by_name["Price"].profile_rank == 3
    assert by_name["Resilience"].is_priority is False


def test_dimension_boundary_ties_are_included_even_beyond_four_positions():
    values = (1.0, 2.0, 3.0, 4.0, 4.0, 9.0, 9.0, 9.0, 9.0)
    scores = dict(zip(DISPLAY_DIMENSIONS, values))

    result = _profile([_market("Tie market", scores=scores)])

    assert result.priority_dimension_names == list(DISPLAY_DIMENSIONS[:5])
    assert result.dimensions[3].profile_rank == result.dimensions[4].profile_rank


def test_market_cap_is_strict_fifteen_while_equal_scores_share_rank():
    markets = [
        _market(f"Market {index:02d}", overall=5.0)
        for index in range(16, 0, -1)
    ]

    result = _profile(markets)

    assert len(result.priority_market_names) == 15
    assert result.priority_market_names == [
        f"Market {index:02d}" for index in range(1, 16)
    ]
    assert {market.score_rank for market in result.markets} == {1}
    assert result.markets[-1].market_name == "Market 16"
    assert result.markets[-1].is_priority_market is False


def test_market_bottom_three_includes_ties_at_third_position():
    scores = dict(
        zip(
            DISPLAY_DIMENSIONS,
            (1.0, 2.0, 3.0, 3.0, 5.0, 6.0, 7.0, 8.0, 9.0),
        )
    )

    result = _profile([_market("Boundary market", scores=scores)])
    market = result.markets[0]

    assert [item.dimension for item in market.weak_dimensions] == list(
        DISPLAY_DIMENSIONS[:4]
    )
    assert market.weak_dimensions[2].rank == market.weak_dimensions[3].rank


def test_missing_region_and_excluded_mfir_records_are_explicit_limitations():
    result = _profile([_market("No region", region=None)], excluded=2)
    limitations = {limitation.code for limitation in result.limitations}

    assert "assessment_scope_not_representative" in limitations
    assert "incomplete_regional_coverage" in limitations
    assert "mfir_records_excluded" in limitations
    assert result.excluded_market_count == 2
    assert all(not dimension.regional_summaries for dimension in result.dimensions)


def test_localized_patterns_cover_regions_lowest_markets_and_full_ordering():
    markets = [
        _market(
            "North weak",
            region="North",
            scores={"Service": 1.0, "Infrastructure": 2.0},
        ),
        _market(
            "North strong",
            region="North",
            scores={"Service": 3.0, "Infrastructure": 4.0},
        ),
        _market(
            "South weak",
            region="South",
            scores={"Service": 6.0, "Infrastructure": 1.0},
        ),
    ]

    result = _profile(markets)
    service = next(
        dimension for dimension in result.dimensions if dimension.dimension == "Service"
    )

    assert service.localized_patterns.regions_where_bottom_one == ["North"]
    assert service.localized_patterns.regions_where_bottom_two == ["North"]
    assert service.localized_patterns.markets_where_lowest == [
        "North strong",
        "North weak",
    ]
    assert [
        market.name for market in service.localized_patterns.ordered_markets
    ] == ["North weak", "North strong", "South weak"]
    assert [market.rank for market in service.localized_patterns.ordered_markets] == [
        1,
        2,
        3,
    ]


def test_single_market_all_equal_scores_uses_semantic_ties():
    result = _profile([_market("All equal")])

    assert result.priority_dimension_names == list(DISPLAY_DIMENSIONS)
    assert {dimension.profile_rank for dimension in result.dimensions} == {1}
    assert {
        dimension.dimension
        for dimension in result.markets[0].weak_dimensions
    } == set(DISPLAY_DIMENSIONS)


def test_driver_unfavorable_rates_follow_orientation_and_descriptive_is_unranked():
    total = 4
    summaries = _grouped(
        _summary(
            "availability.scarcity.category.cereal_food",
            raw=0.8,
            available=total,
            total=total,
        ),
        _summary(
            "availability.scarcity.item.cereal_food.barley",
            raw=0.6,
            available=total,
            total=total,
        ),
        _summary(
            "assortment.sku_class.1_50",
            raw=0.4,
            available=total,
            total=total,
        ),
    )

    result = _profile(
        [_market(f"Market {index}") for index in range(total)],
        summaries,
    )
    metrics = {
        metric.metric_id: metric
        for dimension in result.dimensions
        for metric in dimension.drivers
    }

    assert (
        metrics["availability.scarcity.category.cereal_food"].unfavorable_rate
        == pytest.approx(0.2)
    )
    assert (
        metrics["availability.scarcity.item.cereal_food.barley"].unfavorable_rate
        == 0.6
    )
    assert metrics[
        "availability.scarcity.category.cereal_food"
    ].weakness_rank == 1
    assert metrics[
        "availability.scarcity.item.cereal_food.barley"
    ].weakness_rank == 1
    assert metrics["assortment.sku_class.1_50"].unfavorable_rate is None
    assert metrics["assortment.sku_class.1_50"].weakness_rank is None


def test_quality_retains_measure_maximum_but_ranks_question_drivers():
    total = 3
    summaries = _grouped(
        _summary(
            "quality.measure",
            raw=5.0,
            normalized=6.25,
            available=total,
            total=total,
        ),
        _summary(
            "quality.maximum",
            raw=8.0,
            available=total,
            total=total,
        ),
        _summary(
            "quality.condition.food_protection",
            raw=0.5,
            available=total,
            total=total,
        ),
        _summary(
            "quality.condition.separation",
            raw=0.8,
            available=total,
            total=total,
        ),
    )

    result = _profile(
        [_market(f"Market {index}") for index in range(total)],
        summaries,
    )
    quality = next(
        dimension for dimension in result.dimensions if dimension.dimension == "Food Quality"
    )

    assert {metric.metric_id for metric in quality.subsections} == {
        "quality.measure",
        "quality.maximum",
    }
    assert all(metric.weakness_rank is None for metric in quality.subsections)
    assert [
        metric.metric_id
        for metric in quality.drivers
        if metric.weakness_rank == 1
    ] == ["quality.condition.food_protection"]


def test_item_relevance_thresholds_and_top_three_are_deterministic():
    total = 12
    summaries = _grouped(
        _summary(
            "availability.scarcity.category.cereal_food",
            raw=0.8,
            available=total,
            total=total,
        ),
        _summary(
            "availability.scarcity.item.cereal_food.barley",
            raw=0.9,
            available=2,
            total=total,
        ),
        _summary(
            "availability.scarcity.item.cereal_food.bread",
            raw=0.299,
            available=3,
            total=total,
        ),
        _summary(
            "availability.scarcity.item.cereal_food.flour",
            raw=0.3,
            available=3,
            total=total,
        ),
        _summary(
            "availability.scarcity.item.cereal_food.maize",
            raw=0.6,
            available=3,
            total=total,
        ),
        _summary(
            "availability.scarcity.item.cereal_food.millet",
            raw=0.5,
            available=3,
            total=total,
        ),
        _summary(
            "availability.scarcity.item.cereal_food.other",
            raw=0.4,
            available=3,
            total=total,
        ),
    )

    result = _profile(
        [_market(f"Market {index}") for index in range(total)],
        summaries,
    )
    items = {
        metric.metric_id: metric
        for dimension in result.dimensions
        for metric in dimension.drivers
        if metric.role == "item_driver"
    }

    assert items["availability.scarcity.item.cereal_food.barley"].item_relevant is False
    assert items[
        "availability.scarcity.item.cereal_food.barley"
    ].relevance_reasons == ["insufficient_market_coverage"]
    assert items["availability.scarcity.item.cereal_food.bread"].item_relevant is False
    assert "insufficient_category_contrast" in items[
        "availability.scarcity.item.cereal_food.bread"
    ].relevance_reasons
    assert items["availability.scarcity.item.cereal_food.flour"].item_relevant is False
    assert "outside_group_top_limit" in items[
        "availability.scarcity.item.cereal_food.flour"
    ].relevance_reasons
    assert {
        metric.metric_id for metric in items.values() if metric.item_relevant
    } == {
        "availability.scarcity.item.cereal_food.maize",
        "availability.scarcity.item.cereal_food.millet",
        "availability.scarcity.item.cereal_food.other",
    }

    uncapped = _profile(
        [_market(f"Market {index}") for index in range(total)],
        summaries,
        config=MFIAnalysisConfig(item_max_per_group=10),
    )
    exact = next(
        metric
        for dimension in uncapped.dimensions
        for metric in dimension.drivers
        if metric.metric_id == "availability.scarcity.item.cereal_food.flour"
    )
    assert exact.item_relevant is True


def test_ledger_ids_and_table_references_are_unique_and_deterministic():
    markets = [
        _market("São Tomé", overall=4.0, region="North / East"),
        _market("Sao Tome", overall=6.0, region="North-East"),
    ]

    first = _profile(markets)
    second = _profile(deepcopy(markets))

    assert first.model_dump() == second.model_dump()
    assert len(first.metric_ledger) == len(set(first.metric_ledger))
    market_ids = [
        metric_id
        for metric_id in first.metric_ledger
        if metric_id.endswith(".mfi.stored")
    ]
    assert len(market_ids) == 2
    assert len(set(market_ids)) == 2

    for rows in first.tables.model_dump().values():
        for row in rows:
            assert set(row["ledger_metric_ids"]) <= set(first.metric_ledger)


def test_configuration_validation_rejects_invalid_thresholds():
    with pytest.raises(ValidationError):
        MFIAnalysisConfig(priority_dimension_min=5, priority_dimension_max=4)
    with pytest.raises(ValidationError):
        MFIAnalysisConfig(item_min_market_ratio=1.1)
    with pytest.raises(ValidationError):
        MFIAnalysisConfig(ranking_tie_tolerance=-1)


def test_unavailable_evidence_degrades_to_limitations_without_fabricated_values():
    summary = _summary(
        "service.shopping",
        raw=None,
        normalized=None,
        available=0,
        total=1,
    )

    result = _profile([_market("Missing evidence")], _grouped(summary))
    service = next(
        dimension for dimension in result.dimensions if dimension.dimension == "Service"
    )
    metric = service.subsections[0]

    assert metric.mean_raw_value is None
    assert metric.mean_normalized_value is None
    assert metric.weakness_rank is None
    assert metric.coverage.available_market_count == 0
    assert metric.coverage.coverage_ratio == 0.0
    limitation = next(
        limitation
        for limitation in result.limitations
        if limitation.code == "unavailable_explanatory_evidence"
        and limitation.dimension == "Service"
    )
    assert limitation.metric_ids == ["service.shopping"]


def test_analysis_does_not_mutate_authoritative_inputs():
    markets = [_market("Immutable", overall=4.123456789)]
    original = deepcopy(markets)

    result = _profile(markets)

    assert markets == original
    assert result.markets[0].overall_mfi == 4.123456789
    assert result.mean_mfi_across_assessed_markets == 4.123456789


