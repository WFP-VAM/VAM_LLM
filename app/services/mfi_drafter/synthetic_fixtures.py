"""Parameterised synthetic Full MFI assessments for the regression harness.

Real assessment data is never committed, so every automated test that needs a complete
assessment builds one here. The generator emits official Level-1 scores, all 18 fixed
subsection components, and the full driver population, which is what makes it usable for
evidence-classification, item-relevance, and report-table work — an assessment with no
driver rows exercises none of those paths.

Scores are severity-driven: each dimension of each market gets a severity ``f`` in 0..1,
and every raw component is placed at ``raw_min + f * (raw_max - raw_min)``. Because every
dimension formula is an affine sum of its components, the resulting stored dimension score
is exactly ``10 * f``. Stored values are nevertheless computed with the real
``calculate_dimension_score`` and ``calculate_current_databridge_mfi`` so the loader's
formula validation passes, and a self-test asserts the real and analytic values agree.

Defects are injected as a post-pass over the emitted rows, so a fixture can request a
missing subsection, a duplicated metric, an out-of-range score, or a formula mismatch
without the generator's happy path knowing anything about them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional, Sequence

from .methodology import (
    DISPLAY_DIMENSIONS,
    DRIVER_DEFINITIONS,
    OFFICIAL_SCORE_DEFINITIONS,
    SUBSECTION_DEFINITIONS,
    calculate_current_databridge_mfi,
    calculate_dimension_score,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd


DefectKind = Literal[
    "missing_fixed_subsection",
    "duplicated_metric",
    "duplicate_level_1_score",
    "out_of_range_value",
    "formula_mismatch",
    "overall_formula_mismatch",
    "missing_level_1_score",
    "mfir_only_market",
    "missing_region",
    "unverified_claim",
]

# A uniform severity across dimensions makes every dimension tie, which collapses priority
# selection to all nine. The default ladder keeps the three weakest dimensions unambiguous.
DEFAULT_SEVERITY_LADDER: Mapping[str, float] = {
    "Service": 0.20,
    "Price": 0.28,
    "Infrastructure": 0.36,
    "Assortment": 0.48,
    "Resilience": 0.56,
    "Competition": 0.64,
    "Availability": 0.72,
    "Food Quality": 0.78,
    "Access & Protection": 0.84,
}

QUALITY_MAXIMUM = 8.0


@dataclass(frozen=True)
class SyntheticDefect:
    """One defect to inject into an otherwise valid synthetic assessment."""

    kind: DefectKind
    market: Optional[str] = None
    dimension: Optional[str] = None
    metric_id: Optional[str] = None
    value: Optional[float] = None


@dataclass(frozen=True)
class SyntheticSpec:
    """Declarative description of a synthetic assessment."""

    market_count: int = 6
    region_count: int = 2
    dimension_severity: Optional[Mapping[str, float]] = None
    market_severity_step: float = 0.04
    include_subsections: bool = True
    include_category_drivers: bool = True
    include_question_drivers: bool = True
    include_item_drivers: bool = True
    item_market_ratio: float = 1.0
    item_metric_ratio: float = 1.0
    item_representation: Optional[Mapping[str, float]] = None
    unfavorable_item_metric_ids: Sequence[str] = ()
    item_unfavorable_bonus: float = 0.35
    country: str = "Testland"
    start_date: str = "2026-01-01"
    end_date: str = "2026-01-31"
    traders_sample_size: int = 12
    seed: int = 20260811
    defects: Sequence[SyntheticDefect] = ()

    def market_names(self) -> list[str]:
        return [f"Market {index + 1:02d}" for index in range(self.market_count)]

    def region_for(self, market_index: int) -> str:
        return f"Region {chr(ord('A') + market_index % max(1, self.region_count))}"

    def severity(self, dimension: str, market_index: int) -> float:
        ladder = self.dimension_severity or DEFAULT_SEVERITY_LADDER
        base = float(ladder.get(dimension, 0.5))
        # Spread markets around the dimension severity so rankings and quartiles are real.
        offset = (market_index - (self.market_count - 1) / 2.0) * self.market_severity_step
        return min(1.0, max(0.0, base + offset))


DEFAULT_SPEC = SyntheticSpec()


@dataclass(frozen=True)
class DefectExpectation:
    """What a defect kind is expected to produce, so tests can be table-driven."""

    kind: DefectKind
    #: Loader rejects the upload outright rather than degrading with a warning.
    raises_value_error: bool = False
    #: Methodology warning codes the loader must emit.
    methodology_codes: tuple[str, ...] = ()
    #: Profile limitation codes the deterministic analysis must emit.
    limitation_codes: tuple[str, ...] = ()
    #: Market records the loader must exclude.
    excluded_market_count: int = 0


# An authoritative Level-1 defect is a hard rejection, not a degraded load: the loader
# raises rather than silently dropping a market whose official scores cannot be trusted.
DEFECT_EXPECTATIONS: tuple[DefectExpectation, ...] = (
    DefectExpectation(
        kind="missing_fixed_subsection", methodology_codes=("evidence_missing",)
    ),
    DefectExpectation(
        kind="duplicated_metric", methodology_codes=("evidence_duplicate",)
    ),
    DefectExpectation(kind="duplicate_level_1_score", raises_value_error=True),
    DefectExpectation(kind="out_of_range_value", raises_value_error=True),
    DefectExpectation(kind="missing_level_1_score", raises_value_error=True),
    DefectExpectation(
        kind="formula_mismatch",
        methodology_codes=("dimension_formula_mismatch", "overall_formula_mismatch"),
    ),
    DefectExpectation(
        kind="overall_formula_mismatch", methodology_codes=("overall_formula_mismatch",)
    ),
    DefectExpectation(
        kind="mfir_only_market",
        methodology_codes=("mfir_records_excluded",),
        excluded_market_count=1,
    ),
    DefectExpectation(
        kind="missing_region", limitation_codes=("incomplete_regional_coverage",)
    ),
)


def _component_values(spec: SyntheticSpec, market_index: int) -> dict[str, float]:
    """Place every fixed subsection component at its severity-scaled raw value."""
    values: dict[str, float] = {}
    for definition in SUBSECTION_DEFINITIONS:
        severity = spec.severity(definition.dimension, market_index)
        if definition.metric_id == "quality.maximum":
            values[definition.metric_id] = QUALITY_MAXIMUM
            continue
        if definition.metric_id == "quality.measure":
            values[definition.metric_id] = QUALITY_MAXIMUM * severity
            continue
        raw_min = float(definition.raw_min)
        raw_max = float(definition.raw_max)
        values[definition.metric_id] = raw_min + severity * (raw_max - raw_min)
    return values


def _driver_raw_value(
    spec: SyntheticSpec,
    definition: Any,
    market_index: int,
) -> float:
    """Return a raw driver value whose unfavorable rate tracks dimension severity."""
    severity = spec.severity(definition.dimension, market_index)
    unfavorable = 1.0 - severity
    if definition.metric_id in set(spec.unfavorable_item_metric_ids):
        unfavorable = min(1.0, unfavorable + spec.item_unfavorable_bonus)
    if definition.orientation == "higher_is_worse":
        return unfavorable
    if definition.orientation == "higher_is_better":
        return 1.0 - unfavorable
    return severity


def _represented_market_count(spec: SyntheticSpec, definition: Any, order: int) -> int:
    """Return how many markets carry rows for one optional metric."""
    override = (spec.item_representation or {}).get(definition.metric_id)
    ratio = float(override) if override is not None else float(spec.item_market_ratio)
    ratio = min(1.0, max(0.0, ratio))
    if ratio >= 1.0:
        return spec.market_count
    # Vary deterministically by definition order so coverage is not uniform across items.
    jitter = (order % 3) - 1
    return max(0, min(spec.market_count, int(math.ceil(ratio * spec.market_count)) + jitter))


def _selected_drivers(spec: SyntheticSpec) -> list[Any]:
    selected: list[Any] = []
    item_index = 0
    for definition in DRIVER_DEFINITIONS:
        role = definition.role
        if role == "category_driver" and not spec.include_category_drivers:
            continue
        if role == "question_driver" and not spec.include_question_drivers:
            continue
        if role == "item_driver":
            if not spec.include_item_drivers:
                continue
            if spec.item_metric_ratio < 1.0:
                keep = int(math.ceil(spec.item_metric_ratio * 100)) / 100.0
                if (item_index % 100) / 100.0 >= keep:
                    item_index += 1
                    continue
            item_index += 1
        selected.append(definition)
    return selected


def _row(
    *,
    market: str,
    region: str,
    level: int,
    dimension: str,
    variable: str,
    value: Any,
    spec: SyntheticSpec,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    return {
        "MarketName": market,
        "Adm0Name": spec.country,
        "Adm1Name": region,
        "Adm2Name": market,
        "MarketLatitude": latitude,
        "MarketLongitude": longitude,
        "LevelID": level,
        "DimensionName": dimension,
        "VariableName": variable,
        "OutputValue": value,
        "TradersSampleSize": spec.traders_sample_size,
        "StartDate": spec.start_date,
        "EndDate": spec.end_date,
    }


def expected_dimension_scores(spec: SyntheticSpec) -> dict[str, dict[str, float]]:
    """Analytic oracle: ``10 * severity`` for every market and dimension."""
    scores: dict[str, dict[str, float]] = {}
    for index, market in enumerate(spec.market_names()):
        scores[market] = {
            dimension: 10.0 * spec.severity(dimension, index)
            for dimension in DISPLAY_DIMENSIONS
        }
    return scores


def expected_overall_scores(spec: SyntheticSpec) -> dict[str, float]:
    overall: dict[str, float] = {}
    for market, dimensions in expected_dimension_scores(spec).items():
        value = calculate_current_databridge_mfi(
            [dimensions[name] for name in DISPLAY_DIMENSIONS]
        )
        overall[market] = float(value) if value is not None else float("nan")
    return overall


def expected_dimension_means(spec: SyntheticSpec) -> dict[str, float]:
    """Analytic mean stored score per dimension across all markets."""
    return {
        dimension: sum(
            10.0 * spec.severity(dimension, index) for index in range(spec.market_count)
        )
        / max(1, spec.market_count)
        for dimension in DISPLAY_DIMENSIONS
    }


def expected_priority_dimensions(
    spec: SyntheticSpec,
    *,
    minimum: int = 3,
    maximum: int = 4,
    tolerance: float = 1e-6,
) -> list[str]:
    """Predict the priority dimensions, mirroring the deterministic selection rule.

    A dimension qualifies when it sits in the weakest ``minimum`` ranks or falls below the
    dimension profile mean, and it is kept only while it stays within the ``maximum``-rank
    boundary. This is an independent reimplementation of
    ``analysis._select_priority_dimensions``; keeping it independent is deliberate, since a
    test oracle that called the implementation under test would assert nothing.
    """
    means = expected_dimension_means(spec)
    ordered = sorted(means.items(), key=lambda item: (item[1], item[0]))
    profile_mean = sum(means.values()) / max(1, len(means))
    minimum_boundary = ordered[minimum - 1][1]
    maximum_boundary = ordered[maximum - 1][1]
    selected = [
        name
        for name, value in ordered
        if (value <= minimum_boundary + tolerance or value < profile_mean - tolerance)
        and value <= maximum_boundary + tolerance
    ]
    return selected


def build_dataframe(spec: SyntheticSpec = DEFAULT_SPEC) -> "pd.DataFrame":
    """Build a synthetic assessment in the shape the Phase 1 loader expects."""
    import pandas as pd

    rows: list[dict[str, Any]] = []
    drivers = _selected_drivers(spec)
    representation = {
        definition.metric_id: _represented_market_count(spec, definition, order)
        for order, definition in enumerate(drivers)
        if definition.applicability_rule != "required"
    }

    for index, market in enumerate(spec.market_names()):
        region = spec.region_for(index)
        latitude = 10.0 + index * 0.05
        longitude = 20.0 + index * 0.05
        components = _component_values(spec, index)
        dimension_values = {
            dimension: calculate_dimension_score(dimension, components)
            for dimension in DISPLAY_DIMENSIONS
        }
        overall = calculate_current_databridge_mfi(
            [dimension_values[name] for name in DISPLAY_DIMENSIONS]
        )

        for definition in OFFICIAL_SCORE_DEFINITIONS:
            value = (
                overall
                if definition.dimension == "MFI"
                else dimension_values[definition.dimension]
            )
            rows.append(
                _row(
                    market=market,
                    region=region,
                    level=definition.source_level_id,
                    dimension=definition.csv_dimension,
                    variable=definition.variable_name,
                    value=value,
                    spec=spec,
                    latitude=latitude,
                    longitude=longitude,
                )
            )

        if spec.include_subsections:
            for definition in SUBSECTION_DEFINITIONS:
                rows.append(
                    _row(
                        market=market,
                        region=region,
                        level=definition.source_level_id,
                        dimension=definition.csv_dimension,
                        variable=definition.variable_name,
                        value=components[definition.metric_id],
                        spec=spec,
                        latitude=latitude,
                        longitude=longitude,
                    )
                )

        for definition in drivers:
            limit = representation.get(definition.metric_id)
            if limit is not None and index >= limit:
                continue
            rows.append(
                _row(
                    market=market,
                    region=region,
                    level=definition.source_level_id,
                    dimension=definition.csv_dimension,
                    variable=definition.variable_name,
                    value=_driver_raw_value(spec, definition, index),
                    spec=spec,
                    latitude=latitude,
                    longitude=longitude,
                )
            )

    rows = _apply_defects(rows, spec)
    return pd.DataFrame(rows)


def _apply_defects(
    rows: list[dict[str, Any]], spec: SyntheticSpec
) -> list[dict[str, Any]]:
    """Apply each requested defect as a post-pass over the emitted rows."""
    if not spec.defects:
        return rows

    markets = spec.market_names()
    by_metric = {
        definition.metric_id: definition
        for definition in (*OFFICIAL_SCORE_DEFINITIONS, *SUBSECTION_DEFINITIONS, *DRIVER_DEFINITIONS)
    }

    def _matches(row: Mapping[str, Any], definition: Any, market: str) -> bool:
        return (
            row["MarketName"] == market
            and int(row["LevelID"]) == definition.source_level_id
            and row["DimensionName"] == definition.csv_dimension
            and row["VariableName"] == definition.variable_name
        )

    for defect in spec.defects:
        market = defect.market or markets[0]

        if defect.kind == "missing_fixed_subsection":
            metric_id = defect.metric_id or "service.shopping"
            definition = by_metric[metric_id]
            rows = [row for row in rows if not _matches(row, definition, market)]
            continue

        if defect.kind in {"duplicated_metric", "duplicate_level_1_score"}:
            metric_id = defect.metric_id or (
                "price.dimension.score"
                if defect.kind == "duplicate_level_1_score"
                else "service.shopping"
            )
            definition = by_metric[metric_id]
            duplicates = [dict(row) for row in rows if _matches(row, definition, market)]
            rows.extend(duplicates)
            continue

        if defect.kind == "out_of_range_value":
            metric_id = defect.metric_id or "price.dimension.score"
            definition = by_metric[metric_id]
            for row in rows:
                if _matches(row, definition, market):
                    row["OutputValue"] = 42.0 if defect.value is None else defect.value
            continue

        if defect.kind == "formula_mismatch":
            dimension = defect.dimension or "Price"
            definition = by_metric[f"{_dimension_slug(dimension)}.dimension.score"]
            for row in rows:
                if _matches(row, definition, market):
                    row["OutputValue"] = _perturb(row["OutputValue"])
            continue

        if defect.kind == "overall_formula_mismatch":
            definition = by_metric["mfi.overall"]
            for row in rows:
                if _matches(row, definition, market):
                    row["OutputValue"] = _perturb(row["OutputValue"])
            continue

        if defect.kind == "missing_level_1_score":
            dimension = defect.dimension or "Price"
            definition = by_metric[f"{_dimension_slug(dimension)}.dimension.score"]
            rows = [row for row in rows if not _matches(row, definition, market)]
            continue

        if defect.kind == "mfir_only_market":
            extra = f"{market} MFIr"
            template = next(row for row in rows if row["MarketName"] == market)
            rows.append(
                {
                    **template,
                    "MarketName": extra,
                    "Adm2Name": extra,
                    "DimensionName": "MFI",
                    "VariableName": "MFIScoreMFIr",
                    "LevelID": 1,
                    "OutputValue": 5.0,
                }
            )
            continue

        if defect.kind == "missing_region":
            for row in rows:
                if row["MarketName"] == market:
                    row["Adm1Name"] = ""
            continue

        if defect.kind == "unverified_claim":
            # Handled by the caller when building narratives; no row-level change.
            continue

    return rows


def _dimension_slug(dimension: str) -> str:
    mapping = {
        "Assortment": "assortment",
        "Availability": "availability",
        "Price": "price",
        "Resilience": "resilience",
        "Competition": "competition",
        "Infrastructure": "infrastructure",
        "Service": "service",
        "Food Quality": "quality",
        "Access & Protection": "access_protection",
    }
    return mapping[dimension]


def _perturb(value: Any) -> float:
    try:
        current = float(value)
    except (TypeError, ValueError):
        return 1.0
    return current + 1.5 if current <= 8.5 else current - 1.5


def build_csv_bytes(spec: SyntheticSpec = DEFAULT_SPEC) -> bytes:
    """Serialise a synthetic assessment as UTF-8 CSV bytes."""
    return build_dataframe(spec).to_csv(index=False).encode("utf-8")


def build_loaded(spec: SyntheticSpec = DEFAULT_SPEC) -> dict[str, Any]:
    """Load a synthetic assessment through the real Phase 1 loader."""
    from .data_loader import load_mfi_from_dataframe

    return load_mfi_from_dataframe(build_dataframe(spec))


def build_profile(spec: SyntheticSpec = DEFAULT_SPEC) -> Any:
    """Build the Phase 2 deterministic profile for a synthetic assessment."""
    from .analysis import build_assessment_profile

    loaded = build_loaded(spec)
    return build_assessment_profile(
        loaded["markets_data"], loaded["metric_summaries"], loaded
    )
