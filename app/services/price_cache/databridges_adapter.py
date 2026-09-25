from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from importlib import import_module
from typing import Any, Callable, Mapping, Optional, Sequence

from app.shared.countries import supported_country_options


DEFAULT_BASE_URL = "https://gateway.api.wfp.org/vam-data-bridges/v2"
DEFAULT_TOKEN_URL = (
    "https://login.microsoftonline.com/462ad9ae-d7d9-4206-b874-71b1e079776f/"
    "oauth2/v2.0/token"
)
DEFAULT_SCOPE = "api://wfp-api-mediation-service/.default"
DEFAULT_ENV = "prod"
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
PERMANENT_STATUS_CODES = {400, 403, 404}


class DataBridgesAdapterError(RuntimeError):
    pass


class DataBridgesNormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class DataBridgesAdapterConfig:
    api_key: str
    api_secret: str
    base_url: str = DEFAULT_BASE_URL
    token_url: str = DEFAULT_TOKEN_URL
    scope: str = DEFAULT_SCOPE
    env: str = DEFAULT_ENV
    page_size: int = 1000
    max_workers: int = 5
    request_timeout_seconds: int = 60
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    max_pages: int = 1000


@dataclass(frozen=True)
class PagedFetchResult:
    rows: list[dict[str, Any]]
    pages: int


@dataclass(frozen=True)
class ExchangeRateFetchResult:
    rows: list[dict[str, Any]]
    pages: int = 0
    permission_denied: bool = False
    error: Optional[str] = None


def load_databridges_adapter_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    validate: bool = True,
) -> DataBridgesAdapterConfig:
    values = env or os.environ
    config = DataBridgesAdapterConfig(
        api_key=_env_first(values, "WFP_V2_API_KEY", "DATA_BRIDGES_KEY"),
        api_secret=_env_first(values, "WFP_V2_API_SECRET", "DATA_BRIDGES_SECRET"),
        base_url=_env_first(
            values,
            "WFP_V2_API_BASE_URL",
            "DATA_BRIDGES_API_BASE_URL",
            default=DEFAULT_BASE_URL,
        ).rstrip("/"),
        token_url=_env_first(
            values,
            "WFP_V2_TOKEN_URL",
            "DATA_BRIDGES_TOKEN_URL",
            default=DEFAULT_TOKEN_URL,
        ),
        scope=_env_first(values, "WFP_V2_API_SCOPE", "DATA_BRIDGES_SCOPE", default=DEFAULT_SCOPE),
        env=_env_first(values, "WFP_V2_API_ENV", "DATA_BRIDGES_ENV", default=DEFAULT_ENV),
        page_size=_int_from_env(values, "DATA_BRIDGES_PAGE_SIZE", default=1000, minimum=1),
        max_workers=_int_from_env(values, "DATA_BRIDGES_MAX_WORKERS", default=5, minimum=1),
        request_timeout_seconds=_int_from_env(
            values,
            "DATA_BRIDGES_REQUEST_TIMEOUT_SECONDS",
            "DATA_BRIDGES_TIMEOUT",
            default=60,
            minimum=1,
        ),
        max_retries=_int_from_env(values, "DATA_BRIDGES_MAX_RETRIES", default=3, minimum=1),
        retry_backoff_seconds=_float_from_env(
            values,
            "DATA_BRIDGES_RETRY_BACKOFF_SECONDS",
            default=1.0,
            minimum=0.0,
        ),
        max_pages=_int_from_env(values, "DATA_BRIDGES_MAX_PAGES", default=1000, minimum=1),
    )
    if validate:
        validate_databridges_adapter_config(config)
    return config


def validate_databridges_adapter_config(config: DataBridgesAdapterConfig) -> None:
    if not config.api_key or not config.api_secret:
        raise ValueError(
            "Databridges credentials are not configured. Set WFP_V2_API_KEY "
            "and WFP_V2_API_SECRET, or DATA_BRIDGES_KEY and DATA_BRIDGES_SECRET."
        )
    if not config.base_url:
        raise ValueError("Databridges base URL is required.")


