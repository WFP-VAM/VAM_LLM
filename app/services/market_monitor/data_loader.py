"""Cache-first data loading for the Price Bulletin drafter."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services.price_cache.config import load_price_cache_config
from app.services.price_cache.databridges_adapter import DataBridgesClientAdapter
from app.services.price_cache.migrations import apply_migrations
from app.services.price_cache.repository import PriceCacheRepository
from app.services.price_cache.row_hygiene import (
    deduplicate_monthly_price_rows,
    enrich_price_rows_with_metadata,
    filter_future_monthly_price_rows,
    filter_real_monthly_price_rows,
    price_deduplication_key,
)
from app.services.price_cache.schemas import CacheStatus, CountryMetadata, MonthlyPriceRecord
from app.services.price_cache.sql_repository import SqlPriceCacheRepository, create_price_cache_engine
from app.services.market_monitor.basket_calculation import (
    BasketCalculationSpec,
    apply_primary_food_basket_aliases,
    calculate_basket_series,
    calculate_basket_statistics,
    canonicalize_run_regions,
    legacy_primary_spec,
    missing_required_commodity_ids,
    target_coverage_gaps,
)
from app.services.market_monitor.price_backfill import (
    BasketReferenceMonthMissing,
    CommodityGapStatus,
    PriceRequirement,
    ReportPriceBackfillUnavailable,
    ReportPriceDataResult,
    ReportPriceGapReport,
)
from app.services.market_monitor.features import normalize_secondary_request
from app.shared.countries import (
    COUNTRY_CURRENCIES,
    normalize_country_name as _normalize_country_name,
    resolve_country,
)

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 15 * 60
_RECENT_METADATA_MONTHS = 36
_CHART_HISTORY_MONTHS = 73
_REAL_PRICE_FLAG_COMPONENTS = {"actual", "aggregate", "aggregated"}
_FX_NEAR_STATIC_MAX_RANGE_RATIO = 0.005
_FX_FOOD_BASKET_USD_MAX = 250.0
_FX_COMMODITY_MEDIAN_USD_MAX = 20.0
_FX_MIN_COMMODITY_USD_SAMPLE = 3
_STAPLE_NAME_TOKENS = ("maize", "rice", "wheat", "sorghum", "millet", "teff", "barley", "flour")
_KG_UNITS = {"kg", "kgs", "kilogram", "kilograms"}
_LITRE_UNITS = {"l", "lt", "liter", "litre", "liters", "litres"}
_HEAD_UNITS = {"head", "heads"}
_PIECE_UNITS = {"piece", "pieces", "1 piece", "unit", "units"}

# Worker-side ETL telemetry that operators need but officers should not be
# shown as report warnings.
_OPERATOR_WARNING_MARKERS = (
    "CommodityUnits/List",
    "price metadata references",
    "non-real monthly price row",
    "future-dated monthly price row",
    "duplicate monthly price",
    "conflicting price values",
)

_COUNTRY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_COMMODITY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_PRICE_CACHE: dict[tuple[Any, ...], tuple[float, pd.DataFrame]] = {}
_METADATA_CACHE: dict[Any, tuple[float, dict[str, Any]]] = {}
_REPORTABLE_MONTHS_CACHE: dict[Any, tuple[float, dict[str, Any]]] = {}
_PRICE_CACHE_REPOSITORY: Optional[PriceCacheRepository] = None
_PRICE_CACHE_REPOSITORY_LOCK = threading.Lock()
_ACTIVE_CACHE_VERSION_ID: Optional[str] = None
_ACTIVE_CACHE_VERSION_LOCK = threading.Lock()


class PriceCacheUnavailableError(RuntimeError):
    """Raised when the bulletin cannot be served from the active cache."""


def normalize_country_name(country: str) -> str:
    """Normalize a UI country value to the canonical country name."""
    return _normalize_country_name(country)


def reset_market_monitor_caches_for_tests() -> None:
    global _PRICE_CACHE_REPOSITORY, _ACTIVE_CACHE_VERSION_ID
    _COUNTRY_CACHE.clear()
    _COMMODITY_CACHE.clear()
    _PRICE_CACHE.clear()
    _METADATA_CACHE.clear()
    _REPORTABLE_MONTHS_CACHE.clear()
    _PRICE_CACHE_REPOSITORY = None
    _ACTIVE_CACHE_VERSION_ID = None


def _sync_active_cache_version(active_version_id: Optional[str]) -> None:
    """Invalidate in-process data caches when PriceCache publishes a new version."""
    global _ACTIVE_CACHE_VERSION_ID
    version = str(active_version_id or "")
    with _ACTIVE_CACHE_VERSION_LOCK:
        if _ACTIVE_CACHE_VERSION_ID is None:
            _ACTIVE_CACHE_VERSION_ID = version
            return
        if _ACTIVE_CACHE_VERSION_ID == version:
            return
        _COUNTRY_CACHE.clear()
        _COMMODITY_CACHE.clear()
        _PRICE_CACHE.clear()
        _METADATA_CACHE.clear()
        _REPORTABLE_MONTHS_CACHE.clear()
        _ACTIVE_CACHE_VERSION_ID = version


def get_cache_status_snapshot() -> dict[str, Any]:
    repo = _get_price_cache_repository()
    status = repo.get_cache_status()
    _sync_active_cache_version(status.active_version_id)
    return _cache_status_dict(status)


def get_supported_countries() -> List[Dict[str, Any]]:
    """Return only countries with an active PriceCache country snapshot."""
    repo = _get_price_cache_repository()
    status = repo.get_cache_status()
    _sync_active_cache_version(status.active_version_id)

    cached = _cache_get(_COUNTRY_CACHE, "countries")
    if cached is not None:
        return [dict(item) for item in cached]

    if not status.has_active_cache:
        _cache_set(_COUNTRY_CACHE, "countries", [])
        return []

    countries: list[dict[str, Any]] = []
    for country in repo.list_countries():
        availability = repo.get_country_availability(country.country_iso3)
        has_data = bool(availability and availability.date_start and availability.date_end)
        date_range = None
        latest_cached_date = None
        cache_version_id = country.cache_version_id
        if availability is not None:
            cache_version_id = availability.cache_version_id
            date_range, latest_cached_date, _ = _bounded_cache_dates(
                availability.date_start,
                availability.date_end,
                availability.latest_price_date,
            )
        countries.append(
            {
                "name": country.country_name,
                "iso3": country.country_iso3,
                "currency_code": country.currency_code,
                "currency_name": country.currency_name,
                "has_data": has_data,
                "source": "PriceCache",
                "cache_version_id": cache_version_id,
                "latest_cached_date": latest_cached_date,
                "date_range": date_range,
            }
        )
    countries = sorted(countries, key=lambda item: str(item["name"]))
    _cache_set(_COUNTRY_CACHE, "countries", countries)
    return [dict(item) for item in countries]


def get_available_countries(df: Optional[pd.DataFrame] = None) -> List[str]:
    if df is not None and "Country" in df.columns:
        return sorted(df["Country"].dropna().astype(str).unique().tolist())
    return [str(item["name"]) for item in get_supported_countries() if item.get("has_data")]


def get_available_commodities(
    df_or_country: Optional[Any] = None,
    country: Optional[str] = None,
) -> List[str]:
    """Return commodity names available for a country."""
    if isinstance(df_or_country, pd.DataFrame):
        if not country:
            return []
        country_normalized = normalize_country_name(country)
        country_df = df_or_country[df_or_country["Country"] == country_normalized]
        return sorted(country_df["Commodity"].dropna().astype(str).unique().tolist())

    country_value = country or str(df_or_country or "")
    canonical, iso3 = resolve_country(country_value)
    start_date, end_date = _recent_price_window()
    rows = _get_country_price_df(
        canonical,
        iso3,
        start_date=start_date,
        end_date=end_date,
        latest_value_only=True,
    )
    if rows.empty:
        rows = _get_country_price_df(canonical, iso3, latest_value_only=True)
    names = sorted(rows["Commodity"].dropna().astype(str).unique().tolist()) if not rows.empty else []
    if names:
        return names
    return [item["name"] for item in _get_commodities(canonical, iso3)]


def get_all_commodities(df: Optional[pd.DataFrame] = None) -> List[str]:
    if df is not None and "Commodity" in df.columns:
        return sorted(df["Commodity"].dropna().astype(str).unique().tolist())
    names: set[str] = set()
    for country in get_supported_countries():
        try:
            names.update(get_available_commodities(str(country["name"])))
        except Exception:
            logger.debug("Could not fetch commodities for %s", country["name"], exc_info=True)
    return sorted(names)


def get_commodity_categories(source: Optional[Any] = None) -> Dict[str, List[str]]:
    """Group commodity names into broad report-friendly categories."""
    if isinstance(source, pd.DataFrame):
        commodities = get_all_commodities(source)
    elif isinstance(source, list):
        commodities = [str(item.get("name", item)) if isinstance(item, dict) else str(item) for item in source]
    else:
        commodities = []

    categories: Dict[str, List[str]] = {
        "Cereals": [],
        "Pulses": [],
        "Oil": [],
        "Sugar": [],
        "Condiments": [],
        "Vegetables": [],
        "Livestock": [],
        "Fuel": [],
        "Exchange Rate": [],
        "Milling": [],
        "Wage": [],
        "Other": [],
    }
    for commodity in sorted({c for c in commodities if c}):
        lowered = commodity.lower()
        if any(token in lowered for token in ["sorghum", "maize", "wheat", "rice", "millet", "bread"]):
            categories["Cereals"].append(commodity)
        elif any(token in lowered for token in ["beans", "lentil", "pea", "chickpea", "pulse"]):
            categories["Pulses"].append(commodity)
        elif "oil" in lowered:
            categories["Oil"].append(commodity)
        elif "sugar" in lowered:
            categories["Sugar"].append(commodity)
        elif "salt" in lowered:
            categories["Condiments"].append(commodity)
        elif any(token in lowered for token in ["cabbage", "tomato", "onion", "vegetable", "leaves", "cassava"]):
            categories["Vegetables"].append(commodity)
        elif "livestock" in lowered or any(token in lowered for token in ["goat", "sheep", "cattle", "chicken"]):
            categories["Livestock"].append(commodity)
        elif "fuel" in lowered or any(token in lowered for token in ["petrol", "diesel", "gasoline"]):
            categories["Fuel"].append(commodity)
        elif "exchange" in lowered:
            categories["Exchange Rate"].append(commodity)
        elif "milling" in lowered:
            categories["Milling"].append(commodity)
        elif "wage" in lowered or "labour" in lowered:
            categories["Wage"].append(commodity)
        else:
            categories["Other"].append(commodity)
    return {name: values for name, values in categories.items() if values}


def get_country_metadata(country: str) -> Dict[str, Any]:
    canonical, iso3 = resolve_country(country)
    repo = _get_price_cache_repository()
    status = repo.get_cache_status()
    _sync_active_cache_version(status.active_version_id)

    if not status.has_active_cache:
        raise PriceCacheUnavailableError("No active PriceCache version is available. Run a cache refresh first.")

    country_cache_version = repo.get_active_version_id_for_country(iso3) or status.active_version_id or ""
    cache_key = (iso3, country_cache_version)
    cached = _cache_get(_METADATA_CACHE, cache_key)
    if cached is not None:
        return dict(cached)

    cache_metadata = repo.get_country_metadata(iso3)
    if cache_metadata is None:
        raise PriceCacheUnavailableError(
            f"No active PriceCache metadata is available for {canonical} ({iso3})."
        )
    availability = repo.get_country_availability(iso3)
    if availability is None:
        raise PriceCacheUnavailableError(
            f"No active PriceCache availability is available for {canonical} ({iso3})."
        )

    country_record = cache_metadata.country
    country_name = country_record.country_name or canonical
    currency = _country_currency(country_record, fallback_country=canonical)
    priced_ids = {int(item) for item in availability.priced_commodity_ids or []}
    raw_commodities = list(cache_metadata.commodities)
    priced_commodities = [item for item in raw_commodities if int(item.commodity_id) in priced_ids]
    unpriced_commodities = [item for item in raw_commodities if int(item.commodity_id) not in priced_ids]
    missing_unit_ids = [
        int(item.commodity_id)
        for item in raw_commodities
        if item.commodity_unit_id is None or not str(item.commodity_unit_name or "").strip()
    ]
    price_row_units = (
        _price_row_units_by_commodity(repo, iso3, availability, commodity_ids=missing_unit_ids)
        if missing_unit_ids
        else {}
    )

    def commodity_payload(item: Any, *, priced: bool) -> dict[str, Any]:
        return {
            "id": item.commodity_id,
            "name": item.commodity_name,
            "category": item.category_name or _infer_category(item.commodity_name),
            "unit_id": item.commodity_unit_id or price_row_units.get(item.commodity_id, {}).get("unit_id"),
            "unit": item.commodity_unit_name or price_row_units.get(item.commodity_id, {}).get("unit"),
            "unit_name": item.commodity_unit_name or price_row_units.get(item.commodity_id, {}).get("unit"),
            "priced": priced,
        }

    commodities = [
        commodity_payload(item, priced=True)
        for item in sorted(priced_commodities, key=lambda item: (item.commodity_name.lower(), item.commodity_id))
    ]
    unpriced_payload = [
        commodity_payload(item, priced=False)
        for item in sorted(unpriced_commodities, key=lambda item: (item.commodity_name.lower(), item.commodity_id))
    ]
    commodity_names = [str(item["name"]) for item in commodities if item.get("name")]

    units = _merge_units(cache_metadata, availability.units)
    markets = _markets_payload(cache_metadata, country_name=country_name, iso3=iso3)
    regions = availability.admin1_names or sorted(
        {str(item["admin1_name"]) for item in markets if item.get("admin1_name")}
    )
    date_range, latest_cached_date, has_future_dates = _bounded_cache_dates(
        availability.date_start,
        availability.date_end,
        availability.latest_price_date,
    )
    warnings, operator_warnings = _split_warning_audiences(_cache_warnings(status, country_iso3=iso3))
    if not priced_ids:
        warnings.append(f"PriceCache has no priced commodity IDs for {country_name}; basket setup is disabled.")
    if date_range is None:
        warnings.append(f"PriceCache has no monthly price date range for {country_name}.")
    if has_future_dates:
        warnings.append(
            "PriceCache contains future-dated monthly prices for this country; "
            f"time period options are capped at the current month ({_current_month_start().strftime('%Y-%m')})."
        )
    if not units:
        warnings.append(
            "No commodity unit metadata is available in the active cache for this country; report rows may show blank units."
        )

    metadata = {
        "country": country_name,
        "iso3": iso3,
        "currency": currency,
        "commodities": commodities,
        "unpriced_commodities": unpriced_payload,
        "units": units,
        "commodity_categories": get_commodity_categories(commodity_names),
        "default_commodities": _select_default_commodities(commodity_names),
        "regions": regions,
        "markets": markets,
        "date_range": date_range,
        "metadata_price_window": {
            "start": date_range.get("start") if date_range else None,
            "end": date_range.get("end") if date_range else None,
            "months": None,
            "bounded": False,
            "source": "PriceCache",
        },
        "latest_cached_date": latest_cached_date,
        "cache_version_id": availability.cache_version_id,
        "source": "PriceCache",
        "cache_status": _cache_status_dict(status),
        "warnings": warnings,
        "operator_warnings": operator_warnings,
    }
    _cache_set(_METADATA_CACHE, cache_key, metadata)
    return dict(metadata)


def get_reportable_months(
    country: str,
    *,
    basket_version_id: Optional[str] = None,
    primary_basket_version_id: Optional[str] = None,
    include_secondary_basket: bool = False,
    secondary_basket_version_id: Optional[str] = None,
    admin1_list: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    include_secondary_basket, secondary_basket_version_id = normalize_secondary_request(
        {
            "include_secondary_basket": include_secondary_basket,
            "secondary_basket_version_id": secondary_basket_version_id,
        }
    )
    canonical, iso3 = resolve_country(country)
    repo = _get_price_cache_repository()
    status = repo.get_cache_status()
    _sync_active_cache_version(status.active_version_id)
    if not status.has_active_cache:
        raise PriceCacheUnavailableError("No active PriceCache version is available. Run a cache refresh first.")

    availability = repo.get_country_availability(iso3)
    cache_version_id = getattr(availability, "cache_version_id", None) if availability else None
    requested_primary = str(primary_basket_version_id or basket_version_id or "").strip() or None
    explicit_selection = bool(
        requested_primary
        or include_secondary_basket
        or str(secondary_basket_version_id or "").strip()
        or list(admin1_list or [])
    )
    specs, normalized_inclusion = _resolve_reportability_specs(
        canonical,
        iso3,
        explicit_selection=explicit_selection,
        primary_basket_version_id=requested_primary,
        include_secondary_basket=include_secondary_basket,
        secondary_basket_version_id=secondary_basket_version_id,
    )
    available_regions = list(getattr(availability, "admin1_names", None) or []) if availability else []
    normalized_regions = canonicalize_run_regions(admin1_list, available_regions)
    primary_id = next((spec.basket_version_id for spec in specs if spec.role == "primary"), None)
    secondary_id = next((spec.basket_version_id for spec in specs if spec.role == "secondary"), None)
    cache_key = (
        iso3,
        cache_version_id or "",
        primary_id or "",
        secondary_id or "",
        bool(normalized_inclusion),
        tuple(normalized_regions),
    )
    cached = _cache_get(_REPORTABLE_MONTHS_CACHE, cache_key)
    if cached is not None:
        return dict(cached)

    result = _build_role_aware_reportable_months_payload(
        canonical=canonical,
        iso3=iso3,
        repo=repo,
        availability=availability,
        specs=specs,
        secondary_included=normalized_inclusion,
        run_regions=normalized_regions,
    )
    _cache_set(_REPORTABLE_MONTHS_CACHE, cache_key, result)
    return dict(result)


def refresh_reportable_months_from_databridges(
    country: str,
    *,
    basket_version_id: Optional[str] = None,
    primary_basket_version_id: Optional[str] = None,
    include_secondary_basket: bool = False,
    secondary_basket_version_id: Optional[str] = None,
    admin1_list: Optional[Sequence[str]] = None,
    adapter: Optional[DataBridgesClientAdapter] = None,
) -> Dict[str, Any]:
    include_secondary_basket, secondary_basket_version_id = normalize_secondary_request(
        {
            "include_secondary_basket": include_secondary_basket,
            "secondary_basket_version_id": secondary_basket_version_id,
        }
    )
    canonical, iso3 = resolve_country(country)
    legacy_request = not any(
        [
            str(primary_basket_version_id or "").strip(),
            include_secondary_basket,
            str(secondary_basket_version_id or "").strip(),
            list(admin1_list or []),
        ]
    )
    selection_kwargs = {
        "basket_version_id": basket_version_id,
        "primary_basket_version_id": primary_basket_version_id,
        "include_secondary_basket": include_secondary_basket,
        "secondary_basket_version_id": secondary_basket_version_id,
        "admin1_list": list(admin1_list or []),
    }
    before = get_reportable_months(country) if legacy_request else get_reportable_months(country, **selection_kwargs)
    warnings: list[str] = []
    if not _manual_refresh_enabled():
        return _manual_refresh_response(
            canonical=canonical,
            iso3=iso3,
            status="unavailable",
            before=before,
            warnings=["Manual DataBridges refresh is disabled."],
        )

    repo = _get_price_cache_repository()
    availability = repo.get_country_availability(iso3)
    if availability is None or not availability.cache_version_id:
        raise PriceCacheUnavailableError(f"No active PriceCache availability is available for {canonical} ({iso3}).")
    source_cache_version_id = availability.cache_version_id
    if legacy_request:
        basket = _get_active_food_basket(iso3)
        specs = [BasketCalculationSpec.from_snapshot(basket)] if basket is not None else []
        requested_basket_version = str(basket_version_id or "").strip()
        if basket is not None and requested_basket_version and requested_basket_version != basket.basket_version_id:
            from app.services.market_monitor.food_basket import BasketVersionConflict

            raise BasketVersionConflict(
                "The selected primary basket version is no longer active. Refresh the country basket and run the report again."
            )
    else:
        specs, _normalized_inclusion = _resolve_reportability_specs(
            canonical,
            iso3,
            explicit_selection=True,
            primary_basket_version_id=str(primary_basket_version_id or basket_version_id or "").strip() or None,
            include_secondary_basket=include_secondary_basket,
            secondary_basket_version_id=secondary_basket_version_id,
        )
    if not specs:
        return _manual_refresh_response(
            canonical=canonical,
            iso3=iso3,
            status="no_update",
            before=before,
            source_cache_version_id=source_cache_version_id,
            warnings=[f"No active food basket is configured for {canonical}; refresh skipped."],
        )
    required_ids = sorted({item.commodity_id for spec in specs for item in spec.items})

    start_month, end_month = _manual_refresh_window(before)
    months_checked = _month_labels_between(start_month, end_month)
    if not months_checked:
        return _manual_refresh_response(
            canonical=canonical,
            iso3=iso3,
            status="no_update",
            before=before,
            source_cache_version_id=source_cache_version_id,
            checked_start_month=_month_label(start_month),
            checked_end_month=_month_label(end_month),
            months_checked=[],
            required_commodity_ids=required_ids,
            warnings=["The active cache already has a reportable current month."],
        )

    priced_ids = set(required_ids)
    if not priced_ids:
        return _manual_refresh_response(
            canonical=canonical,
            iso3=iso3,
            status="no_update",
            before=before,
            source_cache_version_id=source_cache_version_id,
            checked_start_month=_month_label(start_month),
            checked_end_month=_month_label(end_month),
            months_checked=months_checked,
            required_commodity_ids=required_ids,
            warnings=[f"PriceCache has no priced commodity IDs for {canonical}; refresh skipped."],
        )

    adapter = adapter or DataBridgesClientAdapter()
    try:
        raw_rows = adapter.fetch_monthly_price_rows(
            iso3,
            start_date=start_month.isoformat(),
            end_date=_month_end(end_month).isoformat(),
        )
    except Exception as exc:
        return _manual_refresh_response(
            canonical=canonical,
            iso3=iso3,
            status="unavailable",
            before=before,
            source_cache_version_id=source_cache_version_id,
            checked_start_month=_month_label(start_month),
            checked_end_month=_month_label(end_month),
            months_checked=months_checked,
            required_commodity_ids=required_ids,
            rows_fetched=0,
            warnings=[f"DataBridges refresh unavailable: {_safe_adapter_error(exc)}"],
        )

    try:
        commodities = adapter.fetch_commodities(iso3)
    except Exception as exc:
        commodities = []
        warnings.append(f"Could not refresh DataBridges commodity metadata: {_safe_adapter_error(exc)}")
    try:
        markets = adapter.fetch_markets(iso3)
    except Exception as exc:
        markets = []
        warnings.append(f"Could not refresh DataBridges market metadata: {_safe_adapter_error(exc)}")

    priced_rows = [
        dict(row)
        for row in raw_rows
        if _to_int(row.get("commodity_id")) in priced_ids
    ]
    real_filter = filter_real_monthly_price_rows(priced_rows)
    future_filter = filter_future_monthly_price_rows(real_filter.rows, current_month_start=_current_month_start())
    enriched = enrich_price_rows_with_metadata(future_filter.rows, markets=markets, commodities=commodities)
    deduped = deduplicate_monthly_price_rows(enriched)
    source_records = repo.get_price_window(
        iso3,
        start_month.isoformat(),
        _month_end(end_month).isoformat(),
        commodity_ids=sorted(priced_ids),
    )
    source_keys = {price_deduplication_key(_monthly_record_to_row_dict(row)) for row in source_records}
    new_rows = [row for row in deduped.rows if price_deduplication_key(row) not in source_keys]
    skipped_existing = len(deduped.rows) - len(new_rows)

    if not new_rows:
        warnings.extend(_manual_refresh_filter_warnings(real_filter, future_filter, deduped))
        after = before
        return _manual_refresh_response(
            canonical=canonical,
            iso3=iso3,
            status="no_update",
            before=before,
            after=after,
            source_cache_version_id=source_cache_version_id,
            checked_start_month=_month_label(start_month),
            checked_end_month=_month_label(end_month),
            months_checked=months_checked,
            required_commodity_ids=required_ids,
            rows_fetched=len(raw_rows),
            rows_real=len(real_filter.rows),
            rows_saved=0,
            rows_skipped_existing=skipped_existing,
            excluded_non_real_rows=real_filter.excluded_rows,
            excluded_future_rows=future_filter.excluded_rows,
            deduplicated_rows=deduped.duplicate_rows,
            warnings=warnings,
        )

    new_version_id = repo.create_cache_version(
        refresh_type="manual_country_latest",
        triggered_by="manual",
        source_host=_optional_text(getattr(getattr(adapter, "config", None), "base_url", None)),
        source_env=_optional_text(getattr(getattr(adapter, "config", None), "env", None)),
    )
    repo.copy_country_snapshot(
        source_cache_version_id=source_cache_version_id,
        target_cache_version_id=new_version_id,
        country_iso3=iso3,
    )
    repo.upsert_country_metadata(
        cache_version_id=new_version_id,
        country_iso3=iso3,
        commodities=commodities,
        markets=markets,
    )
    rows_saved = repo.upsert_monthly_prices(
        cache_version_id=new_version_id,
        country_iso3=iso3,
        prices=new_rows,
    )
    rows_total = repo.count_country_price_rows(cache_version_id=new_version_id, country_iso3=iso3)
    repo.record_country_result(
        cache_version_id=new_version_id,
        country_iso3=iso3,
        status="success",
        rows_prices=rows_total,
        rows_commodities=len(commodities),
        rows_markets=len(markets),
        latest_price_date=max(
            [parsed for parsed in (_date_obj(row.get("price_date")) for row in new_rows) if parsed is not None],
            default=None,
        ),
        validation_summary={
            "manual_country_latest": True,
            "source_cache_version_id": source_cache_version_id,
            "rows_fetched": len(raw_rows),
            "rows_real": len(real_filter.rows),
            "rows_saved": rows_saved,
            "rows_skipped_existing": skipped_existing,
            "excluded_non_real_rows": real_filter.excluded_rows,
            "excluded_future_rows": future_filter.excluded_rows,
            "deduplicated_rows": deduped.duplicate_rows,
            "warnings": list(warnings),
        },
    )
    repo.finalize_cache_version(
        new_version_id,
        status="partial_active",
        validation_summary={"manual_country_latest": True, "country_iso3": iso3},
    )
    repo.set_country_active_version(iso3, new_version_id)
    _invalidate_country_price_cache(iso3)
    after = get_reportable_months(country) if legacy_request else get_reportable_months(country, **selection_kwargs)
    warnings.extend(_manual_refresh_filter_warnings(real_filter, future_filter, deduped))
    return _manual_refresh_response(
        canonical=canonical,
        iso3=iso3,
        status="updated" if rows_saved else "no_update",
        before=before,
        after=after,
        source_cache_version_id=source_cache_version_id,
        new_cache_version_id=new_version_id,
        checked_start_month=_month_label(start_month),
        checked_end_month=_month_label(end_month),
        months_checked=months_checked,
        required_commodity_ids=required_ids,
        rows_fetched=len(raw_rows),
        rows_real=len(real_filter.rows),
        rows_saved=rows_saved,
        rows_skipped_existing=skipped_existing,
        excluded_non_real_rows=real_filter.excluded_rows,
        excluded_future_rows=future_filter.excluded_rows,
        deduplicated_rows=deduped.duplicate_rows,
        warnings=warnings,
    )


def get_cache_metadata_for_report(country: str) -> Dict[str, Any]:
    metadata = get_country_metadata(country)
    status = metadata.get("cache_status") or get_cache_status_snapshot()
    return {
        "source": "PriceCache",
        "cache_version_id": metadata.get("cache_version_id"),
        "active_version_id": status.get("active_version_id"),
        "country_active_version_id": metadata.get("cache_version_id"),
        "status": status.get("status"),
        "active_country_count": status.get("active_country_count"),
        "latest_cached_date": metadata.get("latest_cached_date"),
        "date_range": metadata.get("date_range"),
        "activated_at": status.get("activated_at"),
        "completed_at": status.get("completed_at"),
        "warnings": metadata.get("warnings") or [],
        "operator_warnings": metadata.get("operator_warnings") or [],
    }


def extract_time_series_from_csv(
    country: str,
    time_period: str,
    commodities: List[str],
    admin1_list: List[str],
    csv_path: Optional[Path] = None,
    lookback_months: int = 13,
    return_raw_rows: bool = False,
    currency_code: Optional[str] = None,
    basket_items: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame] | Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Compatibility wrapper that returns PriceCache price time series."""
    if csv_path is not None:
        logger.info("Ignoring csv_path=%s because PriceCache is now the price source.", csv_path)

    canonical, iso3 = resolve_country(country)
    target_date = _parse_time_period(time_period)
    start_date = target_date - pd.DateOffset(months=lookback_months - 1)
    end_date = target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)
    full_date_index = pd.date_range(start=start_date, end=target_date, freq="MS")

    valid_names, commodity_ids, missing = _resolve_commodities(canonical, iso3, commodities)
    basket_components = _normalise_basket_components(basket_items)
    for component in basket_components:
        name = component["commodity_name"]
        commodity_id = component["commodity_id"]
        if name not in valid_names:
            valid_names.append(name)
        if commodity_id not in commodity_ids:
            commodity_ids.append(commodity_id)
    if missing:
        logger.warning("Requested commodities not available in PriceCache for %s: %s", canonical, missing)
    if not valid_names:
        raise ValueError(
            f"No requested commodities are available in the active PriceCache for {canonical}. "
            f"Available commodities: {get_available_commodities(canonical)}"
        )

    df = _get_country_price_df(
        canonical,
        iso3,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
        commodity_ids=commodity_ids,
    )
    if df.empty:
        raise ValueError(
            f"PriceCache returned no monthly price rows for {canonical} from "
            f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}."
        )

    df = df[df["Commodity"].isin(valid_names)].copy()
    if df.empty:
        raise ValueError(f"No PriceCache price rows matched the requested commodities: {valid_names}")

    df, selected_currency, excluded_currencies = _select_report_currency(
        df,
        requested_code=currency_code,
        country=canonical,
    )
    if excluded_currencies:
        logger.warning(
            "Multiple currencies present in PriceCache rows for %s; report uses %s and excludes %s.",
            canonical,
            selected_currency,
            excluded_currencies,
        )
    if df.empty:
        raise ValueError(
            f"No PriceCache price rows remained for {canonical} after selecting a single report currency."
        )

    return _build_time_series_from_price_df(
        df,
        canonical=canonical,
        valid_names=valid_names,
        admin1_list=admin1_list,
        full_date_index=full_date_index,
        basket_components=basket_components,
        return_raw_rows=return_raw_rows,
    )


