"""Pure deterministic Phase 2 analysis for authoritative Full MFI evidence.

This module performs no I/O and makes no LLM calls.  It reads the canonical
Phase 1 market/evidence shape and returns a new, detached Pydantic profile.
Official Level-1 scores are inputs only: they are never rounded, recalculated,
or replaced here.
"""
from __future__ import annotations

from math import ceil
import re
from statistics import median
import unicodedata
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel

from .identity import context_token as _context_token
from .methodology import (
    ANALYSIS_SCHEMA_VERSION,
    DISPLAY_DIMENSIONS,
    METRIC_DEFINITIONS_BY_ID,
    METHODOLOGY_VERSION,
    SCORE_AUTHORITY,
)
from .schemas import (
    MFIAnalysisConfig,
    MFIAnalyzedMetric,
    MFIAssessmentProfile,
    MFICoverageSummary,
    MFIDeterministicTableRow,
    MFIDeterministicTables,
    MFIDimensionProfile,
    MFIEvidenceAvailability,
    MFILedgerSemantics,
    MFILimitation,
    MFILocalizedPatterns,
    MFIMarketDimensionProfile,
    MFIMarketProfile,
    MFIMetricLedgerEntry,
    MFIRegionalDimensionSummary,
    MFIRankedValue,
    MFIStatisticalSummary,
)

ANALYSIS_VERSION = "mfi-analysis-phase2-v1"
_DIMENSION_ORDER = {name: index for index, name in enumerate(DISPLAY_DIMENSIONS)}
_APPLICABILITY_RULES = {
    "required",
    "optional_product_group",
    "optional_item",
    "quality_applicability",
}

# Statistics whose semantics are fixed regardless of the metric's evidence scope.
#
# This tier exists because ``_materialize_analyzed_metric`` stamps one inherited
# ``evidence_scope`` onto all of a metric's ledger entries, so a coverage ratio or a rank
# can arrive carrying "surveyed_traders_in_market" even though its denominator is markets.
# Resolving by statistic first corrects that without mutating ``evidence_scope``, which
# other layers still read.
_STATISTIC_SEMANTICS: Mapping[str, tuple[str, str]] = {
    "coverage": ("coverage", "market_level"),
    "coverage_ratio": ("coverage", "market_level"),
    "rank": ("rank", "descriptive"),
    "rank_lowest_first": ("rank", "descriptive"),
    "count": ("count", "descriptive"),
    "denominator": ("count", "market_level"),
}

# Unweighted statistics computed over per-market official scores. ``statistic`` carries
# which one; the aggregation enum records only that the population is markets.
_CROSS_MARKET_STATISTICS = frozenset(
    {
        "mean",
        "median",
        "minimum",
        "maximum",
        "q1",
        "q3",
        "iqr",
        "range",
        "numerator",
    }
)

# Assessment-level aggregates of per-market metric values.
_METRIC_AGGREGATE_STATISTICS = frozenset(
    {
        "mean_raw_value",
        "mean_normalized_value",
        "derived_unfavorable_rate",
    }
)

# Values describing exactly one market.
_SINGLE_MARKET_STATISTICS = frozenset(
    {
        "stored_level_1_score",
        "market_explanatory_raw_value",
        "market_explanatory_normalized_value",
        "derived_market_unfavorable_rate",
    }
)

_TRADER_SCOPE = "surveyed_traders_in_market"

_CROSS_MARKET_PHRASES: Mapping[str, str] = {
    "mean": "the unweighted mean across assessed markets",
    "median": "the median across assessed markets",
    "minimum": "the lowest value observed among assessed markets",
    "maximum": "the highest value observed among assessed markets",
    "q1": "the first quartile across assessed markets",
    "q3": "the third quartile across assessed markets",
    "iqr": "the interquartile range across assessed markets",
    "range": "the range between the lowest and highest assessed markets",
    "numerator": "the sum of values across assessed markets",
    "denominator": "the number of assessed markets contributing to this statistic",
    "coverage": "the share of assessed markets contributing to this statistic",
}

_RANK_PHRASES: Mapping[str, str] = {
    "nine_dimension_assessment_profile": (
        "the relative weakness rank among the nine assessment dimensions"
    ),
    "nine_dimension_market_profile": (
        "the relative weakness rank among the nine dimensions in this market"
    ),
    "included_assessed_markets": "the relative weakness rank among assessed markets",
    "included_assessed_markets_in_region": (
        "the relative weakness rank among assessed markets in this region"
    ),
}

_REPRESENTATION_SUFFIXES: Mapping[str, str] = {
    # Complete coverage needs no qualifier: the base phrases already say "across assessed
    # markets", so appending one only repeats it.
    "all_assessed_markets": "",
    "represented_assessed_markets": (
        "among the assessed markets where {subject} was represented"
    ),
    "applicable_assessed_markets": (
        "among the assessed markets where {subject} was applicable"
    ),
    "incomplete_assessed_markets": (
        "among the assessed markets with usable evidence for {subject}"
    ),
}

_AVAILABILITY_TO_REPRESENTATION: Mapping[str, str] = {
    "complete": "all_assessed_markets",
    "partial_optional": "represented_assessed_markets",
    "unknown_applicability": "incomplete_assessed_markets",
    "not_applicable": "applicable_assessed_markets",
    "partial_required": "incomplete_assessed_markets",
    "unusable_required": "incomplete_assessed_markets",
}


