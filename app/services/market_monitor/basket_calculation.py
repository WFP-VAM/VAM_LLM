from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd


CANONICAL_BASKET_SERIES_COLUMNS = [
    "Date",
    "BasketRole",
    "BasketVersionId",
    "BasketName",
    "ScopeType",
    "ScopeLabel",
    "Region",
    "Cost",
    "SelectedComponentCount",
    "AvailableComponentCount",
    "MissingComponentNames",
    "Complete",
]


class BasketScopeValidationError(ValueError):
    """Raised when report regions cannot satisfy a configured basket scope."""


@dataclass(frozen=True)
class BasketCalculationItem:
    commodity_id: int
    commodity_name: str
    unit_id: Optional[int]
    unit_name: str
    quantity: float
    sort_order: int
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "commodity_id": self.commodity_id,
            "commodity_name": self.commodity_name,
            "databridges_unit_id": self.unit_id,
            "databridges_unit": self.unit_name,
            "weight_quantity": self.quantity,
            "sort_order": self.sort_order,
            "item_note": self.note,
        }


@dataclass(frozen=True)
class BasketCalculationSpec:
    role: str
    basket_version_id: str
    name: str
    description: Optional[str]
    scope_type: str
    configured_regions: tuple[str, ...]
    items: tuple[BasketCalculationItem, ...]

    @classmethod
    def from_snapshot(cls, snapshot: Any, *, default_role: str = "primary") -> "BasketCalculationSpec":
        role = _wire_value(_read(snapshot, "basket_role", default_role)) or default_role
        scope_type = _wire_value(_read(snapshot, "scope_type", "national")) or "national"
        raw_items = list(_read(snapshot, "items", []) or [])
        items: list[BasketCalculationItem] = []
        for index, raw in enumerate(raw_items, start=1):
            commodity_id = _int_or_none(_read(raw, "commodity_id"))
            quantity = _float_or_none(_read(raw, "weight_quantity", 1.0))
            if commodity_id is None or quantity is None or quantity <= 0:
                continue
            items.append(
                BasketCalculationItem(
                    commodity_id=commodity_id,
                    commodity_name=str(
                        _read(raw, "commodity_name_snapshot", None)
                        or _read(raw, "commodity_name", None)
                        or f"commodity_id={commodity_id}"
                    ).strip(),
                    unit_id=_int_or_none(_read(raw, "databridges_unit_id", None)),
                    unit_name=str(
                        _read(raw, "databridges_unit", None)
                        or _read(raw, "unit", None)
                        or ""
                    ).strip(),
                    quantity=float(quantity),
                    sort_order=_int_or_none(_read(raw, "sort_order", index)) or index,
                    note=_optional_text(_read(raw, "item_note", None)),
                )
            )
        items.sort(key=lambda item: (item.sort_order, item.commodity_id))
        return cls(
            role=role,
            basket_version_id=str(_read(snapshot, "basket_version_id", "") or "").strip(),
            name=str(_read(snapshot, "basket_name", None) or ("MEB" if role == "primary" else role)).strip(),
            description=_optional_text(_read(snapshot, "short_description", None)),
            scope_type=scope_type,
            configured_regions=tuple(
                str(item).strip() for item in (_read(snapshot, "regions", []) or []) if str(item).strip()
            ),
            items=tuple(items),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "basket_role": self.role,
            "basket_version_id": self.basket_version_id,
            "basket_name": self.name,
            "short_description": self.description,
            "scope_type": self.scope_type,
            "regions": list(self.configured_regions),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class BasketCalculationResult:
    national: pd.DataFrame
    regional: pd.DataFrame
    summaries: dict[str, pd.DataFrame]
    applicable_regions: dict[str, list[str]]
    warnings: list[str] = field(default_factory=list)

    def records(self, kind: str) -> list[dict[str, Any]]:
        frame = self.national if kind == "national" else self.regional
        if frame.empty:
            return []
        records = frame.copy()
        records["Date"] = pd.to_datetime(records["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        return json.loads(records.to_json(orient="records"))


def legacy_primary_spec(items: Sequence[Mapping[str, Any]], *, basket_version_id: str = "") -> BasketCalculationSpec:
    return BasketCalculationSpec.from_snapshot(
        {
            "basket_role": "primary",
            "basket_version_id": basket_version_id,
            "basket_name": "MEB",
            "scope_type": "national",
            "regions": [],
            "items": list(items),
        }
    )


def canonicalize_run_regions(
    requested_regions: Optional[Sequence[str]],
    available_regions: Sequence[str],
) -> list[str]:
    available = _dedupe_text(available_regions)
    lookup = {item.casefold(): item for item in available}
    requested = _dedupe_text(requested_regions or [])
    if not requested:
        return available
    canonical: list[str] = []
    unknown: list[str] = []
    for item in requested:
        match = lookup.get(item.casefold())
        if match is None:
            unknown.append(item)
        elif match not in canonical:
            canonical.append(match)
    if unknown:
        raise BasketScopeValidationError(
            "Unknown report region(s): " + ", ".join(unknown) + ". Refresh country metadata and select available regions."
        )
    return canonical


def resolve_spec_regions(
    spec: BasketCalculationSpec,
    *,
    run_regions: Sequence[str],
    available_regions: Sequence[str],
) -> list[str]:
    if spec.scope_type == "national":
        return []
    available_lookup = {item.casefold(): item for item in _dedupe_text(available_regions)}
    run_keys = {item.casefold() for item in run_regions}
    configured: list[str] = []
    for raw in spec.configured_regions:
        canonical = available_lookup.get(raw.casefold(), raw)
        if canonical.casefold() in run_keys and canonical not in configured:
            configured.append(canonical)
    if not configured:
        role_label = "Secondary" if spec.role == "secondary" else "Primary"
        suffix = " Uncheck it to run the primary basket only." if spec.role == "secondary" else ""
        raise BasketScopeValidationError(
            f"{role_label} basket {spec.name!r} has no overlap between its configured regions and the report regions.{suffix}"
        )
    return configured


def calculate_basket_series(
    price_frame: pd.DataFrame,
    specs: Sequence[BasketCalculationSpec],
    *,
    full_date_index: pd.DatetimeIndex,
    run_regions: Optional[Sequence[str]],
    available_regions: Sequence[str],
) -> BasketCalculationResult:
    normalized_run_regions = canonicalize_run_regions(run_regions, available_regions)
    working = _prepare_price_frame(price_frame)
    national_rows: list[dict[str, Any]] = []
    regional_rows: list[dict[str, Any]] = []
    summaries: dict[str, pd.DataFrame] = {}
    applicable: dict[str, list[str]] = {}
    warnings: list[str] = []

    for spec in specs:
        spec_regions = resolve_spec_regions(
            spec,
            run_regions=normalized_run_regions,
            available_regions=available_regions,
        )
        applicable[spec.role] = list(
            normalized_run_regions if spec.scope_type == "national" else spec_regions
        )
        if any(item.unit_id is None for item in spec.items):
            warnings.append(
                f"{spec.role.title()} basket {spec.name!r} contains legacy component unit metadata; "
                "unit-name or commodity-level matching was used where necessary."
            )
        warnings.extend(_unit_drift_warnings(working, spec))

        mean_index = _ComponentMeanIndex(working, spec.items)
        if spec.scope_type == "national":
            for date in full_date_index:
                national_rows.append(_series_row(mean_index, spec, pd.Timestamp(date), region=None))
            for region in normalized_run_regions:
                for date in full_date_index:
                    regional_rows.append(_series_row(mean_index, spec, pd.Timestamp(date), region=region))
        else:
            for region in spec_regions:
                for date in full_date_index:
                    regional_rows.append(_series_row(mean_index, spec, pd.Timestamp(date), region=region))

        spec_national = [row for row in national_rows if row["BasketRole"] == spec.role]
        spec_regional = [row for row in regional_rows if row["BasketRole"] == spec.role]
        summaries[spec.role] = _build_summary_frame(
            spec,
            national_rows=spec_national,
            regional_rows=spec_regional,
            applicable_regions=spec_regions,
            full_date_index=full_date_index,
        )

    return BasketCalculationResult(
        national=_rows_frame(national_rows),
        regional=_rows_frame(regional_rows),
        summaries=summaries,
        applicable_regions=applicable,
        warnings=_dedupe_text(warnings),
    )


def missing_required_commodity_ids(
    price_frame: pd.DataFrame,
    specs: Sequence[BasketCalculationSpec],
    *,
    months: Sequence[pd.Timestamp],
    run_regions: Optional[Sequence[str]],
    available_regions: Sequence[str],
) -> list[int]:
    normalized_run_regions = canonicalize_run_regions(run_regions, available_regions)
    working = _prepare_price_frame(price_frame)
    missing: list[int] = []
    for spec in specs:
        regions = resolve_spec_regions(spec, run_regions=normalized_run_regions, available_regions=available_regions)
        contexts: list[Optional[str]] = [None] if spec.scope_type == "national" else list(regions)
        for item in spec.items:
            if any(not _component_has_price(working, item, pd.Timestamp(month), region) for month in months for region in contexts):
                missing.append(item.commodity_id)
    return _dedupe_ints(missing)


def calculate_basket_statistics(
    price_frame: pd.DataFrame,
    specs: Sequence[BasketCalculationSpec],
    calculation: BasketCalculationResult,
    *,
    target_date: pd.Timestamp,
) -> dict[str, Optional[dict[str, Any]]]:
    working = _prepare_price_frame(price_frame)
    output: dict[str, Optional[dict[str, Any]]] = {"primary": None, "secondary": None}
    for spec in specs:
        summary = calculation.summaries.get(spec.role, pd.DataFrame())
        stats = _statistics_for_frame(summary, target_date, spec)
        stats["applicable_regions"] = list(calculation.applicable_regions.get(spec.role, []))
        stats["scope_label"] = "National" if spec.scope_type == "national" else "Average across selected regions"
        stats["component_contributions"] = _component_contributions(
            working,
            spec,
            target_date,
            regions=calculation.applicable_regions.get(spec.role, []),
        )
        regional_stats: dict[str, Any] = {}
        if not calculation.regional.empty:
            role_rows = calculation.regional[calculation.regional["BasketRole"] == spec.role]
            for region in _dedupe_text(role_rows["Region"].dropna().astype(str).tolist()):
                regional_stats[region] = _statistics_for_frame(
                    role_rows[role_rows["Region"] == region],
                    target_date,
                    spec,
                    scope_label=region,
                )
        stats["regional_statistics"] = regional_stats
        output[spec.role] = stats
    return output


def apply_primary_food_basket_aliases(
    df_national: pd.DataFrame,
    df_regional: pd.DataFrame,
    calculation: BasketCalculationResult,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    national = df_national.copy()
    regional = df_regional.copy()
    primary_summary = calculation.summaries.get("primary")
    primary_national = calculation.national[
        calculation.national["BasketRole"] == "primary"
    ] if not calculation.national.empty else pd.DataFrame()
    if primary_national.empty:
        national["FoodBasket"] = np.nan
    else:
        values = primary_national.set_index("Date")["Cost"]
        national["FoodBasket"] = values.reindex(pd.to_datetime(national.index)).to_numpy()

    if "Date" in regional.columns and "Region" in regional.columns:
        primary_regional = calculation.regional[
            calculation.regional["BasketRole"] == "primary"
        ] if not calculation.regional.empty else pd.DataFrame()
        regional = regional.drop(columns=["FoodBasket"], errors="ignore")
        if primary_regional.empty:
            regional["FoodBasket"] = np.nan
        else:
            alias = primary_regional[["Date", "Region", "Cost"]].rename(columns={"Cost": "FoodBasket"})
            regional["Date"] = pd.to_datetime(regional["Date"], errors="coerce")
            regional = regional.merge(alias, on=["Date", "Region"], how="left")
    elif primary_summary is not None:
        regional["FoodBasket"] = np.nan
    return national, regional


def target_coverage_gaps(
    specs: Sequence[BasketCalculationSpec],
    calculation: BasketCalculationResult,
    *,
    target_date: pd.Timestamp,
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    target = pd.Timestamp(target_date).to_period("M").to_timestamp()
    for spec in specs:
        if spec.scope_type == "national":
            rows = calculation.national[
                (calculation.national["BasketRole"] == spec.role)
                & (pd.to_datetime(calculation.national["Date"]) == target)
            ] if not calculation.national.empty else pd.DataFrame()
        else:
            rows = calculation.regional[
                (calculation.regional["BasketRole"] == spec.role)
                & (pd.to_datetime(calculation.regional["Date"]) == target)
            ] if not calculation.regional.empty else pd.DataFrame()
        for row in rows.to_dict(orient="records"):
            if bool(row.get("Complete")):
                continue
            gaps.append(
                {
                    "basket_role": spec.role,
                    "basket_version_id": spec.basket_version_id,
                    "basket_name": spec.name,
                    "scope_type": spec.scope_type,
                    "scope_label": row.get("ScopeLabel"),
                    "region": row.get("Region"),
                    "missing_component_names": list(row.get("MissingComponentNames") or []),
                }
            )
    return gaps


class _ComponentMeanIndex:
    """Precomputed per-component monthly mean prices.

    Replicates _component_rows semantics -- commodity/month(/region) filtering
    with unit-id-then-unit-name preference that narrows but never empties --
    via grouped aggregations, so building long series avoids scanning the full
    price frame once per (component, month, region) cell.
    """

    def __init__(self, frame: pd.DataFrame, items: Sequence[BasketCalculationItem]) -> None:
        self._national: list[dict[Any, Optional[float]]] = [{} for _ in items]
        self._regional: list[dict[Any, Optional[float]]] = [{} for _ in items]
        if frame is None or frame.empty or not items:
            return
        commodity_ids = pd.to_numeric(frame["Commodity ID"], errors="coerce")
        unit_ids = pd.to_numeric(frame["Unit ID"], errors="coerce")
        unit_names = frame["Unit"].astype(str).str.strip().str.casefold()
        base = pd.DataFrame(
            {
                "month": frame["Price Date"],
                "region_cf": frame["Admin 1"].astype(str).str.casefold(),
                "price": frame["Price"],
            }
        )
        for item_index, item in enumerate(items):
            item_mask = commodity_ids == item.commodity_id
            if not bool(item_mask.any()):
                continue
            tier_masks: list[pd.Series] = []
            if item.unit_id is not None:
                tier_masks.append(item_mask & (unit_ids == item.unit_id))
            if item.unit_name:
                tier_masks.append(item_mask & (unit_names == item.unit_name.casefold()))
            tier_masks.append(item_mask)
            self._national[item_index] = _first_tier_means(base, tier_masks, ["month"])
            self._regional[item_index] = _first_tier_means(base, tier_masks, ["region_cf", "month"])

    def mean_price(self, item_index: int, month: pd.Timestamp, region: Optional[str]) -> Optional[float]:
        if region is None:
            return self._national[item_index].get(month)
        return self._regional[item_index].get((str(region).casefold(), month))


def _first_tier_means(
    base: pd.DataFrame,
    tier_masks: Sequence[pd.Series],
    keys: list[str],
) -> dict[Any, Optional[float]]:
    """Mean price per group taken from the first tier with rows for that group.

    A group key claimed by an earlier tier is never overwritten: rows exist in
    that tier for the cell, so later tiers must not contribute -- mirroring the
    narrowing (not emptying) unit preference of _component_rows. A tier group
    whose prices are all NaN still claims its key with None.
    """
    means: dict[Any, Optional[float]] = {}
    for mask in tier_masks:
        subset = base[mask]
        if subset.empty:
            continue
        grouped = subset.groupby(keys, sort=False)["price"].mean()
        for key, value in grouped.items():
            if key in means:
                continue
            means[key] = float(value) if pd.notna(value) else None
    return means


def _series_row(
    mean_index: _ComponentMeanIndex,
    spec: BasketCalculationSpec,
    date: pd.Timestamp,
    *,
    region: Optional[str],
) -> dict[str, Any]:
    month_key = pd.Timestamp(date).to_period("M").to_timestamp()
    contributions: list[Optional[float]] = []
    missing: list[str] = []
    for item_index, item in enumerate(spec.items):
        mean_price = mean_index.mean_price(item_index, month_key, region)
        if mean_price is None:
            contributions.append(None)
            missing.append(item.commodity_name)
        else:
            contributions.append(float(mean_price) * item.quantity)
    complete = bool(spec.items) and not missing
    cost = round(float(sum(value for value in contributions if value is not None)), 2) if complete else None
    scope_label = "National" if spec.scope_type == "national" and region is None else (
        region if region is not None else "Average across selected regions"
    )
    return {
        "Date": month_key,
        "BasketRole": spec.role,
        "BasketVersionId": spec.basket_version_id,
        "BasketName": spec.name,
        "ScopeType": spec.scope_type,
        "ScopeLabel": scope_label,
        "Region": region,
        "Cost": cost,
        "SelectedComponentCount": len(spec.items),
        "AvailableComponentCount": len(spec.items) - len(missing),
        "MissingComponentNames": missing,
        "Complete": complete,
    }


def _build_summary_frame(
    spec: BasketCalculationSpec,
    *,
    national_rows: Sequence[dict[str, Any]],
    regional_rows: Sequence[dict[str, Any]],
    applicable_regions: Sequence[str],
    full_date_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    if spec.scope_type == "national":
        return _rows_frame(national_rows)
    rows: list[dict[str, Any]] = []
    for date in full_date_index:
        date_value = pd.Timestamp(date).to_period("M").to_timestamp()
        matches = [row for row in regional_rows if pd.Timestamp(row["Date"]) == date_value]
        missing_names = [
            item.commodity_name
            for item in spec.items
            if any(item.commodity_name in (row.get("MissingComponentNames") or []) for row in matches)
        ]
        complete = len(matches) == len(applicable_regions) and bool(matches) and all(row["Complete"] for row in matches)
        cost = round(float(np.mean([row["Cost"] for row in matches])), 2) if complete else None
        rows.append(
            {
                "Date": date_value,
                "BasketRole": spec.role,
                "BasketVersionId": spec.basket_version_id,
                "BasketName": spec.name,
                "ScopeType": spec.scope_type,
                "ScopeLabel": "Average across selected regions",
                "Region": None,
                "Cost": cost,
                "SelectedComponentCount": len(spec.items),
                "AvailableComponentCount": len(spec.items) - len(missing_names),
                "MissingComponentNames": missing_names,
                "Complete": complete,
            }
        )
    return _rows_frame(rows)


def _statistics_for_frame(
    frame: pd.DataFrame,
    target_date: pd.Timestamp,
    spec: BasketCalculationSpec,
    *,
    scope_label: Optional[str] = None,
) -> dict[str, Any]:
    target = pd.Timestamp(target_date).to_period("M").to_timestamp()
    previous = target - pd.DateOffset(months=1)
    year_ago = target - pd.DateOffset(years=1)
    current = _row_at(frame, target)
    mom = _row_at(frame, previous)
    yoy = _row_at(frame, year_ago)
    current_complete = bool(current and current.get("Complete"))
    mom_complete = bool(mom and mom.get("Complete"))
    yoy_complete = bool(yoy and yoy.get("Complete"))
    current_cost = _float_or_none(current.get("Cost")) if current_complete and current else None
    mom_cost = _float_or_none(mom.get("Cost")) if mom_complete and mom else None
    yoy_cost = _float_or_none(yoy.get("Cost")) if yoy_complete and yoy else None
    missing = list(current.get("MissingComponentNames") or []) if current else [item.commodity_name for item in spec.items]
    available_names = [item.commodity_name for item in spec.items if item.commodity_name not in missing]
    stats = {
        "current_cost": _round_or_none(current_cost, 2),
        "current_price": _round_or_none(current_cost, 2),
        "mom_change_pct": _pct_change(current_cost, mom_cost),
        "yoy_change_pct": _pct_change(current_cost, yoy_cost),
        "current_complete": current_complete,
        "mom_complete": current_complete and mom_complete,
        "yoy_complete": current_complete and yoy_complete,
        "mom_reference_complete": mom_complete,
        "yoy_reference_complete": yoy_complete,
        "selected_component_count": len(spec.items),
        "selected_component_names": [item.commodity_name for item in spec.items],
        "configured_component_count": len(spec.items),
        "configured_component_names": [item.commodity_name for item in spec.items],
        "available_component_count": len(available_names),
        "available_component_names": available_names,
        "latest_component_count": len(available_names),
        "latest_component_names": available_names,
        "missing_component_names": missing,
        "missing_latest_component_names": missing,
        "scope_label": scope_label or (current.get("ScopeLabel") if current else None),
    }
    return stats


def _component_contributions(
    frame: pd.DataFrame,
    spec: BasketCalculationSpec,
    target_date: pd.Timestamp,
    *,
    regions: Sequence[str],
) -> list[dict[str, Any]]:
    target = pd.Timestamp(target_date).to_period("M").to_timestamp()
    summary_contexts: list[Optional[str]] = [None] if spec.scope_type == "national" else list(regions)
    per_item: list[tuple[BasketCalculationItem, list[dict[str, Any]], list[dict[str, Any]]]] = []
    for item in spec.items:
        summary_values: list[dict[str, Any]] = []
        for region in summary_contexts:
            price = _component_mean_price(frame, item, target, region)
            value = None if price is None else float(price) * item.quantity
            summary_values.append(
                {
                    "region": region,
                    "mean_price": _round_or_none(price, 2),
                    "absolute_contribution": _round_or_none(value, 2),
                }
            )
        by_region: list[dict[str, Any]] = []
        for region in regions:
            price = _component_mean_price(frame, item, target, region)
            value = None if price is None else float(price) * item.quantity
            by_region.append(
                {
                    "region": region,
                    "mean_price": _round_or_none(price, 2),
                    "absolute_contribution": _round_or_none(value, 2),
                }
            )
        per_item.append((item, summary_values, by_region))
    complete = bool(per_item) and all(
        all(entry["absolute_contribution"] is not None for entry in summary_values)
        for _item, summary_values, _by_region in per_item
    )
    absolute_values: list[Optional[float]] = []
    for _item, summary_values, _by_region in per_item:
        values = [
            entry["absolute_contribution"]
            for entry in summary_values
            if entry["absolute_contribution"] is not None
        ]
        absolute_values.append(float(np.mean(values)) if complete and values else None)
    total = sum(value for value in absolute_values if value is not None) if complete else None
    output: list[dict[str, Any]] = []
    for (item, _summary_values, by_region), absolute in zip(per_item, absolute_values):
        if total and absolute is not None:
            share = round(absolute / total * 100.0, 1)
        else:
            share = None
        output.append(
            {
                "commodity_id": item.commodity_id,
                "commodity_name": item.commodity_name,
                "unit_id": item.unit_id,
                "unit": item.unit_name,
                "quantity": item.quantity,
                "absolute_contribution": _round_or_none(absolute, 2),
                "share_pct": share,
                "by_region": by_region,
            }
        )
    return output


def _prepare_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Commodity ID", "Price Date", "Price", "Admin 1", "Unit ID", "Unit"])
    working = frame.copy()
    working["Price Date"] = pd.to_datetime(working["Price Date"], errors="coerce").dt.to_period("M").dt.to_timestamp()
    working["Price"] = pd.to_numeric(working["Price"], errors="coerce")
    if "Unit ID" not in working.columns:
        working["Unit ID"] = None
    if "Unit" not in working.columns:
        working["Unit"] = ""
    return working


def _unit_drift_warnings(frame: pd.DataFrame, spec: BasketCalculationSpec) -> list[str]:
    """Report components whose cached price rows never match the configured unit.

    The unit filter in _component_rows silently falls back to unit-agnostic
    matching in that case; this keeps the drift visible to operators.
    """
    if frame.empty:
        return []
    warnings: list[str] = []
    commodity_ids = pd.to_numeric(frame["Commodity ID"], errors="coerce")
    unit_ids = pd.to_numeric(frame["Unit ID"], errors="coerce")
    for item in spec.items:
        if item.unit_id is None:
            continue
        commodity_mask = commodity_ids == item.commodity_id
        if not bool(commodity_mask.any()):
            continue
        if not bool((unit_ids[commodity_mask] == item.unit_id).any()):
            warnings.append(
                f"{spec.role.title()} basket {spec.name!r}: cached prices for "
                f"{item.commodity_name} do not match the configured unit "
                f"(id {item.unit_id}); unit-agnostic matching was used."
            )
    return warnings


def _component_rows(frame: pd.DataFrame, item: BasketCalculationItem, date: pd.Timestamp, region: Optional[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    rows = frame[
        (pd.to_numeric(frame["Commodity ID"], errors="coerce") == item.commodity_id)
        & (frame["Price Date"] == pd.Timestamp(date).to_period("M").to_timestamp())
    ]
    if region is not None:
        rows = rows[rows["Admin 1"].astype(str).str.casefold() == str(region).casefold()]
    # Unit matching narrows to unit-consistent rows but never empties the
    # selection: completeness must depend only on a price existing for the
    # commodity/month/region, matching the pre-second-basket semantics.
    if item.unit_id is not None:
        matching = rows[pd.to_numeric(rows["Unit ID"], errors="coerce") == item.unit_id]
        if matching.empty and item.unit_name:
            matching = rows[rows["Unit"].astype(str).str.strip().str.casefold() == item.unit_name.casefold()]
        if not matching.empty:
            rows = matching
    elif item.unit_name:
        matching = rows[rows["Unit"].astype(str).str.strip().str.casefold() == item.unit_name.casefold()]
        if not matching.empty:
            rows = matching
    return rows[pd.to_numeric(rows["Price"], errors="coerce").notna()]


def _component_mean_price(frame: pd.DataFrame, item: BasketCalculationItem, date: pd.Timestamp, region: Optional[str]) -> Optional[float]:
    rows = _component_rows(frame, item, date, region)
    if rows.empty:
        return None
    value = pd.to_numeric(rows["Price"], errors="coerce").mean()
    return None if pd.isna(value) else float(value)


def _component_has_price(frame: pd.DataFrame, item: BasketCalculationItem, date: pd.Timestamp, region: Optional[str]) -> bool:
    return _component_mean_price(frame, item, date, region) is not None


def _row_at(frame: pd.DataFrame, date: pd.Timestamp) -> Optional[dict[str, Any]]:
    if frame is None or frame.empty:
        return None
    matches = frame[pd.to_datetime(frame["Date"], errors="coerce") == pd.Timestamp(date)]
    return matches.iloc[0].to_dict() if not matches.empty else None


def _rows_frame(rows: Sequence[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=CANONICAL_BASKET_SERIES_COLUMNS)
    frame = pd.DataFrame(list(rows), columns=CANONICAL_BASKET_SERIES_COLUMNS)
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    return frame.sort_values(["Date", "BasketRole", "Region"], na_position="first").reset_index(drop=True)


def _pct_change(current: Optional[float], reference: Optional[float]) -> Optional[float]:
    if current is None or reference in (None, 0):
        return None
    return round((float(current) - float(reference)) / float(reference) * 100.0, 1)


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _wire_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        parsed = float(value) if value not in (None, "") else None
        return None if parsed is not None and np.isnan(parsed) else parsed
    except (TypeError, ValueError):
        return None


def _round_or_none(value: Any, digits: int) -> Optional[float]:
    parsed = _float_or_none(value)
    return None if parsed is None else round(parsed, digits)


def _dedupe_text(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _dedupe_ints(values: Iterable[Any]) -> list[int]:
    seen: set[int] = set()
    output: list[int] = []
    for value in values:
        parsed = _int_or_none(value)
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        output.append(parsed)
    return output