def resolve_report_price_data(
    country: str,
    time_period: str,
    commodities: List[str],
    admin1_list: List[str],
    *,
    basket_items: Optional[List[Dict[str, Any]]] = None,
    basket_specs: Optional[Sequence[BasketCalculationSpec | Mapping[str, Any]]] = None,
    currency_code: Optional[str] = None,
    lookback_months: int = 13,
    enable_backfill: bool = True,
    adapter: Optional[DataBridgesClientAdapter] = None,
    enabled_modules: Optional[Sequence[str]] = None,
) -> ReportPriceDataResult:
    """Resolve report-scope prices from cache plus ephemeral targeted backfill."""
    canonical, iso3 = resolve_country(country)
    target_date = _parse_time_period(time_period)
    start_date = target_date - pd.DateOffset(months=lookback_months - 1)
    end_date = target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)
    full_date_index = pd.date_range(start=start_date, end=target_date, freq="MS")
    window_months = [month.strftime("%Y-%m") for month in full_date_index]
    reference_month = target_date.strftime("%Y-%m")

    cache_metadata = get_cache_metadata_for_report(canonical)
    valid_names, commodity_ids, missing = _resolve_commodities(canonical, iso3, commodities)
    resolved_basket_specs = [
        item if isinstance(item, BasketCalculationSpec) else BasketCalculationSpec.from_snapshot(item)
        for item in (basket_specs or [])
    ]
    if not resolved_basket_specs and basket_items:
        resolved_basket_specs = [legacy_primary_spec(basket_items)]
    primary_spec = next((item for item in resolved_basket_specs if item.role == "primary"), None)
    primary_items = [item.to_dict() for item in primary_spec.items] if primary_spec else list(basket_items or [])
    basket_components = _normalise_basket_components(primary_items)
    name_by_id = _commodity_names_by_id(canonical, iso3)
    for spec in resolved_basket_specs:
        for item in spec.items:
            name_by_id[item.commodity_id] = item.commodity_name
            if item.commodity_name not in valid_names:
                valid_names.append(item.commodity_name)
            if item.commodity_id not in commodity_ids:
                commodity_ids.append(item.commodity_id)
    for commodity_id in commodity_ids:
        name = name_by_id.get(commodity_id)
        if name and name not in valid_names:
            valid_names.append(name)
    optional_module_names = _primary_driven_optional_module_names(
        valid_names,
        resolved_basket_specs,
    )

    warnings: list[str] = []
    if missing:
        warning = f"Requested commodities not available in PriceCache for {canonical}: {missing}."
        logger.warning(warning)
        warnings.append(warning)
    if not valid_names:
        raise ValueError(
            f"No requested commodities are available in the active PriceCache for {canonical}. "
            f"Available commodities: {get_available_commodities(canonical)}"
        )

    repo = _get_price_cache_repository()
    availability = repo.get_country_availability(iso3)
    cache_version_id = getattr(availability, "cache_version_id", None) or cache_metadata.get("cache_version_id") or ""
    available_regions = list(getattr(availability, "admin1_names", None) or [])
    normalized_run_regions = canonicalize_run_regions(admin1_list, available_regions)
    commodity_ids = _dedupe_ints(commodity_ids)
    requirements, commodity_specs = _build_report_price_requirements(
        valid_names=valid_names,
        commodity_ids=commodity_ids,
        basket_components=basket_components,
        name_by_id=name_by_id,
        window_months=window_months,
        reference_month=reference_month,
    )

    cache_records = _read_report_price_records(
        iso3,
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        commodity_ids=commodity_ids,
    )
    df = _price_records_to_report_df(cache_records, canonical, iso3, name_by_id)
    df = df[df["Commodity ID"].isin(commodity_ids)].copy() if not df.empty else df
    df, selected_currency, excluded_currencies = _select_report_currency(
        df,
        requested_code=currency_code,
        country=canonical,
    )
    if excluded_currencies:
        warnings.append(
            f"Cached prices for {canonical} are quoted in multiple currencies; report uses "
            f"{selected_currency} and excludes {excluded_currencies}."
        )

    statuses = _detect_report_price_gaps(
        df,
        commodity_specs=commodity_specs,
        window_months=window_months,
        reference_month=reference_month,
    )
    gap_report = ReportPriceGapReport(
        country=canonical,
        iso3=iso3,
        time_period=time_period,
        reference_month=reference_month,
        window_start=window_months[0],
        window_end=window_months[-1],
        requirements=requirements,
        commodity_statuses=statuses,
        warnings=warnings,
    )

    gapped_ids = [
        status.commodity_id
        for status in statuses
        if status.missing_reference_month or status.missing_soft_months
    ]
    if resolved_basket_specs:
        gapped_ids = _dedupe_ints(
            gapped_ids
            + missing_required_commodity_ids(
                df,
                resolved_basket_specs,
                months=list(full_date_index),
                run_regions=normalized_run_regions,
                available_regions=available_regions,
            )
        )
    backfill_info: dict[int, dict[str, Any]] = {}
    merged_records = list(cache_records)
    if enable_backfill and gapped_ids:
        adapter = adapter or DataBridgesClientAdapter()
        backfill_info, backfilled_rows = _fetch_targeted_report_backfill(
            adapter,
            iso3=iso3,
            commodity_ids=gapped_ids,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
        )
        flag_filtered = filter_real_monthly_price_rows(backfilled_rows)
        future_filtered = filter_future_monthly_price_rows(
            flag_filtered.rows,
            current_month_start=_current_month_start(),
        )
        deduped = deduplicate_monthly_price_rows(future_filtered.rows)
        if flag_filtered.excluded_rows:
            warnings.append(
                f"Targeted DataBridges backfill excluded {flag_filtered.excluded_rows} non-real monthly price row(s)."
            )
        if future_filtered.excluded_rows:
            warnings.append(
                "Targeted DataBridges backfill excluded "
                f"{future_filtered.excluded_rows} future-dated monthly price row(s)."
            )
        if deduped.duplicate_rows:
            warnings.append(
                f"Targeted DataBridges backfill deduplicated {deduped.duplicate_rows} duplicate monthly price row(s)."
            )
        backfilled_records = _databridges_rows_to_monthly_records(
            deduped.rows,
            cache_version_id=str(cache_version_id or "targeted-backfill"),
            source_label="DataBridges targeted backfill",
        )
        merged_records = _merge_price_records_cache_first(cache_records, backfilled_records)
        df = _price_records_to_report_df(merged_records, canonical, iso3, name_by_id)
        df = df[df["Commodity ID"].isin(commodity_ids)].copy() if not df.empty else df
        df, selected_currency, excluded_currencies = _select_report_currency(
            df,
            requested_code=currency_code,
            country=canonical,
        )
        statuses = _detect_report_price_gaps(
            df,
            commodity_specs=commodity_specs,
            window_months=window_months,
            reference_month=reference_month,
            backfill_info=backfill_info,
        )
        gap_report.commodity_statuses = statuses
        gap_report.backfill_attempted = True
        gap_report.backfill_rows_fetched = sum(int(info.get("fetched_rows") or 0) for info in backfill_info.values())

    _classify_unresolved_gap_sources(gap_report, df, backfill_info=backfill_info)
    hard_missing = gap_report.hard_missing_statuses()
    if hard_missing and not resolved_basket_specs:
        if any(status.source_status == "source_error" for status in hard_missing):
            raise ReportPriceBackfillUnavailable(gap_report)
        raise BasketReferenceMonthMissing(gap_report)

    frames = _build_time_series_from_price_df(
        df,
        canonical=canonical,
        valid_names=valid_names,
        admin1_list=admin1_list,
        full_date_index=full_date_index,
        basket_components=basket_components,
        return_raw_rows=True,
        allow_empty=True,
    )
    df_national, df_regional, raw_rows = frames
    basket_calculation = calculate_basket_series(
        df,
        resolved_basket_specs,
        full_date_index=full_date_index,
        run_regions=normalized_run_regions,
        available_regions=available_regions,
    )
    if resolved_basket_specs:
        df_national, df_regional = apply_primary_food_basket_aliases(
            df_national,
            df_regional,
            basket_calculation,
        )
        coverage_gaps = target_coverage_gaps(
            resolved_basket_specs,
            basket_calculation,
            target_date=target_date,
        )
        gap_report.basket_coverage_gaps = coverage_gaps
        if coverage_gaps:
            if any(info.get("error") for info in backfill_info.values()):
                raise ReportPriceBackfillUnavailable(gap_report)
            raise BasketReferenceMonthMissing(gap_report)
    basket_role_statistics = calculate_basket_statistics(
        df,
        resolved_basket_specs,
        basket_calculation,
        target_date=target_date,
    )
    warnings.extend(basket_calculation.warnings)
    spec_by_role = {spec.role: spec for spec in resolved_basket_specs}
    for role, payload in basket_role_statistics.items():
        if not payload:
            continue
        spec = spec_by_role.get(role)
        label = f"{role} basket {spec.name!r}" if spec else f"{role} basket"
        if not payload.get("mom_reference_complete"):
            warnings.append(f"{label} has incomplete previous-month coverage; MoM is unavailable.")
        if not payload.get("yoy_reference_complete"):
            warnings.append(f"{label} has incomplete year-ago coverage; YoY is unavailable.")
    if not basket_calculation.regional.empty:
        target_regional = basket_calculation.regional[
            pd.to_datetime(basket_calculation.regional["Date"], errors="coerce") == target_date
        ]
        for row in target_regional.to_dict(orient="records"):
            spec = spec_by_role.get(str(row.get("BasketRole") or ""))
            if spec is None or spec.scope_type != "national" or bool(row.get("Complete")):
                continue
            warnings.append(
                f"{spec.role.title()} basket {spec.name!r} regional breakdown for {row.get('Region')} was omitted "
                f"because components are incomplete: {', '.join(row.get('MissingComponentNames') or [])}."
            )
    selected_currency_code = _resolve_selected_currency_code(
        df,
        selected_currency,
        requested_code=currency_code,
        country=canonical,
    )
    df_history_national = _build_chart_history_national(
        canonical=canonical,
        iso3=iso3,
        target_date=target_date,
        valid_names=valid_names,
        admin1_list=admin1_list,
        basket_components=basket_components,
        commodity_ids=commodity_ids,
        name_by_id=name_by_id,
        currency_code=selected_currency_code or currency_code,
    )
    fuel_result = _resolve_fuel_energy_series(
        canonical=canonical,
        iso3=iso3,
        target_date=target_date,
        full_date_index=full_date_index,
        currency_code=selected_currency_code or currency_code,
        enabled_modules=enabled_modules,
    )
    for column, series in fuel_result["series"].items():
        df_national[column] = series.reindex(full_date_index)
    fuel_history = fuel_result.get("history")
    if isinstance(fuel_history, pd.DataFrame) and not fuel_history.empty:
        for column in fuel_history.columns:
            df_history_national[column] = fuel_history[column]
    livestock_result = _resolve_livestock_animal_products_series(
        canonical=canonical,
        iso3=iso3,
        target_date=target_date,
        full_date_index=full_date_index,
        currency_code=selected_currency_code or currency_code,
        enabled_modules=enabled_modules,
    )
    for column, series in livestock_result["series"].items():
        df_national[column] = series.reindex(full_date_index)
    livestock_history = livestock_result.get("history")
    if isinstance(livestock_history, pd.DataFrame) and not livestock_history.empty:
        for column in livestock_history.columns:
            df_history_national[column] = livestock_history[column]
    labour_result = _resolve_labour_market_series(
        canonical=canonical,
        iso3=iso3,
        target_date=target_date,
        full_date_index=full_date_index,
        currency_code=selected_currency_code or currency_code,
        enabled_modules=enabled_modules,
        price_frame=df_national,
        history_price_frame=df_history_national,
        basket_components=basket_components,
        valid_names=optional_module_names,
    )
    for column, series in labour_result["series"].items():
        df_national[column] = series.reindex(full_date_index)
    labour_history = labour_result.get("history")
    if isinstance(labour_history, pd.DataFrame) and not labour_history.empty:
        for column in labour_history.columns:
            df_history_national[column] = labour_history[column]
    fx_result = _resolve_exchange_rate_series(
        iso3=iso3,
        currency_code=selected_currency_code,
        currency_name=selected_currency,
        full_date_index=full_date_index,
        adapter=adapter,
        price_frame=df_national,
    )
    for column, series in fx_result["series"].items():
        df_national[column] = series.reindex(full_date_index)
    result_warnings = _dedupe_preserve_order(warnings + gap_report.warning_messages())
    result_warnings = _dedupe_preserve_order(result_warnings + fuel_result["warnings"])
    result_warnings = _dedupe_preserve_order(result_warnings + livestock_result["warnings"])
    result_warnings = _dedupe_preserve_order(result_warnings + labour_result["warnings"])
    result_warnings = _dedupe_preserve_order(result_warnings + fx_result["warnings"])
    gap_report.warnings = result_warnings

    target_metadata = {
        "attempted": gap_report.backfill_attempted,
        "report_scoped": True,
        "commodity_ids": sorted(set(gapped_ids)),
        "rows_fetched": gap_report.backfill_rows_fetched,
        "source": "DataBridges targeted backfill",
    }
    cache_metadata = dict(cache_metadata)
    cache_metadata["source"] = "PriceCache + targeted DataBridges backfill" if target_metadata["attempted"] else "PriceCache"
    cache_metadata["currency_code"] = selected_currency_code
    cache_metadata["fuel_energy"] = fuel_result["metadata"]
    cache_metadata["livestock_animal_products"] = livestock_result["metadata"]
    cache_metadata["labour_market"] = labour_result["metadata"]
    cache_metadata["fx"] = fx_result["metadata"]
    cache_metadata["targeted_backfill"] = target_metadata
    cache_metadata["price_gap_report"] = gap_report.to_dict()
    return ReportPriceDataResult(
        df_national=df_national,
        df_regional=df_regional,
        raw_rows=raw_rows,
        warnings=result_warnings,
        gap_report=gap_report,
        cache_metadata=cache_metadata,
        exchange_rate_data=fx_result["exchange_rate_data"],
        fuel_energy_data=fuel_result["fuel_energy_data"],
        livestock_animal_products_data=livestock_result["livestock_animal_products_data"],
        labour_market_data=labour_result["labour_market_data"],
        df_history_national=df_history_national,
        basket_series_national=basket_calculation.national,
        basket_series_regional=basket_calculation.regional,
        basket_statistics=basket_role_statistics,
        basket_calculation_specs=[spec.to_dict() for spec in resolved_basket_specs],
        basket_applicable_regions=basket_calculation.applicable_regions,
    )