class DataBridgesClientAdapter:
    def __init__(
        self,
        config: Optional[DataBridgesAdapterConfig] = None,
        *,
        client_module: Optional[Any] = None,
        api_client: Optional[Any] = None,
        market_prices_api: Optional[Any] = None,
        commodities_api: Optional[Any] = None,
        commodity_units_api: Optional[Any] = None,
        markets_api: Optional[Any] = None,
        currency_api: Optional[Any] = None,
        token_factory: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or load_databridges_adapter_config()
        self._client_module = client_module
        self._api_client = api_client
        self._market_prices_api = market_prices_api
        self._commodities_api = commodities_api
        self._commodity_units_api = commodity_units_api
        self._markets_api = markets_api
        self._currency_api = currency_api
        self._token_factory = token_factory
        self._sleep = sleep
        self._token: Optional[Any] = None
        self._configuration: Optional[Any] = None
        self._api_exception_type: tuple[type[BaseException], ...] = ()

        if not all(
            (
                self._market_prices_api,
                self._commodities_api,
                self._commodity_units_api,
                self._markets_api,
                self._currency_api,
            )
        ):
            self._build_official_clients()

    @property
    def api_client(self) -> Any:
        return self._api_client

    @property
    def configuration(self) -> Any:
        return self._configuration

    def fetch_monthly_price_rows(
        self,
        country_iso3: str,
        *,
        market_id: Optional[int] = None,
        commodity_id: Optional[int] = None,
        price_type_name: Optional[str] = None,
        currency_id: Optional[int] = None,
        price_flag: Optional[str] = None,
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
        latest_value_only: bool = False,
    ) -> list[dict[str, Any]]:
        return self._fetch_monthly_price_rows_result(
            country_iso3,
            market_id=market_id,
            commodity_id=commodity_id,
            price_type_name=price_type_name,
            currency_id=currency_id,
            price_flag=price_flag,
            start_date=start_date,
            end_date=end_date,
            latest_value_only=latest_value_only,
        ).rows

    def fetch_commodities(self, country_iso3: str) -> list[dict[str, Any]]:
        return self._fetch_commodities_result(country_iso3).rows

    def fetch_units(self) -> list[dict[str, Any]]:
        return self._fetch_units_result().rows

    def fetch_markets(self, country_iso3: str) -> list[dict[str, Any]]:
        return self._fetch_markets_result(country_iso3).rows

    def fetch_currencies(self) -> list[dict[str, Any]]:
        return self._fetch_currencies_result().rows

    def fetch_exchange_rate_rows(
        self,
        country_iso3: str,
        *,
        currency_name: Optional[str] = None,
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
    ) -> list[dict[str, Any]]:
        return self.fetch_exchange_rate_rows_result(
            country_iso3,
            currency_name=currency_name,
            start_date=start_date,
            end_date=end_date,
        ).rows

    def fetch_exchange_rate_rows_result(
        self,
        country_iso3: str,
        *,
        currency_name: Optional[str] = None,
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
    ) -> ExchangeRateFetchResult:
        return self._fetch_exchange_rate_rows_result(
            country_iso3,
            currency_name=currency_name,
            start_date=start_date,
            end_date=end_date,
        )

    def dry_run_full_cache(
        self,
        *,
        countries: Optional[Sequence[str]] = None,
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
        max_countries: Optional[int] = None,
        max_workers: Optional[int] = None,
    ) -> dict[str, Any]:
        country_codes = [str(item).upper() for item in (countries or _default_country_codes()) if item]
        if max_countries is not None:
            country_codes = country_codes[: max(0, max_countries)]

        summary: dict[str, Any] = {
            "countries_requested": len(country_codes),
            "countries_completed": 0,
            "countries_failed": 0,
            "rows_prices": 0,
            "rows_commodities": 0,
            "rows_markets": 0,
            "rows_units": 0,
            "rows_currencies": 0,
            "pages_prices": 0,
            "pages_commodities": 0,
            "pages_markets": 0,
            "pages_units": 0,
            "pages_currencies": 0,
            "errors": [],
            "countries": [],
            "start_date": _date_wire(start_date),
            "end_date": _date_wire(end_date),
            "source_host": self.config.base_url,
            "source_env": self.config.env,
        }

        try:
            units = self._fetch_units_result()
            currencies = self._fetch_currencies_result()
            summary["rows_units"] = len(units.rows)
            summary["rows_currencies"] = len(currencies.rows)
            summary["pages_units"] = units.pages
            summary["pages_currencies"] = currencies.pages
        except Exception as exc:
            summary["errors"].append({"scope": "global_metadata", "error": _safe_error(exc)})

        workers = max_workers or self.config.max_workers
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    self._dry_run_country,
                    country_iso3,
                    start_date=start_date,
                    end_date=end_date,
                ): country_iso3
                for country_iso3 in country_codes
            }
            for future in as_completed(futures):
                result = future.result()
                summary["countries"].append(result)
                summary["rows_prices"] += int(result.get("rows_prices") or 0)
                summary["rows_commodities"] += int(result.get("rows_commodities") or 0)
                summary["rows_markets"] += int(result.get("rows_markets") or 0)
                summary["pages_prices"] += int(result.get("pages_prices") or 0)
                summary["pages_commodities"] += int(result.get("pages_commodities") or 0)
                summary["pages_markets"] += int(result.get("pages_markets") or 0)
                if result.get("error"):
                    summary["countries_failed"] += 1
                    summary["errors"].append(
                        {"country_iso3": result.get("country_iso3"), "error": result.get("error")}
                    )
                else:
                    summary["countries_completed"] += 1

        summary["countries"] = sorted(summary["countries"], key=lambda item: str(item["country_iso3"]))
        return summary

    def _fetch_monthly_price_rows_result(
        self,
        country_iso3: str,
        *,
        market_id: Optional[int] = None,
        commodity_id: Optional[int] = None,
        price_type_name: Optional[str] = None,
        currency_id: Optional[int] = None,
        price_flag: Optional[str] = None,
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
        latest_value_only: bool = False,
    ) -> PagedFetchResult:
        params = {
            "country_code": country_iso3.upper(),
            "market_id": market_id,
            "commodity_id": commodity_id,
            "price_type_name": price_type_name,
            "currency_id": currency_id,
            "price_flag": price_flag,
            "start_date": _date_wire(start_date),
            "end_date": _date_wire(end_date),
            "latest_value_only": latest_value_only,
        }
        result = self._fetch_paged(
            self._market_prices_api.market_prices_price_monthly_get,
            params=params,
        )
        return PagedFetchResult(
            rows=[normalize_monthly_price_row(item, country_iso3=country_iso3) for item in result.rows],
            pages=result.pages,
        )

    def _fetch_commodities_result(self, country_iso3: str) -> PagedFetchResult:
        result = self._fetch_paged(
            self._commodities_api.commodities_list_get,
            params={"country_code": country_iso3.upper()},
        )
        return PagedFetchResult(
            rows=[normalize_commodity_row(item, country_iso3=country_iso3) for item in result.rows],
            pages=result.pages,
        )

    def _fetch_units_result(self) -> PagedFetchResult:
        result = self._fetch_paged(self._commodity_units_api.commodity_units_list_get, params={})
        return PagedFetchResult(
            rows=[normalize_unit_row(item) for item in result.rows],
            pages=result.pages,
        )

    def _fetch_markets_result(self, country_iso3: str) -> PagedFetchResult:
        result = self._fetch_paged(
            self._markets_api.markets_list_get,
            params={"country_code": country_iso3.upper()},
        )
        return PagedFetchResult(
            rows=[normalize_market_row(item, country_iso3=country_iso3) for item in result.rows],
            pages=result.pages,
        )

    def _fetch_currencies_result(self) -> PagedFetchResult:
        result = self._fetch_paged(self._currency_api.currency_list_get, params={})
        return PagedFetchResult(
            rows=[normalize_currency_row(item) for item in result.rows],
            pages=result.pages,
        )

    def _fetch_exchange_rate_rows_result(
        self,
        country_iso3: str,
        *,
        currency_name: Optional[str] = None,
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
    ) -> ExchangeRateFetchResult:
        method = getattr(self._currency_api, "currency_usd_indirect_quotation_get", None)
        if method is None:
            return ExchangeRateFetchResult(
                rows=[],
                pages=0,
                permission_denied=False,
                error="DataBridges CurrencyApi does not expose currency_usd_indirect_quotation_get.",
            )

        params = {
            "country_code": str(country_iso3 or "").upper(),
            "currency_name": currency_name,
        }
        start_bound = _to_date_value(start_date)
        end_bound = _to_date_value(end_date)
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        pages = 0
        seen_raw = 0
        first_total: Optional[int] = None
        try:
            for page in range(1, self.config.max_pages + 1):
                kwargs = _clean_params(
                    {
                        **params,
                        "page": page,
                        "format": "json",
                        "env": self.config.env,
                        "_request_timeout": float(self.config.request_timeout_seconds),
                    }
                )
                payload = self._request_with_retry(method, kwargs)
                page_items = _payload_items(payload)
                pages += 1
                if not page_items:
                    break
                seen_raw += len(page_items)
                total = _payload_total(payload)
                if first_total is None and total is not None:
                    first_total = total

                page_dates: list[date] = []
                for item in page_items:
                    try:
                        row = normalize_exchange_rate_row(item, country_iso3=country_iso3)
                    except Exception as exc:
                        errors.append(_safe_error(exc))
                        continue
                    row_date = row["date"]
                    page_dates.append(row_date)
                    if start_bound is not None and row_date < start_bound:
                        continue
                    if end_bound is not None and row_date > end_bound:
                        continue
                    rows.append(row)

                if start_bound is not None and page_dates and min(page_dates) < start_bound:
                    break
                if first_total is not None and seen_raw >= first_total:
                    break
            else:
                raise DataBridgesAdapterError(f"Databridges pagination exceeded {self.config.max_pages} pages.")
        except Exception as exc:
            message = _safe_error(exc)
            return ExchangeRateFetchResult(
                rows=[],
                pages=pages,
                permission_denied=_looks_permission_denied(message, exc),
                error=message,
            )

        return ExchangeRateFetchResult(
            rows=rows,
            pages=pages,
            permission_denied=False,
            error="; ".join(errors[:3]) if errors else None,
        )

    def _fetch_paged(self, method: Callable[..., Any], *, params: dict[str, Any]) -> PagedFetchResult:
        rows: list[dict[str, Any]] = []
        pages = 0
        first_total: Optional[int] = None
        for page in range(1, self.config.max_pages + 1):
            kwargs = _clean_params(
                {
                    **params,
                    "page": page,
                    "format": "json",
                    "env": self.config.env,
                    "_request_timeout": float(self.config.request_timeout_seconds),
                }
            )
            payload = self._request_with_retry(method, kwargs)
            page_items = _payload_items(payload)
            pages += 1
            if not page_items:
                break
            rows.extend(page_items)
            total = _payload_total(payload)
            if first_total is None and total is not None:
                first_total = total
            if first_total is not None and len(rows) >= first_total:
                break
        else:
            raise DataBridgesAdapterError(f"Databridges pagination exceeded {self.config.max_pages} pages.")
        return PagedFetchResult(rows=rows, pages=pages)

    def _request_with_retry(self, method: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        refreshed_for_auth = False
        attempt = 1
        while True:
            try:
                return method(**kwargs)
            except Exception as exc:
                if _is_auth_error(exc) and not refreshed_for_auth:
                    self._refresh_token(force=True)
                    refreshed_for_auth = True
                    continue
                if _is_permanent_error(exc) or attempt >= self.config.max_retries:
                    raise DataBridgesAdapterError(_safe_error(exc)) from exc
                if not _is_transient_error(exc):
                    raise DataBridgesAdapterError(_safe_error(exc)) from exc
                self._sleep(self.config.retry_backoff_seconds * (2 ** (attempt - 1)))
                attempt += 1

    def _dry_run_country(
        self,
        country_iso3: str,
        *,
        start_date: Optional[date | str],
        end_date: Optional[date | str],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "country_iso3": country_iso3,
            "rows_prices": 0,
            "rows_commodities": 0,
            "rows_markets": 0,
            "pages_prices": 0,
            "pages_commodities": 0,
            "pages_markets": 0,
            "error": None,
        }
        try:
            commodities = self._fetch_commodities_result(country_iso3)
            markets = self._fetch_markets_result(country_iso3)
            prices = self._fetch_monthly_price_rows_result(
                country_iso3,
                start_date=start_date,
                end_date=end_date,
            )
            result.update(
                {
                    "rows_prices": len(prices.rows),
                    "rows_commodities": len(commodities.rows),
                    "rows_markets": len(markets.rows),
                    "pages_prices": prices.pages,
                    "pages_commodities": commodities.pages,
                    "pages_markets": markets.pages,
                }
            )
        except Exception as exc:
            result["error"] = _safe_error(exc)
        return result

    def _build_official_clients(self) -> None:
        module = self._client_module or import_module("data_bridges_client")
        rest_module = import_module("data_bridges_client.rest")
        token_module = import_module("data_bridges_client.token")
        api_exception = getattr(rest_module, "ApiException", None)
        if isinstance(api_exception, type):
            self._api_exception_type = (api_exception,)

        token_cls = self._token_factory or getattr(token_module, "WfpApiToken")
        self._token = _create_wfp_token(token_cls, self.config)
        access_token = self._refresh_token()
        configuration_cls = getattr(module, "Configuration")
        try:
            configuration = configuration_cls(host=self.config.base_url, access_token=access_token)
        except TypeError:
            configuration = configuration_cls(host=self.config.base_url)
            configuration.access_token = access_token
        for attr in ("timeout", "connection_pool_maxsize"):
            if hasattr(configuration, attr):
                try:
                    setattr(configuration, attr, self.config.request_timeout_seconds)
                except Exception:
                    pass
        self._configuration = configuration
        self._api_client = self._api_client or module.ApiClient(configuration)
        self._market_prices_api = self._market_prices_api or module.MarketPricesApi(self._api_client)
        self._commodities_api = self._commodities_api or module.CommoditiesApi(self._api_client)
        self._commodity_units_api = self._commodity_units_api or module.CommodityUnitsApi(self._api_client)
        self._markets_api = self._markets_api or module.MarketsApi(self._api_client)
        self._currency_api = self._currency_api or module.CurrencyApi(self._api_client)

    def _refresh_token(self, *, force: bool = False) -> str:
        if self._token is None:
            return ""
        refresh = getattr(self._token, "refresh")
        try:
            access_token = refresh(scopes=[self.config.scope], force=force)
        except TypeError:
            try:
                access_token = refresh(scopes=[self.config.scope])
            except TypeError:
                access_token = refresh()
        if not access_token:
            access_token = getattr(self._token, "access_token", None) or getattr(self._token, "token", None)
        if not access_token and hasattr(self._token, "get_token"):
            access_token = self._token.get_token()
        if not access_token:
            raise DataBridgesAdapterError("WfpApiToken did not return an access token.")
        if self._configuration is not None:
            self._configuration.access_token = access_token
        return str(access_token)


def normalize_monthly_price_row(row: Any, *, country_iso3: Optional[str] = None) -> dict[str, Any]:
    mapping = _to_mapping(row)
    resolved_country = str(
        _field(mapping, "countryIso3", "country_iso3", "countryCode", "country_code", default=country_iso3) or ""
    ).upper()
    commodity_id = _to_int(_field(mapping, "commodityId", "commodity_id", "commodityID"))
    market_id = _to_int(_field(mapping, "marketId", "market_id", "marketID"))
    price_date = _to_month_start(_field(mapping, "commodityPriceDate", "commodity_price_date", "priceDate", "price_date", "date"))
    price = _to_float(_field(mapping, "commodityPrice", "commodity_price", "price"))
    missing = []
    if not resolved_country:
        missing.append("country_iso3")
    if commodity_id is None:
        missing.append("commodity_id")
    if market_id is None:
        missing.append("market_id")
    if price_date is None:
        missing.append("price_date")
    if price is None:
        missing.append("price")
    if missing:
        raise DataBridgesNormalizationError(f"Monthly price row missing required fields: {', '.join(missing)}")

    normalized = {
        "country_iso3": resolved_country,
        "commodity_id": commodity_id,
        "commodity_name": _optional_str(_field(mapping, "commodityName", "commodity_name")),
        "market_id": market_id,
        "market_name": _optional_str(_field(mapping, "marketName", "market_name")),
        "admin1_name": _optional_str(_field(mapping, "admin1Name", "adm1Name", "admin1_name", "adm1_name")),
        "admin2_name": _optional_str(_field(mapping, "admin2Name", "adm2Name", "admin2_name", "adm2_name")),
        "price_date": price_date,
        "price": price,
        "currency_id": _to_int(_field(mapping, "currencyId", "currency_id")),
        "currency_code": _optional_str(_field(mapping, "currencyCode", "currency_code")),
        "currency_name": _optional_str(_field(mapping, "currencyName", "currency_name")),
        "commodity_unit_id": _to_int(_field(mapping, "commodityUnitId", "commodity_unit_id", "unitId", "unit_id")),
        "commodity_unit_name": _optional_str(
            _field(mapping, "commodityUnitName", "commodity_unit_name", "unitName", "unit")
        ),
        "price_type_id": _to_int(_field(mapping, "priceTypeId", "price_type_id")),
        "price_type_name": _optional_str(_field(mapping, "priceTypeName", "price_type_name")) or "",
        "price_flag": _optional_str(
            _field(mapping, "commodityPriceFlag", "commodity_price_flag", "priceFlag", "price_flag")
        )
        or "",
        "original_frequency": _optional_str(
            _field(mapping, "originalFrequency", "original_frequency", "commodityPriceFrequency", "frequency")
        ),
        "observations": _to_int(
            _field(mapping, "commodityPriceObservations", "commodity_price_observations", "observations")
        ),
        "data_source": _optional_str(
            _field(mapping, "commodityPriceSourceName", "commodity_price_source_name", "dataSource", "source")
        ),
    }
    normalized["source_payload_hash"] = _hash_payload(mapping)
    return normalized


def normalize_commodity_row(row: Any, *, country_iso3: str) -> dict[str, Any]:
    mapping = _to_mapping(row)
    commodity_id = _to_int(_field(mapping, "id", "commodityId", "commodity_id", "commodityID"))
    commodity_name = _optional_str(_field(mapping, "name", "commodityName", "commodity_name"))
    if commodity_id is None or not commodity_name:
        raise DataBridgesNormalizationError("Commodity row missing required commodity ID or name.")
    return {
        "country_iso3": country_iso3.upper(),
        "commodity_id": commodity_id,
        "commodity_name": commodity_name,
        "commodity_unit_id": _to_int(_field(mapping, "commodityUnitId", "commodity_unit_id", "unitId")),
        "commodity_unit_name": _optional_str(_field(mapping, "commodityUnitName", "commodity_unit_name", "unitName")),
        "category_name": _optional_str(_field(mapping, "categoryName", "category_name")),
        "active": _to_bool(_field(mapping, "active", "isActive", "is_active", default=True)),
    }


def normalize_unit_row(row: Any) -> dict[str, Any]:
    mapping = _to_mapping(row)
    unit_id = _to_int(_field(mapping, "commodityUnitId", "commodity_unit_id", "unitId", "id"))
    unit_name = _optional_str(_field(mapping, "commodityUnitName", "commodity_unit_name", "unitName", "name"))
    if unit_id is None or not unit_name:
        raise DataBridgesNormalizationError("Unit row missing required unit ID or name.")
    return {
        "commodity_unit_id": unit_id,
        "commodity_unit_name": unit_name,
        "conversion_to_kg_l": _to_float(
            _field(mapping, "conversionToKgL", "conversion_to_kg_l", "conversionFactor", "conversion_factor")
        ),
        "active": _to_bool(_field(mapping, "active", "isActive", "is_active", default=True)),
    }


def normalize_market_row(row: Any, *, country_iso3: str) -> dict[str, Any]:
    mapping = _to_mapping(row)
    market_id = _to_int(_field(mapping, "marketId", "market_id", "marketID", "id"))
    market_name = _optional_str(_field(mapping, "marketName", "market_name", "name"))
    if market_id is None or not market_name:
        raise DataBridgesNormalizationError("Market row missing required market ID or name.")
    return {
        "country_iso3": country_iso3.upper(),
        "market_id": market_id,
        "market_name": market_name,
        "admin1_name": _optional_str(_field(mapping, "admin1Name", "admin1_name", "adm1Name")),
        "admin2_name": _optional_str(_field(mapping, "admin2Name", "admin2_name", "adm2Name")),
        "latitude": _to_float(_field(mapping, "marketLatitude", "market_latitude", "latitude", "lat")),
        "longitude": _to_float(_field(mapping, "marketLongitude", "market_longitude", "longitude", "lon")),
        "active": _to_bool(_field(mapping, "active", "isActive", "is_active", default=True)),
    }


def normalize_currency_row(row: Any) -> dict[str, Any]:
    mapping = _to_mapping(row)
    currency_id = _to_int(_field(mapping, "currencyId", "currency_id", "id"))
    currency_name = _optional_str(_field(mapping, "currencyName", "currency_name", "name"))
    if currency_id is None or not currency_name:
        raise DataBridgesNormalizationError("Currency row missing required currency ID or name.")
    return {
        "currency_id": currency_id,
        "currency_code": _optional_str(_field(mapping, "currencyCode", "currency_code", "code")),
        "currency_name": currency_name,
    }


def normalize_exchange_rate_row(row: Any, *, country_iso3: Optional[str] = None) -> dict[str, Any]:
    mapping = _to_mapping(row)
    resolved_country = str(
        _field(mapping, "countryISO3", "countryIso3", "country_iso3", "countryCode", "country_code", default=country_iso3)
        or ""
    ).upper()
    rate_date = _to_date_value(_field(mapping, "date", "quotationDate", "quotation_date", "valueDate", "value_date"))
    value = _to_float(_field(mapping, "value", "exchangeRate", "exchange_rate", "rate"))
    missing = []
    if not resolved_country:
        missing.append("country_iso3")
    if rate_date is None:
        missing.append("date")
    if value is None:
        missing.append("value")
    if missing:
        raise DataBridgesNormalizationError(f"Exchange rate row missing required fields: {', '.join(missing)}")

    is_official_raw = _field(mapping, "isOfficial", "is_official", "official", default=True)
    return {
        "country_iso3": resolved_country,
        "currency_code": _optional_str(_field(mapping, "currencyCode", "currency_code", "name", "code")),
        "currency_name": _optional_str(_field(mapping, "currencyName", "currency_name")),
        "date": rate_date,
        "value": value,
        "is_official": _to_bool(is_official_raw),
        "frequency": _optional_str(_field(mapping, "frequency", "originalFrequency", "original_frequency")),
        "source_payload_hash": _hash_payload(mapping),
    }


def _default_country_codes() -> list[str]:
    return [str(item["iso3"]) for item in supported_country_options()]


def _create_wfp_token(token_cls: Callable[..., Any], config: DataBridgesAdapterConfig) -> Any:
    attempts = (
        lambda: token_cls(api_key=config.api_key, api_secret=config.api_secret),
        lambda: token_cls(client_id=config.api_key, client_secret=config.api_secret),
        lambda: token_cls(config.api_key, config.api_secret),
    )
    last_error: Optional[TypeError] = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
    raise last_error or TypeError("Could not initialize WfpApiToken.")


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value not in (None, "")}