def build_assessment_profile(
    markets_data: Sequence[Mapping[str, Any] | BaseModel],
    metric_summaries: Mapping[str, Sequence[Mapping[str, Any] | BaseModel]],
    metadata: Optional[Mapping[str, Any]] = None,
    config: Optional[MFIAnalysisConfig | Mapping[str, Any]] = None,
) -> MFIAssessmentProfile:
    """Build the complete deterministic assessment profile.

    Args:
        markets_data: Included, complete Full MFI markets from the Phase 1 loader.
        metric_summaries: Phase 1 deterministic typed-evidence summaries.
        metadata: Assessment context and optional exclusion/methodology metadata.
        config: Validated analysis thresholds.  Defaults are the Phase 2 contract.

    Returns:
        A detached :class:`MFIAssessmentProfile`.

    Raises:
        ValueError: If authoritative market identities or Level-1 scores are
            incomplete, duplicated, non-numeric, or outside 0-10.
    """
    analysis_config = _coerce_config(config)
    context = dict(metadata or {})
    markets = [_as_dict(market) for market in markets_data]
    _validate_authoritative_markets(markets)
    assessed_count = len(markets)

    summaries = _flatten_metric_summaries(metric_summaries)
    ledger: dict[str, MFIMetricLedgerEntry] = {}
    limitations: list[MFILimitation] = []

    def add_ledger(
        ledger_id: str,
        *,
        label: str,
        value: float | int,
        statistic: str,
        unit: str,
        orientation: str,
        evidence_scope: str,
        dimension: Optional[str] = None,
        market_name: Optional[str] = None,
        region: Optional[str] = None,
        coverage: Optional[MFICoverageSummary] = None,
        source_metric_ids: Optional[Iterable[str]] = None,
        availability: Optional[MFIEvidenceAvailability] = None,
        subject_label: Optional[str] = None,
        semantics: Optional[MFILedgerSemantics] = None,
    ) -> str:
        if ledger_id in ledger:
            raise ValueError(f"Duplicate MFI metric-ledger ID: {ledger_id}")
        derived = _ledger_semantics(
            statistic=statistic,
            unit=unit,
            evidence_scope=evidence_scope,
            market_name=market_name,
            region=region,
            availability=availability,
            subject_label=subject_label,
        )
        if semantics is not None:
            # Field-by-field so a call site can correct one aspect without restating
            # the rest of the derivation.
            derived = derived.model_copy(
                update=semantics.model_dump(exclude_none=True)
            )
        ledger[ledger_id] = MFIMetricLedgerEntry(
            ledger_id=ledger_id,
            label=label,
            value=float(value),
            statistic=statistic,
            unit=unit,
            orientation=orientation,
            evidence_scope=evidence_scope,
            dimension=dimension,
            market_name=market_name,
            region=region,
            coverage=coverage,
            source_metric_ids=sorted(set(source_metric_ids or [])),
            aggregation_method=derived.aggregation_method,
            population_basis=derived.population_basis,
            pooled_denominator_available=bool(derived.pooled_denominator_available),
            representation_basis=derived.representation_basis,
            permitted_subject_phrase=derived.permitted_subject_phrase or "",
        )
        return ledger_id

    full_coverage = _coverage(assessed_count, assessed_count)
    overall_values = [float(market["overall_mfi"]) for market in markets]
    overall_statistics = _statistics(overall_values, assessed_count)
    _add_statistic_ledger(
        add_ledger,
        "assessment.mfi",
        "Overall MFI across assessed markets",
        overall_statistics,
        unit="score",
        orientation="higher_is_better",
        evidence_scope="included_assessed_markets",
        source_metric_ids=["mfi.overall"],
    )

    dimension_statistics: dict[str, MFIStatisticalSummary] = {}
    dimension_scores: list[tuple[str, float]] = []
    dimension_stat_ids: dict[str, list[str]] = {}
    for dimension in DISPLAY_DIMENSIONS:
        values = [
            float(market["dimension_scores"][dimension])
            for market in markets
        ]
        statistics = _statistics(values, assessed_count)
        dimension_statistics[dimension] = statistics
        dimension_scores.append((dimension, statistics.mean))
        dimension_stat_ids[dimension] = _add_statistic_ledger(
            add_ledger,
            f"assessment.dimension.{_slug(dimension)}",
            f"{dimension} across assessed markets",
            statistics,
            unit="score",
            orientation="higher_is_better",
            evidence_scope="included_assessed_markets",
            dimension=dimension,
            source_metric_ids=[_official_metric_id(dimension)],
        )

    dimension_profile_mean = (
        sum(score for _, score in dimension_scores) / len(dimension_scores)
    )
    profile_mean_id = add_ledger(
        "assessment.dimension_profile.mean",
        label="Mean of the nine assessment dimension means",
        value=dimension_profile_mean,
        statistic="mean",
        unit="score",
        orientation="higher_is_better",
        evidence_scope="nine_dimension_assessment_profile",
        coverage=full_coverage,
        source_metric_ids=[_official_metric_id(d) for d in DISPLAY_DIMENSIONS],
        # This mean averages the nine dimension means, not the assessed markets, so the
        # market-population derivation would describe the wrong denominator.
        semantics=MFILedgerSemantics(
            permitted_subject_phrase=(
                "the mean of the nine dimension means across assessed markets"
            ),
        ),
    )

    dimension_ranked = _rank_records(
        [
            {"name": dimension, "value": score}
            for dimension, score in dimension_scores
        ],
        tolerance=analysis_config.ranking_tie_tolerance,
        unfavorable_first=True,
        stable_key=lambda record: _DIMENSION_ORDER[str(record["name"])],
    )
    dimension_rank_by_name = {
        str(record["name"]): (int(record["rank"]), int(record["selection_order"]))
        for record in dimension_ranked
    }
    priority_dimensions, priority_reasons = _select_priority_dimensions(
        dimension_ranked,
        profile_mean=dimension_profile_mean,
        config=analysis_config,
    )
    dimension_rank_ids: dict[str, str] = {}
    for dimension, (rank, _) in dimension_rank_by_name.items():
        dimension_rank_ids[dimension] = add_ledger(
            f"assessment.dimension.{_slug(dimension)}.rank",
            label=f"{dimension} relative weakness rank",
            value=rank,
            statistic="rank_lowest_first",
            unit="rank",
            orientation="higher_is_better",
            evidence_scope="nine_dimension_assessment_profile",
            dimension=dimension,
            coverage=full_coverage,
            source_metric_ids=[_official_metric_id(dimension)],
        )

    regions: dict[str, list[dict[str, Any]]] = {}
    missing_region_markets: list[str] = []
    for market in markets:
        region = str(market.get("region") or "").strip()
        if region:
            regions.setdefault(region, []).append(market)
        else:
            missing_region_markets.append(str(market["market_name"]))
    if missing_region_markets:
        add_ledger(
            "assessment.limitation.missing_region.count",
            label="Assessed markets without a region identifier",
            value=len(missing_region_markets),
            statistic="count",
            unit="count",
            orientation="descriptive",
            evidence_scope="included_assessed_markets",
            coverage=full_coverage,
            source_metric_ids=[],
            semantics=MFILedgerSemantics(
                permitted_subject_phrase=(
                    "the number of assessed markets without a region identifier"
                ),
            ),
        )
        limitations.append(
            MFILimitation(
                code="incomplete_regional_coverage",
                message=(
                    f"{len(missing_region_markets)} assessed market"
                    f"{'s have' if len(missing_region_markets) != 1 else ' has'} no "
                    "region identifier and is excluded from regional summaries."
                ),
                metric_ids=[],
            )
        )

    (
        regional_by_dimension,
        regional_dimension_ranks,
        regional_table_rows,
    ) = _build_regional_profiles(
        regions,
        assessed_count,
        analysis_config,
        add_ledger,
    )

    analyzed_subsections: dict[str, list[MFIAnalyzedMetric]] = {}
    analyzed_drivers: dict[str, list[MFIAnalyzedMetric]] = {}
    metric_table_rows: dict[str, list[MFIDeterministicTableRow]] = {
        "subsection": [],
        "driver": [],
        "relevant_item": [],
    }
    unavailable_by_dimension: dict[str, list[str]] = {}
    for dimension in DISPLAY_DIMENSIONS:
        dimension_summaries = summaries.get(dimension, [])
        subsections, drivers, unavailable = _analyze_evidence(
            dimension=dimension,
            summaries=dimension_summaries,
            assessed_count=assessed_count,
            config=analysis_config,
            add_ledger=add_ledger,
        )
        analyzed_subsections[dimension] = subsections
        analyzed_drivers[dimension] = drivers
        if unavailable:
            unavailable_by_dimension[dimension] = unavailable
        metric_table_rows["subsection"].extend(
            _metric_table_row("subsection", metric) for metric in subsections
        )
        metric_table_rows["driver"].extend(
            _metric_table_row("driver", metric) for metric in drivers
        )
        metric_table_rows["relevant_item"].extend(
            _metric_table_row("relevant_item", metric)
            for metric in drivers
            if metric.item_relevant
        )

    for dimension in DISPLAY_DIMENSIONS:
        unavailable = unavailable_by_dimension.get(dimension)
        if unavailable:
            add_ledger(
                (
                    "assessment.limitation.unavailable_evidence."
                    f"{_slug(dimension)}.count"
                ),
                label=f"{dimension} unusable required metrics",
                value=len(unavailable),
                statistic="count",
                unit="count",
                orientation="descriptive",
                evidence_scope="included_assessed_markets",
                dimension=dimension,
                coverage=full_coverage,
                source_metric_ids=sorted(unavailable),
                semantics=MFILedgerSemantics(
                    permitted_subject_phrase=(
                        "the number of required metrics in this dimension without "
                        "usable assessment evidence"
                    ),
                ),
            )
            limitations.append(
                MFILimitation(
                    code="unavailable_explanatory_evidence",
                    message=(
                        f"{dimension} has {len(unavailable)} required subsection or "
                        "driver metric(s) without usable assessment evidence."
                    ),
                    dimension=dimension,
                    metric_ids=sorted(unavailable),
                )
            )

    # Partial representation of optional items is normal and does not affect the
    # authoritative Level-1 score, so it is disclosed as coverage rather than warned
    # about. The ledger entry keeps it citable by the narrative layer.
    for dimension in DISPLAY_DIMENSIONS:
        partial_optional = sorted(
            metric.metric_id
            for metric in analyzed_drivers.get(dimension, [])
            if metric.availability is not None
            and metric.availability.classification in {"partial_optional", "unknown_applicability"}
            and metric.availability.applicability_rule in {"optional_item", "optional_product_group"}
            and metric.coverage.available_market_count > 0
        )
        if not partial_optional:
            continue
        add_ledger(
            f"assessment.coverage.partial_optional_items.{_slug(dimension)}.count",
            label=f"{dimension} optional items with incomplete evidence and uncertain applicability",
            value=len(partial_optional),
            statistic="count",
            unit="count",
            orientation="descriptive",
            evidence_scope="included_assessed_markets",
            dimension=dimension,
            coverage=full_coverage,
            source_metric_ids=partial_optional,
            semantics=MFILedgerSemantics(
                permitted_subject_phrase=(
                    "the number of optional items in this dimension with usable evidence in only "
                    "some assessed markets; applicability elsewhere is unknown"
                ),
            ),
        )

    if any(
        metric.role == "item_driver" and metric.coverage.available_market_count
        for metrics in analyzed_drivers.values()
        for metric in metrics
    ):
        limitations.append(
            MFILimitation(
                code="item_trader_denominator_unavailable",
                message=(
                    "Item eligibility uses assessed-market coverage. The processed "
                    "data do not provide reliable item-specific trader denominators."
                ),
            )
        )

    market_profiles, market_table_rows = _build_market_profiles(
        markets,
        assessed_count,
        analysis_config,
        add_ledger,
    )
    _add_priority_market_evidence_ledger(
        markets,
        market_profiles,
        assessed_count,
        add_ledger,
    )
    priority_market_names = [
        profile.market_name for profile in market_profiles if profile.is_priority_market
    ]

    localized_by_dimension = _build_localized_patterns(
        markets,
        regional_dimension_ranks,
        dimension_statistics,
        analysis_config,
        ledger,
    )

    dimensions: list[MFIDimensionProfile] = []
    dimension_table_rows: list[MFIDeterministicTableRow] = []
    for dimension in DISPLAY_DIMENSIONS:
        rank, order = dimension_rank_by_name[dimension]
        is_priority = dimension in priority_dimensions
        metric_ids = [
            *dimension_stat_ids[dimension],
            dimension_rank_ids[dimension],
            profile_mean_id,
        ]
        dimensions.append(
            MFIDimensionProfile(
                dimension=dimension,
                statistics=dimension_statistics[dimension],
                profile_rank=rank,
                selection_order=order,
                is_priority=is_priority,
                priority_reasons=priority_reasons.get(dimension, []),
                subsections=analyzed_subsections[dimension],
                drivers=analyzed_drivers[dimension],
                regional_summaries=regional_by_dimension[dimension],
                localized_patterns=localized_by_dimension[dimension],
                ledger_metric_ids=metric_ids,
            )
        )
        dimension_table_rows.append(
            MFIDeterministicTableRow(
                row_id=f"dimension.{_slug(dimension)}",
                values={
                    "dimension": dimension,
                    "mean": dimension_statistics[dimension].mean,
                    "median": dimension_statistics[dimension].median,
                    "minimum": dimension_statistics[dimension].minimum,
                    "maximum": dimension_statistics[dimension].maximum,
                    "q1": dimension_statistics[dimension].q1,
                    "q3": dimension_statistics[dimension].q3,
                    "iqr": dimension_statistics[dimension].iqr,
                    "range": dimension_statistics[dimension].score_range,
                    "rank": rank,
                    "is_priority": is_priority,
                    "priority_reasons": priority_reasons.get(dimension, []),
                },
                ledger_metric_ids=metric_ids,
            )
        )

    excluded_count = _excluded_count(context)
    if excluded_count:
        add_ledger(
            "assessment.excluded_market_records.count",
            label="Excluded MFIr-only market records",
            value=excluded_count,
            statistic="count",
            unit="count",
            orientation="descriptive",
            evidence_scope="assessment_input_records",
            coverage=None,
            source_metric_ids=[],
            semantics=MFILedgerSemantics(
                permitted_subject_phrase=(
                    "the number of MFIr-only market records excluded from the assessment"
                ),
            ),
        )
        limitations.append(
            MFILimitation(
                code="mfir_records_excluded",
                message=(
                    f"{excluded_count} MFIr-only market record"
                    f"{'s were' if excluded_count != 1 else ' was'} excluded from "
                    "the Full MFI assessment profile."
                ),
            )
        )
    limitations.insert(
        0,
        MFILimitation(
            code="assessment_scope_not_representative",
            severity="info",
            message=(
                "All summaries are unweighted descriptions of included assessed "
                "markets and are not population-representative estimates."
            ),
        ),
    )
    limitations = _deduplicate_limitations(limitations)

    tables = MFIDeterministicTables(
        dimension_rows=dimension_table_rows,
        regional_rows=regional_table_rows,
        subsection_rows=sorted(
            metric_table_rows["subsection"], key=lambda row: row.row_id
        ),
        driver_rows=sorted(metric_table_rows["driver"], key=lambda row: row.row_id),
        relevant_item_rows=sorted(
            metric_table_rows["relevant_item"], key=lambda row: row.row_id
        ),
        priority_market_rows=market_table_rows,
    )
    _validate_table_ledger_references(tables, ledger)

    priority_dimension_names = [
        str(record["name"])
        for record in dimension_ranked
        if str(record["name"]) in priority_dimensions
    ]
    profile = MFIAssessmentProfile(
        analysis_schema_version=ANALYSIS_SCHEMA_VERSION,
        analysis_version=ANALYSIS_VERSION,
        methodology_version=str(
            context.get("methodology_version") or METHODOLOGY_VERSION
        ),
        score_authority=str(context.get("score_authority") or SCORE_AUTHORITY),
        assessed_market_count=assessed_count,
        excluded_market_count=excluded_count,
        mean_mfi_across_assessed_markets=overall_statistics.mean,
        overall_statistics=overall_statistics,
        dimension_profile_mean=dimension_profile_mean,
        dimensions=dimensions,
        markets=market_profiles,
        priority_dimension_names=priority_dimension_names,
        priority_market_names=priority_market_names,
        limitations=limitations,
        metric_ledger=dict(sorted(ledger.items())),
        tables=tables,
    )
    from .facts import enrich_profile
    return MFIAssessmentProfile.model_validate(enrich_profile(profile.model_dump(), markets, context))


