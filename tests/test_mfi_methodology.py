from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.mfi_drafter.methodology import (
    ALL_METRIC_DEFINITIONS,
    DRIVER_DEFINITIONS,
    METRIC_DEFINITIONS_BY_ID,
    OFFICIAL_SCORE_DEFINITIONS,
    SCORE_VALIDATION_ABS_TOLERANCE,
    SUBSECTION_DEFINITIONS,
    calculate_current_databridge_mfi,
    calculate_dimension_score,
    within_score_tolerance,
)

# The loader matches CSV rows on these exact (level, dimension, variable) keys.
_BY_KEY = {definition.key: definition for definition in ALL_METRIC_DEFINITIONS}


def definition_for(level_id, dimension_name, variable_name):
    return _BY_KEY.get((level_id, dimension_name, variable_name))


def test_registry_has_authoritative_counts_and_unique_exact_keys():
    assert len(OFFICIAL_SCORE_DEFINITIONS) == 10
    assert len(SUBSECTION_DEFINITIONS) == 18
    assert len(DRIVER_DEFINITIONS) == 187
    assert len(ALL_METRIC_DEFINITIONS) == 215
    assert len(_BY_KEY) == len(ALL_METRIC_DEFINITIONS)
    assert len({definition.metric_id for definition in ALL_METRIC_DEFINITIONS}) == 215
    assert all(definition.source_level_name for definition in ALL_METRIC_DEFINITIONS)


def test_registry_and_definitions_are_immutable():
    definition = ALL_METRIC_DEFINITIONS[0]
    with pytest.raises(TypeError):
        METRIC_DEFINITIONS_BY_ID[definition.metric_id] = definition
    with pytest.raises(FrozenInstanceError):
        definition.display_name = "Changed"


def test_substring_collision_between_category_and_item_is_impossible():
    category = definition_for(5, "Availability", "AvailabilityScarcity_FCer")
    item = definition_for(5, "Availability", "AvailabilityScarcity_FCerBarley")

    assert category is not None and item is not None
    assert category.metric_id == "availability.scarcity.category.cereal_food"
    assert item.metric_id == "availability.scarcity.item.cereal_food.barley"
    assert category.key != item.key


def test_registered_levels_ranges_and_polarities_are_explicit():
    assert {definition.source_level_id for definition in OFFICIAL_SCORE_DEFINITIONS} == {1}
    assert all(
        definition.raw_min == 0 and definition.raw_max == 10
        for definition in OFFICIAL_SCORE_DEFINITIONS
    )
    assert {definition.source_level_id for definition in SUBSECTION_DEFINITIONS} == {
        3,
        4,
        6,
    }
    assert all(definition.raw_min <= definition.raw_max for definition in ALL_METRIC_DEFINITIONS)

    availability_category = definition_for(
        5, "Availability", "AvailabilityScarcity_FCer"
    )
    availability_item = definition_for(
        5, "Availability", "AvailabilityScarcity_FCerBarley"
    )
    price_category = definition_for(5, "Price", "PriceIncrease_FCer")
    price_item = definition_for(5, "Price", "PriceIncrease_FCerBarley")
    competition = definition_for(6, "Competition", "CompetitionLess_FCer")

    assert availability_category.orientation == "higher_is_better"
    assert availability_item.orientation == "higher_is_worse"
    assert price_category.orientation == "higher_is_better"
    assert price_item.orientation == "higher_is_worse"
    assert competition.orientation == "higher_is_better"


@pytest.mark.parametrize(
    "definition",
    [
        definition
        for definition in ALL_METRIC_DEFINITIONS
        if definition.normalization != "none"
        and definition.normalization != "quality_ratio"
    ],
    ids=lambda definition: definition.metric_id,
)
def test_every_fixed_normalization_at_minimum_midpoint_and_maximum(definition):
    minimum = definition.raw_min
    midpoint = (definition.raw_min + definition.raw_max) / 2
    maximum = definition.raw_max

    values = [
        definition.normalize(minimum),
        definition.normalize(midpoint),
        definition.normalize(maximum),
    ]

    if definition.normalization == "identity_0_10":
        expected = [minimum, midpoint, maximum]
    else:
        expected = [0.0, 5.0, 10.0]
    assert values == pytest.approx(expected)


@pytest.mark.parametrize("dynamic_maximum", [2.0, 4.0, 8.0])
def test_dynamic_quality_normalization_at_minimum_midpoint_and_maximum(
    dynamic_maximum,
):
    definition = next(
        definition
        for definition in SUBSECTION_DEFINITIONS
        if definition.metric_id == "quality.measure"
    )

    assert definition.normalize(0.0, dynamic_max=dynamic_maximum) == 0.0
    assert definition.normalize(
        dynamic_maximum / 2, dynamic_max=dynamic_maximum
    ) == 5.0
    assert definition.normalize(
        dynamic_maximum, dynamic_max=dynamic_maximum
    ) == 10.0
    assert definition.normalize(1.0, dynamic_max=0.0) is None


@pytest.mark.parametrize(
    ("dimension", "components", "expected"),
    [
        (
            "Assortment",
            {"assortment.breadth": 5.0, "assortment.depth": 3.0},
            6.0,
        ),
        (
            "Availability",
            {"availability.scarcity": 3.0, "availability.runout": 3.0},
            5.0,
        ),
        (
            "Price",
            {"price.increase": 4.0, "price.stability": 2.0},
            5.0,
        ),
        (
            "Resilience",
            {
                "resilience.responsiveness": 2.0,
                "resilience.vulnerability": 2.0,
            },
            5.0,
        ),
        (
            "Competition",
            {
                "competition.concentration": 3.0,
                "competition.monopoly": 3.0,
            },
            5.0,
        ),
        (
            "Infrastructure",
            {
                "infrastructure.condition": 6.0,
                "infrastructure.features": 1.0,
            },
            5.0,
        ),
        (
            "Service",
            {"service.shopping": 1.5, "service.checkout": 1.5},
            5.0,
        ),
        (
            "Food Quality",
            {"quality.measure": 3.0, "quality.maximum": 6.0},
            5.0,
        ),
        (
            "Access & Protection",
            {
                "access_protection.access": 3.0,
                "access_protection.protection": 3.0,
            },
            5.0,
        ),
    ],
)
def test_dimension_validation_formulas(dimension, components, expected):
    assert calculate_dimension_score(dimension, components) == pytest.approx(
        expected, abs=SCORE_VALIDATION_ABS_TOLERANCE
    )


def test_current_overall_formula_uses_alpha_and_beta_one_half():
    dimensions = [8.0, 7.0, 6.0, 5.0, 4.0, 8.0, 7.0, 6.0, 5.0]
    mean_score = sum(dimensions) / 9
    minimum = min(dimensions)
    expected = mean_score - 0.5 * (
        ((mean_score - minimum) ** 2 + 0.5**2) ** 0.5 - 0.5
    )

    actual = calculate_current_databridge_mfi(dimensions)

    assert actual == pytest.approx(expected, abs=SCORE_VALIDATION_ABS_TOLERANCE)
    assert within_score_tolerance(actual, expected)
    assert not within_score_tolerance(
        actual + SCORE_VALIDATION_ABS_TOLERANCE * 2, expected
    )
