from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from app.shared.countries import supported_country_options

from .config import PriceCacheConfig, load_price_cache_config
from .databridges_adapter import DataBridgesClientAdapter
from .migrations import apply_migrations
from .row_hygiene import (
    FuturePriceFilterResult,
    PriceDeduplicationResult,
    PriceFlagFilterResult,
    current_month_start_utc as _current_month_start,
    deduplicate_monthly_price_rows as _deduplicate_monthly_price_rows,
    enrich_price_rows_with_metadata as _enrich_price_rows_with_metadata,
    filter_future_monthly_price_rows as _filter_future_monthly_price_rows,
    filter_real_monthly_price_rows as _filter_real_monthly_price_rows,
)
from .sql_repository import SqlPriceCacheRepository, create_price_cache_engine
from .validation import (
    CountryCacheValidationResult,
    validate_country_snapshot,
)


logger = logging.getLogger(__name__)


class RefreshLockUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CountryRefreshOutcome:
    country_iso3: str
    country_name: str
    status: str
    rows_prices: int = 0
    rows_commodities: int = 0
    rows_markets: int = 0
    latest_price_date: str | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "country_iso3": self.country_iso3,
            "country_name": self.country_name,
            "status": self.status,
            "rows_prices": self.rows_prices,
            "rows_commodities": self.rows_commodities,
            "rows_markets": self.rows_markets,
            "latest_price_date": self.latest_price_date,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


