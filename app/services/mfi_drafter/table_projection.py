"""Reader-facing projections for canonical MFI deterministic tables.

Phase 2 tables are deliberately presentation-neutral and remain part of the public
assessment profile.  This module is the only place where those rows are selected,
formatted, labelled, and prepared for report renderers.  It never mutates the profile.
"""
from __future__ import annotations

import csv
import io
import json
import math
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


MFIReportCellFormat = Literal[
    "text",
    "score_statistic",
    "percentage",
    "percentage_point",
    "integer",
    "list",
    "boolean",
    "coverage",
]
MFIReportAlignment = Literal["left", "center", "right"]
MFILedgerLinkagePolicy = Literal["required", "not_applicable"]


class MFIReportTableProjectionError(ValueError):
    """Raised when a canonical MFI table cannot be projected safely."""


class MFIReportTableColumn(BaseModel):
    """One immutable, explicitly rendered report-table column."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    format: MFIReportCellFormat = "text"
    alignment: MFIReportAlignment = "left"
    width_hint: float = 1.0
    ledger_linkage_policy: MFILedgerLinkagePolicy = "required"

    @field_validator("key", "label")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("table column key and label must be non-empty")
        return cleaned

    @field_validator("width_hint")
    @classmethod
    def _positive_width(cls, value: float) -> float:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError("table column width_hint must be finite and positive")
        return number


class MFIReportTableSpec(BaseModel):
    """Immutable projection contract for one canonical deterministic table."""

    model_config = ConfigDict(frozen=True)

    spec_id: str
    canonical_source_table: str
    columns: tuple[MFIReportTableColumn, ...]
    maximum_rows: Optional[int] = None

    @field_validator("spec_id", "canonical_source_table")
    @classmethod
    def _nonempty_identity(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("table spec identity fields must be non-empty")
        return cleaned

    @model_validator(mode="after")
    def _validate_contract(self) -> "MFIReportTableSpec":
        if not self.columns:
            raise ValueError("table specs require at least one column")
        if len(self.columns) > 8:
            raise ValueError("reader-facing MFI tables may not exceed eight columns")
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("table spec column keys must be unique")
        if self.maximum_rows is not None and self.maximum_rows < 1:
            raise ValueError("maximum_rows must be positive when provided")
        return self


def _column(
    key: str,
    label: str,
    format: MFIReportCellFormat = "text",
    alignment: MFIReportAlignment = "left",
    width: float = 1.0,
    linkage: MFILedgerLinkagePolicy = "required",
) -> MFIReportTableColumn:
    return MFIReportTableColumn(
        key=key,
        label=label,
        format=format,
        alignment=alignment,
        width_hint=width,
        ledger_linkage_policy=linkage,
    )


_REPORT_TABLE_SPECS = (
    MFIReportTableSpec(
        spec_id="mfi.dimension_summary.v1",
        canonical_source_table="dimension_rows",
        maximum_rows=9,
        columns=(
            _column("dimension", "Dimension", width=1.7),
            _column("mean", "Mean", "score_statistic", "right"),
            _column("median", "Median", "score_statistic", "right"),
            _column("minimum", "Minimum", "score_statistic", "right"),
            _column("maximum", "Maximum", "score_statistic", "right"),
            _column("iqr", "IQR", "score_statistic", "right"),
            _column("rank", "Rank", "integer", "right", 0.7),
            _column("is_priority", "Priority", "boolean", "center", 0.8),
        ),
    ),
    MFIReportTableSpec(
        spec_id="mfi.regional_summary.v1",
        canonical_source_table="regional_rows",
        columns=(
            _column("region", "Region", width=1.5),
            _column("dimension", "Dimension", width=1.6),
            _column("mean", "Mean", "score_statistic", "right"),
            _column("market_count", "Assessed markets", "integer", "right"),
            _column("coverage", "Coverage", "coverage", "right", 1.6),
        ),
    ),
    MFIReportTableSpec(
        spec_id="mfi.official_subsection.v1",
        canonical_source_table="subsection_rows",
        maximum_rows=2,
        columns=(
            _column("display_name", "Official subsection", width=2.4),
            _column(
                "mean_normalized_value",
                "Normalized score",
                "score_statistic",
                "right",
                1.3,
            ),
            _column("weakness_rank", "Weakness rank", "integer", "right", 1.0),
            _column("coverage", "Coverage", "coverage", "right", 1.6),
        ),
    ),
    MFIReportTableSpec(
        spec_id="mfi.ranked_driver.v1",
        canonical_source_table="driver_rows",
        maximum_rows=4,
        columns=(
            _column("display_name", "Driver or question", width=2.5),
            _column(
                "unfavorable_rate",
                "Unfavorable rate",
                "percentage",
                "right",
                1.2,
            ),
            _column("weakness_rank", "Weakness rank", "integer", "right", 1.0),
            _column("evidence_scope", "Evidence scope", width=1.7),
            _column("coverage", "Coverage", "coverage", "right", 1.5),
        ),
    ),
    MFIReportTableSpec(
        spec_id="mfi.relevant_item.v1",
        canonical_source_table="relevant_item_rows",
        columns=(
            _column("item_name", "Relevant item", width=1.8),
            _column("question_group", "Question group", width=1.8),
            _column(
                "unfavorable_rate",
                "Unfavorable rate",
                "percentage",
                "right",
                1.2,
            ),
            _column(
                "category_contrast",
                "Category contrast",
                "percentage_point",
                "right",
                1.2,
            ),
            _column(
                "represented_markets",
                "Represented markets",
                "integer",
                "right",
                1.1,
            ),
        ),
    ),
    MFIReportTableSpec(
        spec_id="mfi.priority_market.v1",
        canonical_source_table="priority_market_rows",
        maximum_rows=15,
        columns=(
            _column("market_name", "Assessed market", width=2.0),
            _column("region", "Region", width=1.5),
            _column("overall_mfi", "MFI score", "score_statistic", "right"),
            _column("score_rank", "Score rank", "integer", "right", 0.8),
            _column("weak_dimensions", "Weak dimensions", "list", width=2.5),
        ),
    ),
)

if len({spec.spec_id for spec in _REPORT_TABLE_SPECS}) != len(_REPORT_TABLE_SPECS):
    raise RuntimeError("MFI report-table spec IDs must be unique")
if len({spec.canonical_source_table for spec in _REPORT_TABLE_SPECS}) != len(
    _REPORT_TABLE_SPECS
):
    raise RuntimeError("Each canonical MFI table must have exactly one projection")

MFI_REPORT_TABLE_SPEC_BY_SOURCE: Mapping[str, MFIReportTableSpec] = MappingProxyType(
    {spec.canonical_source_table: spec for spec in _REPORT_TABLE_SPECS}
)


def build_mfi_raw_table_downloads(
    profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return one canonical JSON bundle and one CSV for each Phase 2 table."""
    tables = _mapping(profile.get("tables"), "assessment_profile.tables")
    missing = [
        source for source in MFI_REPORT_TABLE_SPEC_BY_SOURCE if source not in tables
    ]
    if missing:
        raise MFIReportTableProjectionError(
            f"Canonical table bundle is missing: {', '.join(missing)}"
        )
    canonical = {
        source: tables[source]
        for source in MFI_REPORT_TABLE_SPEC_BY_SOURCE
    }
    downloads = [
        {
            "label": "Complete raw-table JSON",
            "file_name": "mfi_canonical_tables.json",
            "mime": "application/json",
            "data": json.dumps(
                canonical,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        }
    ]
    for source, rows in canonical.items():
        downloads.append(
            {
                "label": f"{source.replace('_rows', '').replace('_', ' ').title()} CSV",
                "file_name": f"mfi_{source.removesuffix('_rows')}_raw.csv",
                "mime": "text/csv",
                "data": _canonical_rows_csv(rows),
            }
        )
    return downloads


def _canonical_row(value: Any) -> Mapping[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    row = _mapping(value, "canonical table row")
    _mapping(row.get("values"), "canonical row values")
    return row


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if not isinstance(value, Mapping):
        raise MFIReportTableProjectionError(f"{label} must be an object")
    return value


def _canonical_rows_csv(rows: Any) -> bytes:
    if not isinstance(rows, list):
        raise MFIReportTableProjectionError("Canonical table rows must be a list")
    prepared = [_canonical_row(row) for row in rows]
    value_keys = list(
        dict.fromkeys(
            key
            for row in prepared
            for key in _mapping(row.get("values"), "canonical row values")
        )
    )
    columns = ["row_id", *value_keys, "ledger_metric_ids"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in prepared:
        values = _mapping(row.get("values"), "canonical row values")
        record = {"row_id": str(row.get("row_id") or "")}
        record.update({key: _csv_value(values.get(key)) for key in value_keys})
        record["ledger_metric_ids"] = _csv_value(
            row.get("ledger_metric_ids", []) or []
        )
        writer.writerow(record)
    return stream.getvalue().encode("utf-8-sig")


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if value is None:
        return ""
    return value