def _coerce_config(
    config: Optional[MFIAnalysisConfig | Mapping[str, Any]],
) -> MFIAnalysisConfig:
    if config is None:
        return MFIAnalysisConfig()
    if isinstance(config, MFIAnalysisConfig):
        return config
    return MFIAnalysisConfig.model_validate(config)


def _as_dict(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return dict(value)


def _validate_authoritative_markets(markets: Sequence[Mapping[str, Any]]) -> None:
    if not markets:
        raise ValueError("At least one complete Full MFI market is required")
    names: set[str] = set()
    for market in markets:
        market_name = str(market.get("market_name") or "").strip()
        if not market_name:
            raise ValueError("Every assessed market requires a market_name")
        normalized_name = _normalized_name(market_name)
        if normalized_name in names:
            raise ValueError(f"Duplicate assessed market name: {market_name}")
        names.add(normalized_name)
        _validated_score(market.get("overall_mfi"), f"{market_name} MFIScoreMFI")
        dimension_scores = market.get("dimension_scores")
        if not isinstance(dimension_scores, Mapping):
            raise ValueError(f"{market_name} has no authoritative dimension_scores")
        missing = [
            dimension
            for dimension in DISPLAY_DIMENSIONS
            if dimension not in dimension_scores
        ]
        if missing:
            raise ValueError(
                f"{market_name} is missing authoritative dimension score(s): "
                + ", ".join(missing)
            )
        for dimension in DISPLAY_DIMENSIONS:
            _validated_score(
                dimension_scores[dimension],
                f"{market_name} {dimension}",
            )


def _validated_score(value: Any, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} is missing or non-numeric")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is missing or non-numeric") from exc
    if not 0.0 <= score <= 10.0:
        raise ValueError(f"{label} is outside 0-10")
    return score


def _coverage(available: int, total: int) -> MFICoverageSummary:
    return MFICoverageSummary(
        available_market_count=int(available),
        total_assessed_market_count=int(total),
        missing_count=max(int(total) - int(available), 0),
        coverage_ratio=float(available) / float(total) if total else 0.0,
    )


def _statistics(values: Sequence[float], total: int) -> MFIStatisticalSummary:
    if not values:
        raise ValueError("Statistical summaries require at least one value")
    numeric = sorted(float(value) for value in values)
    q1 = _linear_quantile(numeric, 0.25)
    q3 = _linear_quantile(numeric, 0.75)
    numerator = sum(numeric)
    return MFIStatisticalSummary(
        mean=numerator / len(numeric),
        median=float(median(numeric)),
        minimum=numeric[0],
        maximum=numeric[-1],
        q1=q1,
        q3=q3,
        iqr=q3 - q1,
        score_range=numeric[-1] - numeric[0],
        numerator=numerator,
        denominator=len(numeric),
        coverage=_coverage(len(numeric), total),
    )


def _linear_quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("Quantiles require at least one value")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * float(probability)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return float(sorted_values[lower]) + (
        float(sorted_values[upper]) - float(sorted_values[lower])
    ) * fraction