def _payload_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    mapping = _to_mapping(payload)
    for key in ("items", "Items", "data", "Data", "value", "values"):
        value = mapping.get(key)
        if isinstance(value, list):
            return value
    return []


def _payload_total(payload: Any) -> Optional[int]:
    mapping = _to_mapping(payload)
    for key in ("totalItems", "total_items", "TotalItems", "total", "count", "totalCount"):
        value = mapping.get(key)
        parsed = _to_int(value)
        if parsed is not None:
            return parsed
    return None


def _to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        try:
            as_dict = value.to_dict()
            if isinstance(as_dict, dict):
                return as_dict
        except Exception:
            pass
    if hasattr(value, "model_dump"):
        try:
            as_dict = value.model_dump()
            if isinstance(as_dict, dict):
                return as_dict
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {key: val for key, val in vars(value).items() if not key.startswith("_")}
    return {}


def _field(mapping: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        key = name.lower()
        if key in lowered:
            return lowered[key]
    return default


def _date_wire(value: Optional[date | str]) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _to_month_start(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw[:10] if len(raw) >= 10 else raw)
    except ValueError:
        return None
    return date(parsed.year, parsed.month, 1)


def _to_date_value(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw[:10] if len(raw) >= 10 else raw)
    except ValueError:
        return None
    return parsed.date()


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


def _optional_str(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value).strip()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _hash_payload(mapping: dict[str, Any]) -> str:
    def default(value: Any) -> str:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    payload = json.dumps(mapping, sort_keys=True, separators=(",", ":"), default=default)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _env_first(values: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = values.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return default


def _int_from_env(
    values: Mapping[str, str],
    *names: str,
    default: int,
    minimum: int,
) -> int:
    raw = _env_first(values, *names)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"Expected integer for {names[0]}, got {raw!r}.") from exc
    if parsed < minimum:
        raise ValueError(f"Expected {names[0]} >= {minimum}, got {parsed}.")
    return parsed


def _float_from_env(
    values: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
) -> float:
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        parsed = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"Expected numeric value for {name}, got {raw!r}.") from exc
    if parsed < minimum:
        raise ValueError(f"Expected {name} >= {minimum}, got {parsed}.")
    return parsed