class PriceCacheRefreshWorker:
    def __init__(
        self,
        *,
        repository: SqlPriceCacheRepository,
        adapter: DataBridgesClientAdapter,
        config: PriceCacheConfig,
        country_options: Optional[Sequence[dict[str, Any]]] = None,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.config = config
        self.country_options = list(country_options) if country_options is not None else supported_country_options()

    def run(
        self,
        *,
        countries: Optional[Sequence[str]] = None,
        triggered_by: str = "worker",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not self.config.refresh_enabled and not dry_run:
            raise RuntimeError("PRICE_CACHE_REFRESH_ENABLED is false.")

        selected_options = self._select_countries(countries)
        country_codes = [str(option["iso3"]).upper() for option in selected_options]
        version_id = str(uuid.uuid4())
        owner = f"{triggered_by}:{version_id}"
        lock_acquired = False
        outcomes: list[CountryRefreshOutcome] = []
        units: list[dict[str, Any]] = []
        currencies: list[dict[str, Any]] = []
        unit_lookup: dict[int, dict[str, Any]] = {}
        global_warnings: list[dict[str, Any]] = []

        effective_start_date = start_date if start_date is not None else self.config.refresh_start_date
        effective_end_date = end_date if end_date is not None else self.config.refresh_end_date

        if not dry_run:
            lock_acquired = self.repository.acquire_refresh_lock(
                "weekly_full_refresh",
                owner,
                self.config.refresh_lock_timeout_minutes,
            )
            if not lock_acquired:
                raise RefreshLockUnavailable("A price cache refresh is already running.")
            version_id = self.repository.create_cache_version(
                cache_version_id=version_id,
                refresh_type="weekly_full",
                triggered_by=triggered_by,
                source_host=self.adapter.config.base_url,
                source_env=self.adapter.config.env,
            )

        try:
            try:
                units = self.adapter.fetch_units()
                unit_lookup.update(_collect_units(units))
            except Exception as exc:
                message = _safe_error(exc)
                global_warnings.append(
                    {
                        "scope": "units",
                        "warning": (
                            "CommodityUnits/List could not be fetched; units will be derived from "
                            f"commodity and monthly price rows where available. Error: {message}"
                        ),
                    }
                )

            try:
                currencies = self.adapter.fetch_currencies()
                if not dry_run:
                    self.repository.insert_currencies(version_id, currencies)
            except Exception as exc:
                message = _safe_error(exc)
                if not dry_run:
                    self.repository.publish_cache_version(
                        version_id,
                        country_iso3s=[],
                        status="failed",
                        validation_summary={"global_metadata_error": message},
                        error_message=message,
                    )
                return self._summary(
                    cache_version_id=version_id,
                    status="failed",
                    countries_requested=country_codes,
                    outcomes=outcomes,
                    rows_units=len(unit_lookup),
                    rows_currencies=0,
                    errors=[{"scope": "global_metadata", "error": message}],
                    dry_run=dry_run,
                    warnings=global_warnings,
                )

            for option in selected_options:
                outcome = self._refresh_country(
                    cache_version_id=version_id,
                    option=option,
                    start_date=effective_start_date,
                    end_date=effective_end_date,
                    unit_lookup=unit_lookup,
                    dry_run=dry_run,
                )
                outcomes.append(outcome)

            successful_countries = [outcome.country_iso3 for outcome in outcomes if outcome.status == "success"]
            status = _version_status(len(successful_countries), len(outcomes))
            errors = [
                {"country_iso3": outcome.country_iso3, "errors": list(outcome.errors)}
                for outcome in outcomes
                if outcome.errors
            ]
            validation_summary = {
                "countries_requested": len(outcomes),
                "countries_successful": len(successful_countries),
                "countries_failed": len(outcomes) - len(successful_countries),
                "errors": errors,
                "warnings": [
                    *global_warnings,
                    *[
                        {"country_iso3": outcome.country_iso3, "warnings": list(outcome.warnings)}
                        for outcome in outcomes
                        if outcome.warnings
                    ],
                ],
            }

            if not dry_run:
                merged_units = sorted(unit_lookup.values(), key=lambda row: str(row["commodity_unit_name"]).lower())
                if merged_units:
                    self.repository.insert_units(version_id, merged_units)
                self.repository.publish_cache_version(
                    version_id,
                    country_iso3s=successful_countries,
                    status=status,
                    validation_summary=validation_summary,
                    error_message=None if successful_countries else "No countries passed validation.",
                )
                if successful_countries:
                    self.repository.cleanup_old_versions(retain_versions=self.config.retain_versions)

            return self._summary(
                cache_version_id=version_id,
                status=status,
                countries_requested=country_codes,
                outcomes=outcomes,
                rows_units=len(unit_lookup),
                rows_currencies=len(currencies),
                errors=errors,
                dry_run=dry_run,
                warnings=validation_summary["warnings"],
            )
        finally:
            if lock_acquired:
                self.repository.release_refresh_lock("weekly_full_refresh", owner)

    def _refresh_country(
        self,
        *,
        cache_version_id: str,
        option: dict[str, Any],
        start_date: Optional[str],
        end_date: Optional[str],
        unit_lookup: dict[int, dict[str, Any]],
        dry_run: bool,
    ) -> CountryRefreshOutcome:
        country_iso3 = str(option["iso3"]).upper()
        country_name = str(option.get("name") or country_iso3)
        started_at = datetime.now(timezone.utc)
        try:
            commodities = self.adapter.fetch_commodities(country_iso3)
            markets = self.adapter.fetch_markets(country_iso3)
            prices = self.adapter.fetch_monthly_price_rows(
                country_iso3,
                start_date=start_date,
                end_date=end_date,
            )
            flag_filter = _filter_real_monthly_price_rows(prices)
            prices = flag_filter.rows
            future_filter = _filter_future_monthly_price_rows(prices, current_month_start=_current_month_start())
            prices = future_filter.rows
            prices = _enrich_price_rows_with_metadata(prices, markets=markets, commodities=commodities)
            deduplication = _deduplicate_monthly_price_rows(prices)
            prices = deduplication.rows
            unit_lookup.update(_collect_units(commodities, prices))
            previous_count = 0 if dry_run else self.repository.count_active_country_price_rows(country_iso3)
            validation = validate_country_snapshot(
                country_iso3=country_iso3,
                prices=prices,
                commodities=commodities,
                markets=markets,
                previous_rows_prices=previous_count,
                max_country_drop_ratio=self.config.validate_max_country_drop_ratio,
            )
            validation.warnings.extend(_price_flag_filter_warnings(country_iso3, flag_filter))
            validation.warnings.extend(_future_price_filter_warnings(country_iso3, future_filter))
            validation.warnings.extend(_deduplication_warnings(country_iso3, deduplication))
            if not validation.valid:
                self._record_country_failure(
                    cache_version_id=cache_version_id,
                    country_iso3=country_iso3,
                    validation=validation,
                    started_at=started_at,
                    dry_run=dry_run,
                )
                return _outcome(country_name=country_name, validation=validation, status="failed")

            currency_code = _first_present(
                [price.get("currency_code") for price in prices],
                fallback=option.get("currency_code"),
            )
            currency_name = _first_present(
                [price.get("currency_name") for price in prices],
                fallback=option.get("currency_name"),
            )
            if not dry_run:
                self.repository.insert_country_snapshot(
                    cache_version_id=cache_version_id,
                    country_iso3=country_iso3,
                    country_name=country_name,
                    commodities=commodities,
                    markets=markets,
                    prices=prices,
                    latest_price_date=validation.latest_price_date,
                    currency_code=currency_code,
                    currency_name=currency_name,
                )
                self.repository.record_country_result(
                    cache_version_id=cache_version_id,
                    country_iso3=country_iso3,
                    status="success",
                    rows_prices=validation.rows_prices,
                    rows_commodities=validation.rows_commodities,
                    rows_markets=validation.rows_markets,
                    latest_price_date=validation.latest_price_date,
                    validation_summary=validation.to_summary(),
                    started_at=started_at,
                )
            return _outcome(country_name=country_name, validation=validation, status="success")
        except Exception as exc:
            message = _safe_error(exc)
            if not dry_run:
                self.repository.record_country_result(
                    cache_version_id=cache_version_id,
                    country_iso3=country_iso3,
                    status="failed",
                    error_message=message,
                    validation_summary={"errors": [message]},
                    started_at=started_at,
                )
            return CountryRefreshOutcome(
                country_iso3=country_iso3,
                country_name=country_name,
                status="failed",
                errors=(message,),
            )

    def _record_country_failure(
        self,
        *,
        cache_version_id: str,
        country_iso3: str,
        validation: CountryCacheValidationResult,
        started_at: datetime,
        dry_run: bool,
    ) -> None:
        if dry_run:
            return
        self.repository.record_country_result(
            cache_version_id=cache_version_id,
            country_iso3=country_iso3,
            status="failed",
            rows_prices=validation.rows_prices,
            rows_commodities=validation.rows_commodities,
            rows_markets=validation.rows_markets,
            latest_price_date=validation.latest_price_date,
            validation_summary=validation.to_summary(),
            error_message="; ".join(validation.errors),
            started_at=started_at,
        )

    def _select_countries(self, countries: Optional[Sequence[str]]) -> list[dict[str, Any]]:
        if not countries:
            return list(self.country_options)
        requested = {str(country).upper() for country in countries if country}
        selected = [
            option
            for option in self.country_options
            if str(option.get("iso3") or "").upper() in requested
            or str(option.get("name") or "").upper() in requested
        ]
        found = {str(option.get("iso3") or "").upper() for option in selected}
        missing = sorted(requested - found)
        if missing:
            raise ValueError(f"Unsupported country code(s): {', '.join(missing)}.")
        return selected

    def _summary(
        self,
        *,
        cache_version_id: str,
        status: str,
        countries_requested: Sequence[str],
        outcomes: Sequence[CountryRefreshOutcome],
        rows_units: int,
        rows_currencies: int,
        errors: Sequence[dict[str, Any]],
        dry_run: bool,
        warnings: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        successful = [outcome for outcome in outcomes if outcome.status == "success"]
        return {
            "cache_version_id": cache_version_id,
            "status": status,
            "dry_run": dry_run,
            "countries_requested": len(countries_requested),
            "countries_successful": len(successful),
            "countries_failed": len(outcomes) - len(successful),
            "rows_prices": sum(outcome.rows_prices for outcome in successful),
            "rows_commodities": sum(outcome.rows_commodities for outcome in successful),
            "rows_markets": sum(outcome.rows_markets for outcome in successful),
            "rows_units": rows_units,
            "rows_currencies": rows_currencies,
            "errors": list(errors),
            "warnings": list(warnings),
            "countries": [outcome.to_dict() for outcome in outcomes],
        }


def build_worker() -> PriceCacheRefreshWorker:
    config = load_price_cache_config()
    # The worker's bulk snapshot writes legitimately run longer than the app's
    # statement_timeout cap, so its engine opts out of it.
    engine = create_price_cache_engine(config, apply_statement_timeout=False)
    apply_migrations(engine, config.backend)
    repository = SqlPriceCacheRepository(engine)
    adapter = DataBridgesClientAdapter()
    return PriceCacheRefreshWorker(repository=repository, adapter=adapter, config=config)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly DataBridges price cache refresh worker.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run a full or country-filtered cache refresh.")
    run_parser.add_argument("--country", action="append", dest="countries", help="ISO3 code. Repeatable.")
    run_parser.add_argument("--triggered-by", default="worker", help="Refresh trigger label.")
    run_parser.add_argument("--start-date", help="Optional price start date, YYYY-MM-DD.")
    run_parser.add_argument("--end-date", help="Optional price end date, YYYY-MM-DD.")
    run_parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without DB writes.")
    run_parser.add_argument("--json", action="store_true", help="Print JSON summary.")

    args = parser.parse_args(argv)
    if args.command != "run":
        return 2

    summary = build_worker().run(
        countries=args.countries,
        triggered_by=args.triggered_by,
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(_format_summary(summary))
    return 1 if summary["status"] == "failed" else 0


def _version_status(successful: int, requested: int) -> str:
    if successful <= 0:
        return "failed"
    if successful == requested:
        return "active"
    return "partial_active"


def _outcome(
    *,
    country_name: str,
    validation: CountryCacheValidationResult,
    status: str,
) -> CountryRefreshOutcome:
    return CountryRefreshOutcome(
        country_iso3=validation.country_iso3,
        country_name=country_name,
        status=status,
        rows_prices=validation.rows_prices,
        rows_commodities=validation.rows_commodities,
        rows_markets=validation.rows_markets,
        latest_price_date=validation.latest_price_date.isoformat() if validation.latest_price_date else None,
        errors=tuple(validation.errors),
        warnings=tuple(validation.warnings),
    )


def _future_price_filter_warnings(country_iso3: str, result: FuturePriceFilterResult) -> list[str]:
    if result.excluded_rows <= 0:
        return []
    return [
        (
            f"Excluded {result.excluded_rows} future-dated monthly price row(s) for {country_iso3.upper()} "
            f"(latest excluded date {result.max_excluded_date}); only months up to the current month are cached."
        )
    ]


def _price_flag_filter_warnings(country_iso3: str, result: PriceFlagFilterResult) -> list[str]:
    if result.excluded_rows <= 0:
        return []
    flag_summary = ", ".join(f"{flag}={count}" for flag, count in result.excluded_flags)
    return [
        (
            f"Excluded {result.excluded_rows} non-real monthly price row(s) for {country_iso3.upper()} "
            f"based on price_flag ({flag_summary}); only actual/aggregate rows are cached."
        )
    ]


def _deduplication_warnings(country_iso3: str, result: PriceDeduplicationResult) -> list[str]:
    if result.duplicate_rows <= 0:
        return []
    warning = (
        f"Deduplicated {result.duplicate_rows} duplicate monthly price row(s) across "
        f"{result.duplicate_keys} canonical key(s) for {country_iso3.upper()}; kept the row "
        "with the most complete metadata and highest observation count per key."
    )
    warnings = [warning]
    if result.conflicting_price_keys:
        warnings.append(
            f"{result.conflicting_price_keys} duplicate monthly price key(s) for {country_iso3.upper()} "
            "had conflicting price values; the deterministic best-ranked row was kept."
        )
    return warnings


def _first_present(values: Sequence[Any], *, fallback: Any = None) -> Optional[str]:
    for value in values:
        if value not in (None, ""):
            return str(value)
    if fallback not in (None, ""):
        return str(fallback)
    return None


def _collect_units(*collections: Sequence[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    units: dict[int, dict[str, Any]] = {}
    for rows in collections:
        for row in rows or []:
            unit_id = _maybe_int(_first_present_field(row, "commodity_unit_id", "commodityUnitId", "unit_id", "unitId"))
            unit_name = _first_present_field(
                row,
                "commodity_unit_name",
                "commodityUnitName",
                "unit_name",
                "unitName",
                "unit",
            )
            if unit_id is None or not unit_name:
                continue
            units[unit_id] = {
                "commodity_unit_id": unit_id,
                "commodity_unit_name": unit_name,
                "conversion_to_kg_l": row.get("conversion_to_kg_l"),
                "active": row.get("active", True),
            }
    return units


def _first_present_field(row: dict[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return str(value)
    return None


def _maybe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_error(exc: BaseException) -> str:
    return str(exc)


def _format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"Price cache refresh {summary['status']}",
        f"Version: {summary['cache_version_id']}",
        f"Countries: {summary['countries_successful']} successful, {summary['countries_failed']} failed, {summary['countries_requested']} requested",
        f"Rows: prices={summary['rows_prices']}, commodities={summary['rows_commodities']}, markets={summary['rows_markets']}, units={summary['rows_units']}, currencies={summary['rows_currencies']}",
    ]
    if summary.get("errors"):
        lines.append("Errors:")
        for error in summary["errors"]:
            scope = error.get("country_iso3") or error.get("scope") or "unknown"
            lines.append(f"- {scope}: {error}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