def _add_statistic_ledger(
    add_ledger: Any,
    prefix: str,
    label: str,
    statistics: MFIStatisticalSummary,
    *,
    unit: str,
    orientation: str,
    evidence_scope: str,
    dimension: Optional[str] = None,
    market_name: Optional[str] = None,
    region: Optional[str] = None,
    source_metric_ids: Optional[Iterable[str]] = None,
) -> list[str]:
    values = (
        ("mean", statistics.mean),
        ("median", statistics.median),
        ("minimum", statistics.minimum),
        ("maximum", statistics.maximum),
        ("q1", statistics.q1),
        ("q3", statistics.q3),
        ("iqr", statistics.iqr),
        ("range", statistics.score_range),
        ("numerator", statistics.numerator),
        ("denominator", statistics.denominator),
        ("coverage", statistics.coverage.coverage_ratio),
    )
    return [
        add_ledger(
            f"{prefix}.{statistic}",
            label=f"{label}: {statistic}",
            value=value,
            statistic=statistic,
            unit=(
                "count"
                if statistic == "denominator"
                else "proportion"
                if statistic == "coverage"
                else unit
            ),
            orientation=(
                "descriptive" if statistic == "coverage" else orientation
            ),
            evidence_scope=evidence_scope,
            dimension=dimension,
            market_name=market_name,
            region=region,
            coverage=statistics.coverage,
            source_metric_ids=source_metric_ids,
        )
        for statistic, value in values
    ]


def _rank_records(
    records: Sequence[Mapping[str, Any]],
    *,
    tolerance: float,
    unfavorable_first: bool,
    stable_key: Any,
) -> list[dict[str, Any]]:
    ordered = sorted(
        (dict(record) for record in records),
        key=lambda record: (
            float(record["value"]) if unfavorable_first else -float(record["value"]),
            stable_key(record),
        ),
    )
    for index, record in enumerate(ordered):
        value = float(record["value"])
        strictly_better = sum(
            (
                float(other["value"]) < value - tolerance
                if unfavorable_first
                else float(other["value"]) > value + tolerance
            )
            for other in ordered
        )
        record["rank"] = strictly_better + 1
        record["selection_order"] = index + 1
    return ordered


def _select_priority_dimensions(
    ranked: Sequence[Mapping[str, Any]],
    *,
    profile_mean: float,
    config: MFIAnalysisConfig,
) -> tuple[set[str], dict[str, list[str]]]:
    tolerance = config.ranking_tie_tolerance
    minimum_boundary = float(ranked[config.priority_dimension_min - 1]["value"])
    maximum_boundary = float(ranked[config.priority_dimension_max - 1]["value"])
    selected: set[str] = set()
    reasons: dict[str, list[str]] = {}
    for record in ranked:
        name = str(record["name"])
        value = float(record["value"])
        bottom_rank = value <= minimum_boundary + tolerance
        below_mean = value < profile_mean - tolerance
        candidate = bottom_rank or below_mean
        within_cap_boundary = value <= maximum_boundary + tolerance
        if candidate and within_cap_boundary:
            selected.add(name)
            reason: list[str] = []
            if bottom_rank:
                reason.append("bottom_rank")
            if below_mean:
                reason.append("below_profile_mean")
            reasons[name] = reason
    return selected, reasons


def _build_regional_profiles(
    regions: Mapping[str, Sequence[Mapping[str, Any]]],
    assessed_count: int,
    config: MFIAnalysisConfig,
    add_ledger: Any,
) -> tuple[
    dict[str, list[MFIRegionalDimensionSummary]],
    dict[str, dict[str, int]],
    list[MFIDeterministicTableRow],
]:
    by_dimension: dict[str, list[MFIRegionalDimensionSummary]] = {
        dimension: [] for dimension in DISPLAY_DIMENSIONS
    }
    ranks: dict[str, dict[str, int]] = {}
    rows: list[MFIDeterministicTableRow] = []
    for region in sorted(regions, key=_normalized_name):
        region_markets = list(regions[region])
        region_token = _context_token(region)
        records = []
        stats_by_dimension: dict[str, MFIStatisticalSummary] = {}
        ledger_by_dimension: dict[str, list[str]] = {}
        for dimension in DISPLAY_DIMENSIONS:
            values = [
                float(market["dimension_scores"][dimension])
                for market in region_markets
            ]
            stats = _statistics(values, assessed_count)
            stats_by_dimension[dimension] = stats
            records.append({"name": dimension, "value": stats.mean})
            ledger_by_dimension[dimension] = _add_statistic_ledger(
                add_ledger,
                f"region.{region_token}.dimension.{_slug(dimension)}",
                f"{dimension} in region {region}",
                stats,
                unit="score",
                orientation="higher_is_better",
                evidence_scope="included_assessed_markets_in_region",
                dimension=dimension,
                region=region,
                source_metric_ids=[_official_metric_id(dimension)],
            )
        ranked = _rank_records(
            records,
            tolerance=config.ranking_tie_tolerance,
            unfavorable_first=True,
            stable_key=lambda record: _DIMENSION_ORDER[str(record["name"])],
        )
        ranks[region] = {}
        for record in ranked:
            dimension = str(record["name"])
            rank = int(record["rank"])
            order = int(record["selection_order"])
            ranks[region][dimension] = rank
            rank_id = add_ledger(
                f"region.{region_token}.dimension.{_slug(dimension)}.rank",
                label=f"{dimension} weakness rank in region {region}",
                value=rank,
                statistic="rank_lowest_first",
                unit="rank",
                orientation="higher_is_better",
                evidence_scope="included_assessed_markets_in_region",
                dimension=dimension,
                region=region,
                coverage=stats_by_dimension[dimension].coverage,
                source_metric_ids=[_official_metric_id(dimension)],
            )
            metric_ids = [*ledger_by_dimension[dimension], rank_id]
            summary = MFIRegionalDimensionSummary(
                region=region,
                statistics=stats_by_dimension[dimension],
                rank=rank,
                selection_order=order,
                ledger_metric_ids=metric_ids,
            )
            by_dimension[dimension].append(summary)
            rows.append(
                MFIDeterministicTableRow(
                    row_id=f"region.{region_token}.dimension.{_slug(dimension)}",
                    values={
                        "region": region,
                        "dimension": dimension,
                        "mean": summary.statistics.mean,
                        "median": summary.statistics.median,
                        "minimum": summary.statistics.minimum,
                        "maximum": summary.statistics.maximum,
                        "q1": summary.statistics.q1,
                        "q3": summary.statistics.q3,
                        "iqr": summary.statistics.iqr,
                        "range": summary.statistics.score_range,
                        "rank": summary.rank,
                        "market_count": len(region_markets),
                        "scope": "included_assessed_markets_in_region",
                    },
                    ledger_metric_ids=metric_ids,
                )
            )
    for dimension in by_dimension:
        by_dimension[dimension].sort(key=lambda summary: _normalized_name(summary.region))
    rows.sort(key=lambda row: row.row_id)
    return by_dimension, ranks, rows


def _flatten_metric_summaries(
    grouped: Mapping[str, Sequence[Mapping[str, Any] | BaseModel]],
) -> dict[str, list[dict[str, Any]]]:
    result = {dimension: [] for dimension in DISPLAY_DIMENSIONS}
    seen: set[str] = set()
    for group_dimension, values in grouped.items():
        for value in values:
            summary = _as_dict(value)
            metric_id = str(summary.get("metric_id") or "")
            if not metric_id:
                raise ValueError("Every metric summary requires a metric_id")
            if metric_id in seen:
                raise ValueError(f"Duplicate metric summary: {metric_id}")
            seen.add(metric_id)
            definition = METRIC_DEFINITIONS_BY_ID.get(metric_id)
            dimension = str(
                summary.get("dimension")
                or (definition.dimension if definition else group_dimension)
            )
            if dimension not in result:
                continue
            result[dimension].append(summary)
    for dimension in result:
        result[dimension].sort(key=lambda summary: str(summary["metric_id"]))
    return result


def _representation_basis(
    *,
    statistic: str,
    evidence_scope: str,
    availability: Optional[MFIEvidenceAvailability],
) -> str:
    """Decide which assessed markets stand behind a value.

    Coverage counts alone cannot separate an optional item that was simply not traded
    everywhere from required evidence that failed to load, and the two need different
    wording, so the R1 classification is consulted when it is available.
    """
    if evidence_scope == "nine_dimension_assessment_profile":
        return "assessment_dimension_profile"
    if evidence_scope == "assessment_input_records":
        return "assessment_input_records"
    if statistic in _SINGLE_MARKET_STATISTICS:
        return "single_assessed_market"
    if availability is not None:
        return _AVAILABILITY_TO_REPRESENTATION.get(
            availability.classification, "all_assessed_markets"
        )
    return "all_assessed_markets"


_RESPONDENT_NOUNS = ("trader", "respondent", "response", "vendor")