def _is_auth_error(exc: BaseException) -> bool:
    return _status_code(exc) == 401


def _is_transient_error(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status in RETRYABLE_STATUS_CODES:
        return True
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in exc.__class__.__name__.lower()


def _is_permanent_error(exc: BaseException) -> bool:
    status = _status_code(exc)
    return status in PERMANENT_STATUS_CODES


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        parsed = _to_int(value)
        if parsed is not None:
            return parsed
    response = getattr(exc, "response", None)
    if response is not None:
        parsed = _to_int(getattr(response, "status_code", None))
        if parsed is not None:
            return parsed
    return None


def _safe_error(exc: BaseException) -> str:
    message = str(exc)
    for secret_name in ("WFP_V2_API_SECRET", "DATA_BRIDGES_SECRET"):
        secret = os.getenv(secret_name)
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def _looks_permission_denied(message: str, exc: Optional[BaseException] = None) -> bool:
    status = _status_code(exc) if exc is not None else None
    if status == 403:
        return True
    text = str(message or "").lower()
    return "403" in text or "forbidden" in text or "permission" in text or "unauthorized" in text


def _format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "DataBridges full-cache dry run",
        f"Countries: {summary['countries_completed']} completed, {summary['countries_failed']} failed, {summary['countries_requested']} requested",
        f"Rows: prices={summary['rows_prices']}, commodities={summary['rows_commodities']}, markets={summary['rows_markets']}, units={summary['rows_units']}, currencies={summary['rows_currencies']}",
        f"Pages: prices={summary['pages_prices']}, commodities={summary['pages_commodities']}, markets={summary['pages_markets']}, units={summary['pages_units']}, currencies={summary['pages_currencies']}",
    ]
    if summary.get("errors"):
        lines.append("Errors:")
        for error in summary["errors"]:
            scope = error.get("country_iso3") or error.get("scope") or "unknown"
            lines.append(f"- {scope}: {error.get('error')}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DataBridges price cache ingestion utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry_run = subparsers.add_parser("dry-run-full", help="Fetch and normalize a full cache snapshot without DB writes.")
    dry_run.add_argument("--country", action="append", dest="countries", help="ISO3 country code. Repeatable.")
    dry_run.add_argument("--start-date", help="Optional price start date, YYYY-MM-DD.")
    dry_run.add_argument("--end-date", help="Optional price end date, YYYY-MM-DD.")
    dry_run.add_argument("--max-countries", type=int, help="Limit the number of countries for smoke testing.")
    dry_run.add_argument("--max-workers", type=int, help="Override DATA_BRIDGES_MAX_WORKERS.")
    dry_run.add_argument("--json", action="store_true", help="Print JSON summary.")

    args = parser.parse_args(argv)
    if args.command == "dry-run-full":
        adapter = DataBridgesClientAdapter()
        summary = adapter.dry_run_full_cache(
            countries=args.countries,
            start_date=args.start_date,
            end_date=args.end_date,
            max_countries=args.max_countries,
            max_workers=args.max_workers,
        )
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        else:
            print(_format_summary(summary))
        return 1 if summary.get("errors") else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