def _market_monitor_fx_enabled() -> bool:
    raw = os.getenv("MARKET_MONITOR_FX_ENABLED")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _market_monitor_fuel_enabled() -> bool:
    raw = os.getenv("MARKET_MONITOR_FUEL_ENABLED")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _market_monitor_livestock_enabled() -> bool:
    raw = os.getenv("MARKET_MONITOR_LIVESTOCK_ENABLED")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _market_monitor_labour_enabled() -> bool:
    raw = os.getenv("MARKET_MONITOR_LABOUR_ENABLED")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _module_requested(enabled_modules: Optional[Sequence[str]], module_id: str) -> bool:
    return module_id in {str(item or "").strip() for item in enabled_modules or []}


def _fuel_transport_kind(name: Any) -> Optional[str]:
    text = str(name or "").strip().lower()
    if not text.startswith("fuel ("):
        return None
    if "diesel" in text:
        return "diesel"
    if "petrol" in text or "gasoline" in text or "super petrol" in text or "parallel" in text:
        return "petrol_gasoline"
    return None


def _fuel_display_name(kind: str) -> str:
    if kind == "diesel":
        return "Diesel"
    if kind == "petrol_gasoline":
        return "Petrol/Gasoline"
    return str(kind or "Fuel").replace("_", " ").title()


def _fuel_column_name(kind: str) -> str:
    if kind == "diesel":
        return "Fuel (diesel)"
    if kind == "petrol_gasoline":
        return "Fuel (petrol/gasoline)"
    return f"Fuel ({str(kind or 'other').replace('_', ' ')})"


def _fuel_kind_sort_key(kind: str) -> tuple[int, str]:
    order = {"diesel": 0, "petrol_gasoline": 1}
    return (order.get(kind, 99), kind)