def _safe_subject(text: Optional[str]) -> Optional[str]:
    """Return a subject label only if it is safe inside a permitted phrase.

    Labels come from assessment data and are rejected outright rather than sanitised on
    two grounds. A digit could be quoted into a claim and would then fail numeric
    authorization, since only a cited value's own rendering is authorized. A respondent
    noun would contradict the phrase it sits in — a market-level statistic must never
    read as though it counted traders, which is the wording defect this metadata exists
    to make preventable.
    """
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned or any(character.isdigit() for character in cleaned):
        return None
    lowered = cleaned.casefold()
    if any(noun in lowered for noun in _RESPONDENT_NOUNS):
        return None
    return cleaned


def _subject_phrase(
    *,
    statistic: str,
    unit: str,
    evidence_scope: str,
    population_basis: str,
    representation_basis: str,
    market_name: Optional[str],
    region: Optional[str],
    subject_label: Optional[str],
) -> str:
    """Return a deterministic noun phrase describing the value correctly.

    The phrase never contains a digit. It is exposed to the drafting prompts, and numeric
    authorization only accepts tokens matching a cited value's rendering, so a digit
    quoted from here would be rejected as an unauthorized number.
    """
    # Market and region names are never interpolated: a name such as "Camp 4" would put a
    # digit into a phrase the drafting prompts can quote, and numeric authorization would
    # then reject it as an unauthorized number. Consumers already receive `market_name`
    # and `region` as separate fields.
    subject = _safe_subject(subject_label) or "this item"
    suffix_template = _REPRESENTATION_SUFFIXES.get(representation_basis, "")
    suffix = suffix_template.format(subject=subject) if suffix_template else ""
    where = " in this market" if market_name else ""

    if statistic == "rank":
        return "the relative weakness rank among comparable evidence metrics"

    if statistic == "rank_lowest_first":
        return _RANK_PHRASES.get(evidence_scope, "the relative weakness rank")

    if statistic == "coverage_ratio":
        return "the share of assessed markets with usable evidence for this metric"

    if statistic in _CROSS_MARKET_PHRASES:
        phrase = _CROSS_MARKET_PHRASES[statistic]
        return f"{phrase} in this region" if region else phrase

    if statistic in _METRIC_AGGREGATE_STATISTICS:
        if population_basis == "trader_level_within_market":
            base = {
                "derived_unfavorable_rate": (
                    "the unweighted mean market-level unfavorable rate"
                ),
                "mean_raw_value": (
                    "the unweighted mean of market-level trader proportions"
                ),
                "mean_normalized_value": (
                    "the unweighted mean of market-level normalized scores"
                ),
            }[statistic]
        else:
            base = {
                "derived_unfavorable_rate": (
                    "the share of assessed markets where this condition was unfavorable"
                ),
                "mean_raw_value": (
                    "the share of assessed markets satisfying this condition"
                    if unit == "proportion"
                    else "the unweighted mean value across assessed markets"
                ),
                "mean_normalized_value": "the unweighted mean normalized score",
            }[statistic]
        return f"{base} {suffix}" if suffix else base

    if statistic == "stored_level_1_score":
        # "Level-1" would put a digit into a quotable phrase; say it in words instead.
        return "the stored authoritative score recorded for this market"

    if statistic in _SINGLE_MARKET_STATISTICS:
        if population_basis == "trader_level_within_market":
            if statistic == "derived_market_unfavorable_rate":
                return (
                    f"the proportion of surveyed traders{where} reporting the "
                    "unfavorable condition"
                )
            if statistic == "market_explanatory_normalized_value":
                return f"the normalized score derived from surveyed traders{where}"
            return f"the proportion of surveyed traders{where}"
        if statistic == "market_explanatory_normalized_value":
            return "the normalized score recorded for this market"
        if unit == "proportion":
            return "the condition recorded for this market"
        return "the value recorded for this market"

    return "this deterministic assessment value"


def _ledger_semantics(
    *,
    statistic: str,
    unit: str,
    evidence_scope: str,
    market_name: Optional[str] = None,
    region: Optional[str] = None,
    availability: Optional[MFIEvidenceAvailability] = None,
    subject_label: Optional[str] = None,
) -> MFILedgerSemantics:
    """Derive aggregation semantics for one ledger entry.

    Resolution is tiered and first match wins. Statistic is consulted before evidence
    scope because a metric's scope is inherited unchanged by all of its ledger entries,
    including ones whose denominator is markets rather than traders.

    Aggregation is never derived from ``market_name``: a market's rank among all assessed
    markets carries a market name but is not a single-market value.

    Raises:
        ValueError: if the statistic is not in the derivation table. Failing here is
            deliberate — every ledger entry passes through this function, so an
            unmapped statistic is caught at once rather than silently defaulting.
    """
    fixed = _STATISTIC_SEMANTICS.get(statistic)
    if fixed is not None:
        aggregation_method, population_basis = fixed
    elif statistic in _CROSS_MARKET_STATISTICS:
        aggregation_method, population_basis = "unweighted_market_mean", "market_level"
    elif statistic in _METRIC_AGGREGATE_STATISTICS:
        aggregation_method = "unweighted_market_mean"
        population_basis = (
            "trader_level_within_market"
            if evidence_scope == _TRADER_SCOPE
            else "market_level"
        )
    elif statistic in _SINGLE_MARKET_STATISTICS:
        aggregation_method = "market_value"
        population_basis = (
            "trader_level_within_market"
            if evidence_scope == _TRADER_SCOPE
            else "market_level"
        )
    else:
        raise ValueError(
            f"Unmapped MFI ledger statistic {statistic!r}; add it to the R2 "
            "aggregation-semantics derivation table."
        )

    representation_basis = _representation_basis(
        statistic=statistic,
        evidence_scope=evidence_scope,
        availability=availability,
    )
    phrase = _subject_phrase(
        statistic=statistic,
        unit=unit,
        evidence_scope=evidence_scope,
        population_basis=population_basis,
        representation_basis=representation_basis,
        market_name=market_name,
        region=region,
        subject_label=subject_label,
    )
    return MFILedgerSemantics(
        aggregation_method=aggregation_method,
        population_basis=population_basis,
        # A denominator may be stated only when it is an assessed-market count carried in
        # ``coverage``. Trader-level values never qualify: the assessment provides no
        # applicability-specific respondent counts.
        pooled_denominator_available=(
            population_basis == "market_level" and aggregation_method != "market_value"
        ),
        representation_basis=representation_basis,
        permitted_subject_phrase=phrase,
    )


def _classify_evidence_availability(
    *,
    definition: Any,
    role: str,
    available: int,
    assessed_count: int,
    invalid: int,
    raw: Optional[float],
    config: MFIAnalysisConfig,
    explicitly_inapplicable: int = 0,
) -> MFIEvidenceAvailability:
    """Classify why a metric's evidence is incomplete.

    Required evidence that is absent or invalid is a methodology problem.
    Partial optional evidence retains its measured coverage, but non-applicability
    requires explicit source evidence; missing values alone do not establish it.
    """
    rule = str(getattr(definition, "applicability_rule", "required") or "required")
    complete = available >= assessed_count and invalid == 0

    if not complete and explicitly_inapplicable > 0 and available + explicitly_inapplicable == assessed_count and invalid == 0:
        classification = "not_applicable"
    elif rule == "quality_applicability":
        # The special component rule does not establish source applicability.
        classification = "complete" if complete else "unknown_applicability"
    elif rule in {"optional_item", "optional_product_group"}:
        classification = "complete" if complete else "unknown_applicability"
    elif invalid > 0 or available == 0 or raw is None:
        classification = "unusable_required"
    elif available < assessed_count:
        classification = "partial_required"
    else:
        classification = "complete"

    warrants_warning = classification == "unusable_required" or (
        classification == "partial_required"
        and available < config.partial_required_warning_ratio * assessed_count
    )
    return MFIEvidenceAvailability(
        classification=classification,
        applicability_rule=rule if rule in _APPLICABILITY_RULES else "required",
        role=role,
        represented_market_count=available,
        total_assessed_market_count=assessed_count,
        invalid_market_count=invalid,
        warrants_warning=warrants_warning,
    )


