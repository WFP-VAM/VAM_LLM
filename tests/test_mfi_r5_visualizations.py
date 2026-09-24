"""Regression coverage for R5 visualization data contracts."""

from __future__ import annotations

from itertools import combinations

import matplotlib
import pytest

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from app.services.mfi_drafter import visualization as visualization_contract
from app.services.mfi_drafter.visualization import (
    MAP_LABEL_MAX,
    MFIMapLabelInput,
    MFIVisualizationContractError,
    boxes_overlap,
    format_market_coverage,
    place_map_callouts,
    select_map_label_inputs,
    validate_dimension_chart_coverage,
)
from app.shared.report_blocks import build_mfi_report_blocks

@pytest.mark.parametrize(
    ("value", "plotted", "expected"),
    [
        (
            {
                "available_market_count": 27,
                "total_assessed_market_count": 27,
                "missing_count": 0,
                "coverage_ratio": 1.0,
            },
            27,
            "27/27 markets (100.0%)",
        ),
        (
            {
                "available_market_count": 3,
                "total_assessed_market_count": 4,
                "missing_count": 1,
                "coverage_ratio": 0.75,
            },
            3,
            "3/4 markets (75.0%)",
        ),
    ],
)
def test_dimension_coverage_adapter_and_formatter(value, plotted, expected) -> None:
    coverage = validate_dimension_chart_coverage(
        value,
        plotted_market_count=plotted,
        dimension="Service",
    )
    assert format_market_coverage(coverage) == expected


@pytest.mark.parametrize(
    ("value", "plotted", "message"),
    [
        (None, 1, "must be an object"),
        (
            {
                "available_market_count": 1,
                "total_assessed_market_count": 1,
                "missing_count": 0,
            },
            1,
            "missing required field",
        ),
        (
            {
                "available_market_count": "1",
                "total_assessed_market_count": 1,
                "missing_count": 0,
                "coverage_ratio": 1.0,
            },
            1,
            "incompatible typed shape",
        ),
        (
            {
                "available_market_count": -1,
                "total_assessed_market_count": 1,
                "missing_count": 2,
                "coverage_ratio": -1.0,
            },
            -1,
            "counts cannot be negative",
        ),
        (
            {
                "available_market_count": 2,
                "total_assessed_market_count": 1,
                "missing_count": 0,
                "coverage_ratio": 1.0,
            },
            2,
            "cannot exceed",
        ),
        (
            {
                "available_market_count": 1,
                "total_assessed_market_count": 2,
                "missing_count": 0,
                "coverage_ratio": 0.5,
            },
            1,
            "missing_count",
        ),
        (
            {
                "available_market_count": 1,
                "total_assessed_market_count": 2,
                "missing_count": 1,
                "coverage_ratio": 0.6,
            },
            1,
            "coverage_ratio is inconsistent",
        ),
        (
            {
                "available_market_count": 1,
                "total_assessed_market_count": 2,
                "missing_count": 1,
                "coverage_ratio": 0.5,
            },
            2,
            "chart contains 2 scores",
        ),
    ],
)
def test_dimension_coverage_adapter_rejects_malformed_contracts(
    value, plotted, message
) -> None:
    with pytest.raises(MFIVisualizationContractError, match=message):
        validate_dimension_chart_coverage(
            value,
            plotted_market_count=plotted,
            dimension="Price",
        )


def _label_inputs(
    count: int,
    *,
    mode: str,
) -> list[MFIMapLabelInput]:
    values: list[MFIMapLabelInput] = []
    for index in range(count):
        if mode == "coincident":
            lon, lat = 34.45, 31.50
        elif mode == "near":
            lon = 34.45 + (index % 4) * 0.00004
            lat = 31.50 + (index // 4) * 0.00004
        else:
            lon = 34.0 + index * 0.08
            lat = 31.0 + (index % 3) * 0.10
        values.append(
            MFIMapLabelInput(
                market_name=f"Market {count - index:02d}",
                longitude=lon,
                latitude=lat,
                selection_order=(index % 5) + 1,
                score_rank=(index % 4) + 1,
            )
        )
    return values


def _placed(values: list[MFIMapLabelInput]):
    fig, ax = plt.subplots(figsize=(12, 10), dpi=100)
    source_points = [(item.longitude, item.latitude) for item in values]
    scatter = ax.scatter(
        [item.longitude for item in values],
        [item.latitude for item in values],
    )
    fig.subplots_adjust(left=0.08, right=0.70, bottom=0.09, top=0.90)
    placements = place_map_callouts(ax, values)
    rendered_points = [tuple(float(value) for value in point) for point in scatter.get_offsets()]
    return fig, placements, source_points, rendered_points


@pytest.mark.parametrize("mode", ["sparse", "near", "coincident"])
def test_map_callouts_are_deterministic_capped_and_non_overlapping(mode) -> None:
    values = _label_inputs(18, mode=mode)
    first_figure, first, source_points, rendered_points = _placed(values)
    second_figure, second, _, _ = _placed(values)
    try:
        assert len(first) == MAP_LABEL_MAX
        assert len(second) == MAP_LABEL_MAX
        assert [item.market_name for item in first] == [
            item.market_name for item in select_map_label_inputs(values)
        ]
        assert [item.number for item in first] == list(range(1, MAP_LABEL_MAX + 1))
        assert [item.offset_points for item in first] == pytest.approx(
            [item.offset_points for item in second]
        )
        assert rendered_points == pytest.approx(source_points)
        assert [(item.longitude, item.latitude) for item in first] == pytest.approx(
            [
                (item.longitude, item.latitude)
                for item in select_map_label_inputs(values)
            ]
        )
        assert not any(boxes_overlap(left, right) for left, right in combinations(first, 2))
    finally:
        plt.close(first_figure)
        plt.close(second_figure)


def test_map_selection_uses_order_then_normalized_name() -> None:
    values = [
        MFIMapLabelInput("Zulu", 1.0, 1.0, 2, 2),
        MFIMapLabelInput("beta", 1.0, 1.0, 1, 1),
        MFIMapLabelInput("Alpha", 1.0, 1.0, 1, 1),
    ]
    assert [item.market_name for item in select_map_label_inputs(values)] == [
        "Alpha",
        "beta",
        "Zulu",
    ]


def test_map_callouts_use_deterministic_edge_lane_when_offsets_are_exhausted(
    monkeypatch,
) -> None:
    monkeypatch.setattr(visualization_contract, "_OFFSET_RADII_POINTS", ())
    values = _label_inputs(8, mode="coincident")
    figure, placements, _, _ = _placed(values)
    try:
        assert len(placements) == 8
        assert all(item.used_edge_lane for item in placements)
        assert not any(
            boxes_overlap(left, right)
            for left, right in combinations(placements, 2)
        )
    finally:
        plt.close(figure)


def test_geographic_report_caption_explains_numbered_callouts() -> None:
    blocks = build_mfi_report_blocks(
        {
            "visualizations": {"geographic_map": "image"},
            "assessment_profile": {
                "dimensions": [],
                "markets": [],
                "priority_dimension_names": [],
                "metric_ledger": {},
                "tables": {
                    "dimension_rows": [],
                    "regional_rows": [],
                    "subsection_rows": [],
                    "driver_rows": [],
                    "relevant_item_rows": [],
                    "priority_market_rows": [],
                },
            },
        }
    )
    map_block = next(
        block
        for block in blocks
        if block.type == "figure" and block.figure_id == "geographic_map"
    )
    assert "numbered callouts identify selected review markets" in map_block.caption
