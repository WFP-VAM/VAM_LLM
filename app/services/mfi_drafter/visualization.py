"""Strict presentation contracts for MFI visualizations.

The analytical profile is authoritative.  Chart code may format that profile, but it
must never invent replacement coverage or move a market's geographic point to make a
label fit.  This module keeps those rules independently testable from the LangGraph
node and from matplotlib image serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from .schemas import MFICoverageSummary


MAP_LABEL_MAX = 15
_COVERAGE_FIELDS = frozenset(
    {
        "available_market_count",
        "total_assessed_market_count",
        "missing_count",
        "coverage_ratio",
    }
)
_OFFSET_DIRECTIONS = (
    (1.0, 1.0),
    (1.0, -1.0),
    (-1.0, 1.0),
    (-1.0, -1.0),
    (1.0, 0.0),
    (-1.0, 0.0),
    (0.0, 1.0),
    (0.0, -1.0),
)
_OFFSET_RADII_POINTS = (12.0, 22.0, 32.0, 42.0, 54.0, 66.0, 80.0)


class MFIVisualizationContractError(ValueError):
    """Raised when canonical analytical data cannot safely drive a chart."""


@dataclass(frozen=True)
class MFIMapLabelInput:
    """One selected assessed market eligible for a numbered map callout."""

    market_name: str
    longitude: float
    latitude: float
    selection_order: int
    score_rank: int
    market_key: str | None = None


@dataclass(frozen=True)
class MFIMapCalloutPlacement:
    """Deterministic display-space placement for one numbered callout."""

    number: int
    market_name: str
    longitude: float
    latitude: float
    selection_order: int
    score_rank: int
    offset_points: tuple[float, float]
    bbox_pixels: tuple[float, float, float, float]
    used_edge_lane: bool = False
    market_key: str | None = None


def validate_dimension_chart_coverage(
    value: Any,
    *,
    plotted_market_count: int,
    dimension: str,
    tolerance: float = 1e-9,
) -> MFICoverageSummary:
    """Validate canonical coverage before it is rendered in a chart title."""

    context = f"{dimension or 'dimension'} chart coverage"
    if not isinstance(value, Mapping):
        raise MFIVisualizationContractError(f"{context} must be an object")

    missing_fields = sorted(_COVERAGE_FIELDS.difference(value))
    if missing_fields:
        raise MFIVisualizationContractError(
            f"{context} is missing required field(s): {', '.join(missing_fields)}"
        )

    try:
        coverage = MFICoverageSummary.model_validate(
            {field: value[field] for field in _COVERAGE_FIELDS},
            strict=True,
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise MFIVisualizationContractError(
            f"{context} has an incompatible typed shape"
        ) from exc

    available = coverage.available_market_count
    total = coverage.total_assessed_market_count
    missing = coverage.missing_count
    ratio = coverage.coverage_ratio

    if available < 0 or total < 0 or missing < 0:
        raise MFIVisualizationContractError(f"{context} counts cannot be negative")
    if available > total:
        raise MFIVisualizationContractError(
            f"{context} available markets cannot exceed assessed markets"
        )
    if missing != total - available:
        raise MFIVisualizationContractError(
            f"{context} missing_count does not equal total minus available"
        )
    if not 0.0 <= ratio <= 1.0:
        raise MFIVisualizationContractError(
            f"{context} coverage_ratio must be between zero and one"
        )
    expected_ratio = float(available) / float(total) if total else 0.0
    if not isclose(ratio, expected_ratio, rel_tol=0.0, abs_tol=tolerance):
        raise MFIVisualizationContractError(
            f"{context} coverage_ratio is inconsistent with its counts"
        )
    if plotted_market_count < 0:
        raise MFIVisualizationContractError(
            f"{context} plotted market count cannot be negative"
        )
    if available != int(plotted_market_count):
        raise MFIVisualizationContractError(
            f"{context} reports {available} available markets but the chart contains "
            f"{int(plotted_market_count)} scores"
        )
    return coverage


def format_market_coverage(
    coverage: MFICoverageSummary,
    *,
    subject: str = "markets",
) -> str:
    """Format one already validated coverage summary consistently."""

    return (
        f"{coverage.available_market_count}/"
        f"{coverage.total_assessed_market_count} {subject} "
        f"({coverage.coverage_ratio * 100.0:.1f}%)"
    )


def select_map_label_inputs(
    values: Sequence[MFIMapLabelInput],
    *,
    maximum: int = MAP_LABEL_MAX,
) -> tuple[MFIMapLabelInput, ...]:
    """Return the deterministic, capped callout population."""

    if maximum < 0:
        raise ValueError("maximum map label count cannot be negative")
    ordered = sorted(
        values,
        key=lambda item: (
            int(item.selection_order),
            item.market_name.casefold(),
            item.market_name,
        ),
    )
    return tuple(ordered[:maximum])


def place_map_callouts(
    ax: Any,
    values: Sequence[MFIMapLabelInput],
    *,
    maximum: int = MAP_LABEL_MAX,
    use_selection_numbers: bool = False,
) -> tuple[MFIMapCalloutPlacement, ...]:
    """Place small numbered callouts without moving the underlying market points.

    Candidate offsets are tried in a fixed expanding sequence.  If a dense or edge-bound
    cluster exhausts those candidates, two deterministic lanes inside the right edge of
    the axes provide a final collision-free placement surface.
    """

    selected = select_map_label_inputs(values, maximum=maximum)
    if not selected:
        return ()

    figure = ax.figure
    figure.canvas.draw()
    axes_box = ax.get_window_extent()
    pixels_per_point = float(figure.dpi) / 72.0
    padding_pixels = 3.0 * pixels_per_point
    accepted_boxes: list[tuple[float, float, float, float]] = []
    placements: list[MFIMapCalloutPlacement] = []

    for number, item in enumerate(selected, start=1):
        if use_selection_numbers:
            number = int(item.selection_order)
        anchor_x, anchor_y = ax.transData.transform(
            (float(item.longitude), float(item.latitude))
        )
        label_width = max(
            16.0 * pixels_per_point,
            (8.0 + 6.0 * len(str(number))) * pixels_per_point,
        )
        label_height = 16.0 * pixels_per_point
        chosen: tuple[float, float, tuple[float, float, float, float], bool] | None = None

        for offset_x, offset_y in _candidate_offsets():
            center_x = anchor_x + offset_x * pixels_per_point
            center_y = anchor_y + offset_y * pixels_per_point
            box = _centered_box(center_x, center_y, label_width, label_height)
            if not _box_inside(box, axes_box):
                continue
            if any(_boxes_overlap(box, existing, padding_pixels) for existing in accepted_boxes):
                continue
            chosen = (offset_x, offset_y, box, False)
            break

        if chosen is None:
            for center_x, center_y in _edge_lane_centers(
                axes_box,
                label_width=label_width,
                label_height=label_height,
                maximum=max(maximum, 1),
                anchor_y=anchor_y,
            ):
                box = _centered_box(center_x, center_y, label_width, label_height)
                if any(
                    _boxes_overlap(box, existing, padding_pixels)
                    for existing in accepted_boxes
                ):
                    continue
                chosen = (
                    (center_x - anchor_x) / pixels_per_point,
                    (center_y - anchor_y) / pixels_per_point,
                    box,
                    True,
                )
                break

        if chosen is None:
            raise MFIVisualizationContractError(
                "Unable to place the selected-market map callouts without overlap"
            )

        offset_x, offset_y, box, used_edge_lane = chosen
        accepted_boxes.append(box)
        placements.append(
            MFIMapCalloutPlacement(
                number=number,
                market_name=item.market_name,
                longitude=float(item.longitude),
                latitude=float(item.latitude),
                selection_order=int(item.selection_order),
                score_rank=int(item.score_rank),
                offset_points=(offset_x, offset_y),
                bbox_pixels=box,
                used_edge_lane=used_edge_lane,
                market_key=item.market_key,
            )
        )

    return tuple(placements)


def _candidate_offsets() -> tuple[tuple[float, float], ...]:
    return tuple(
        (radius * direction_x, radius * direction_y)
        for radius in _OFFSET_RADII_POINTS
        for direction_x, direction_y in _OFFSET_DIRECTIONS
    )


def _edge_lane_centers(
    axes_box: Any,
    *,
    label_width: float,
    label_height: float,
    maximum: int,
    anchor_y: float,
) -> tuple[tuple[float, float], ...]:
    inset = 3.0
    lane_x_values = (
        float(axes_box.x1) - label_width / 2.0 - inset,
        float(axes_box.x1) - label_width * 1.7 - inset,
    )
    usable_bottom = float(axes_box.y0) + label_height / 2.0 + inset
    usable_top = float(axes_box.y1) - label_height / 2.0 - inset
    if maximum == 1:
        y_values = ((usable_bottom + usable_top) / 2.0,)
    else:
        step = (usable_top - usable_bottom) / float(maximum - 1)
        y_values = tuple(usable_bottom + index * step for index in range(maximum))
    candidates = [
        (lane_x, y_value) for lane_x in lane_x_values for y_value in y_values
    ]
    candidates.sort(key=lambda point: (abs(point[1] - anchor_y), -point[1], -point[0]))
    return tuple(candidates)


def _centered_box(
    center_x: float,
    center_y: float,
    width: float,
    height: float,
) -> tuple[float, float, float, float]:
    return (
        center_x - width / 2.0,
        center_y - height / 2.0,
        center_x + width / 2.0,
        center_y + height / 2.0,
    )


def _box_inside(box: tuple[float, float, float, float], axes_box: Any) -> bool:
    return (
        box[0] >= float(axes_box.x0)
        and box[1] >= float(axes_box.y0)
        and box[2] <= float(axes_box.x1)
        and box[3] <= float(axes_box.y1)
    )


def _boxes_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    padding: float,
) -> bool:
    return not (
        left[2] + padding <= right[0]
        or right[2] + padding <= left[0]
        or left[3] + padding <= right[1]
        or right[3] + padding <= left[1]
    )