def _analyze_evidence(
    *,
    dimension: str,
    summaries: Sequence[Mapping[str, Any]],
    assessed_count: int,
    config: MFIAnalysisConfig,
    add_ledger: Any,
) -> tuple[list[MFIAnalyzedMetric], list[MFIAnalyzedMetric], list[str]]:
    prepared: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for summary in summaries:
        metric_id = str(summary["metric_id"])
        definition = METRIC_DEFINITIONS_BY_ID.get(metric_id)
        role = str(summary.get("role") or (definition.role if definition else ""))
        available = int(summary.get("available_market_count") or 0)
        total = assessed_count
        coverage = _coverage(available, total)
        raw = _optional_float(summary.get("mean_raw_value"))
        normalized = _optional_float(summary.get("mean_normalized_value"))
        orientation = str(
            summary.get("orientation")
            or (definition.orientation if definition else "descriptive")
        )
        unfavorable = _unfavorable_rate(raw, orientation)
        availability = _classify_evidence_availability(
            definition=definition,
            role=role,
            available=available,
            assessed_count=assessed_count,
            invalid=int(summary.get("missing_count") or 0),
            raw=raw,
            config=config,
            explicitly_inapplicable=sum(int((summary.get("applicability_counts") or {}).get(key, 0)) for key in ("not_applicable", "not_represented")),
        )
        if availability.warrants_warning:
            unavailable.append(metric_id)
        prepared.append(
            {
                "summary": dict(summary),
                "definition": definition,
                "metric_id": metric_id,
                "role": role,
                "coverage": coverage,
                "availability": availability,
                "raw": raw,
                "normalized": normalized,
                "orientation": orientation,
                "unfavorable": unfavorable,
                "weakness_rank": None,
                "group_rank": None,
                "item_relevant": False,
                "relevance_reasons": [],
                "matching_category_metric_id": None,
            }
        )

    subsection_records = [
        item
        for item in prepared
        if item["role"] in {"official_subsection", "dimension_validation_component"}
    ]
    driver_records = [
        item
        for item in prepared
        if item["role"] in {"category_driver", "question_driver", "item_driver"}
    ]

    rankable_subsections = [
        item
        for item in subsection_records
        if dimension != "Food Quality" and item["normalized"] is not None
    ]
    subsection_ranks = _rank_evidence(
        rankable_subsections,
        value_key="normalized",
        tolerance=config.ranking_tie_tolerance,
        unfavorable_first=True,
    )
    for item in rankable_subsections:
        item["weakness_rank"] = subsection_ranks[item["metric_id"]]

    for family_roles in (
        {"category_driver", "question_driver"},
        {"item_driver"},
    ):
        population = [
            item
            for item in driver_records
            if item["role"] in family_roles and item["unfavorable"] is not None
        ]
        ranks = _rank_evidence(
            population,
            value_key="unfavorable",
            tolerance=config.ranking_tie_tolerance,
            unfavorable_first=False,
        )
        for item in population:
            item["weakness_rank"] = ranks[item["metric_id"]]

    grouped_drivers: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in driver_records:
        if item["unfavorable"] is None:
            continue
        definition = item["definition"]
        family = "item" if item["role"] == "item_driver" else "category_question"
        key = (
            family,
            str(definition.question_group if definition else ""),
            str(definition.product_group if definition else ""),
        )
        grouped_drivers.setdefault(key, []).append(item)
    for population in grouped_drivers.values():
        ranks = _rank_evidence(
            population,
            value_key="unfavorable",
            tolerance=config.ranking_tie_tolerance,
            unfavorable_first=False,
        )
        for item in population:
            item["group_rank"] = ranks[item["metric_id"]]

    _mark_relevant_items(driver_records, assessed_count, config)

    subsections = [
        _materialize_analyzed_metric(item, add_ledger) for item in subsection_records
    ]
    drivers = [
        _materialize_analyzed_metric(item, add_ledger) for item in driver_records
    ]
    subsections.sort(
        key=lambda metric: (
            metric.weakness_rank is None,
            metric.weakness_rank or 10**9,
            metric.metric_id,
        )
    )
    drivers.sort(
        key=lambda metric: (
            metric.weakness_rank is None,
            metric.weakness_rank or 10**9,
            -(metric.severity_weight or 0),
            metric.metric_id,
        )
    )
    return subsections, drivers, sorted(set(unavailable))


def _rank_evidence(
    population: Sequence[Mapping[str, Any]],
    *,
    value_key: str,
    tolerance: float,
    unfavorable_first: bool,
) -> dict[str, int]:
    if not population:
        return {}
    records = [
        {
            "name": item["metric_id"],
            "value": float(item[value_key]),
            "severity": (
                item["definition"].severity_weight
                if item.get("definition") is not None
                and item["definition"].severity_weight is not None
                else 0
            ),
        }
        for item in population
    ]
    ranked = _rank_records(
        records,
        tolerance=tolerance,
        unfavorable_first=unfavorable_first,
        stable_key=lambda record: (
            -int(record["severity"]),
            str(record["name"]),
        ),
    )
    return {str(record["name"]): int(record["rank"]) for record in ranked}


def _mark_relevant_items(
    driver_records: Sequence[dict[str, Any]],
    assessed_count: int,
    config: MFIAnalysisConfig,
) -> None:
    categories: dict[tuple[str, str], dict[str, Any]] = {}
    for item in driver_records:
        definition = item["definition"]
        if not definition or item["role"] != "category_driver":
            continue
        categories[
            (str(definition.question_group or ""), str(definition.product_group or ""))
        ] = item

    required_markets = max(
        config.item_min_market_count,
        ceil(config.item_min_market_ratio * assessed_count),
    )
    qualifying_by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in driver_records:
        if item["role"] != "item_driver":
            continue
        definition = item["definition"]
        group = (
            str(definition.question_group or "") if definition else "",
            str(definition.product_group or "") if definition else "",
        )
        category = categories.get(group)
        item["matching_category_metric_id"] = (
            category["metric_id"] if category is not None else None
        )
        if item["coverage"].available_market_count < required_markets:
            item["relevance_reasons"].append("insufficient_market_coverage")
            continue
        if item["unfavorable"] is None:
            item["relevance_reasons"].append("unavailable_item_rate")
            continue
        if category is None or category["unfavorable"] is None:
            item["relevance_reasons"].append("matching_category_unavailable")
            continue
        contrast = float(item["unfavorable"]) - float(category["unfavorable"])
        if contrast < config.item_category_contrast - config.ranking_tie_tolerance:
            item["relevance_reasons"].append("insufficient_category_contrast")
            continue
        qualifying_by_group.setdefault(group, []).append(item)

    for candidates in qualifying_by_group.values():
        ordered = sorted(
            candidates,
            key=lambda item: (-float(item["unfavorable"]), item["metric_id"]),
        )
        for index, item in enumerate(ordered):
            if index < config.item_max_per_group:
                item["item_relevant"] = True
                item["relevance_reasons"].append("meets_relevance_thresholds")
            else:
                item["relevance_reasons"].append("outside_group_top_limit")


def _materialize_analyzed_metric(
    item: Mapping[str, Any],
    add_ledger: Any,
) -> MFIAnalyzedMetric:
    summary = item["summary"]
    definition = item["definition"]
    metric_id = str(item["metric_id"])
    source_metric_ids = list(summary.get("contributing_metric_ids") or [])
    if not source_metric_ids and item["raw"] is not None:
        source_metric_ids = [metric_id]
    ledger_ids: list[str] = []
    prefix = f"assessment.metric.{metric_id}"
    common = {
        "unit": str(summary.get("unit") or (definition.unit if definition else "")),
        "orientation": str(item["orientation"]),
        "evidence_scope": str(
            summary.get("evidence_scope")
            or (definition.evidence_scope if definition else "")
        ),
        "dimension": str(summary.get("dimension") or definition.dimension),
        "coverage": item["coverage"],
        "source_metric_ids": source_metric_ids,
        # Coverage counts alone cannot say *why* a metric covers fewer markets than were
        # assessed, and an optional item, an inapplicable Food Quality condition, and a
        # required-evidence failure each need different wording.
        "availability": item.get("availability"),
        "subject_label": (
            (definition.item_name if definition else None)
            or str(summary.get("display_name") or "")
        ),
    }
    if item["raw"] is not None:
        ledger_ids.append(
            add_ledger(
                f"{prefix}.mean_raw",
                label=f"{summary.get('display_name', metric_id)}: mean raw value",
                value=item["raw"],
                statistic="mean_raw_value",
                **common,
            )
        )
    if item["normalized"] is not None:
        ledger_ids.append(
            add_ledger(
                f"{prefix}.mean_normalized",
                label=f"{summary.get('display_name', metric_id)}: mean normalized value",
                value=item["normalized"],
                statistic="mean_normalized_value",
                **{**common, "unit": "score"},
            )
        )
    ledger_ids.append(
        add_ledger(
            f"{prefix}.coverage",
            label=f"{summary.get('display_name', metric_id)}: assessed-market coverage",
            value=item["coverage"].coverage_ratio,
            statistic="coverage_ratio",
            **{**common, "unit": "proportion"},
        )
    )
    if item["unfavorable"] is not None:
        ledger_ids.append(
            add_ledger(
                f"{prefix}.unfavorable_rate",
                label=f"{summary.get('display_name', metric_id)}: unfavorable rate",
                value=item["unfavorable"],
                statistic="derived_unfavorable_rate",
                **{**common, "unit": "proportion", "orientation": "higher_is_worse"},
            )
        )
    if item["weakness_rank"] is not None:
        ledger_ids.append(
            add_ledger(
                f"{prefix}.weakness_rank",
                label=f"{summary.get('display_name', metric_id)}: weakness rank",
                value=item["weakness_rank"],
                statistic="rank",
                **{**common, "unit": "rank"},
            )
        )
    return MFIAnalyzedMetric(
        metric_id=metric_id,
        dimension=common["dimension"],
        display_name=str(summary.get("display_name") or definition.display_name),
        role=str(item["role"]),
        mean_raw_value=item["raw"],
        mean_normalized_value=item["normalized"],
        unit=common["unit"],
        orientation=common["orientation"],
        evidence_scope=common["evidence_scope"],
        coverage=item["coverage"],
        availability=item.get("availability"),
        unfavorable_rate=item["unfavorable"],
        weakness_rank=item["weakness_rank"],
        group_rank=item["group_rank"],
        product_group=definition.product_group if definition else None,
        question_group=definition.question_group if definition else None,
        item_name=definition.item_name if definition else None,
        severity_weight=definition.severity_weight if definition else None,
        item_relevant=bool(item["item_relevant"]),
        relevance_reasons=list(item["relevance_reasons"]),
        matching_category_metric_id=item["matching_category_metric_id"],
        source_metric_ids=source_metric_ids,
        ledger_metric_ids=ledger_ids,
    )