def _fuel_transport_candidates(canonical: str, iso3: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for commodity in _get_commodities(canonical, iso3):
        commodity_id = _to_int(commodity.get("id"))
        name = str(commodity.get("name") or "").strip()
        kind = _fuel_transport_kind(name)
        if commodity_id is None or not name or kind is None or commodity_id in seen:
            continue
        seen.add(commodity_id)
        candidates.append(
            {
                "commodity_id": commodity_id,
                "commodity_name": name,
                "kind": kind,
                "label": _fuel_display_name(kind),
            }
        )
    return sorted(candidates, key=lambda item: (_fuel_kind_sort_key(str(item["kind"])), item["commodity_name"].lower()))


def _resolve_fuel_energy_series(
    *,
    canonical: str,
    iso3: str,
    target_date: pd.Timestamp,
    full_date_index: pd.DatetimeIndex,
    currency_code: Optional[str],
    enabled_modules: Optional[Sequence[str]],
) -> dict[str, Any]:
    requested = _module_requested(enabled_modules, "fuel_energy")
    metadata: dict[str, Any] = {
        "enabled": _market_monitor_fuel_enabled(),
        "requested": requested,
        "source": "not_requested" if not requested else "PriceCache",
        "currency_code": currency_code,
        "unit": f"{str(currency_code or 'LCU').strip().upper() or 'LCU'}/Litre",
        "rows_fetched": 0,
        "series_count": 0,
        "candidates": [],
        "omitted_candidates": [],
        "error": None,
    }
    empty_history = pd.DataFrame(index=full_date_index)
    empty = {
        "series": {},
        "history": empty_history,
        "warnings": [],
        "metadata": metadata,
        "fuel_energy_data": None,
    }
    if not requested:
        return empty
    if not metadata["enabled"]:
        metadata["source"] = "disabled"
        return {
            **empty,
            "warnings": ["Fuel & Energy omitted: MARKET_MONITOR_FUEL_ENABLED is disabled."],
            "metadata": metadata,
        }

    candidates = _fuel_transport_candidates(canonical, iso3)
    metadata["candidates"] = [
        {
            "commodity_id": item["commodity_id"],
            "commodity_name": item["commodity_name"],
            "kind": item["kind"],
        }
        for item in candidates
    ]
    if not candidates:
        return {
            **empty,
            "warnings": [f"Fuel & Energy omitted: no transport fuel price series available for {canonical}."],
            "metadata": metadata,
        }

    commodity_ids = _dedupe_ints([item["commodity_id"] for item in candidates])
    name_by_id = {int(item["commodity_id"]): str(item["commodity_name"]) for item in candidates}
    candidate_by_id = {int(item["commodity_id"]): item for item in candidates}
    try:
        records = _read_report_price_records(
            iso3,
            full_date_index[0].strftime("%Y-%m-%d"),
            (target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d"),
            commodity_ids=commodity_ids,
        )
    except Exception as exc:
        metadata["source"] = "error"
        metadata["error"] = _safe_adapter_error(exc)
        return {
            **empty,
            "warnings": [f"Fuel & Energy omitted: {metadata['error']}"],
            "metadata": metadata,
        }

    metadata["rows_fetched"] = len(records)
    df = _price_records_to_report_df(records, canonical, iso3, name_by_id)
    if not df.empty:
        df = df[df["Commodity ID"].isin(commodity_ids)].copy()
    df, selected_currency, excluded_currencies = _select_report_currency(
        df,
        requested_code=currency_code,
        country=canonical,
    )
    if selected_currency and len(str(selected_currency).strip()) == 3:
        metadata["currency_code"] = str(selected_currency).strip().upper()
        metadata["unit"] = f"{metadata['currency_code']}/Litre"
    warnings: list[str] = []
    if excluded_currencies:
        warnings.append(
            f"Fuel prices for {canonical} are quoted in multiple currencies; report uses "
            f"{metadata['currency_code']} and excludes {excluded_currencies}."
        )

    frame, series_payloads, omitted = _fuel_frame_and_payload(
        df,
        candidate_by_id=candidate_by_id,
        full_date_index=full_date_index,
        target_date=target_date,
        currency_code=str(metadata["currency_code"] or currency_code or "LCU"),
    )
    metadata["omitted_candidates"] = omitted
    metadata["series_count"] = len(series_payloads)
    if not series_payloads:
        warnings.append(
            f"Fuel & Energy omitted: no transport fuel series for {canonical} has enough monthly observations."
        )
        return {
            **empty,
            "warnings": _dedupe_preserve_order(warnings),
            "metadata": metadata,
        }

    history = _resolve_fuel_history_frame(
        canonical=canonical,
        iso3=iso3,
        target_date=target_date,
        candidates=candidates,
        currency_code=str(metadata["currency_code"] or currency_code or "LCU"),
    )
    primary = series_payloads[0]
    fuel_energy_data = {
        "available": True,
        "country": canonical,
        "currency_code": metadata["currency_code"],
        "unit": metadata["unit"],
        "latest_month": max(str(item["latest_month"]) for item in series_payloads),
        "primary_series": primary["kind"],
        "series": series_payloads,
        "regional_disparities": _fuel_regional_disparities(df, candidate_by_id, series_payloads),
        "driver_hint": _fuel_driver_hint(primary),
        "source": metadata["source"],
    }
    return {
        "series": {column: frame[column] for column in frame.columns},
        "history": history,
        "warnings": _dedupe_preserve_order(warnings),
        "metadata": metadata,
        "fuel_energy_data": fuel_energy_data,
    }


def _fuel_frame_and_payload(
    df: pd.DataFrame,
    *,
    candidate_by_id: dict[int, dict[str, Any]],
    full_date_index: pd.DatetimeIndex,
    target_date: pd.Timestamp,
    currency_code: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    frame = pd.DataFrame(index=full_date_index)
    if df.empty:
        return frame, [], [
            {
                "commodity_id": item["commodity_id"],
                "commodity_name": item["commodity_name"],
                "kind": item["kind"],
                "reason": "no_price_rows",
            }
            for item in candidate_by_id.values()
        ]

    working = df.copy()
    working["Commodity ID"] = pd.to_numeric(working["Commodity ID"], errors="coerce")
    working = working[working["Commodity ID"].notna()].copy()
    working["Commodity ID"] = working["Commodity ID"].astype(int)
    working = working[working["Commodity ID"].isin(candidate_by_id.keys())].copy()
    working["Price"] = pd.to_numeric(working["Price"], errors="coerce")
    working["Price Date"] = pd.to_datetime(working["Price Date"], errors="coerce")
    working = working[working["Price"].notna() & working["Price Date"].notna()]
    if working.empty:
        return frame, [], [
            {
                "commodity_id": item["commodity_id"],
                "commodity_name": item["commodity_name"],
                "kind": item["kind"],
                "reason": "no_valid_price_rows",
            }
            for item in candidate_by_id.values()
        ]

    working["FuelKind"] = working["Commodity ID"].map(lambda cid: candidate_by_id[int(cid)]["kind"])
    working["Month"] = working["Price Date"].dt.to_period("M").dt.to_timestamp()
    monthly = (
        working.groupby(["Month", "FuelKind"], dropna=True)["Price"]
        .mean()
        .reset_index()
    )
    payloads: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for kind in sorted(working["FuelKind"].dropna().unique(), key=_fuel_kind_sort_key):
        column = _fuel_column_name(str(kind))
        series = (
            monthly[monthly["FuelKind"] == kind]
            .set_index("Month")["Price"]
            .reindex(full_date_index)
            .astype(float)
            .round(2)
        )
        stats = _fuel_stats_from_series(series, target_date=target_date)
        kind_rows = working[working["FuelKind"] == kind]
        kind_candidates = [
            candidate_by_id[int(cid)]
            for cid in sorted(kind_rows["Commodity ID"].dropna().astype(int).unique())
            if int(cid) in candidate_by_id
        ]
        if stats is None:
            for candidate in kind_candidates:
                omitted.append(
                    {
                        "commodity_id": candidate["commodity_id"],
                        "commodity_name": candidate["commodity_name"],
                        "kind": candidate["kind"],
                        "reason": "insufficient_monthly_observations",
                    }
                )
            continue
        frame[column] = series
        payloads.append(
            {
                "kind": str(kind),
                "label": _fuel_display_name(str(kind)),
                "column_name": column,
                "commodity_ids": [int(item["commodity_id"]) for item in kind_candidates],
                "commodity_names": [str(item["commodity_name"]) for item in kind_candidates],
                "current_price": stats["current_price"],
                "mom_change_pct": stats["mom_change_pct"],
                "yoy_change_pct": stats["yoy_change_pct"],
                "latest_month": stats["latest_month"],
                "previous_month": stats["previous_month"],
                "yoy_reference_month": stats["yoy_reference_month"],
                "unit": _fuel_unit_from_rows(kind_rows),
                "currency_code": currency_code,
                "axis_unit": f"{str(currency_code or 'LCU').strip().upper() or 'LCU'}/Litre",
            }
        )
    payloads.sort(key=lambda item: _fuel_kind_sort_key(str(item["kind"])))
    return frame, payloads, omitted


def _fuel_stats_from_series(series: pd.Series, *, target_date: pd.Timestamp) -> Optional[dict[str, Any]]:
    values = pd.to_numeric(series, errors="coerce")
    values = values[values.index <= target_date].dropna()
    if values.empty:
        return None
    latest_month = pd.Timestamp(values.index[-1])
    previous_month = latest_month - pd.DateOffset(months=1)
    previous_value = series.get(previous_month, np.nan)
    if pd.isna(previous_value):
        return None
    yoy_month = latest_month - pd.DateOffset(years=1)
    yoy_value = series.get(yoy_month, np.nan)
    current_value = float(values.iloc[-1])
    mom = _pct_change(current_value, previous_value)
    yoy = None if pd.isna(yoy_value) else _pct_change(current_value, yoy_value)
    return {
        "current_price": round(current_value, 2),
        "mom_change_pct": None if mom is None else round(float(mom), 1),
        "yoy_change_pct": None if yoy is None else round(float(yoy), 1),
        "latest_month": latest_month.strftime("%Y-%m"),
        "previous_month": previous_month.strftime("%Y-%m"),
        "yoy_reference_month": yoy_month.strftime("%Y-%m") if not pd.isna(yoy_value) else None,
    }


def _fuel_unit_from_rows(df: pd.DataFrame) -> str:
    if df.empty or "Unit" not in df.columns:
        return "Litre"
    values = [
        str(item or "").strip()
        for item in df["Unit"].dropna().astype(str)
        if str(item or "").strip()
    ]
    if not values:
        return "Litre"
    unit = pd.Series(values).value_counts().idxmax()
    if str(unit).strip().lower() in {"l", "lt", "liter", "litre", "litres", "liters"}:
        return "Litre"
    return str(unit).strip()


def _resolve_fuel_history_frame(
    *,
    canonical: str,
    iso3: str,
    target_date: pd.Timestamp,
    candidates: list[dict[str, Any]],
    currency_code: str,
) -> pd.DataFrame:
    history_start = target_date - pd.DateOffset(months=_CHART_HISTORY_MONTHS - 1)
    history_index = pd.date_range(start=history_start, end=target_date, freq="MS")
    commodity_ids = _dedupe_ints([item["commodity_id"] for item in candidates])
    candidate_by_id = {int(item["commodity_id"]): item for item in candidates}
    name_by_id = {int(item["commodity_id"]): str(item["commodity_name"]) for item in candidates}
    try:
        records = _read_report_price_records(
            iso3,
            history_start.strftime("%Y-%m-%d"),
            (target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d"),
            commodity_ids=commodity_ids,
        )
        df = _price_records_to_report_df(records, canonical, iso3, name_by_id)
        if not df.empty:
            df = df[df["Commodity ID"].isin(commodity_ids)].copy()
        df, _selected_currency, _excluded = _select_report_currency(
            df,
            requested_code=currency_code,
            country=canonical,
        )
        frame, _payloads, _omitted = _fuel_frame_and_payload(
            df,
            candidate_by_id=candidate_by_id,
            full_date_index=history_index,
            target_date=target_date,
            currency_code=currency_code,
        )
        return frame
    except Exception as exc:
        logger.warning("Could not build fuel history for %s: %s", iso3, _safe_adapter_error(exc))
        return pd.DataFrame(index=history_index)


def _fuel_regional_disparities(
    df: pd.DataFrame,
    candidate_by_id: dict[int, dict[str, Any]],
    series_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if df.empty or "Admin 1" not in df.columns:
        return []
    working = df.copy()
    working["Commodity ID"] = pd.to_numeric(working["Commodity ID"], errors="coerce")
    working = working[working["Commodity ID"].notna()].copy()
    working["Commodity ID"] = working["Commodity ID"].astype(int)
    working = working[working["Commodity ID"].isin(candidate_by_id.keys())].copy()
    working["Price"] = pd.to_numeric(working["Price"], errors="coerce")
    working["Price Date"] = pd.to_datetime(working["Price Date"], errors="coerce")
    working = working[working["Price"].notna() & working["Price Date"].notna()].copy()
    if working.empty:
        return []
    working["FuelKind"] = working["Commodity ID"].map(lambda cid: candidate_by_id[int(cid)]["kind"])
    working["Month"] = working["Price Date"].dt.to_period("M").dt.to_timestamp()
    disparities: list[dict[str, Any]] = []
    for payload in series_payloads:
        kind = str(payload.get("kind") or "")
        latest_month = pd.Timestamp(f"{payload.get('latest_month')}-01")
        subset = working[(working["FuelKind"] == kind) & (working["Month"] == latest_month)].copy()
        subset = subset[subset["Admin 1"].notna()]
        if subset.empty:
            continue
        regional = subset.groupby("Admin 1", dropna=True)["Price"].mean().dropna()
        regional = regional[regional > 0]
        if len(regional) < 2:
            continue
        min_region = str(regional.idxmin())
        max_region = str(regional.idxmax())
        min_value = float(regional.loc[min_region])
        max_value = float(regional.loc[max_region])
        if min_value <= 0:
            continue
        spread_pct = (max_value - min_value) / min_value * 100.0
        if spread_pct < 10.0:
            continue
        disparities.append(
            {
                "kind": kind,
                "label": payload.get("label"),
                "latest_month": payload.get("latest_month"),
                "highest_region": max_region,
                "highest_price": round(max_value, 2),
                "lowest_region": min_region,
                "lowest_price": round(min_value, 2),
                "spread_pct": round(float(spread_pct), 1),
            }
        )
    return disparities


def _fuel_driver_hint(primary_series: dict[str, Any]) -> str:
    mom = primary_series.get("mom_change_pct")
    try:
        mom_value = float(mom)
    except Exception:
        return "Movement should be attributed cautiously to fuel-market, logistics, or administered price conditions."
    if mom_value > 2.0:
        return "The increase is consistent with higher fuel-market, supply/logistics, or administered price pressure."
    if mom_value < -2.0:
        return "The decline is consistent with easing fuel-market, supply/logistics, or administered price pressure."
    return "The limited month-on-month movement is consistent with broadly stable fuel-market or administered pricing conditions."


def _unit_key(unit: Any) -> str:
    text = str(unit or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    if text in _KG_UNITS:
        return "kg"
    if text in _LITRE_UNITS:
        return "l"
    if text in _HEAD_UNITS:
        return "head"
    if text in _PIECE_UNITS:
        return "piece"
    if text in {"day", "days"}:
        return "day"
    return text


def _unit_label(unit: Any, *, default: str = "unit") -> str:
    key = _unit_key(unit)
    if key == "kg":
        return "KG"
    if key == "l":
        return "L"
    if key == "head":
        return "Head"
    if key == "piece":
        return "piece"
    if key == "day":
        return "day"
    return str(unit or default).strip() or default


def _price_axis_unit(currency_code: Optional[str], unit: Any) -> str:
    code = str(currency_code or "LCU").strip().upper() or "LCU"
    return f"{code}/{_unit_label(unit)}"


def _series_stats_from_series(
    series: pd.Series,
    *,
    target_date: pd.Timestamp,
    current_key: str,
) -> Optional[dict[str, Any]]:
    values = pd.to_numeric(series, errors="coerce")
    values = values[values.index <= target_date].dropna()
    if values.empty:
        return None
    latest_month = pd.Timestamp(values.index[-1])
    previous_month = latest_month - pd.DateOffset(months=1)
    previous_value = series.get(previous_month, np.nan)
    if pd.isna(previous_value):
        return None
    yoy_month = latest_month - pd.DateOffset(years=1)
    yoy_value = series.get(yoy_month, np.nan)
    current_value = float(values.iloc[-1])
    mom = _pct_change(current_value, previous_value)
    yoy = None if pd.isna(yoy_value) else _pct_change(current_value, yoy_value)
    return {
        current_key: round(current_value, 2),
        "mom_change_pct": None if mom is None else round(float(mom), 1),
        "yoy_change_pct": None if yoy is None else round(float(yoy), 1),
        "latest_month": latest_month.strftime("%Y-%m"),
        "previous_month": previous_month.strftime("%Y-%m"),
        "yoy_reference_month": yoy_month.strftime("%Y-%m") if not pd.isna(yoy_value) else None,
    }


def _animal_product_type(name: Any, unit: Any = None) -> Optional[str]:
    text = str(name or "").strip().lower()
    unit_key = _unit_key(unit)
    if not text:
        return None
    if text.startswith("livestock (") and unit_key in {"head", ""}:
        return "live_animal"
    if text.startswith("meat (") and unit_key in {"kg", ""}:
        return "meat"
    if text.startswith("poultry") and unit_key in {"kg", ""}:
        return "meat"
    if (text == "milk" or text.startswith("milk (")) and unit_key in {"l", "kg", ""}:
        return "milk"
    if re.match(r"^eggs?(\s*\(|$)", text) and unit_key in {"piece", ""}:
        return "eggs"
    return None


def _animal_group_display(group: str) -> str:
    labels = {
        "live_animal": "Live animal",
        "meat": "Meat",
        "milk": "Milk",
        "eggs": "Eggs",
    }
    return labels.get(group, str(group or "Animal product").replace("_", " ").title())


def _animal_column_name(group: str, commodity_name: str) -> str:
    prefix = "Livestock" if group == "live_animal" else "Animal Products"
    return f"{prefix} - {str(commodity_name or '').strip()}"


def _animal_sort_key(group: str, label: str) -> tuple[int, str]:
    order = {"meat": 0, "milk": 1, "eggs": 2, "live_animal": 3}
    return (order.get(group, 99), str(label or "").lower())


def _livestock_animal_candidates(canonical: str, iso3: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for commodity in _get_commodities(canonical, iso3):
        commodity_id = _to_int(commodity.get("id"))
        name = str(commodity.get("name") or "").strip()
        unit = commodity.get("unit") or commodity.get("unit_name")
        group = _animal_product_type(name, unit)
        if commodity_id is None or not name or group is None or commodity_id in seen:
            continue
        seen.add(commodity_id)
        candidates.append(
            {
                "commodity_id": commodity_id,
                "commodity_name": name,
                "group": group,
                "label": name,
                "unit": _unit_label(unit, default="unit"),
            }
        )
    return sorted(candidates, key=lambda item: _animal_sort_key(str(item["group"]), str(item["label"])))


def _resolve_livestock_animal_products_series(
    *,
    canonical: str,
    iso3: str,
    target_date: pd.Timestamp,
    full_date_index: pd.DatetimeIndex,
    currency_code: Optional[str],
    enabled_modules: Optional[Sequence[str]],
) -> dict[str, Any]:
    requested = _module_requested(enabled_modules, "livestock_animal_products")
    metadata: dict[str, Any] = {
        "enabled": _market_monitor_livestock_enabled(),
        "requested": requested,
        "source": "not_requested" if not requested else "PriceCache",
        "currency_code": currency_code,
        "rows_fetched": 0,
        "series_count": 0,
        "chart_mode": None,
        "candidates": [],
        "omitted_candidates": [],
        "error": None,
    }
    empty_history = pd.DataFrame(index=full_date_index)
    empty = {
        "series": {},
        "history": empty_history,
        "warnings": [],
        "metadata": metadata,
        "livestock_animal_products_data": None,
    }
    if not requested:
        return empty
    if not metadata["enabled"]:
        metadata["source"] = "disabled"
        return {
            **empty,
            "warnings": ["Livestock & Animal Products omitted: MARKET_MONITOR_LIVESTOCK_ENABLED is disabled."],
            "metadata": metadata,
        }

    candidates = _livestock_animal_candidates(canonical, iso3)
    metadata["candidates"] = [
        {
            "commodity_id": item["commodity_id"],
            "commodity_name": item["commodity_name"],
            "group": item["group"],
            "unit": item.get("unit"),
        }
        for item in candidates
    ]
    if not candidates:
        return {
            **empty,
            "warnings": [f"Livestock & Animal Products omitted: no animal product price series available for {canonical}."],
            "metadata": metadata,
        }

    commodity_ids = _dedupe_ints([item["commodity_id"] for item in candidates])
    name_by_id = {int(item["commodity_id"]): str(item["commodity_name"]) for item in candidates}
    candidate_by_id = {int(item["commodity_id"]): item for item in candidates}
    try:
        records = _read_report_price_records(
            iso3,
            full_date_index[0].strftime("%Y-%m-%d"),
            (target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d"),
            commodity_ids=commodity_ids,
        )
    except Exception as exc:
        metadata["source"] = "error"
        metadata["error"] = _safe_adapter_error(exc)
        return {
            **empty,
            "warnings": [f"Livestock & Animal Products omitted: {metadata['error']}"],
            "metadata": metadata,
        }

    metadata["rows_fetched"] = len(records)
    df = _price_records_to_report_df(records, canonical, iso3, name_by_id)
    if not df.empty:
        df = df[df["Commodity ID"].isin(commodity_ids)].copy()
    df, selected_currency, excluded_currencies = _select_report_currency(
        df,
        requested_code=currency_code,
        country=canonical,
    )
    if selected_currency and len(str(selected_currency).strip()) == 3:
        metadata["currency_code"] = str(selected_currency).strip().upper()
    warnings: list[str] = []
    if excluded_currencies:
        warnings.append(
            f"Livestock and animal product prices for {canonical} are quoted in multiple currencies; report uses "
            f"{metadata['currency_code']} and excludes {excluded_currencies}."
        )

    frame, series_payloads, omitted = _animal_frame_and_payload(
        df,
        candidate_by_id=candidate_by_id,
        full_date_index=full_date_index,
        target_date=target_date,
        currency_code=str(metadata["currency_code"] or currency_code or "LCU"),
    )
    metadata["omitted_candidates"] = omitted
    metadata["series_count"] = len(series_payloads)
    if not series_payloads:
        warnings.append(
            f"Livestock & Animal Products omitted: no animal product series for {canonical} has enough monthly observations."
        )
        return {
            **empty,
            "warnings": _dedupe_preserve_order(warnings),
            "metadata": metadata,
        }

    chart = _animal_chart_metadata(series_payloads, str(metadata["currency_code"] or currency_code or "LCU"))
    metadata["chart_mode"] = chart["mode"]
    history = _resolve_animal_history_frame(
        canonical=canonical,
        iso3=iso3,
        target_date=target_date,
        candidates=candidates,
        currency_code=str(metadata["currency_code"] or currency_code or "LCU"),
    )
    primary = series_payloads[0]
    livestock_data = {
        "available": True,
        "country": canonical,
        "currency_code": metadata["currency_code"],
        "latest_month": max(str(item["latest_month"]) for item in series_payloads),
        "primary_series": primary["column_name"],
        "series": series_payloads,
        "chart": chart,
        "regional_disparities": _animal_regional_disparities(df, candidate_by_id, series_payloads),
        "driver_hint": _animal_driver_hint(primary),
        "source": metadata["source"],
    }
    return {
        "series": {column: frame[column] for column in frame.columns},
        "history": history,
        "warnings": _dedupe_preserve_order(warnings),
        "metadata": metadata,
        "livestock_animal_products_data": livestock_data,
    }


def _animal_frame_and_payload(
    df: pd.DataFrame,
    *,
    candidate_by_id: dict[int, dict[str, Any]],
    full_date_index: pd.DatetimeIndex,
    target_date: pd.Timestamp,
    currency_code: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    frame = pd.DataFrame(index=full_date_index)
    if df.empty:
        return frame, [], [
            {
                "commodity_id": item["commodity_id"],
                "commodity_name": item["commodity_name"],
                "group": item["group"],
                "reason": "no_price_rows",
            }
            for item in candidate_by_id.values()
        ]

    working = df.copy()
    working["Commodity ID"] = pd.to_numeric(working["Commodity ID"], errors="coerce")
    working = working[working["Commodity ID"].notna()].copy()
    working["Commodity ID"] = working["Commodity ID"].astype(int)
    working = working[working["Commodity ID"].isin(candidate_by_id.keys())].copy()
    working["Price"] = pd.to_numeric(working["Price"], errors="coerce")
    working["Price Date"] = pd.to_datetime(working["Price Date"], errors="coerce")
    working = working[working["Price"].notna() & working["Price Date"].notna()]
    if working.empty:
        return frame, [], [
            {
                "commodity_id": item["commodity_id"],
                "commodity_name": item["commodity_name"],
                "group": item["group"],
                "reason": "no_valid_price_rows",
            }
            for item in candidate_by_id.values()
        ]

    working["Month"] = working["Price Date"].dt.to_period("M").dt.to_timestamp()
    monthly = (
        working.groupby(["Month", "Commodity ID"], dropna=True)["Price"]
        .mean()
        .reset_index()
    )
    payloads: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for commodity_id in sorted(working["Commodity ID"].dropna().astype(int).unique()):
        candidate = candidate_by_id.get(int(commodity_id))
        if not candidate:
            continue
        column = _animal_column_name(str(candidate["group"]), str(candidate["commodity_name"]))
        series = (
            monthly[monthly["Commodity ID"] == commodity_id]
            .set_index("Month")["Price"]
            .reindex(full_date_index)
            .astype(float)
            .round(2)
        )
        stats = _series_stats_from_series(series, target_date=target_date, current_key="current_price")
        rows = working[working["Commodity ID"] == commodity_id]
        if stats is None:
            omitted.append(
                {
                    "commodity_id": candidate["commodity_id"],
                    "commodity_name": candidate["commodity_name"],
                    "group": candidate["group"],
                    "reason": "insufficient_monthly_observations",
                }
            )
            continue
        unit = _module_unit_from_rows(rows, default=str(candidate.get("unit") or "unit"))
        frame[column] = series
        payloads.append(
            {
                "group": str(candidate["group"]),
                "group_label": _animal_group_display(str(candidate["group"])),
                "label": str(candidate["label"]),
                "column_name": column,
                "commodity_id": int(candidate["commodity_id"]),
                "commodity_name": str(candidate["commodity_name"]),
                "current_price": stats["current_price"],
                "mom_change_pct": stats["mom_change_pct"],
                "yoy_change_pct": stats["yoy_change_pct"],
                "latest_month": stats["latest_month"],
                "previous_month": stats["previous_month"],
                "yoy_reference_month": stats["yoy_reference_month"],
                "unit": unit,
                "unit_key": _unit_key(unit),
                "currency_code": currency_code,
                "axis_unit": _price_axis_unit(currency_code, unit),
            }
        )
    payloads.sort(key=lambda item: _animal_sort_key(str(item["group"]), str(item["label"])))
    return frame, payloads, omitted


def _module_unit_from_rows(df: pd.DataFrame, *, default: str) -> str:
    if df.empty or "Unit" not in df.columns:
        return _unit_label(default, default=default)
    values = [
        str(item or "").strip()
        for item in df["Unit"].dropna().astype(str)
        if str(item or "").strip()
    ]
    if not values:
        return _unit_label(default, default=default)
    return _unit_label(pd.Series(values).value_counts().idxmax(), default=default)


def _animal_chart_metadata(series_payloads: list[dict[str, Any]], currency_code: str) -> dict[str, Any]:
    unit_counts = Counter(str(item.get("unit_key") or "") for item in series_payloads)
    ordered = unit_counts.most_common()
    dominant_unit = ordered[0][0] if ordered else ""
    dominant_count = ordered[0][1] if ordered else 0
    next_count = ordered[1][1] if len(ordered) > 1 else 0
    use_absolute = len(unit_counts) == 1 or (dominant_count >= 2 and dominant_count > next_count)
    if use_absolute:
        chart_series = [item for item in series_payloads if str(item.get("unit_key") or "") == dominant_unit]
        unit = chart_series[0].get("unit") if chart_series else "unit"
        return {
            "mode": "absolute",
            "series": [
                {"column_name": item["column_name"], "label": item["label"], "unit": item.get("unit")}
                for item in chart_series
            ],
            "axis_label": _price_axis_unit(currency_code, unit),
            "unit": unit,
        }
    return {
        "mode": "indexed",
        "series": [
            {"column_name": item["column_name"], "label": item["label"], "unit": item.get("unit")}
            for item in series_payloads
        ],
        "axis_label": "Index (first month = 100)",
        "unit": None,
    }


def _resolve_animal_history_frame(
    *,
    canonical: str,
    iso3: str,
    target_date: pd.Timestamp,
    candidates: list[dict[str, Any]],
    currency_code: str,
) -> pd.DataFrame:
    history_start = target_date - pd.DateOffset(months=_CHART_HISTORY_MONTHS - 1)
    history_index = pd.date_range(start=history_start, end=target_date, freq="MS")
    commodity_ids = _dedupe_ints([item["commodity_id"] for item in candidates])
    candidate_by_id = {int(item["commodity_id"]): item for item in candidates}
    name_by_id = {int(item["commodity_id"]): str(item["commodity_name"]) for item in candidates}
    try:
        records = _read_report_price_records(
            iso3,
            history_start.strftime("%Y-%m-%d"),
            (target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d"),
            commodity_ids=commodity_ids,
        )
        df = _price_records_to_report_df(records, canonical, iso3, name_by_id)
        if not df.empty:
            df = df[df["Commodity ID"].isin(commodity_ids)].copy()
        df, _selected_currency, _excluded = _select_report_currency(
            df,
            requested_code=currency_code,
            country=canonical,
        )
        frame, _payloads, _omitted = _animal_frame_and_payload(
            df,
            candidate_by_id=candidate_by_id,
            full_date_index=history_index,
            target_date=target_date,
            currency_code=currency_code,
        )
        return frame
    except Exception as exc:
        logger.warning("Could not build animal product history for %s: %s", iso3, _safe_adapter_error(exc))
        return pd.DataFrame(index=history_index)


def _animal_regional_disparities(
    df: pd.DataFrame,
    candidate_by_id: dict[int, dict[str, Any]],
    series_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if df.empty or "Admin 1" not in df.columns:
        return []
    working = df.copy()
    working["Commodity ID"] = pd.to_numeric(working["Commodity ID"], errors="coerce")
    working = working[working["Commodity ID"].notna()].copy()
    working["Commodity ID"] = working["Commodity ID"].astype(int)
    working = working[working["Commodity ID"].isin(candidate_by_id.keys())].copy()
    working["Price"] = pd.to_numeric(working["Price"], errors="coerce")
    working["Price Date"] = pd.to_datetime(working["Price Date"], errors="coerce")
    working = working[working["Price"].notna() & working["Price Date"].notna()].copy()
    if working.empty:
        return []
    working["Month"] = working["Price Date"].dt.to_period("M").dt.to_timestamp()
    disparities: list[dict[str, Any]] = []
    for payload in series_payloads:
        commodity_id = int(payload.get("commodity_id") or -1)
        latest_month = pd.Timestamp(f"{payload.get('latest_month')}-01")
        subset = working[(working["Commodity ID"] == commodity_id) & (working["Month"] == latest_month)].copy()
        subset = subset[subset["Admin 1"].notna()]
        if subset.empty:
            continue
        regional = subset.groupby("Admin 1", dropna=True)["Price"].mean().dropna()
        regional = regional[regional > 0]
        if len(regional) < 2:
            continue
        min_region = str(regional.idxmin())
        max_region = str(regional.idxmax())
        min_value = float(regional.loc[min_region])
        max_value = float(regional.loc[max_region])
        if min_value <= 0:
            continue
        spread_pct = (max_value - min_value) / min_value * 100.0
        if spread_pct < 10.0:
            continue
        disparities.append(
            {
                "commodity_id": commodity_id,
                "label": payload.get("label"),
                "latest_month": payload.get("latest_month"),
                "highest_region": max_region,
                "highest_price": round(max_value, 2),
                "lowest_region": min_region,
                "lowest_price": round(min_value, 2),
                "spread_pct": round(float(spread_pct), 1),
            }
        )
    return disparities


def _animal_driver_hint(primary_series: dict[str, Any]) -> str:
    mom = primary_series.get("mom_change_pct")
    try:
        mom_value = float(mom)
    except Exception:
        return "Movement should be attributed cautiously to seasonal demand, supply, feed, pasture, or water conditions."
    if mom_value > 2.0:
        return "The increase is consistent with seasonal demand, supply constraints, or feed, pasture, and water pressure."
    if mom_value < -2.0:
        return "The decline is consistent with easing demand, improved supply, or better feed, pasture, and water conditions."
    return "The limited month-on-month movement is consistent with broadly stable animal-product market conditions."


def _labour_kind(name: Any, unit: Any = None) -> Optional[str]:
    text = str(name or "").strip().lower()
    unit_key = _unit_key(unit)
    if not text.startswith("wage (") or unit_key not in {"day", ""}:
        return None
    if "non-qualified" in text or "non qualified" in text or "casual" in text or "unskilled" in text:
        return "casual_unskilled"
    if "qualified" in text or "skilled" in text:
        return "skilled_qualified"
    return "other_wage"


def _labour_display_name(kind: str) -> str:
    if kind == "casual_unskilled":
        return "Casual/unskilled wage"
    if kind == "skilled_qualified":
        return "Skilled/qualified wage"
    return "Daily wage"


def _labour_column_name(kind: str) -> str:
    if kind == "casual_unskilled":
        return "Labour - Casual wage"
    if kind == "skilled_qualified":
        return "Labour - Skilled wage"
    return "Labour - Daily wage"


def _labour_sort_key(kind: str) -> tuple[int, str]:
    order = {"casual_unskilled": 0, "skilled_qualified": 1, "other_wage": 2}
    return (order.get(kind, 99), kind)


def _labour_candidates(canonical: str, iso3: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for commodity in _get_commodities(canonical, iso3):
        commodity_id = _to_int(commodity.get("id"))
        name = str(commodity.get("name") or "").strip()
        unit = commodity.get("unit") or commodity.get("unit_name")
        kind = _labour_kind(name, unit)
        if commodity_id is None or not name or kind is None or commodity_id in seen:
            continue
        seen.add(commodity_id)
        candidates.append(
            {
                "commodity_id": commodity_id,
                "commodity_name": name,
                "kind": kind,
                "label": _labour_display_name(kind),
                "unit": _unit_label(unit, default="day"),
            }
        )
    return sorted(candidates, key=lambda item: (_labour_sort_key(str(item["kind"])), item["commodity_name"].lower()))


def _resolve_labour_market_series(
    *,
    canonical: str,
    iso3: str,
    target_date: pd.Timestamp,
    full_date_index: pd.DatetimeIndex,
    currency_code: Optional[str],
    enabled_modules: Optional[Sequence[str]],
    price_frame: pd.DataFrame,
    history_price_frame: pd.DataFrame,
    basket_components: list[dict[str, Any]],
    valid_names: list[str],
) -> dict[str, Any]:
    requested = _module_requested(enabled_modules, "labour_market")
    metadata: dict[str, Any] = {
        "enabled": _market_monitor_labour_enabled(),
        "requested": requested,
        "source": "not_requested" if not requested else "PriceCache",
        "currency_code": currency_code,
        "rows_fetched": 0,
        "series_count": 0,
        "candidates": [],
        "omitted_candidates": [],
        "staple": None,
        "error": None,
    }
    empty_history = pd.DataFrame(index=full_date_index)
    empty = {
        "series": {},
        "history": empty_history,
        "warnings": [],
        "metadata": metadata,
        "labour_market_data": None,
    }
    if not requested:
        return empty
    if not metadata["enabled"]:
        metadata["source"] = "disabled"
        return {
            **empty,
            "warnings": ["Labour Market omitted: MARKET_MONITOR_LABOUR_ENABLED is disabled."],
            "metadata": metadata,
        }

    candidates = _labour_candidates(canonical, iso3)
    metadata["candidates"] = [
        {
            "commodity_id": item["commodity_id"],
            "commodity_name": item["commodity_name"],
            "kind": item["kind"],
            "unit": item.get("unit"),
        }
        for item in candidates
    ]
    if not candidates:
        return {
            **empty,
            "warnings": [f"Labour Market omitted: no wage price series available for {canonical}."],
            "metadata": metadata,
        }

    commodity_ids = _dedupe_ints([item["commodity_id"] for item in candidates])
    name_by_id = {int(item["commodity_id"]): str(item["commodity_name"]) for item in candidates}
    candidate_by_id = {int(item["commodity_id"]): item for item in candidates}
    try:
        records = _read_report_price_records(
            iso3,
            full_date_index[0].strftime("%Y-%m-%d"),
            (target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d"),
            commodity_ids=commodity_ids,
        )
    except Exception as exc:
        metadata["source"] = "error"
        metadata["error"] = _safe_adapter_error(exc)
        return {
            **empty,
            "warnings": [f"Labour Market omitted: {metadata['error']}"],
            "metadata": metadata,
        }

    metadata["rows_fetched"] = len(records)
    df = _price_records_to_report_df(records, canonical, iso3, name_by_id)
    if not df.empty:
        df = df[df["Commodity ID"].isin(commodity_ids)].copy()
    df, selected_currency, excluded_currencies = _select_report_currency(
        df,
        requested_code=currency_code,
        country=canonical,
    )
    if selected_currency and len(str(selected_currency).strip()) == 3:
        metadata["currency_code"] = str(selected_currency).strip().upper()
    warnings: list[str] = []
    if excluded_currencies:
        warnings.append(
            f"Wage prices for {canonical} are quoted in multiple currencies; report uses "
            f"{metadata['currency_code']} and excludes {excluded_currencies}."
        )

    frame, wage_payloads, omitted = _labour_frame_and_payload(
        df,
        candidate_by_id=candidate_by_id,
        full_date_index=full_date_index,
        target_date=target_date,
        currency_code=str(metadata["currency_code"] or currency_code or "LCU"),
    )
    metadata["omitted_candidates"] = omitted
    metadata["series_count"] = len(wage_payloads)
    if not wage_payloads:
        warnings.append(f"Labour Market omitted: no wage series for {canonical} has enough monthly observations.")
        return {
            **empty,
            "warnings": _dedupe_preserve_order(warnings),
            "metadata": metadata,
        }

    staple = _select_labour_staple_series(
        price_frame,
        basket_components=basket_components,
        valid_names=valid_names,
    )
    metadata["staple"] = None if staple is None else {"name": staple[0], "source": staple[2]}
    primary = wage_payloads[0]
    primary_series = frame[primary["column_name"]]
    purchasing_power = None
    if staple is not None:
        staple_name, staple_series, staple_source = staple
        pp_series = (pd.to_numeric(primary_series, errors="coerce") / pd.to_numeric(staple_series, errors="coerce")).replace(
            [np.inf, -np.inf],
            np.nan,
        )
        pp_column = f"Labour - Purchasing power ({staple_name})"
        pp_stats = _series_stats_from_series(pp_series, target_date=target_date, current_key="current_kg")
        if pp_stats is not None:
            frame[pp_column] = pp_series.astype(float).round(2)
            purchasing_power = {
                "available": True,
                "column_name": pp_column,
                "label": f"{staple_name} purchasing power",
                "staple_name": staple_name,
                "staple_source": staple_source,
                "current_kg": pp_stats["current_kg"],
                "mom_change_pct": pp_stats["mom_change_pct"],
                "yoy_change_pct": pp_stats["yoy_change_pct"],
                "latest_month": pp_stats["latest_month"],
                "previous_month": pp_stats["previous_month"],
                "yoy_reference_month": pp_stats["yoy_reference_month"],
                "unit": f"kg of {staple_name} per day's wage",
            }

    history = _resolve_labour_history_frame(
        canonical=canonical,
        iso3=iso3,
        target_date=target_date,
        candidates=candidates,
        currency_code=str(metadata["currency_code"] or currency_code or "LCU"),
        history_price_frame=history_price_frame,
        purchasing_power=purchasing_power,
    )
    chart_series = []
    chart_axis = _price_axis_unit(str(metadata["currency_code"] or currency_code or "LCU"), "day")
    chart_mode = "nominal_wage"
    if purchasing_power:
        chart_mode = "purchasing_power"
        chart_axis = purchasing_power["unit"]
        chart_series.append(
            {
                "column_name": purchasing_power["column_name"],
                "label": "Purchasing power",
                "axis": "primary",
            }
        )
        chart_series.append(
            {
                "column_name": primary["column_name"],
                "label": primary["label"],
                "axis": "secondary",
                "axis_label": primary["axis_unit"],
            }
        )
    else:
        chart_series.append({"column_name": primary["column_name"], "label": primary["label"], "axis": "primary"})

    labour_data = {
        "available": True,
        "country": canonical,
        "currency_code": metadata["currency_code"],
        "latest_month": max(str(item["latest_month"]) for item in wage_payloads),
        "primary_series": primary["kind"],
        "series": wage_payloads,
        "purchasing_power": purchasing_power,
        "availability": None,
        "chart": {
            "mode": chart_mode,
            "series": chart_series,
            "axis_label": chart_axis,
        },
        "driver_hint": _labour_driver_hint(primary, purchasing_power),
        "source": metadata["source"],
    }
    return {
        "series": {column: frame[column] for column in frame.columns},
        "history": history,
        "warnings": _dedupe_preserve_order(warnings),
        "metadata": metadata,
        "labour_market_data": labour_data,
    }


def _labour_frame_and_payload(
    df: pd.DataFrame,
    *,
    candidate_by_id: dict[int, dict[str, Any]],
    full_date_index: pd.DatetimeIndex,
    target_date: pd.Timestamp,
    currency_code: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    frame = pd.DataFrame(index=full_date_index)
    if df.empty:
        return frame, [], [
            {
                "commodity_id": item["commodity_id"],
                "commodity_name": item["commodity_name"],
                "kind": item["kind"],
                "reason": "no_price_rows",
            }
            for item in candidate_by_id.values()
        ]

    working = df.copy()
    working["Commodity ID"] = pd.to_numeric(working["Commodity ID"], errors="coerce")
    working = working[working["Commodity ID"].notna()].copy()
    working["Commodity ID"] = working["Commodity ID"].astype(int)
    working = working[working["Commodity ID"].isin(candidate_by_id.keys())].copy()
    working["Price"] = pd.to_numeric(working["Price"], errors="coerce")
    working["Price Date"] = pd.to_datetime(working["Price Date"], errors="coerce")
    working = working[working["Price"].notna() & working["Price Date"].notna()]
    if working.empty:
        return frame, [], [
            {
                "commodity_id": item["commodity_id"],
                "commodity_name": item["commodity_name"],
                "kind": item["kind"],
                "reason": "no_valid_price_rows",
            }
            for item in candidate_by_id.values()
        ]

    working["LabourKind"] = working["Commodity ID"].map(lambda cid: candidate_by_id[int(cid)]["kind"])
    working["Month"] = working["Price Date"].dt.to_period("M").dt.to_timestamp()
    monthly = (
        working.groupby(["Month", "LabourKind"], dropna=True)["Price"]
        .mean()
        .reset_index()
    )
    payloads: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for kind in sorted(working["LabourKind"].dropna().unique(), key=_labour_sort_key):
        column = _labour_column_name(str(kind))
        series = (
            monthly[monthly["LabourKind"] == kind]
            .set_index("Month")["Price"]
            .reindex(full_date_index)
            .astype(float)
            .round(2)
        )
        stats = _series_stats_from_series(series, target_date=target_date, current_key="current_wage")
        kind_rows = working[working["LabourKind"] == kind]
        kind_candidates = [
            candidate_by_id[int(cid)]
            for cid in sorted(kind_rows["Commodity ID"].dropna().astype(int).unique())
            if int(cid) in candidate_by_id
        ]
        if stats is None:
            for candidate in kind_candidates:
                omitted.append(
                    {
                        "commodity_id": candidate["commodity_id"],
                        "commodity_name": candidate["commodity_name"],
                        "kind": candidate["kind"],
                        "reason": "insufficient_monthly_observations",
                    }
                )
            continue
        unit = _module_unit_from_rows(kind_rows, default="day")
        frame[column] = series
        payloads.append(
            {
                "kind": str(kind),
                "label": _labour_display_name(str(kind)),
                "column_name": column,
                "commodity_ids": [int(item["commodity_id"]) for item in kind_candidates],
                "commodity_names": [str(item["commodity_name"]) for item in kind_candidates],
                "current_wage": stats["current_wage"],
                "mom_change_pct": stats["mom_change_pct"],
                "yoy_change_pct": stats["yoy_change_pct"],
                "latest_month": stats["latest_month"],
                "previous_month": stats["previous_month"],
                "yoy_reference_month": stats["yoy_reference_month"],
                "unit": unit,
                "currency_code": currency_code,
                "axis_unit": _price_axis_unit(currency_code, unit),
            }
        )
    payloads.sort(key=lambda item: _labour_sort_key(str(item["kind"])))
    return frame, payloads, omitted


def _select_labour_staple_series(
    price_frame: pd.DataFrame,
    *,
    basket_components: list[dict[str, Any]],
    valid_names: list[str],
) -> Optional[tuple[str, pd.Series, str]]:
    if price_frame is None or price_frame.empty:
        return None
    for component in basket_components:
        name = str(component.get("commodity_name") or "").strip()
        unit = component.get("databridges_unit")
        if name in price_frame.columns and _is_staple_name(name) and _unit_key(unit) in {"kg", ""}:
            series = pd.to_numeric(price_frame[name], errors="coerce")
            if series.dropna().gt(0).any():
                return name, series, "food_basket"
    candidates: list[tuple[int, str, pd.Series]] = []
    for name in valid_names:
        if not isinstance(name, str) or name not in price_frame.columns:
            continue
        if not _is_staple_name(name):
            continue
        if _is_module_auxiliary_column(name):
            continue
        series = pd.to_numeric(price_frame[name], errors="coerce")
        usable = int(series.dropna().gt(0).sum())
        if usable <= 0:
            continue
        candidates.append((usable, name, series))
    if not candidates:
        for name in price_frame.columns:
            if not isinstance(name, str) or not _is_staple_name(name) or _is_module_auxiliary_column(name):
                continue
            series = pd.to_numeric(price_frame[name], errors="coerce")
            usable = int(series.dropna().gt(0).sum())
            if usable > 0:
                candidates.append((usable, name, series))
    if not candidates:
        return None
    usable, name, series = sorted(candidates, key=lambda item: (-item[0], item[1].lower()))[0]
    return name, series, "report_data"


def _is_staple_name(name: Any) -> bool:
    text = str(name or "").strip().lower()
    return any(token in text for token in _STAPLE_NAME_TOKENS)


def _is_module_auxiliary_column(name: Any) -> bool:
    text = str(name or "").strip().lower()
    return any(
        token in text
        for token in (
            "exchange",
            "fuel",
            "petrol",
            "diesel",
            "gasoline",
            "wage",
            "labour",
            "labor",
            "animal products -",
            "livestock -",
            "purchasing power",
            "milling",
            "transport",
            "freight",
        )
    )


def _resolve_labour_history_frame(
    *,
    canonical: str,
    iso3: str,
    target_date: pd.Timestamp,
    candidates: list[dict[str, Any]],
    currency_code: str,
    history_price_frame: pd.DataFrame,
    purchasing_power: Optional[dict[str, Any]],
) -> pd.DataFrame:
    history_start = target_date - pd.DateOffset(months=_CHART_HISTORY_MONTHS - 1)
    history_index = pd.date_range(start=history_start, end=target_date, freq="MS")
    commodity_ids = _dedupe_ints([item["commodity_id"] for item in candidates])
    candidate_by_id = {int(item["commodity_id"]): item for item in candidates}
    name_by_id = {int(item["commodity_id"]): str(item["commodity_name"]) for item in candidates}
    try:
        records = _read_report_price_records(
            iso3,
            history_start.strftime("%Y-%m-%d"),
            (target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d"),
            commodity_ids=commodity_ids,
        )
        df = _price_records_to_report_df(records, canonical, iso3, name_by_id)
        if not df.empty:
            df = df[df["Commodity ID"].isin(commodity_ids)].copy()
        df, _selected_currency, _excluded = _select_report_currency(
            df,
            requested_code=currency_code,
            country=canonical,
        )
        frame, payloads, _omitted = _labour_frame_and_payload(
            df,
            candidate_by_id=candidate_by_id,
            full_date_index=history_index,
            target_date=target_date,
            currency_code=currency_code,
        )
        if purchasing_power and payloads and isinstance(history_price_frame, pd.DataFrame):
            primary_column = payloads[0]["column_name"]
            staple_name = purchasing_power.get("staple_name")
            pp_column = purchasing_power.get("column_name")
            if (
                primary_column in frame.columns
                and isinstance(staple_name, str)
                and staple_name in history_price_frame.columns
                and isinstance(pp_column, str)
            ):
                staple_series = pd.to_numeric(history_price_frame[staple_name], errors="coerce").reindex(history_index)
                pp_series = (
                    pd.to_numeric(frame[primary_column], errors="coerce") / staple_series
                ).replace([np.inf, -np.inf], np.nan)
                frame[pp_column] = pp_series.astype(float).round(2)
        return frame
    except Exception as exc:
        logger.warning("Could not build labour market history for %s: %s", iso3, _safe_adapter_error(exc))
        return pd.DataFrame(index=history_index)


def _labour_driver_hint(primary_series: dict[str, Any], purchasing_power: Optional[dict[str, Any]]) -> str:
    mom = primary_series.get("mom_change_pct")
    try:
        mom_value = float(mom)
    except Exception:
        return "Movement should be attributed cautiously to seasonal labour demand, harvest cycles, or broader economic conditions."
    if mom_value > 2.0:
        return "The wage increase is consistent with stronger seasonal labour demand, harvest activity, or broader economic conditions."
    if mom_value < -2.0:
        return "The wage decline is consistent with weaker seasonal labour demand, harvest-cycle effects, or broader economic pressure."
    if purchasing_power:
        return "Stable nominal wages mean food purchasing power is driven mainly by staple price movements."
    return "The limited wage movement is consistent with broadly stable labour-market conditions."


def _resolve_selected_currency_code(
    df: pd.DataFrame,
    selected_currency: Optional[str],
    *,
    requested_code: Optional[str],
    country: str,
) -> Optional[str]:
    if not df.empty and "Currency Code" in df.columns:
        codes = [
            str(value).strip().upper()
            for value in df["Currency Code"].dropna().astype(str)
            if str(value).strip()
        ]
        if codes:
            return pd.Series(codes).value_counts().idxmax()
    if requested_code and str(requested_code).strip():
        return str(requested_code).strip().upper()
    default_currency = COUNTRY_CURRENCIES.get(country) or {}
    code = default_currency.get("code")
    if code:
        return str(code).strip().upper()
    if selected_currency and len(str(selected_currency).strip()) == 3:
        return str(selected_currency).strip().upper()
    return None


def _build_chart_history_national(
    *,
    canonical: str,
    iso3: str,
    target_date: pd.Timestamp,
    valid_names: list[str],
    admin1_list: list[str],
    basket_components: list[dict[str, Any]],
    commodity_ids: list[int],
    name_by_id: dict[int, str],
    currency_code: Optional[str],
) -> pd.DataFrame:
    history_start = target_date - pd.DateOffset(months=_CHART_HISTORY_MONTHS - 1)
    history_index = pd.date_range(start=history_start, end=target_date, freq="MS")
    try:
        records = _read_report_price_records(
            iso3,
            history_start.strftime("%Y-%m-%d"),
            (target_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)).strftime("%Y-%m-%d"),
            commodity_ids=commodity_ids,
        )
        df = _price_records_to_report_df(records, canonical, iso3, name_by_id)
        if not df.empty:
            df = df[df["Commodity ID"].isin(commodity_ids)].copy()
        df, _selected, _excluded = _select_report_currency(
            df,
            requested_code=currency_code,
            country=canonical,
        )
        history_frames = _build_time_series_from_price_df(
            df,
            canonical=canonical,
            valid_names=valid_names,
            admin1_list=admin1_list,
            full_date_index=history_index,
            basket_components=basket_components,
            return_raw_rows=False,
            allow_empty=True,
        )
        return history_frames[0]
    except Exception as exc:
        logger.warning("Could not build chart history for %s: %s", iso3, _safe_adapter_error(exc))
        return pd.DataFrame(index=history_index)


def _resolve_exchange_rate_series(
    *,
    iso3: str,
    currency_code: Optional[str],
    currency_name: Optional[str],
    full_date_index: pd.DatetimeIndex,
    adapter: Optional[DataBridgesClientAdapter],
    price_frame: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "enabled": _market_monitor_fx_enabled(),
        "source": "DataBridges",
        "currency_code": currency_code,
        "currency_name": currency_name,
        "rows_fetched": 0,
        "permission_denied": False,
        "error": None,
        "official": {"included": False, "missing_months": []},
        "unofficial": {"included": False, "missing_months": []},
        "usability": _empty_fx_usability_metadata(),
    }
    empty = {"series": {}, "warnings": [], "metadata": metadata, "exchange_rate_data": None}
    if not metadata["enabled"]:
        metadata["source"] = "disabled"
        return empty
    if not currency_code or str(currency_code).strip().upper() == "USD":
        metadata["source"] = "skipped"
        metadata["error"] = "No non-USD report currency was resolved."
        return empty

    fx_adapter = adapter if adapter is not None and hasattr(adapter, "fetch_exchange_rate_rows_result") else None
    if adapter is not None and fx_adapter is None:
        metadata["error"] = "Injected DataBridges adapter does not support FX rows."
        return empty
    if fx_adapter is None:
        try:
            fx_adapter = DataBridgesClientAdapter()
        except Exception as exc:
            metadata["error"] = _safe_adapter_error(exc)
            return {
                **empty,
                "warnings": [f"DataBridges FX omitted: {metadata['error']}"],
                "metadata": metadata,
            }

    try:
        fetch_result = fx_adapter.fetch_exchange_rate_rows_result(
            iso3,
            currency_name=currency_code,
            start_date=full_date_index[0].date(),
            end_date=(full_date_index[-1] + pd.DateOffset(months=1) - pd.DateOffset(days=1)).date(),
        )
    except Exception as exc:
        metadata["error"] = _safe_adapter_error(exc)
        return {
            **empty,
            "warnings": [f"DataBridges FX omitted: {metadata['error']}"],
            "metadata": metadata,
        }

    rows = list(getattr(fetch_result, "rows", []) or [])
    metadata["rows_fetched"] = len(rows)
    metadata["permission_denied"] = bool(getattr(fetch_result, "permission_denied", False))
    metadata["error"] = getattr(fetch_result, "error", None)
    if metadata["permission_denied"]:
        return {
            **empty,
            "warnings": [f"DataBridges FX omitted: {metadata['error'] or 'permission denied'}"],
            "metadata": metadata,
        }

    code = str(currency_code or "").strip().upper()
    if code:
        rows = [
            row
            for row in rows
            if not row.get("currency_code") or str(row.get("currency_code")).strip().upper() == code
        ]
    metadata["rows_matched_currency"] = len(rows)
    if not rows:
        reason = metadata["error"] or f"no exchange-rate rows found for {iso3} {code}"
        return {
            **empty,
            "warnings": [f"DataBridges FX omitted: {reason}."],
            "metadata": metadata,
        }

    series: dict[str, pd.Series] = {}
    warnings: list[str] = []
    official_series, official_missing = _monthly_exchange_rate_series(
        rows,
        full_date_index=full_date_index,
        official=True,
    )
    metadata["official"]["missing_months"] = official_missing
    if official_series is not None:
        series["ExchangeRate"] = official_series
        metadata["official"]["included"] = True
    else:
        warnings.append(f"Official FX omitted: missing {', '.join(official_missing)}.")

    unofficial_rows = [row for row in rows if row.get("is_official") is False]
    unofficial_series, unofficial_missing = _monthly_exchange_rate_series(
        rows,
        full_date_index=full_date_index,
        official=False,
    )
    metadata["unofficial"]["missing_months"] = unofficial_missing
    if unofficial_series is not None:
        series["ExchangeRateUnofficial"] = unofficial_series
        metadata["unofficial"]["included"] = True
    elif unofficial_rows:
        warnings.append(f"Unofficial FX omitted: missing {', '.join(unofficial_missing)}.")
    else:
        warnings.append("Unofficial FX omitted: no unofficial/parallel exchange-rate rows returned.")

    partial = _partial_latest_fx_month(
        rows,
        full_date_index,
        included_official=metadata["official"]["included"],
        included_unofficial=metadata["unofficial"]["included"],
    )
    metadata["partial_latest_month"] = partial
    if partial.get("partial"):
        warnings.append(
            "DataBridges FX latest month "
            f"{partial['month']} is partial through {partial['through_date']}; "
            "monthly averages use available observations."
        )

    usability = _assess_exchange_rate_usability(
        official_series=series.get("ExchangeRate"),
        unofficial_series=series.get("ExchangeRateUnofficial"),
        price_frame=price_frame,
    )
    metadata["usability"] = usability
    selected_series_name = usability.get("selected_series")
    if selected_series_name == "unofficial":
        if "ExchangeRate" in series:
            series.pop("ExchangeRate", None)
            metadata["official"]["included"] = False
        warnings.append(
            "Official FX omitted: near-static official rate is inconsistent with the local price scale; "
            "using unofficial/parallel FX for exchange-rate analysis."
        )
    elif selected_series_name is None and series.get("ExchangeRate") is not None:
        series.clear()
        metadata["official"]["included"] = False
        reason = usability.get("omitted_reason") or "official FX did not pass usability checks"
        warnings.append(f"DataBridges FX omitted: {reason}.")

    selected_column = "ExchangeRate" if selected_series_name == "official" else "ExchangeRateUnofficial"
    selected_rate_series = series.get(selected_column)
    exchange_rate_data = _exchange_rate_data_from_series(
        selected_rate_series,
        currency_code=code,
        target_date=full_date_index[-1],
        source_series=str(selected_series_name) if selected_series_name else None,
        rate_type=str(selected_series_name) if selected_series_name else None,
    )
    return {
        "series": series,
        "warnings": warnings,
        "metadata": metadata,
        "exchange_rate_data": exchange_rate_data,
    }


def _monthly_exchange_rate_series(
    rows: list[dict[str, Any]],
    *,
    full_date_index: pd.DatetimeIndex,
    official: bool,
) -> tuple[Optional[pd.Series], list[str]]:
    if not rows:
        return None, [month.strftime("%Y-%m") for month in full_date_index]
    df = pd.DataFrame(rows)
    if df.empty or "date" not in df.columns or "value" not in df.columns:
        return None, [month.strftime("%Y-%m") for month in full_date_index]
    if "is_official" not in df.columns:
        df["is_official"] = True
    df = df[df["is_official"] == official].copy()
    if df.empty:
        return None, [month.strftime("%Y-%m") for month in full_date_index]
    df["Date"] = pd.to_datetime(df["date"], errors="coerce")
    df["Value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["Date", "Value"])
    if df.empty:
        return None, [month.strftime("%Y-%m") for month in full_date_index]
    df["Month"] = df["Date"].dt.to_period("M").dt.to_timestamp()
    monthly = df.groupby("Month")["Value"].mean().reindex(full_date_index)
    missing = [month.strftime("%Y-%m") for month in full_date_index if pd.isna(monthly.loc[month])]
    if missing:
        return None, missing
    return monthly.astype(float).round(6), []


def _empty_fx_usability_metadata() -> dict[str, Any]:
    return {
        "official_usable": None,
        "selected_series": None,
        "omitted_reason": None,
        "near_static": False,
        "price_scale_inconsistent": False,
        "official_range_ratio": None,
        "official_range_pct": None,
        "official_unique_rounded_values": None,
        "food_basket_usd": None,
        "commodity_median_usd": None,
        "commodity_usd_sample_size": 0,
    }


def _assess_exchange_rate_usability(
    *,
    official_series: Optional[pd.Series],
    unofficial_series: Optional[pd.Series],
    price_frame: Optional[pd.DataFrame],
) -> dict[str, Any]:
    usability = _empty_fx_usability_metadata()
    official_present = official_series is not None and not official_series.dropna().empty
    unofficial_present = unofficial_series is not None and not unofficial_series.dropna().empty
    if not official_present:
        usability["official_usable"] = False if unofficial_present else None
        usability["selected_series"] = "unofficial" if unofficial_present else None
        return usability

    static_diag = _fx_static_diagnostics(official_series)
    scale_diag = _fx_price_scale_diagnostics(price_frame, official_series)
    usability.update(static_diag)
    usability.update(scale_diag)
    official_usable = not (bool(static_diag["near_static"]) and bool(scale_diag["price_scale_inconsistent"]))
    usability["official_usable"] = official_usable
    if official_usable:
        usability["selected_series"] = "official"
    elif unofficial_present:
        usability["selected_series"] = "unofficial"
        usability["omitted_reason"] = "near-static official rate inconsistent with local price scale"
    else:
        usability["selected_series"] = None
        usability["omitted_reason"] = (
            "near-static official-only rate inconsistent with local price scale and no usable unofficial FX"
        )
    return usability


def _fx_static_diagnostics(series: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {
            "near_static": False,
            "official_range_ratio": None,
            "official_range_pct": None,
            "official_unique_rounded_values": 0,
        }
    median = float(values.median())
    range_value = float(values.max() - values.min())
    range_ratio = None if median == 0 else abs(range_value / median)
    unique_rounded = int(values.round(6).nunique())
    near_static = unique_rounded <= 1 or (
        range_ratio is not None and range_ratio <= _FX_NEAR_STATIC_MAX_RANGE_RATIO
    )
    return {
        "near_static": bool(near_static),
        "official_range_ratio": None if range_ratio is None else round(float(range_ratio), 8),
        "official_range_pct": None if range_ratio is None else round(float(range_ratio * 100.0), 4),
        "official_unique_rounded_values": unique_rounded,
    }


def _fx_price_scale_diagnostics(
    price_frame: Optional[pd.DataFrame],
    fx_series: pd.Series,
) -> dict[str, Any]:
    diagnostics = {
        "price_scale_inconsistent": False,
        "food_basket_usd": None,
        "commodity_median_usd": None,
        "commodity_usd_sample_size": 0,
    }
    if price_frame is None or price_frame.empty or fx_series is None or fx_series.dropna().empty:
        return diagnostics
    try:
        rate = float(pd.to_numeric(fx_series, errors="coerce").dropna().iloc[-1])
    except Exception:
        return diagnostics
    if rate <= 0 or pd.isna(rate):
        return diagnostics

    frame = price_frame.copy()
    try:
        frame.index = pd.to_datetime(frame.index, errors="coerce")
        frame = frame[frame.index.notna()].sort_index()
    except Exception:
        pass
    if frame.empty:
        return diagnostics
    current = frame.iloc[-1]

    try:
        food_basket = float(pd.to_numeric(pd.Series([current.get("FoodBasket")]), errors="coerce").iloc[0])
    except Exception:
        food_basket = float("nan")
    if not pd.isna(food_basket) and food_basket > 0:
        diagnostics["food_basket_usd"] = round(food_basket / rate, 4)

    commodity_values = []
    for column in frame.columns:
        if column in {"FoodBasket", "ExchangeRate", "ExchangeRateUnofficial", "FuelPrice"}:
            continue
        try:
            value = float(pd.to_numeric(pd.Series([current.get(column)]), errors="coerce").iloc[0])
        except Exception:
            continue
        if not pd.isna(value) and value > 0:
            commodity_values.append(value / rate)
    diagnostics["commodity_usd_sample_size"] = len(commodity_values)
    if commodity_values:
        diagnostics["commodity_median_usd"] = round(float(pd.Series(commodity_values).median()), 4)

    basket_flag = (
        diagnostics["food_basket_usd"] is not None
        and float(diagnostics["food_basket_usd"]) > _FX_FOOD_BASKET_USD_MAX
    )
    commodity_flag = (
        diagnostics["commodity_usd_sample_size"] >= _FX_MIN_COMMODITY_USD_SAMPLE
        and diagnostics["commodity_median_usd"] is not None
        and float(diagnostics["commodity_median_usd"]) > _FX_COMMODITY_MEDIAN_USD_MAX
    )
    diagnostics["price_scale_inconsistent"] = bool(basket_flag or commodity_flag)
    return diagnostics


def _partial_latest_fx_month(
    rows: list[dict[str, Any]],
    full_date_index: pd.DatetimeIndex,
    *,
    included_official: bool,
    included_unofficial: bool,
) -> dict[str, Any]:
    latest_month = pd.Timestamp(full_date_index[-1]).to_period("M").to_timestamp()
    if latest_month.date() != _current_month_start():
        return {"partial": False}
    included_flags = []
    if included_official:
        included_flags.append(True)
    if included_unofficial:
        included_flags.append(False)
    if not included_flags:
        return {"partial": False}
    dates = []
    for row in rows:
        if row.get("is_official") not in included_flags:
            continue
        parsed = pd.to_datetime(row.get("date"), errors="coerce")
        if pd.isna(parsed):
            continue
        if parsed.to_period("M").to_timestamp() == latest_month:
            dates.append(parsed.normalize())
    if not dates:
        return {"partial": False}
    through = max(dates)
    month_end = latest_month + pd.offsets.MonthEnd(0)
    if through >= month_end:
        return {"partial": False}
    return {
        "partial": True,
        "month": latest_month.strftime("%Y-%m"),
        "through_date": through.date().isoformat(),
    }


def _exchange_rate_data_from_series(
    series: Optional[pd.Series],
    *,
    currency_code: str,
    target_date: pd.Timestamp,
    source_series: Optional[str] = None,
    rate_type: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if series is None or series.dropna().empty:
        return None
    current = float(series.iloc[-1])
    mom = _pct_change(current, series.iloc[-2] if len(series) >= 2 else None)
    yoy = _pct_change(current, series.iloc[0] if len(series) >= 1 else None)
    history = series.rename("Close").to_frame()
    return {
        "symbol": f"USD{currency_code}:DATABRIDGES",
        "currency_code": currency_code,
        "current_rate": round(current, 6),
        "unit": f"{currency_code} per 1 USD",
        "quotation": "local_currency_per_usd",
        "higher_value_indicates": "local_currency_depreciation",
        "daily_change_pct": None,
        "weekly_change_pct": None,
        "monthly_change_pct": None if mom is None else round(mom, 2),
        "yearly_change_pct": None if yoy is None else round(yoy, 2),
        "trend": _exchange_rate_trend(yoy),
        "last_update": pd.Timestamp(target_date).isoformat(),
        "historical_data_json": history.to_json(date_format="iso"),
        "is_mock": False,
        "source": "DataBridges",
        "source_series": source_series,
        "rate_type": rate_type,
    }


def _pct_change(current: Any, previous: Any) -> Optional[float]:
    try:
        curr = float(current)
        prev = float(previous)
    except Exception:
        return None
    if prev == 0 or pd.isna(prev) or pd.isna(curr):
        return None
    return (curr - prev) / prev * 100.0


def _exchange_rate_trend(yoy_change_pct: Optional[float]) -> str:
    yoy = float(yoy_change_pct or 0.0)
    if yoy > 30:
        return "rapid_depreciation"
    if yoy > 10:
        return "depreciation"
    if yoy < -10:
        return "appreciation"
    return "stable"


def _build_time_series_from_price_df(
    df: pd.DataFrame,
    *,
    canonical: str,
    valid_names: List[str],
    admin1_list: List[str],
    full_date_index: pd.DatetimeIndex,
    basket_components: Optional[List[Dict[str, Any]]] = None,
    return_raw_rows: bool = False,
    allow_empty: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame] | Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    basket_components = basket_components or []
    valid_names = _dedupe_preserve_order([str(name) for name in valid_names if str(name).strip()])
    if df.empty and not allow_empty:
        raise ValueError(f"No PriceCache price rows matched the requested commodities: {valid_names}")

    df = df.copy()
    if df.empty:
        national_pivot = pd.DataFrame(index=full_date_index, columns=valid_names, dtype=float)
        national_pivot["FoodBasket"] = np.nan
        national_pivot["ExchangeRate"] = np.nan
        national_pivot["FuelPrice"] = np.nan
        national_pivot.index.name = "Date"
        valid_regions = list(admin1_list or [])
        regional_index = pd.MultiIndex.from_product(
            [full_date_index, valid_regions],
            names=["Date", "Region"],
        )
        df_regional = pd.DataFrame(index=regional_index).reset_index()
        if "FoodBasket" not in df_regional.columns:
            df_regional["FoodBasket"] = np.nan
        raw_rows = _empty_price_df()
        if return_raw_rows:
            return national_pivot, df_regional, raw_rows
        return national_pivot, df_regional

    df["Month"] = df["Price Date"].dt.to_period("M").dt.to_timestamp()

    national_pivot = df.pivot_table(
        index="Month",
        columns="Commodity",
        values="Price",
        aggfunc="mean",
    ).reindex(full_date_index)

    for name in valid_names:
        if name not in national_pivot.columns:
            national_pivot[name] = np.nan
    national_pivot = national_pivot[valid_names].round(2)
    if basket_components:
        basket_by_id = df.pivot_table(
            index="Month",
            columns="Commodity ID",
            values="Price",
            aggfunc="mean",
        ).reindex(full_date_index)
        basket_component_ids = [component["commodity_id"] for component in basket_components]
        weighted_components = pd.DataFrame(index=full_date_index)
        for component in basket_components:
            commodity_id = component["commodity_id"]
            if commodity_id not in basket_by_id.columns:
                basket_by_id[commodity_id] = np.nan
            weighted_components[commodity_id] = basket_by_id[commodity_id] * component["weight_quantity"]
        national_pivot["FoodBasket"] = weighted_components.sum(
            axis=1,
            min_count=len(basket_components),
        ).round(2)
    else:
        national_pivot["FoodBasket"] = national_pivot[valid_names].sum(axis=1, skipna=True).round(2)
        national_pivot.loc[national_pivot[valid_names].isna().all(axis=1), "FoodBasket"] = np.nan
    national_pivot["ExchangeRate"] = np.nan
    national_pivot["FuelPrice"] = np.nan
    national_pivot.index.name = "Date"

    available_regions = sorted(df["Admin 1"].dropna().astype(str).unique().tolist())
    if admin1_list:
        valid_regions = [region for region in admin1_list if region in available_regions]
        if not valid_regions:
            raise ValueError(
                f"Requested regions are not available for {canonical}: {admin1_list}. "
                f"Available regions: {available_regions}"
            )
    else:
        valid_regions = available_regions

    df_regional_data = df[df["Admin 1"].isin(valid_regions)].copy()
    if basket_components:
        weights_by_id = {component["commodity_id"]: component["weight_quantity"] for component in basket_components}
        regional_pivot = df_regional_data[
            df_regional_data["Commodity ID"].isin(weights_by_id.keys())
        ].pivot_table(
            index=["Month", "Admin 1"],
            columns="Commodity ID",
            values="Price",
            aggfunc="mean",
        )
        for commodity_id in weights_by_id:
            if commodity_id not in regional_pivot.columns:
                regional_pivot[commodity_id] = np.nan
        weighted = pd.DataFrame(index=regional_pivot.index)
        for commodity_id, quantity in weights_by_id.items():
            weighted[commodity_id] = regional_pivot[commodity_id] * quantity
        regional_agg = weighted.sum(axis=1, min_count=len(weights_by_id)).rename("FoodBasket").reset_index()
        regional_agg.columns = ["Date", "Region", "FoodBasket"]
    else:
        regional_agg = (
            df_regional_data.groupby(["Month", "Admin 1", "Commodity"], dropna=True)["Price"]
            .mean()
            .reset_index()
            .groupby(["Month", "Admin 1"], dropna=True)["Price"]
            .sum()
            .reset_index()
        )
        regional_agg.columns = ["Date", "Region", "FoodBasket"]
    regional_agg["FoodBasket"] = regional_agg["FoodBasket"].round(2)

    regional_index = pd.MultiIndex.from_product(
        [full_date_index, valid_regions],
        names=["Date", "Region"],
    )
    df_regional = pd.DataFrame(index=regional_index).reset_index()
    df_regional = df_regional.merge(regional_agg, on=["Date", "Region"], how="left")
    df_regional = df_regional.sort_values(["Date", "Region"]).reset_index(drop=True)

    if return_raw_rows:
        raw_columns = [
            "Country",
            "Country ISO3",
            "Commodity",
            "Commodity ID",
            "Price Type",
            "Price Date",
            "Price",
            "Admin 1",
            "Admin 2",
            "Market Name",
            "Market ID",
            "Unit ID",
            "Unit",
            "Currency",
            "Data Type",
            "Price Flag",
            "Observations",
            "Data Source",
        ]
        raw_rows = df[raw_columns].sort_values(
            ["Price Date", "Admin 1", "Market Name", "Commodity"],
            na_position="last",
        ).reset_index(drop=True)
        return national_pivot, df_regional, raw_rows

    return national_pivot, df_regional


def _read_report_price_records(
    iso3: str,
    start_date: str,
    end_date: str,
    *,
    commodity_ids: Sequence[int],
) -> list[MonthlyPriceRecord]:
    repo = _get_price_cache_repository()
    status = repo.get_cache_status()
    _sync_active_cache_version(status.active_version_id)
    ids = tuple(sorted({int(item) for item in commodity_ids if item is not None}))
    return repo.get_price_window(iso3, start_date, end_date, commodity_ids=ids or None)


def _price_records_to_report_df(
    records: list[MonthlyPriceRecord],
    canonical: str,
    iso3: str,
    name_by_id: dict[int, str],
) -> pd.DataFrame:
    df = _normalise_cached_price_rows(records, canonical, iso3)
    return _apply_report_commodity_names(df, name_by_id)


def _apply_report_commodity_names(df: pd.DataFrame, name_by_id: dict[int, str]) -> pd.DataFrame:
    if df.empty or "Commodity ID" not in df.columns:
        return df
    df = df.copy()
    for commodity_id, name in name_by_id.items():
        df.loc[df["Commodity ID"] == commodity_id, "Commodity"] = name
    return df


def _commodity_names_by_id(canonical: str, iso3: str) -> dict[int, str]:
    names: dict[int, str] = {}
    for commodity in _get_commodities(canonical, iso3):
        commodity_id = _to_int(commodity.get("id"))
        name = str(commodity.get("name") or "").strip()
        if commodity_id is not None and name:
            names[commodity_id] = name
    return names


def _build_report_price_requirements(
    *,
    valid_names: list[str],
    commodity_ids: list[int],
    basket_components: list[dict[str, Any]],
    name_by_id: dict[int, str],
    window_months: list[str],
    reference_month: str,
) -> tuple[list[PriceRequirement], list[dict[str, Any]]]:
    basket_by_id = {component["commodity_id"]: component for component in basket_components}
    specs: list[dict[str, Any]] = []
    for commodity_id in commodity_ids:
        name = name_by_id.get(commodity_id)
        if not name and commodity_id in basket_by_id:
            name = basket_by_id[commodity_id]["commodity_name"]
        if not name:
            continue
        specs.append(
            {
                "commodity_id": commodity_id,
                "commodity_name": name,
                "is_basket": commodity_id in basket_by_id,
            }
        )
    seen_ids = {item["commodity_id"] for item in specs}
    for component in basket_components:
        if component["commodity_id"] in seen_ids:
            continue
        specs.append(
            {
                "commodity_id": component["commodity_id"],
                "commodity_name": component["commodity_name"],
                "is_basket": True,
            }
        )
    requested_names = {str(name) for name in valid_names}
    specs = [
        item
        for item in specs
        if item["is_basket"] or item["commodity_name"] in requested_names
    ]

    requirements: list[PriceRequirement] = []
    for item in specs:
        for month in window_months:
            is_hard = bool(item["is_basket"] and month == reference_month)
            requirements.append(
                PriceRequirement(
                    commodity_id=int(item["commodity_id"]),
                    commodity_name=str(item["commodity_name"]),
                    month=month,
                    hard=is_hard,
                    is_basket=bool(item["is_basket"]),
                    reason="reference_month_food_basket" if is_hard else "trailing_window",
                )
            )
    return requirements, specs


def _detect_report_price_gaps(
    df: pd.DataFrame,
    *,
    commodity_specs: list[dict[str, Any]],
    window_months: list[str],
    reference_month: str,
    backfill_info: Optional[dict[int, dict[str, Any]]] = None,
) -> list[CommodityGapStatus]:
    backfill_info = backfill_info or {}
    months_by_id: dict[int, set[str]] = {}
    if not df.empty:
        working = df.copy()
        working["MonthLabel"] = working["Price Date"].dt.to_period("M").astype(str)
        working = working[pd.to_numeric(working["Price"], errors="coerce").notna()]
        for commodity_id, group in working.groupby("Commodity ID"):
            parsed_id = _to_int(commodity_id)
            if parsed_id is None:
                continue
            months_by_id[parsed_id] = set(group["MonthLabel"].dropna().astype(str))

    statuses: list[CommodityGapStatus] = []
    for item in commodity_specs:
        commodity_id = int(item["commodity_id"])
        present = months_by_id.get(commodity_id, set())
        is_basket = bool(item["is_basket"])
        missing_reference = is_basket and reference_month not in present
        soft_months = [
            month
            for month in window_months
            if month not in present and not (is_basket and month == reference_month)
        ]
        info = backfill_info.get(commodity_id, {})
        latest = max(present) if present else None
        status = CommodityGapStatus(
            commodity_id=commodity_id,
            commodity_name=str(item["commodity_name"]),
            is_basket=is_basket,
            missing_reference_month=missing_reference,
            missing_soft_months=soft_months,
            source_status="source_error" if info.get("error") else "cache_checked",
            latest_source_month=latest,
            backfill_attempted=bool(info.get("attempted")),
            fetched_rows=int(info.get("fetched_rows") or 0),
            error=info.get("error"),
        )
        statuses.append(status)
    return statuses


def _classify_unresolved_gap_sources(
    gap_report: ReportPriceGapReport,
    df: pd.DataFrame,
    *,
    backfill_info: dict[int, dict[str, Any]],
) -> None:
    latest_by_id = _latest_month_by_commodity(df)
    for status in gap_report.commodity_statuses:
        if status.source_status == "source_error":
            continue
        status.latest_source_month = latest_by_id.get(status.commodity_id) or status.latest_source_month
        if not (status.missing_reference_month or status.missing_soft_months):
            status.source_status = "complete"
            continue
        info = backfill_info.get(status.commodity_id, {})
        if info.get("attempted"):
            if int(info.get("fetched_rows") or 0) <= 0 and status.latest_source_month is None:
                status.source_status = "no_source_data"
            else:
                status.source_status = "source_lacks_reference" if status.missing_reference_month else "source_has_data"
        elif status.latest_source_month is None:
            status.source_status = "no_source_data"
        else:
            status.source_status = "source_lacks_reference" if status.missing_reference_month else "source_has_data"


def _latest_month_by_commodity(df: pd.DataFrame) -> dict[int, str]:
    if df.empty:
        return {}
    working = df.copy()
    working["MonthLabel"] = working["Price Date"].dt.to_period("M").astype(str)
    out: dict[int, str] = {}
    for commodity_id, group in working.groupby("Commodity ID"):
        parsed_id = _to_int(commodity_id)
        if parsed_id is not None:
            out[parsed_id] = max(group["MonthLabel"].dropna().astype(str))
    return out


def _fetch_targeted_report_backfill(
    adapter: DataBridgesClientAdapter,
    *,
    iso3: str,
    commodity_ids: list[int],
    start_date: str,
    end_date: str,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    ids = _dedupe_ints(commodity_ids)
    info = {
        commodity_id: {"attempted": True, "fetched_rows": 0, "error": None}
        for commodity_id in ids
    }
    try:
        commodities = adapter.fetch_commodities(iso3)
    except Exception as exc:
        commodities = []
        logger.warning("Could not fetch DataBridges commodity metadata for %s: %s", iso3, _safe_adapter_error(exc))
    try:
        markets = adapter.fetch_markets(iso3)
    except Exception as exc:
        markets = []
        logger.warning("Could not fetch DataBridges market metadata for %s: %s", iso3, _safe_adapter_error(exc))

    raw_rows: list[dict[str, Any]] = []
    workers = min(_adapter_max_workers(adapter), 5, max(1, len(ids)))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                adapter.fetch_monthly_price_rows,
                iso3,
                commodity_id=commodity_id,
                start_date=start_date,
                end_date=end_date,
            ): commodity_id
            for commodity_id in ids
        }
        for future in as_completed(futures):
            commodity_id = futures[future]
            try:
                rows = [dict(row) for row in future.result()]
                info[commodity_id]["fetched_rows"] = len(rows)
                raw_rows.extend(rows)
            except Exception as exc:
                info[commodity_id]["error"] = _safe_adapter_error(exc)

    enriched = enrich_price_rows_with_metadata(raw_rows, markets=markets, commodities=commodities)
    return info, enriched


def _adapter_max_workers(adapter: DataBridgesClientAdapter) -> int:
    config = getattr(adapter, "config", None)
    value = _to_int(getattr(config, "max_workers", None))
    return value if value is not None and value > 0 else 5


def _safe_adapter_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    text = re.sub(r"(?i)(api[_-]?secret|client[_-]?secret|token|authorization)[=:]\S+", r"\1=<redacted>", text)
    if len(text) > 500:
        text = text[:497] + "..."
    return text


def _databridges_rows_to_monthly_records(
    rows: Sequence[dict[str, Any]],
    *,
    cache_version_id: str,
    source_label: str,
) -> list[MonthlyPriceRecord]:
    records: list[MonthlyPriceRecord] = []
    for row in rows:
        country_iso3 = str(row.get("country_iso3") or "").upper()
        commodity_id = _to_int(row.get("commodity_id"))
        market_id = _to_int(row.get("market_id"))
        price_date = _date_obj(row.get("price_date"))
        price = _to_float(row.get("price"))
        if not country_iso3 or commodity_id is None or market_id is None or price_date is None or price is None:
            continue
        records.append(
            MonthlyPriceRecord(
                cache_version_id=cache_version_id,
                country_iso3=country_iso3,
                commodity_id=commodity_id,
                market_id=market_id,
                price_date=price_date,
                price=price,
                currency_id=_to_int(row.get("currency_id")),
                currency_code=_optional_text(row.get("currency_code")),
                currency_name=_optional_text(row.get("currency_name")),
                commodity_unit_id=_to_int(row.get("commodity_unit_id")),
                commodity_unit_name=_optional_text(row.get("commodity_unit_name")),
                price_type_id=_to_int(row.get("price_type_id")),
                price_type_name=_optional_text(row.get("price_type_name")) or "",
                price_flag=_optional_text(row.get("price_flag")) or "",
                original_frequency=_optional_text(row.get("original_frequency")),
                observations=_to_int(row.get("observations")),
                source_payload_hash=_optional_text(row.get("source_payload_hash")),
                admin1_name=_optional_text(row.get("admin1_name")),
                admin2_name=_optional_text(row.get("admin2_name")),
                market_name=_optional_text(row.get("market_name")),
                commodity_name=_optional_text(row.get("commodity_name")),
                data_source=_optional_text(row.get("data_source")) or source_label,
            )
        )
    return records


def _merge_price_records_cache_first(
    cached: Sequence[MonthlyPriceRecord],
    backfilled: Sequence[MonthlyPriceRecord],
) -> list[MonthlyPriceRecord]:
    merged: list[MonthlyPriceRecord] = []
    seen: set[tuple[Any, ...]] = set()
    for row in list(cached) + list(backfilled):
        key = price_deduplication_key(_monthly_record_to_row_dict(row))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _monthly_record_to_row_dict(row: MonthlyPriceRecord) -> dict[str, Any]:
    return {
        "country_iso3": row.country_iso3,
        "commodity_id": row.commodity_id,
        "market_id": row.market_id,
        "price_date": row.price_date,
        "price": row.price,
        "currency_id": row.currency_id,
        "currency_code": row.currency_code,
        "currency_name": row.currency_name,
        "commodity_unit_id": row.commodity_unit_id,
        "commodity_unit_name": row.commodity_unit_name,
        "price_type_id": row.price_type_id,
        "price_type_name": row.price_type_name,
        "price_flag": row.price_flag,
        "observations": row.observations,
        "source_payload_hash": row.source_payload_hash,
        "commodity_name": row.commodity_name,
        "market_name": row.market_name,
        "admin1_name": row.admin1_name,
        "admin2_name": row.admin2_name,
        "data_source": row.data_source,
    }


def _dedupe_ints(values: Sequence[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        parsed = _to_int(value)
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


def _optional_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def calculate_statistics_from_csv(
    df_national: pd.DataFrame,
    commodities: List[str],
    food_basket_components: Optional[List[Dict[str, Any]]] = None,
    currency_code: Optional[str] = None,
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "food_basket": {},
        "commodities": {},
        "auxiliary": {},
        "exchange_rate": {},
    }
    if df_national.empty:
        return stats

    current_idx = -1
    mom_idx = -2 if len(df_national) >= 2 else -1
    yoy_idx = 0
    current = df_national.iloc[current_idx]
    mom = df_national.iloc[mom_idx]
    yoy = df_national.iloc[yoy_idx]
    basket_components = _normalise_basket_components(food_basket_components)
    if basket_components:
        selected_components = [
            component["commodity_name"]
            for component in basket_components
            if component["commodity_name"] in df_national.columns
        ]
        configured_components = [component["commodity_name"] for component in basket_components]
    else:
        selected_components = [commodity for commodity in commodities if commodity in df_national.columns]
        configured_components = list(selected_components)
    historical_components = [
        commodity for commodity in selected_components if df_national[commodity].notna().any()
    ]
    latest_components = [
        commodity for commodity in selected_components if pd.notna(current.get(commodity))
    ]
    missing_latest_components = [
        commodity for commodity in selected_components if commodity not in latest_components
    ]

    for column in df_national.columns:
        current_val = current[column]
        if pd.isna(current_val):
            continue
        mom_val = mom[column]
        yoy_val = yoy[column]
        mom_pct = round(((current_val - mom_val) / mom_val * 100), 1) if pd.notna(mom_val) and mom_val != 0 else None
        yoy_pct = round(((current_val - yoy_val) / yoy_val * 100), 1) if pd.notna(yoy_val) and yoy_val != 0 else None
        item = {
            "current_price": round(float(current_val), 2),
            "mom_change_pct": float(mom_pct) if mom_pct is not None else None,
            "yoy_change_pct": float(yoy_pct) if yoy_pct is not None else None,
        }
        if column == "FoodBasket":
            item.update(
                {
                    "selected_component_count": len(selected_components),
                    "selected_component_names": selected_components,
                    "configured_component_count": len(configured_components),
                    "configured_component_names": configured_components,
                    "historical_component_count": len(historical_components),
                    "historical_component_names": historical_components,
                    "latest_component_count": len(latest_components),
                    "latest_component_names": latest_components,
                    "available_component_count": len(latest_components),
                    "available_component_names": latest_components,
                    "missing_latest_component_names": missing_latest_components,
                    "missing_component_names": missing_latest_components,
                }
            )
            stats["food_basket"] = item
        elif column in {"ExchangeRate", "ExchangeRateUnofficial"}:
            stats["auxiliary"][column] = item
            unit = f"{str(currency_code or 'LCU').strip().upper() or 'LCU'} per 1 USD"
            item["unit"] = unit
            item["quotation"] = "local_currency_per_usd"
            item["higher_value_indicates"] = "local_currency_depreciation"
            fx_item = {
                "current": item["current_price"],
                "mom": item["mom_change_pct"],
                "yoy": item["yoy_change_pct"],
                "current_rate": item["current_price"],
                "mom_change_pct": item["mom_change_pct"],
                "yoy_change_pct": item["yoy_change_pct"],
                "unit": unit,
                "quotation": "local_currency_per_usd",
                "higher_value_indicates": "local_currency_depreciation",
            }
            if column == "ExchangeRate":
                stats["exchange_rate"]["official"] = fx_item
            else:
                stats["exchange_rate"]["unofficial"] = fx_item
        elif _is_module_auxiliary_column(column):
            stats["auxiliary"][column] = item
        elif column in commodities:
            stats["commodities"][column] = item

    if not stats["food_basket"] and "FoodBasket" in df_national.columns:
        # Preserve the legacy primary alias shape even when strict component
        # coverage makes the target value unavailable.
        stats["food_basket"] = {
            "current_price": None,
            "mom_change_pct": None,
            "yoy_change_pct": None,
            "selected_component_count": len(selected_components),
            "selected_component_names": selected_components,
            "configured_component_count": len(configured_components),
            "configured_component_names": configured_components,
            "historical_component_count": len(historical_components),
            "historical_component_names": historical_components,
            "latest_component_count": len(latest_components),
            "latest_component_names": latest_components,
            "available_component_count": len(latest_components),
            "available_component_names": latest_components,
            "missing_latest_component_names": missing_latest_components,
            "missing_component_names": missing_latest_components,
        }
    return stats


def check_data_availability(
    country: str,
    time_period: str,
    commodities: List[str],
    csv_path: Optional[Path] = None,
    currency_code: Optional[str] = None,
) -> Dict[str, Any]:
    if csv_path is not None:
        logger.info("Ignoring csv_path=%s because PriceCache is now the price source.", csv_path)

    try:
        canonical, iso3 = resolve_country(country)
        metadata = get_country_metadata(canonical)
        available_commodities = [str(item["name"]) for item in metadata.get("commodities", [])]
        missing = [item for item in commodities if item not in available_commodities]
        target_date = _parse_time_period(time_period)
        start_date = target_date - pd.DateOffset(months=12)
        df = _get_country_price_df(
            canonical,
            iso3,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=target_date.strftime("%Y-%m-%d"),
        )
        expected_months = pd.date_range(start=start_date, end=target_date, freq="MS")
        actual_months = set()
        if not df.empty:
            actual_months = set(df["Price Date"].dt.to_period("M").dt.to_timestamp())
        data_gaps = [month.strftime("%Y-%m") for month in expected_months if month not in actual_months]

        warnings = list(metadata.get("warnings") or [])
        if missing:
            warnings.append(
                f"Requested commodities not available in PriceCache for {metadata.get('country')}: {missing}."
            )
        if data_gaps:
            warnings.append(f"PriceCache price data has gaps in {len(data_gaps)} month(s): {data_gaps}.")
        if metadata.get("date_range") is None:
            warnings.append(f"PriceCache returned no monthly price date range for {metadata.get('country')}.")

        if not df.empty and "Currency" in df.columns:
            currencies_present = sorted(
                {str(value) for value in df["Currency"].dropna().astype(str) if str(value).strip()}
            )
            if len(currencies_present) > 1:
                _, selected_currency, _excluded = _select_report_currency(
                    df,
                    requested_code=currency_code,
                    country=canonical,
                )
                warnings.append(
                    f"Cached prices for {metadata.get('country')} are quoted in multiple currencies "
                    f"({', '.join(currencies_present)}). Report calculations use a single currency "
                    f"({selected_currency}); rows in other currencies are excluded."
                )

        return {
            "available": True,
            "country_normalized": metadata.get("country") or canonical,
            "iso3": iso3,
            "countries": get_available_countries(),
            "commodities": available_commodities,
            "regions": metadata.get("regions", []),
            "markets": [market.get("market_name") for market in metadata.get("markets", [])],
            "date_range": metadata.get("date_range"),
            "missing_commodities": missing,
            "data_gaps": data_gaps,
            "warnings": warnings,
            "cache_metadata": get_cache_metadata_for_report(canonical),
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "country_normalized": None,
            "countries": get_available_countries(),
            "commodities": [],
            "regions": [],
            "markets": [],
            "date_range": None,
            "missing_commodities": commodities,
            "data_gaps": [],
            "warnings": [str(exc)],
            "cache_metadata": _safe_cache_metadata(),
        }


def _get_country_price_df(
    canonical: str,
    iso3: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    commodity_ids: Optional[Iterable[int]] = None,
    latest_value_only: bool = False,
) -> pd.DataFrame:
    repo = _get_price_cache_repository()
    status = repo.get_cache_status()
    _sync_active_cache_version(status.active_version_id)

    availability = repo.get_country_availability(iso3)
    country_cache_version = getattr(availability, "cache_version_id", None) if availability else None
    ids = tuple(sorted({int(item) for item in commodity_ids or [] if item is not None}))
    cache_key = (iso3, country_cache_version or "", start_date, end_date, ids, latest_value_only)
    cached = _cache_get(_PRICE_CACHE, cache_key)
    if cached is not None:
        return cached.copy()

    if availability is None:
        df = _empty_price_df()
        _cache_set(_PRICE_CACHE, cache_key, df)
        return df.copy()

    query_start = start_date or _date_wire(availability.date_start)
    query_end = end_date or _date_wire(availability.date_end)
    if not query_start or not query_end:
        df = _empty_price_df()
        _cache_set(_PRICE_CACHE, cache_key, df)
        return df.copy()

    rows = repo.get_price_window(
        iso3,
        query_start,
        query_end,
        commodity_ids=ids or None,
    )
    df = _normalise_cached_price_rows(rows, canonical, iso3)
    if latest_value_only and not df.empty:
        latest_date = df["Price Date"].max()
        df = df[df["Price Date"] == latest_date].copy()
    _cache_set(_PRICE_CACHE, cache_key, df)
    return df.copy()


def _normalise_cached_price_rows(
    rows: list[MonthlyPriceRecord],
    canonical: str,
    iso3: str,
) -> pd.DataFrame:
    records = []
    for row in _sort_cached_price_rows(rows):
        flag = str(row.price_flag or "").strip().lower()
        if not _is_real_price_flag(flag):
            continue

        price = pd.to_numeric(row.price, errors="coerce")
        price_date = pd.to_datetime(row.price_date, errors="coerce")
        if pd.isna(price) or pd.isna(price_date):
            continue

        records.append(
            {
                "Country": canonical,
                "Country ISO3": iso3,
                "Commodity": row.commodity_name or str(row.commodity_id),
                "Commodity ID": row.commodity_id,
                "Price Type": row.price_type_name or "",
                "Price Date": price_date.to_period("M").to_timestamp(),
                "Price": float(price),
                "Admin 1": row.admin1_name or "Unknown",
                "Admin 2": row.admin2_name or "",
                "Market Name": row.market_name or "Unknown",
                "Market ID": row.market_id,
                "Unit ID": row.commodity_unit_id,
                "Unit": row.commodity_unit_name or "",
                "Currency": row.currency_name or row.currency_code or "",
                "Currency Code": row.currency_code or "",
                "Data Type": _data_type_from_flag(flag),
                "Price Flag": flag,
                "Observations": row.observations,
                "Data Source": row.data_source or "PriceCache",
            }
        )

    columns = [
        "Country",
        "Country ISO3",
        "Commodity",
        "Commodity ID",
        "Price Type",
        "Price Date",
        "Price",
        "Admin 1",
        "Admin 2",
        "Market Name",
        "Market ID",
        "Unit ID",
        "Unit",
        "Currency",
        "Currency Code",
        "Data Type",
        "Price Flag",
        "Observations",
        "Data Source",
    ]
    if not records:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(records)
    sort_columns = [
        "Commodity ID",
        "Commodity",
        "Price Type",
        "Currency",
        "Unit",
        "Price Date",
        "Market ID",
        "Market Name",
        "Price Flag",
        "Price",
        "Observations",
        "Data Source",
    ]
    return df.sort_values(sort_columns, na_position="last").drop_duplicates().reset_index(drop=True)


def _get_commodities(canonical: str, iso3: str) -> list[dict[str, Any]]:
    repo = _get_price_cache_repository()
    status = repo.get_cache_status()
    _sync_active_cache_version(status.active_version_id)

    cached = _cache_get(_COMMODITY_CACHE, iso3)
    if cached is not None:
        return list(cached)

    metadata = _get_repository_country_metadata(iso3)
    availability = repo.get_country_availability(iso3)
    priced_ids = {int(item) for item in (availability.priced_commodity_ids if availability else [])}
    commodities = []
    for row in metadata.commodities:
        if int(row.commodity_id) not in priced_ids:
            continue
        name = str(row.commodity_name or "").strip()
        if not name:
            continue
        commodities.append(
            {
                "id": row.commodity_id,
                "name": name,
                "category": row.category_name or _infer_category(name),
                "category_id": None,
                "unit_id": row.commodity_unit_id,
                "unit": row.commodity_unit_name,
                "unit_name": row.commodity_unit_name,
                "country": metadata.country.country_name or canonical,
                "iso3": iso3,
                "priced": True,
            }
        )
    commodities = sorted(commodities, key=_commodity_sort_key)
    _cache_set(_COMMODITY_CACHE, iso3, commodities)
    return list(commodities)


def _sort_cached_price_rows(rows: list[MonthlyPriceRecord]) -> list[MonthlyPriceRecord]:
    return sorted(rows, key=_cached_price_sort_key)


def _cached_price_sort_key(row: MonthlyPriceRecord) -> tuple[Any, ...]:
    return (
        _sort_int(row.commodity_id),
        _sort_text(row.commodity_name),
        _sort_text(row.price_type_name),
        _sort_text(row.currency_name or row.currency_code),
        _sort_text(row.commodity_unit_name),
        _sort_text(row.price_date),
        _sort_int(row.market_id),
        _sort_text(row.market_name),
        _sort_text(row.price_flag),
        _sort_float(row.price),
        _sort_int(row.observations),
        _sort_text(row.data_source),
    )


def _commodity_sort_key(item: dict[str, Any]) -> tuple[str, int]:
    commodity_id = _to_int(item.get("id"))
    return (str(item.get("name") or "").strip().lower(), commodity_id if commodity_id is not None else 10**12)


def _normalise_basket_components(items: Optional[List[Dict[str, Any]]]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        commodity_id = _to_int(item.get("commodity_id"))
        name = str(
            item.get("commodity_name_snapshot")
            or item.get("commodity_name")
            or item.get("name")
            or ""
        ).strip()
        weight = _to_float(item.get("weight_quantity"))
        if commodity_id is None or not name or weight is None or weight <= 0:
            continue
        if commodity_id in seen:
            continue
        seen.add(commodity_id)
        components.append(
            {
                "commodity_id": commodity_id,
                "commodity_name": name,
                "weight_quantity": weight,
                "databridges_unit": item.get("databridges_unit") or item.get("unit"),
            }
        )
    return components


def _primary_driven_optional_module_names(
    valid_names: Sequence[str],
    basket_specs: Sequence[BasketCalculationSpec],
) -> list[str]:
    primary_names = {
        item.commodity_name.casefold()
        for spec in basket_specs
        if spec.role == "primary"
        for item in spec.items
    }
    secondary_only_names = {
        item.commodity_name.casefold()
        for spec in basket_specs
        if spec.role == "secondary"
        for item in spec.items
        if item.commodity_name.casefold() not in primary_names
    }
    return [
        name for name in valid_names if str(name).casefold() not in secondary_only_names
    ]


def _resolve_commodities(
    canonical: str,
    iso3: str,
    requested: List[str],
) -> Tuple[List[str], List[int], List[str]]:
    commodities = _get_commodities(canonical, iso3)
    by_name: dict[str, dict[str, Any]] = {}
    for item in sorted(commodities, key=_commodity_sort_key):
        by_name.setdefault(str(item["name"]).lower(), item)
    by_id = {str(item["id"]): item for item in commodities if item.get("id") is not None}

    selected = requested or _select_default_commodities([str(item["name"]) for item in commodities])
    valid_names: list[str] = []
    valid_ids: list[int] = []
    missing: list[str] = []
    for item in selected:
        key = str(item).strip().lower()
        commodity = by_name.get(key) or by_id.get(str(item).strip())
        if not commodity:
            missing.append(str(item))
            continue
        name = str(commodity["name"])
        if name not in valid_names:
            valid_names.append(name)
        if commodity.get("id") is not None:
            valid_ids.append(int(commodity["id"]))
    return valid_names, valid_ids, missing


def _select_default_commodities(available: List[str], max_items: int = 5) -> List[str]:
    defaults: list[str] = []
    priority_patterns = ["maize", "wheat", "rice", "sorghum", "millet", "beans", "lentil", "oil", "salt", "sugar"]
    for pattern in priority_patterns:
        for commodity in available:
            if pattern in commodity.lower() and commodity not in defaults:
                defaults.append(commodity)
                break
        if len(defaults) >= max_items:
            return defaults
    for commodity in available:
        if commodity not in defaults:
            defaults.append(commodity)
        if len(defaults) >= max_items:
            break
    return defaults


def _infer_category(commodity: str) -> str:
    categories = get_commodity_categories([commodity])
    return next(iter(categories.keys()), "Other")


def _parse_time_period(time_period: str) -> pd.Timestamp:
    try:
        return pd.to_datetime(f"{time_period}-01").to_period("M").to_timestamp()
    except Exception as exc:
        raise ValueError(f"Invalid time period '{time_period}'. Expected YYYY-MM.") from exc


def _recent_price_window(months: int = _RECENT_METADATA_MONTHS) -> Tuple[str, str]:
    today = pd.Timestamp.today().normalize()
    start = (today - pd.DateOffset(months=months)).replace(day=1)
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def _get_price_cache_repository() -> PriceCacheRepository:
    global _PRICE_CACHE_REPOSITORY
    if _PRICE_CACHE_REPOSITORY is not None:
        return _PRICE_CACHE_REPOSITORY
    with _PRICE_CACHE_REPOSITORY_LOCK:
        if _PRICE_CACHE_REPOSITORY is None:
            config = load_price_cache_config()
            engine = create_price_cache_engine(config)
            apply_migrations(engine, config.backend)
            _PRICE_CACHE_REPOSITORY = SqlPriceCacheRepository(engine)
    return _PRICE_CACHE_REPOSITORY


def _get_repository_country_metadata(iso3: str) -> CountryMetadata:
    repo = _get_price_cache_repository()
    metadata = repo.get_country_metadata(iso3)
    if metadata is None:
        raise PriceCacheUnavailableError(f"No active PriceCache metadata is available for {iso3}.")
    return metadata


def _resolve_reportability_specs(
    canonical: str,
    iso3: str,
    *,
    explicit_selection: bool,
    primary_basket_version_id: Optional[str],
    include_secondary_basket: bool,
    secondary_basket_version_id: Optional[str],
) -> tuple[list[BasketCalculationSpec], bool]:
    if not explicit_selection:
        basket = _get_active_food_basket(iso3)
        return ([BasketCalculationSpec.from_snapshot(basket)] if basket is not None else []), False

    from app.services.market_monitor.food_basket import (
        BasketNotConfigured,
        BasketVersionConflict,
        resolve_baskets_for_report,
    )

    try:
        selection = resolve_baskets_for_report(
            canonical,
            primary_basket_version_id=primary_basket_version_id,
            include_secondary_basket=include_secondary_basket,
            secondary_basket_version_id=secondary_basket_version_id,
        )
    except BasketNotConfigured:
        if primary_basket_version_id:
            raise BasketVersionConflict(
                "The selected primary basket version is no longer active. Refresh the country baskets and try again."
            )
        return [], False
    specs = [BasketCalculationSpec.from_snapshot(selection.primary)]
    if selection.secondary_basket_included and selection.secondary is not None:
        specs.append(BasketCalculationSpec.from_snapshot(selection.secondary))
    return specs, bool(selection.secondary_basket_included)


def _build_role_aware_reportable_months_payload(
    *,
    canonical: str,
    iso3: str,
    repo: PriceCacheRepository,
    availability: Any,
    specs: Sequence[BasketCalculationSpec],
    secondary_included: bool,
    run_regions: Sequence[str],
) -> dict[str, Any]:
    cache_version_id = getattr(availability, "cache_version_id", None) if availability else None
    start_d = _date_obj(getattr(availability, "date_start", None)) if availability else None
    latest_cached = _date_obj(getattr(availability, "date_end", None)) if availability else None
    current_month = _current_month_start()
    if latest_cached and latest_cached > current_month:
        latest_cached = current_month
    primary = next((spec for spec in specs if spec.role == "primary"), None)
    secondary = next((spec for spec in specs if spec.role == "secondary"), None)
    base: dict[str, Any] = {
        "country": canonical,
        "iso3": iso3,
        "cache_version_id": cache_version_id,
        "basket_version_id": primary.basket_version_id if primary else None,
        "primary_basket_version_id": primary.basket_version_id if primary else None,
        "secondary_basket_version_id": secondary.basket_version_id if secondary else None,
        "secondary_basket_included": bool(secondary_included and secondary is not None),
        "applicable_regions": {"primary": [], "secondary": []},
        "primary_reportable_months": [],
        "secondary_reportable_months": [],
        "joint_reportable_months": [],
        "latest_primary_reportable_month": None,
        "latest_secondary_reportable_month": None,
        "latest_joint_reportable_month": None,
        "missing_by_basket_and_month": {"primary": None, "secondary": None},
        "reportable_months": [],
        "latest_reportable_month": None,
        "latest_cached_month": _month_label(latest_cached),
        "latest_cached_real_month": None,
        "missing_by_month": {},
        "warnings": [],
    }
    if availability is None:
        base["warnings"].append(f"No active PriceCache availability is available for {canonical}.")
        return base
    if primary is None:
        base["warnings"].append(f"No active food basket is configured for {canonical}.")
        return base
    if start_d is None or latest_cached is None or start_d > latest_cached:
        base["warnings"].append(f"PriceCache has no monthly price date range for {canonical}.")
        return base

    required_ids = sorted({item.commodity_id for spec in specs for item in spec.items})
    if not required_ids:
        base["warnings"].append(f"The active primary food basket for {canonical} has no commodities.")
        return base
    rows = repo.get_price_window(
        iso3,
        start_d.isoformat(),
        _month_end(latest_cached).isoformat(),
        commodity_ids=required_ids,
    )
    frame = _normalise_cached_price_rows(rows, canonical, iso3)
    if not frame.empty:
        latest_real = frame["Price Date"].max()
        if pd.notna(latest_real):
            base["latest_cached_real_month"] = pd.Timestamp(latest_real).strftime("%Y-%m")
    months = pd.date_range(start=start_d, end=latest_cached, freq="MS")
    available_regions = list(getattr(availability, "admin1_names", None) or [])
    calculation = calculate_basket_series(
        frame,
        specs,
        full_date_index=months,
        run_regions=run_regions,
        available_regions=available_regions,
    )
    base["applicable_regions"] = {
        "primary": list(calculation.applicable_regions.get("primary", [])),
        "secondary": list(calculation.applicable_regions.get("secondary", [])),
    }
    base["warnings"].extend(calculation.warnings)

    role_reportable: dict[str, list[str]] = {"primary": [], "secondary": []}
    for spec in specs:
        summary = calculation.summaries.get(spec.role, pd.DataFrame())
        if not summary.empty:
            role_reportable[spec.role] = [
                pd.Timestamp(value).strftime("%Y-%m")
                for value in summary.loc[summary["Complete"].astype(bool), "Date"].tolist()
            ]
        base["missing_by_basket_and_month"][spec.role] = _reportability_missing_for_spec(
            spec,
            calculation,
        )

    primary_months = role_reportable["primary"]
    secondary_months = role_reportable["secondary"] if secondary_included else []
    joint_months = (
        [month for month in primary_months if month in set(secondary_months)]
        if secondary_included and secondary is not None
        else list(primary_months)
    )
    base.update(
        {
            "primary_reportable_months": primary_months,
            "secondary_reportable_months": secondary_months,
            "joint_reportable_months": joint_months,
            "latest_primary_reportable_month": primary_months[-1] if primary_months else None,
            "latest_secondary_reportable_month": secondary_months[-1] if secondary_months else None,
            "latest_joint_reportable_month": joint_months[-1] if joint_months else None,
            "reportable_months": joint_months,
            "latest_reportable_month": joint_months[-1] if joint_months else None,
        }
    )
    base["missing_by_month"] = _flatten_effective_missing(
        base["missing_by_basket_and_month"],
        include_secondary=bool(secondary_included and secondary is not None),
    )
    if not joint_months:
        base["warnings"].append(f"No complete actual-price basket month is available for {canonical}.")
    return base


def _reportability_missing_for_spec(
    spec: BasketCalculationSpec,
    calculation: Any,
) -> dict[str, Any]:
    if spec.scope_type == "national":
        rows = calculation.national[calculation.national["BasketRole"] == spec.role]
    else:
        rows = calculation.regional[calculation.regional["BasketRole"] == spec.role]
    months: dict[str, list[dict[str, Any]]] = {}
    if not rows.empty:
        for row in rows.to_dict(orient="records"):
            if bool(row.get("Complete")):
                continue
            label = pd.Timestamp(row["Date"]).strftime("%Y-%m")
            months.setdefault(label, []).append(
                {
                    "region": row.get("Region"),
                    "scope_label": row.get("ScopeLabel"),
                    "missing_component_names": list(row.get("MissingComponentNames") or []),
                }
            )
    return {
        "basket_version_id": spec.basket_version_id,
        "basket_name": spec.name,
        "scope_type": spec.scope_type,
        "scope_label": "National" if spec.scope_type == "national" else "Average across selected regions",
        "months": months,
    }


def _flatten_effective_missing(
    missing_by_role: Mapping[str, Any],
    *,
    include_secondary: bool,
) -> dict[str, list[str]]:
    roles = ["primary", "secondary"] if include_secondary else ["primary"]
    output: dict[str, list[str]] = {}
    for role in roles:
        role_payload = missing_by_role.get(role)
        if not isinstance(role_payload, Mapping):
            continue
        basket_name = str(role_payload.get("basket_name") or role)
        for month, entries in (role_payload.get("months") or {}).items():
            for entry in entries or []:
                region = entry.get("region") if isinstance(entry, Mapping) else None
                for name in (entry.get("missing_component_names") if isinstance(entry, Mapping) else []) or []:
                    if include_secondary:
                        label = f"{role} basket {basket_name}"
                        if region:
                            label += f" / {region}"
                        label += f": {name}"
                    else:
                        label = str(name)
                    if label not in output.setdefault(str(month), []):
                        output[str(month)].append(label)
    return output


def _get_active_food_basket(iso3: str) -> Any:
    from app.services.market_monitor.food_basket import create_food_basket_repository

    return create_food_basket_repository().get_active_basket(iso3)


def _manual_refresh_enabled() -> bool:
    value = str(os.environ.get("MARKET_MONITOR_MANUAL_REFRESH_ENABLED", "true")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _manual_refresh_window(reportability: dict[str, Any]) -> tuple[date, date]:
    current_month = _current_month_start()
    latest_reportable = _parse_month_label(reportability.get("latest_reportable_month"))
    if latest_reportable is not None:
        return _add_months(latest_reportable, 1), current_month
    start = (pd.Timestamp(current_month) - pd.DateOffset(months=12)).date().replace(day=1)
    return start, current_month


def _manual_refresh_response(
    *,
    canonical: str,
    iso3: str,
    status: str,
    before: dict[str, Any],
    after: Optional[dict[str, Any]] = None,
    source_cache_version_id: Optional[str] = None,
    new_cache_version_id: Optional[str] = None,
    checked_start_month: Optional[str] = None,
    checked_end_month: Optional[str] = None,
    months_checked: Optional[list[str]] = None,
    required_commodity_ids: Optional[list[int]] = None,
    rows_fetched: int = 0,
    rows_real: int = 0,
    rows_saved: int = 0,
    rows_skipped_existing: int = 0,
    excluded_non_real_rows: int = 0,
    excluded_future_rows: int = 0,
    deduplicated_rows: int = 0,
    warnings: Optional[list[str]] = None,
) -> dict[str, Any]:
    after = after or before
    before_months = set(before.get("reportable_months") or [])
    after_months = set(after.get("reportable_months") or [])
    return {
        "country": canonical,
        "iso3": iso3,
        "status": status,
        "source_cache_version_id": source_cache_version_id or before.get("cache_version_id"),
        "new_cache_version_id": new_cache_version_id,
        "checked_start_month": checked_start_month,
        "checked_end_month": checked_end_month,
        "months_checked": months_checked or [],
        "required_commodity_ids": list(required_commodity_ids or []),
        "latest_reportable_month_before": before.get("latest_reportable_month"),
        "latest_reportable_month_after": after.get("latest_reportable_month"),
        "new_reportable_months": sorted(after_months - before_months),
        "rows_fetched": rows_fetched,
        "rows_real": rows_real,
        "rows_saved": rows_saved,
        "rows_skipped_existing": rows_skipped_existing,
        "excluded_non_real_rows": excluded_non_real_rows,
        "excluded_future_rows": excluded_future_rows,
        "deduplicated_rows": deduplicated_rows,
        "missing_by_month": after.get("missing_by_month") or {},
        "missing_by_basket_and_month": after.get("missing_by_basket_and_month") or {
            "primary": None,
            "secondary": None,
        },
        "reportability_before": before,
        "reportability_after": after,
        "warnings": _dedupe_preserve_order([str(item) for item in warnings or []]),
    }


def _manual_refresh_filter_warnings(flag_filter: Any, future_filter: Any, deduplication: Any) -> list[str]:
    warnings: list[str] = []
    if getattr(flag_filter, "excluded_rows", 0):
        parts = ", ".join(f"{flag}={count}" for flag, count in getattr(flag_filter, "excluded_flags", ()) or ())
        warnings.append(f"DataBridges refresh excluded {flag_filter.excluded_rows} non-real monthly price row(s): {parts}.")
    if getattr(future_filter, "excluded_rows", 0):
        warnings.append(
            "DataBridges refresh excluded "
            f"{future_filter.excluded_rows} future-dated monthly price row(s)."
        )
    if getattr(deduplication, "duplicate_rows", 0):
        warnings.append(
            f"DataBridges refresh deduplicated {deduplication.duplicate_rows} duplicate monthly price row(s)."
        )
    return warnings


def _invalidate_country_price_cache(iso3: str) -> None:
    country = str(iso3 or "").upper()
    _COUNTRY_CACHE.clear()
    _COMMODITY_CACHE.pop(country, None)
    for cache in (_PRICE_CACHE, _METADATA_CACHE, _REPORTABLE_MONTHS_CACHE):
        for key in list(cache.keys()):
            if key == country or (isinstance(key, tuple) and key and key[0] == country):
                cache.pop(key, None)


def _month_label(value: Any) -> Optional[str]:
    parsed = _date_obj(value)
    return parsed.strftime("%Y-%m") if parsed else None


def _parse_month_label(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value)[:7], "%Y-%m").date().replace(day=1)
    except ValueError:
        return None


def _month_labels_between(start: date, end: date) -> list[str]:
    if start > end:
        return []
    return [month.strftime("%Y-%m") for month in pd.date_range(start=start, end=end, freq="MS")]


def _add_months(value: date, months: int) -> date:
    return (pd.Timestamp(value) + pd.DateOffset(months=months)).date().replace(day=1)


def _month_end(value: date) -> date:
    return (pd.Timestamp(value) + pd.offsets.MonthEnd(0)).date()


def _markets_payload(metadata: CountryMetadata, *, country_name: str, iso3: str) -> list[dict[str, Any]]:
    markets = []
    for row in metadata.markets:
        markets.append(
            {
                "market_id": row.market_id,
                "market_name": row.market_name,
                "admin1_name": row.admin1_name or "",
                "admin1_code": None,
                "admin2_name": row.admin2_name or "",
                "admin2_code": None,
                "latitude": row.latitude,
                "longitude": row.longitude,
                "country": country_name,
                "iso3": iso3,
            }
        )
    return sorted(markets, key=lambda item: item["market_name"])


def _merge_units(metadata: CountryMetadata, derived_units: list[Any]) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for unit in metadata.units:
        by_id[unit.commodity_unit_id] = {
            "id": unit.commodity_unit_id,
            "name": unit.commodity_unit_name,
            "conversion_to_kg_l": unit.conversion_to_kg_l,
            "source": "cached_units",
        }
    for unit in derived_units:
        by_id.setdefault(
            unit.commodity_unit_id,
            {
                "id": unit.commodity_unit_id,
                "name": unit.commodity_unit_name,
                "conversion_to_kg_l": unit.conversion_to_kg_l,
                "source": "price_rows",
            },
        )
    return sorted(by_id.values(), key=lambda item: str(item["name"]).lower())


def _price_row_units_by_commodity(
    repo: PriceCacheRepository,
    iso3: str,
    availability: Any,
    *,
    commodity_ids: Optional[Sequence[int]] = None,
) -> dict[int, dict[str, Any]]:
    if hasattr(repo, "get_commodity_price_units"):
        return {
            commodity_id: {"unit_id": unit_id, "unit": unit_name}
            for commodity_id, (unit_id, unit_name) in repo.get_commodity_price_units(
                iso3, commodity_ids=commodity_ids
            ).items()
            if unit_name
        }
    # Legacy full-window scan kept for fake repositories without the aggregate.
    if not hasattr(repo, "get_price_window"):
        return {}
    start = _date_wire(getattr(availability, "date_start", None))
    end = _date_wire(getattr(availability, "date_end", None))
    if not start or not end:
        return {}
    units: dict[int, dict[str, Any]] = {}
    for row in repo.get_price_window(iso3, start, end):
        unit_name = str(row.commodity_unit_name or "").strip()
        if not unit_name:
            continue
        units.setdefault(
            int(row.commodity_id),
            {
                "unit_id": row.commodity_unit_id,
                "unit": unit_name,
            },
        )
    return units


def _country_currency(country: Any, *, fallback_country: str) -> dict[str, str]:
    code = getattr(country, "currency_code", None)
    name = getattr(country, "currency_name", None)
    if code or name:
        return {"code": str(code or "USD"), "name": str(name or code or "US Dollar")}
    return COUNTRY_CURRENCIES.get(fallback_country, {"code": "USD", "name": "US Dollar"})


def _cache_status_dict(status: CacheStatus) -> dict[str, Any]:
    warnings, operator_warnings = _split_warning_audiences(_cache_warnings(status))
    return {
        "source": "PriceCache",
        "has_active_cache": status.has_active_cache,
        "active_version_id": status.active_version_id,
        "status": status.status,
        "activated_at": _datetime_wire(status.activated_at),
        "completed_at": _datetime_wire(status.completed_at),
        "rows_prices": status.rows_prices,
        "rows_commodities": status.rows_commodities,
        "rows_markets": status.rows_markets,
        "rows_countries": status.rows_countries,
        "active_country_count": status.active_country_count,
        "validation_summary": status.validation_summary,
        "error_message": status.error_message,
        "warnings": warnings,
        "operator_warnings": operator_warnings,
    }


def _cache_warnings(status: CacheStatus, *, country_iso3: str | None = None) -> list[str]:
    summary = status.validation_summary or {}
    raw_items = list(summary.get("warnings") or [])
    if country_iso3:
        return _country_cache_warnings(raw_items, country_iso3=country_iso3, error_message=status.error_message)

    warnings: list[str] = []
    unit_warning: Optional[str] = None
    metadata_reference_countries = 0
    metadata_reference_count = 0
    dedup_countries: set[str] = set()
    dedup_rows = 0
    excluded_non_real_countries: set[str] = set()
    excluded_non_real_rows = 0
    conflict_countries: set[str] = set()
    conflict_keys = 0
    other_warnings: list[str] = []

    for item in raw_items:
        if isinstance(item, str):
            other_warnings.append(_sanitize_cache_warning(item))
            continue
        if not isinstance(item, dict):
            other_warnings.append(_sanitize_cache_warning(str(item)))
            continue

        warning = item.get("warning")
        if warning:
            sanitized = _sanitize_cache_warning(str(warning))
            if "CommodityUnits/List" in sanitized:
                unit_warning = unit_warning or sanitized
            else:
                other_warnings.append(sanitized)

        nested = item.get("warnings")
        if not isinstance(nested, list):
            continue
        item_country = str(item.get("country_iso3") or "").upper()
        for nested_warning in nested:
            text = str(nested_warning)
            metadata_match = re.search(r"Found\s+(\d+)\s+price metadata references", text)
            if metadata_match:
                metadata_reference_countries += 1
                metadata_reference_count += int(metadata_match.group(1))
                continue
            dedup_match = re.search(r"Deduplicated\s+(\d+)\s+duplicate monthly price row", text)
            if dedup_match:
                dedup_rows += int(dedup_match.group(1))
                if item_country:
                    dedup_countries.add(item_country)
                continue
            excluded_match = re.search(r"Excluded\s+(\d+)\s+non-real monthly price row", text)
            if excluded_match:
                excluded_non_real_rows += int(excluded_match.group(1))
                if item_country:
                    excluded_non_real_countries.add(item_country)
                continue
            conflict_match = re.search(r"(\d+)\s+duplicate monthly price key\(s\).*conflicting price values", text)
            if conflict_match:
                conflict_keys += int(conflict_match.group(1))
                if item_country:
                    conflict_countries.add(item_country)
                continue
            other_warnings.append(_sanitize_cache_warning(text))

    if unit_warning:
        warnings.append(unit_warning)
    if metadata_reference_countries:
        warnings.append(
            "Price metadata references were missing from current commodity/market metadata for "
            f"{metadata_reference_countries} country/countries ({metadata_reference_count} reference(s)); "
            "historical price rows were still cached."
        )
    if dedup_rows:
        warnings.append(
            f"Deduplicated {dedup_rows} duplicate monthly price row(s) across "
            f"{len(dedup_countries)} country/countries before cache publication."
        )
    if excluded_non_real_rows:
        warnings.append(
            f"Excluded {excluded_non_real_rows} non-real monthly price row(s) across "
            f"{len(excluded_non_real_countries)} country/countries before cache publication; "
            "only actual/aggregate rows are cached."
        )
    if conflict_keys:
        warnings.append(
            f"{conflict_keys} duplicate monthly price key(s) across {len(conflict_countries)} "
            "country/countries had conflicting price values; deterministic best-ranked rows were kept."
        )
    warnings.extend(other_warnings[:10])
    if len(other_warnings) > 10:
        warnings.append(f"{len(other_warnings) - 10} additional cache warning(s) omitted from this summary.")
    if status.error_message:
        warnings.append(str(status.error_message))
    return _dedupe_preserve_order(warnings)


def _country_cache_warnings(
    raw_items: list[Any],
    *,
    country_iso3: str,
    error_message: str | None = None,
) -> list[str]:
    country = country_iso3.upper()
    warnings: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            warnings.append(_sanitize_cache_warning(item))
            continue
        if not isinstance(item, dict):
            warnings.append(_sanitize_cache_warning(str(item)))
            continue

        item_country = str(item.get("country_iso3") or "").upper()
        warning = item.get("warning")
        if warning and not item_country:
            warnings.append(_sanitize_cache_warning(str(warning)))
        if item_country != country:
            continue
        nested = item.get("warnings")
        if isinstance(nested, list):
            warnings.extend(_sanitize_cache_warning(str(value)) for value in nested if value)
    if error_message:
        warnings.append(str(error_message))
    return _dedupe_preserve_order(warnings)


def _sanitize_cache_warning(warning: str) -> str:
    text_value = str(warning).strip()
    if len(text_value) > 500:
        text_value = text_value[:497] + "..."
    return text_value


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _bounded_cache_dates(start: Any, end: Any, latest: Any) -> tuple[Optional[dict[str, str]], Optional[str], bool]:
    start_d = _date_obj(start)
    end_d = _date_obj(end)
    latest_d = _date_obj(latest) or end_d
    current_month = _current_month_start()
    has_future_dates = any(value is not None and value > current_month for value in (end_d, latest_d))

    if end_d and end_d > current_month:
        end_d = current_month
    if latest_d and latest_d > current_month:
        latest_d = end_d or current_month
    if start_d and end_d and start_d > end_d:
        return None, _date_wire(latest_d), has_future_dates
    return _date_range_dict(start_d, end_d), _date_wire(latest_d), has_future_dates


def _current_month_start() -> date:
    today = date.today()
    return date(today.year, today.month, 1)


def _date_obj(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _safe_cache_metadata() -> dict[str, Any]:
    try:
        return get_cache_status_snapshot()
    except Exception:
        return {"source": "PriceCache", "has_active_cache": False}


def _date_range_dict(start: Any, end: Any) -> Optional[dict[str, str]]:
    if not start or not end:
        return None
    return {"start": _date_wire(start), "end": _date_wire(end)}


def _date_wire(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)[:10]


def _datetime_wire(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _empty_price_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "Country",
            "Country ISO3",
            "Commodity",
            "Commodity ID",
            "Price Type",
            "Price Date",
            "Price",
            "Admin 1",
            "Admin 2",
            "Market Name",
            "Market ID",
            "Unit ID",
            "Unit",
            "Currency",
            "Currency Code",
            "Data Type",
            "Price Flag",
            "Observations",
            "Data Source",
        ]
    )


def _is_real_price_flag(flag: Any) -> bool:
    normalized = str(flag or "").strip().lower()
    if not normalized:
        return True
    components = [component.strip() for component in normalized.split(",") if component.strip()]
    return bool(components) and all(component in _REAL_PRICE_FLAG_COMPONENTS for component in components)


def _data_type_from_flag(flag: str) -> str:
    components = [component.strip() for component in str(flag or "").split(",") if component.strip()]
    if any(component in {"aggregate", "aggregated"} for component in components):
        return "Aggregated"
    if components:
        return components[0].title()
    return ""


def _select_report_currency(
    df: pd.DataFrame,
    *,
    requested_code: Optional[str] = None,
    country: Optional[str] = None,
) -> tuple[pd.DataFrame, Optional[str], list[str]]:
    """Restrict a price dataframe to a single currency.

    Databridges can quote the same series in several currencies (e.g. LBP and
    USD in Lebanon); averaging across them would corrupt every statistic, so one
    currency is selected per report: the requested currency if present, then the
    country default, then the most frequent currency in the data.
    """
    if df.empty or "Currency" not in df.columns:
        return df, None, []

    labels = df["Currency"].fillna("").astype(str)
    codes = (
        df["Currency Code"].fillna("").astype(str)
        if "Currency Code" in df.columns
        else labels
    )
    distinct_labels = sorted({label for label in labels if label.strip()})
    if len(distinct_labels) <= 1:
        return df, (distinct_labels[0] if distinct_labels else None), []

    preferences: list[str] = []
    if requested_code:
        preferences.append(str(requested_code))
    default_currency = COUNTRY_CURRENCIES.get(country or "")
    if default_currency:
        preferences.extend([default_currency.get("code") or "", default_currency.get("name") or ""])

    selected_label: Optional[str] = None
    for preference in preferences:
        if not str(preference).strip():
            continue
        match_mask = (codes.str.lower() == str(preference).lower()) | (
            labels.str.lower() == str(preference).lower()
        )
        if match_mask.any():
            selected_label = labels[match_mask].iloc[0]
            break
    if selected_label is None:
        selected_label = labels[labels.str.strip() != ""].value_counts().idxmax()

    excluded = sorted(set(distinct_labels) - {selected_label})
    return df[labels == selected_label].copy(), selected_label, excluded


def _split_warning_audiences(warnings: list[str]) -> tuple[list[str], list[str]]:
    """Separate officer-relevant warnings from refresh-worker ETL telemetry."""
    user_warnings: list[str] = []
    operator_warnings: list[str] = []
    for warning in warnings:
        text_value = str(warning)
        if any(marker in text_value for marker in _OPERATOR_WARNING_MARKERS):
            operator_warnings.append(text_value)
        else:
            user_warnings.append(text_value)
    return user_warnings, operator_warnings


def _field(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row:
            return row[name]
    lowered = {key.lower(): value for key, value in row.items()}
    for name in names:
        key = name.lower()
        if key in lowered:
            return lowered[key]
    return default


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _sort_int(value: Any) -> int:
    parsed = _to_int(value)
    return parsed if parsed is not None else 10**12


def _sort_float(value: Any) -> float:
    parsed = _to_float(value)
    return parsed if parsed is not None else float("inf")


def _cache_get(cache: dict[Any, tuple[float, Any]], key: Any) -> Any:
    item = cache.get(key)
    if not item:
        return None
    created_at, value = item
    if time.time() - created_at > _CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return value


def _cache_set(cache: dict[Any, tuple[float, Any]], key: Any, value: Any) -> None:
    cache[key] = (time.time(), value)
