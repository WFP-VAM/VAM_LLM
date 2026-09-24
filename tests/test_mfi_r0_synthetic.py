"""Phase R0 self-tests for the synthetic assessment generator.

The generator underpins every later regression suite, so it is verified against an
independent analytic oracle rather than against recorded output. If these fail, no
downstream R0 assertion can be trusted.
"""

from __future__ import annotations

import pytest

from app.services.mfi_drafter.methodology import (
    DISPLAY_DIMENSIONS,
    DRIVER_DEFINITIONS,
    SUBSECTION_DEFINITIONS,
)
from app.services.mfi_drafter.synthetic_fixtures import (
    DEFAULT_SPEC,
    DEFECT_EXPECTATIONS,
    SyntheticDefect,
    SyntheticSpec,
    build_csv_bytes,
    build_dataframe,
    build_loaded,
    build_profile,
    expected_dimension_scores,
    expected_overall_scores,
    expected_priority_dimensions,
)


def _codes(warnings) -> set[str]:
    collected = set()
    for warning in warnings or []:
        code = (
            warning.get("code")
            if isinstance(warning, dict)
            else getattr(warning, "code", None)
        )
        if code:
            collected.add(str(code))
    return collected


def test_default_spec_loads_without_warnings() -> None:
    loaded = build_loaded(DEFAULT_SPEC)

    assert loaded["warnings"] == []
    assert loaded["methodology_warnings"] == []
    assert loaded["excluded_market_records"] == []
    assert len(loaded["markets_data"]) == DEFAULT_SPEC.market_count


def test_stored_scores_match_the_analytic_oracle() -> None:
    """Every stored dimension score must equal ``10 * severity`` exactly.

    The generator computes stored values with the real methodology formulas, so agreement
    with the independent oracle proves the severity model and the formulas agree.
    """
    loaded = build_loaded(DEFAULT_SPEC)
    expected = expected_dimension_scores(DEFAULT_SPEC)
    expected_overall = expected_overall_scores(DEFAULT_SPEC)

    for market in loaded["markets_data"]:
        name = market["market_name"]
        for dimension, value in (market["dimension_scores"] or {}).items():
            assert value == pytest.approx(expected[name][dimension], abs=1e-9), (
                f"{name}/{dimension}"
            )
        assert market["overall_mfi"] == pytest.approx(expected_overall[name], abs=1e-9)


def test_priority_dimensions_match_the_selection_oracle() -> None:
    profile = build_profile(DEFAULT_SPEC).model_dump()

    assert profile["priority_dimension_names"] == expected_priority_dimensions(
        DEFAULT_SPEC
    )
    # A degenerate ladder would select every dimension and silently weaken later suites.
    assert 3 <= len(profile["priority_dimension_names"]) <= 4


def test_generator_emits_the_full_metric_population() -> None:
    """Driver rows are the point of the generator; without them most paths are dead."""
    profile = build_profile(DEFAULT_SPEC).model_dump()
    tables = profile["tables"]

    assert len(tables["subsection_rows"]) == len(SUBSECTION_DEFINITIONS)
    assert len(tables["driver_rows"]) == len(DRIVER_DEFINITIONS)
    assert {dimension["dimension"] for dimension in profile["dimensions"]} == set(
        DISPLAY_DIMENSIONS
    )


def test_full_item_coverage_produces_no_evidence_limitation() -> None:
    """With every optional item represented everywhere, nothing is partial."""
    profile = build_profile(DEFAULT_SPEC).model_dump()

    codes = {limitation["code"] for limitation in profile["limitations"]}
    assert "unavailable_explanatory_evidence" not in codes


def test_partial_item_coverage_is_classified_not_warned() -> None:
    """Fixed in R1: partial optional representation is disclosed, never warned about.

    This is the synthetic analogue of the diagnostic sample's Availability and Price
    warnings. The evidence must remain queryable per metric after the warning is gone.
    """
    spec = SyntheticSpec(item_market_ratio=0.5)
    profile = build_profile(spec).model_dump()

    assert not [
        limitation
        for limitation in profile["limitations"]
        if limitation["code"] == "unavailable_explanatory_evidence"
    ]

    partial = [
        metric
        for dimension in profile["dimensions"]
        for metric in dimension["drivers"]
        if (metric.get("availability") or {}).get("classification") == "unknown_applicability"
    ]
    assert partial, "partial optional coverage must still be classified"
    assert all(not metric["availability"]["warrants_warning"] for metric in partial)


def test_item_relevance_can_be_forced() -> None:
    """Item/category contrast must be reachable, or ranked-item tables stay empty."""
    targets = (
        "price.increase.item.cereal_food.barley",
        "price.stability.item.cereal_food.rice",
    )
    profile = build_profile(
        SyntheticSpec(unfavorable_item_metric_ids=targets)
    ).model_dump()

    relevant = {
        metric["metric_id"]
        for dimension in profile["dimensions"]
        for metric in dimension["drivers"]
        if metric.get("item_relevant")
    }
    assert set(targets) <= relevant
    for row in profile["tables"]["relevant_item_rows"]:
        assert row["values"].get("matching_category_metric_id")


def test_csv_round_trip_is_stable() -> None:
    first = build_csv_bytes(DEFAULT_SPEC)
    second = build_csv_bytes(DEFAULT_SPEC)

    assert first == second
    assert first.startswith(b"MarketName,")


def test_dataframe_has_no_missing_required_columns() -> None:
    frame = build_dataframe(DEFAULT_SPEC)

    required = {
        "MarketName",
        "Adm0Name",
        "Adm1Name",
        "LevelID",
        "DimensionName",
        "VariableName",
        "OutputValue",
        "TradersSampleSize",
        "StartDate",
        "EndDate",
    }
    assert required <= set(frame.columns)
    assert not frame["OutputValue"].isna().any()


@pytest.mark.parametrize(
    "expectation", DEFECT_EXPECTATIONS, ids=lambda item: item.kind
)
def test_each_defect_kind_is_detected(expectation) -> None:
    """Every injectable defect must surface as a rejection, warning, or limitation."""
    spec = SyntheticSpec(defects=(SyntheticDefect(kind=expectation.kind),))

    if expectation.raises_value_error:
        with pytest.raises(ValueError):
            build_loaded(spec)
        return

    loaded = build_loaded(spec)
    if expectation.methodology_codes:
        assert set(expectation.methodology_codes) <= _codes(
            loaded["methodology_warnings"]
        )
    assert len(loaded["excluded_market_records"]) == expectation.excluded_market_count

    if expectation.limitation_codes:
        profile = build_profile(spec).model_dump()
        codes = {limitation["code"] for limitation in profile["limitations"]}
        assert set(expectation.limitation_codes) <= codes