def _build_market_profiles(
    markets: Sequence[Mapping[str, Any]],
    assessed_count: int,
    config: MFIAnalysisConfig,
    add_ledger: Any,
) -> tuple[list[MFIMarketProfile], list[MFIDeterministicTableRow]]:
    ranked_markets = _rank_records(
        [
            {
                "name": str(market["market_name"]),
                "value": float(market["overall_mfi"]),
                "market": market,
            }
            for market in markets
        ],
        tolerance=config.ranking_tie_tolerance,
        unfavorable_first=True,
        stable_key=lambda record: _normalized_name(str(record["name"])),
    )
    selected_names = {
        str(record["name"])
        for record in ranked_markets[: config.priority_market_max]
    }
    full_coverage = _coverage(assessed_count, assessed_count)
    profiles: list[MFIMarketProfile] = []
    rows: list[MFIDeterministicTableRow] = []
    for record in ranked_markets:
        market = record["market"]
        market_name = str(record["name"])
        market_token = _context_token(market_name)
        overall_id = add_ledger(
            f"market.{market_token}.mfi.stored",
            label=f"Stored overall MFI for {market_name}",
            value=record["value"],
            statistic="stored_level_1_score",
            unit="score",
            orientation="higher_is_better",
            evidence_scope="assessed_market",
            market_name=market_name,
            region=str(market.get("region") or "") or None,
            coverage=full_coverage,
            source_metric_ids=["mfi.overall"],
        )
        rank_id = add_ledger(
            f"market.{market_token}.mfi.rank",
            label=f"Overall MFI relative weakness rank for {market_name}",
            value=record["rank"],
            statistic="rank_lowest_first",
            unit="rank",
            orientation="higher_is_better",
            evidence_scope="included_assessed_markets",
            market_name=market_name,
            region=str(market.get("region") or "") or None,
            coverage=full_coverage,
            source_metric_ids=["mfi.overall"],
        )
        dimension_records = _rank_records(
            [
                {
                    "name": dimension,
                    "value": float(market["dimension_scores"][dimension]),
                }
                for dimension in DISPLAY_DIMENSIONS
            ],
            tolerance=config.ranking_tie_tolerance,
            unfavorable_first=True,
            stable_key=lambda value: _DIMENSION_ORDER[str(value["name"])],
        )
        weak_boundary = float(
            dimension_records[config.market_weak_dimension_count - 1]["value"]
        )
        dimension_profiles: list[MFIMarketDimensionProfile] = []
        for dimension_record in dimension_records:
            dimension = str(dimension_record["name"])
            score = float(dimension_record["value"])
            score_id = add_ledger(
                f"market.{market_token}.dimension.{_slug(dimension)}.stored",
                label=f"Stored {dimension} score for {market_name}",
                value=score,
                statistic="stored_level_1_score",
                unit="score",
                orientation="higher_is_better",
                evidence_scope="assessed_market",
                dimension=dimension,
                market_name=market_name,
                region=str(market.get("region") or "") or None,
                coverage=full_coverage,
                source_metric_ids=[_official_metric_id(dimension)],
            )
            dimension_rank_id = add_ledger(
                f"market.{market_token}.dimension.{_slug(dimension)}.rank",
                label=f"{dimension} relative weakness rank for {market_name}",
                value=dimension_record["rank"],
                statistic="rank_lowest_first",
                unit="rank",
                orientation="higher_is_better",
                evidence_scope="nine_dimension_market_profile",
                dimension=dimension,
                market_name=market_name,
                region=str(market.get("region") or "") or None,
                coverage=full_coverage,
                source_metric_ids=[_official_metric_id(dimension)],
            )
            dimension_profiles.append(
                MFIMarketDimensionProfile(
                    dimension=dimension,
                    score=score,
                    rank=int(dimension_record["rank"]),
                    selection_order=int(dimension_record["selection_order"]),
                    is_weak=(
                        score
                        <= weak_boundary + config.ranking_tie_tolerance
                    ),
                    ledger_metric_ids=[score_id, dimension_rank_id],
                )
            )
        is_priority = market_name in selected_names
        profile = MFIMarketProfile(
            market_name=market_name,
            region=str(market.get("region") or "") or None,
            overall_mfi=float(record["value"]),
            score_rank=int(record["rank"]),
            selection_order=int(record["selection_order"]),
            is_priority_market=is_priority,
            selection_reasons=["lowest_overall_mfi"] if is_priority else [],
            weak_dimensions=[
                item for item in dimension_profiles if item.is_weak
            ],
            dimension_profile=dimension_profiles,
            ledger_metric_ids=[overall_id, rank_id],
        )
        profiles.append(profile)
        if is_priority:
            weak_ids = [
                metric_id
                for item in profile.weak_dimensions
                for metric_id in item.ledger_metric_ids
            ]
            rows.append(
                MFIDeterministicTableRow(
                    row_id=f"priority_market.{market_token}",
                    values={
                        "market_name": market_name,
                        "region": profile.region,
                        "overall_mfi": profile.overall_mfi,
                        "score_rank": profile.score_rank,
                        "selection_order": profile.selection_order,
                        "weak_dimensions": [
                            item.dimension for item in profile.weak_dimensions
                        ],
                    },
                    ledger_metric_ids=[overall_id, rank_id, *weak_ids],
                )
            )
    return profiles, rows


def _add_priority_market_evidence_ledger(
    markets: Sequence[Mapping[str, Any]],
    market_profiles: Sequence[MFIMarketProfile],
    assessed_count: int,
    add_ledger: Any,
) -> None:
    """Add auditable local evidence only for selected markets' weak dimensions."""
    market_by_name = {
        str(market["market_name"]): market for market in markets
    }
    for profile in market_profiles:
        if not profile.is_priority_market:
            continue
        market = market_by_name[profile.market_name]
        market_token = _context_token(profile.market_name)
        weak_dimensions = {item.dimension for item in profile.weak_dimensions}
        for group_name in ("subsections", "drivers"):
            grouped = market.get(group_name)
            if not isinstance(grouped, Mapping):
                continue
            for dimension in DISPLAY_DIMENSIONS:
                if dimension not in weak_dimensions:
                    continue
                metrics = grouped.get(dimension)
                if not isinstance(metrics, Sequence):
                    continue
                for raw_metric in metrics:
                    if not isinstance(raw_metric, Mapping):
                        continue
                    if (
                        raw_metric.get("applicability_status") != "available"
                        or raw_metric.get("validation_status") != "valid"
                    ):
                        continue
                    metric_id = str(raw_metric.get("metric_id") or "")
                    if not metric_id:
                        continue
                    available = int(raw_metric.get("market_coverage") or 0)
                    total = int(
                        raw_metric.get("market_coverage_total")
                        or assessed_count
                    )
                    coverage = _coverage(available, total)
                    prefix = f"market.{market_token}.metric.{metric_id}"
                    common = {
                        "orientation": str(
                            raw_metric.get("orientation") or "descriptive"
                        ),
                        "evidence_scope": str(
                            raw_metric.get("evidence_scope")
                            or "assessed_market"
                        ),
                        "dimension": dimension,
                        "market_name": profile.market_name,
                        "region": profile.region,
                        "coverage": coverage,
                        "source_metric_ids": [metric_id],
                    }
                    raw_value = raw_metric.get("raw_value")
                    if raw_value is not None:
                        add_ledger(
                            f"{prefix}.raw",
                            label=(
                                f"{raw_metric.get('display_name', metric_id)} "
                                f"in {profile.market_name}: raw value"
                            ),
                            value=float(raw_value),
                            statistic="market_explanatory_raw_value",
                            unit=str(raw_metric.get("unit") or ""),
                            **common,
                        )
                    normalized = raw_metric.get("normalized_value")
                    if normalized is not None:
                        add_ledger(
                            f"{prefix}.normalized",
                            label=(
                                f"{raw_metric.get('display_name', metric_id)} "
                                f"in {profile.market_name}: normalized value"
                            ),
                            value=float(normalized),
                            statistic="market_explanatory_normalized_value",
                            unit="score",
                            **common,
                        )
                    unfavorable = _unfavorable_rate(
                        _optional_float(raw_value),
                        common["orientation"],
                    )
                    if (
                        unfavorable is not None
                        and str(raw_metric.get("unit") or "") == "proportion"
                    ):
                        add_ledger(
                            f"{prefix}.unfavorable_rate",
                            label=(
                                f"{raw_metric.get('display_name', metric_id)} "
                                f"in {profile.market_name}: unfavorable rate"
                            ),
                            value=unfavorable,
                            statistic="derived_market_unfavorable_rate",
                            unit="proportion",
                            **{**common, "orientation": "higher_is_worse"},
                        )
                    add_ledger(
                        f"{prefix}.coverage",
                        label=(
                            f"{raw_metric.get('display_name', metric_id)}: "
                            "assessed-market coverage"
                        ),
                        value=coverage.coverage_ratio,
                        statistic="coverage_ratio",
                        unit="proportion",
                        **common,
                    )


def _build_localized_patterns(
    markets: Sequence[Mapping[str, Any]],
    regional_ranks: Mapping[str, Mapping[str, int]],
    dimension_statistics: Mapping[str, MFIStatisticalSummary],
    config: MFIAnalysisConfig,
    ledger: Mapping[str, MFIMetricLedgerEntry],
) -> dict[str, MFILocalizedPatterns]:
    result: dict[str, MFILocalizedPatterns] = {}
    for dimension in DISPLAY_DIMENSIONS:
        region_bottom_one = sorted(
            [
                region
                for region, ranks in regional_ranks.items()
                if ranks[dimension] == 1
            ],
            key=_normalized_name,
        )
        region_bottom_two = sorted(
            [
                region
                for region, ranks in regional_ranks.items()
                if ranks[dimension] <= 2
            ],
            key=_normalized_name,
        )
        ranked_markets = _rank_records(
            [
                {
                    "name": str(market["market_name"]),
                    "value": float(market["dimension_scores"][dimension]),
                }
                for market in markets
            ],
            tolerance=config.ranking_tie_tolerance,
            unfavorable_first=True,
            stable_key=lambda record: _normalized_name(str(record["name"])),
        )
        market_values: list[MFIRankedValue] = []
        lowest_markets: list[str] = []
        for record in ranked_markets:
            market_name = str(record["name"])
            ledger_id = (
                f"market.{_context_token(market_name)}."
                f"dimension.{_slug(dimension)}.stored"
            )
            if ledger_id not in ledger:
                raise ValueError(f"Localized pattern has no ledger value: {ledger_id}")
            market_values.append(
                MFIRankedValue(
                    name=market_name,
                    value=float(record["value"]),
                    rank=int(record["rank"]),
                    selection_order=int(record["selection_order"]),
                    ledger_metric_id=ledger_id,
                )
            )
        for market in markets:
            market_dimension_records = _rank_records(
                [
                    {"name": name, "value": float(market["dimension_scores"][name])}
                    for name in DISPLAY_DIMENSIONS
                ],
                tolerance=config.ranking_tie_tolerance,
                unfavorable_first=True,
                stable_key=lambda record: _DIMENSION_ORDER[str(record["name"])],
            )
            dimension_record = next(
                record
                for record in market_dimension_records
                if record["name"] == dimension
            )
            if int(dimension_record["rank"]) == 1:
                lowest_markets.append(str(market["market_name"]))
        result[dimension] = MFILocalizedPatterns(
            regions_where_bottom_one=region_bottom_one,
            regions_where_bottom_two=region_bottom_two,
            markets_where_lowest=sorted(lowest_markets, key=_normalized_name),
            ordered_markets=market_values,
            score_range=dimension_statistics[dimension].score_range,
            iqr=dimension_statistics[dimension].iqr,
        )
    return result


def _metric_table_row(
    table_name: str,
    metric: MFIAnalyzedMetric,
) -> MFIDeterministicTableRow:
    return MFIDeterministicTableRow(
        row_id=f"{table_name}.{metric.metric_id}",
        values={
            "metric_id": metric.metric_id,
            "dimension": metric.dimension,
            "display_name": metric.display_name,
            "role": metric.role,
            "mean_raw_value": metric.mean_raw_value,
            "mean_normalized_value": metric.mean_normalized_value,
            "unfavorable_rate": metric.unfavorable_rate,
            "weakness_rank": metric.weakness_rank,
            "group_rank": metric.group_rank,
            "unit": metric.unit,
            "orientation": metric.orientation,
            "evidence_scope": metric.evidence_scope,
            "available_market_count": metric.coverage.available_market_count,
            "total_assessed_market_count": (
                metric.coverage.total_assessed_market_count
            ),
            "coverage_ratio": metric.coverage.coverage_ratio,
            "product_group": metric.product_group,
            "question_group": metric.question_group,
            "item_name": metric.item_name,
            "severity_weight": metric.severity_weight,
            "item_relevant": metric.item_relevant,
            "matching_category_metric_id": metric.matching_category_metric_id,
            "relevance_reasons": metric.relevance_reasons,
        },
        ledger_metric_ids=metric.ledger_metric_ids,
    )


def _unfavorable_rate(
    mean_raw_value: Optional[float],
    orientation: str,
) -> Optional[float]:
    if mean_raw_value is None:
        return None
    if orientation == "higher_is_better":
        return 1.0 - mean_raw_value
    if orientation == "higher_is_worse":
        return mean_raw_value
    return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _official_metric_id(dimension: str) -> str:
    for metric_id, definition in METRIC_DEFINITIONS_BY_ID.items():
        if (
            definition.role == "official_score"
            and definition.dimension == dimension
        ):
            return metric_id
    raise ValueError(f"No official metric is registered for {dimension}")


def _excluded_count(metadata: Mapping[str, Any]) -> int:
    records = metadata.get("excluded_market_records")
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        return len(records)
    if isinstance(records, (int, float)):
        return int(records)
    survey = metadata.get("survey_metadata")
    if isinstance(survey, Mapping):
        value = survey.get("excluded_market_records")
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _deduplicate_limitations(
    limitations: Sequence[MFILimitation],
) -> list[MFILimitation]:
    seen: set[tuple[str, Optional[str], Optional[str], Optional[str]]] = set()
    result: list[MFILimitation] = []
    for limitation in limitations:
        key = (
            limitation.code,
            limitation.market_name,
            limitation.region,
            limitation.dimension,
        )
        if key not in seen:
            seen.add(key)
            result.append(limitation)
    return result


def _validate_table_ledger_references(
    tables: MFIDeterministicTables,
    ledger: Mapping[str, MFIMetricLedgerEntry],
) -> None:
    for rows in (
        tables.dimension_rows,
        tables.regional_rows,
        tables.subsection_rows,
        tables.driver_rows,
        tables.relevant_item_rows,
        tables.priority_market_rows,
    ):
        for row in rows:
            missing = [
                metric_id
                for metric_id in row.ledger_metric_ids
                if metric_id not in ledger
            ]
            if missing:
                raise ValueError(
                    f"Table row {row.row_id} references missing ledger IDs: {missing}"
                )


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_")
    return slug or "unnamed"
