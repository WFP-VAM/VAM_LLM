"""Lightweight requests-based connector for the WFP Databridges API."""
from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Sequence
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://gateway.api.wfp.org/vam-data-bridges/v2"
DEFAULT_TOKEN_URL = (
    "https://login.microsoftonline.com/462ad9ae-d7d9-4206-b874-71b1e079776f/"
    "oauth2/v2.0/token"
)
DEFAULT_SCOPE = "api://wfp-api-mediation-service/.default"
DEFAULT_ENV = "prod"


class DataBridgesAuth:
    """Client-credentials token helper with scope-aware caching."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        timeout: int = 60,
        max_retries: int = 3,
        session: Optional[requests.Session] = None,
    ) -> None:
        api_key = str(api_key or "").strip()
        api_secret = str(api_secret or "").strip()
        if not api_key or not api_secret:
            raise ValueError(
                "Databridges credentials are not configured. Set WFP_V2_API_KEY "
                "and WFP_V2_API_SECRET, or compatibility aliases DATA_BRIDGES_KEY "
                "and DATA_BRIDGES_SECRET."
            )
        if _looks_like_secret_reference(api_key):
            raise ValueError(
                "WFP_V2_API_KEY looks like a Secret Manager reference instead of "
                "the resolved Azure client ID. Configure Cloud Run with a secret "
                "binding (--update-secrets), not an environment value containing "
                "'<secret>:latest'."
            )
        try:
            uuid.UUID(api_key)
        except ValueError as exc:
            raise ValueError(
                "WFP_V2_API_KEY must resolve to the Azure application/client UUID. "
                "Check the Cloud Run secret binding."
            ) from exc
        if _looks_like_secret_reference(api_secret):
            raise ValueError(
                "WFP_V2_API_SECRET looks like a Secret Manager reference instead "
                "of the resolved client secret. Configure Cloud Run with a secret "
                "binding (--update-secrets)."
            )

        self.api_key = api_key
        self.api_secret = api_secret
        self.token_url = token_url
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.session = session or requests.Session()
        self._token_cache: dict[tuple[str, ...], dict[str, object]] = {}

    def get_token(self, scopes: Sequence[str]) -> str:
        scope_key = tuple(sorted(scope for scope in scopes if scope))
        now = time.time()
        cached = self._token_cache.get(scope_key)
        if cached and now < float(cached["expires_at"]):
            return str(cached["access_token"])

        payload = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "scope": " ".join(scope_key),
        }

        response: Optional[requests.Response] = None
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(
                    self.token_url,
                    data=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                break
            except requests.exceptions.Timeout as exc:
                last_error = TimeoutError(
                    "Timed out while requesting a Databridges access token "
                    f"for scopes {scope_key} after {self.timeout}s."
                )
                response = None
            except requests.exceptions.RequestException as exc:
                detail = _safe_token_error(response)
                suffix = f": {detail}" if detail else f": {exc}"
                if response is not None:
                    suffix = f" with HTTP {response.status_code}{suffix}"
                last_error = RuntimeError(
                    "Failed to request a Databridges access token "
                    f"for scopes {scope_key}{suffix}"
                )
                response = None

            if attempt < self.max_retries:
                time.sleep(min(attempt, 5))

        if response is None:
            raise last_error or RuntimeError("Unknown Databridges token error.")

        try:
            token_payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Databridges token endpoint returned non-JSON data.") from exc

        access_token = token_payload.get("access_token")
        if not access_token:
            detail = _format_safe_token_payload(token_payload)
            suffix = f": {detail}" if detail else "."
            raise RuntimeError(f"Databridges token response did not include access_token{suffix}")

        expires_in = int(token_payload.get("expires_in", 3600))
        granted_scopes = set(str(token_payload.get("scope", "")).split())
        if scope_key and granted_scopes and not set(scope_key).issubset(granted_scopes):
            raise ValueError(f"Could not acquire requested Databridges scopes: {scope_key}")

        self._token_cache[scope_key] = {
            "access_token": access_token,
            "expires_at": now + max(expires_in - 60, 1),
        }
        return str(access_token)


class DataBridgesClient:
    COMMODITIES_SCOPE = DEFAULT_SCOPE
    MARKETS_SCOPE = DEFAULT_SCOPE
    MONTHLY_PRICES_SCOPE = DEFAULT_SCOPE
    MFI_SURVEYS_SCOPE = DEFAULT_SCOPE
    MFI_PROCESSED_SCOPE = DEFAULT_SCOPE

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token_url: str = DEFAULT_TOKEN_URL,
        timeout: int = 60,
        max_retries: int = 3,
        env: Optional[str] = DEFAULT_ENV,
        scope: str = DEFAULT_SCOPE,
        session: Optional[requests.Session] = None,
        auth_provider: Optional[DataBridgesAuth] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.env = (env or "").strip() or None
        self.scope = (scope or "").strip() or DEFAULT_SCOPE
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "UNIFIED_APP/DatabridgesConnector"})
        self.auth_provider = auth_provider or DataBridgesAuth(
            api_key,
            api_secret,
            token_url=token_url,
            timeout=timeout,
            max_retries=max_retries,
            session=self.session,
        )

    def list_commodities(
        self,
        country_code: str,
        commodity_name: Optional[str] = None,
        commodity_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"countryCode": country_code, "format": "json"}
        if commodity_name:
            params["commodityName"] = commodity_name
        if commodity_id is not None:
            params["commodityId"] = commodity_id
        return self._paginate(
            "/Commodities/List",
            params=params,
            scopes=[self.scope],
        )

    def list_markets(self, country_code: str) -> list[dict[str, Any]]:
        return self._paginate(
            "/Markets/List",
            params={"countryCode": country_code, "format": "json"},
            scopes=[self.scope],
        )

    def list_monthly_prices(
        self,
        country_code: str,
        commodity_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        latest_value_only: bool = False,
        price_flag: Optional[str] = None,
        price_type_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "countryCode": country_code,
            "latestValueOnly": _wire_bool(latest_value_only),
            "format": "json",
        }
        if commodity_id is not None:
            params["commodityId"] = commodity_id
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if price_flag:
            params["priceFlag"] = price_flag
        if price_type_name:
            params["priceTypeName"] = price_type_name
        return self._paginate(
            "/MarketPrices/PriceMonthly",
            params=params,
            scopes=[self.scope],
        )

    def list_mfi_surveys(
        self,
        adm0_code: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if adm0_code is not None:
            params["adm0Code"] = adm0_code
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        return self._paginate(
            "/MFI/Surveys",
            params=params,
            scopes=[self.scope],
        )

    def list_mfi_processed_data(
        self,
        survey_id: int,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        params = {
            "surveyID": survey_id,
            "pageSize": page_size,
            "format": "json",
        }
        return self._paginate(
            "/MFI/Surveys/ProcessedData",
            params=params,
            scopes=[self.scope],
            page_size=page_size,
        )

    def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any],
        scopes: Sequence[str],
        page_size: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        first_total_items: Optional[int] = None

        while True:
            payload = self._get(path, params={**params, "page": page}, scopes=scopes)
            page_items = _payload_items(payload)
            if not page_items:
                break

            items.extend(page_items)
            total_items = _payload_total(payload)
            if first_total_items is None and total_items is not None:
                first_total_items = total_items
            if first_total_items is not None and len(items) >= first_total_items:
                break
            if page_size is not None and len(page_items) < page_size:
                break

            page += 1
            if page > 1000:
                raise RuntimeError(f"Databridges pagination runaway detected for {path}.")

        return items

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any],
        scopes: Sequence[str],
    ) -> Any:
        request_params = {key: value for key, value in params.items() if value is not None}
        if self.env:
            request_params["env"] = self.env

        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(
                url,
                params=request_params,
                headers=self._auth_headers(scopes),
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise TimeoutError(
                f"Timed out while requesting Databridges {path} page "
                f"{request_params.get('page')} after {self.timeout}s."
            ) from exc
        except requests.exceptions.HTTPError as exc:
            detail = getattr(response, "text", "")[:500]
            raise RuntimeError(
                f"Databridges request failed for {path} page "
                f"{request_params.get('page')} with HTTP {response.status_code}: {detail}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"Databridges request failed for {path} page "
                f"{request_params.get('page')}: {exc}"
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"Databridges {path} returned non-JSON data.") from exc

    def _auth_headers(self, scopes: Sequence[str]) -> dict[str, str]:
        access_token = self.auth_provider.get_token(scopes)
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }


def get_databridges_client() -> DataBridgesClient:
    global _CLIENT
    if _CLIENT is None:
        timeout = int(os.getenv("DATA_BRIDGES_TIMEOUT", "60"))
        max_retries = int(os.getenv("DATA_BRIDGES_MAX_RETRIES", "3"))
        _CLIENT = DataBridgesClient(
            _env_first("WFP_V2_API_KEY", "DATA_BRIDGES_KEY"),
            _env_first("WFP_V2_API_SECRET", "DATA_BRIDGES_SECRET"),
            base_url=_env_first("WFP_V2_API_BASE_URL", "DATA_BRIDGES_API_BASE_URL", default=DEFAULT_BASE_URL),
            token_url=_env_first("WFP_V2_TOKEN_URL", "DATA_BRIDGES_TOKEN_URL", default=DEFAULT_TOKEN_URL),
            timeout=timeout,
            max_retries=max_retries,
            env=_env_first("WFP_V2_API_ENV", "DATA_BRIDGES_ENV", default=DEFAULT_ENV),
            scope=_env_first("WFP_V2_API_SCOPE", "DATA_BRIDGES_SCOPE", default=DEFAULT_SCOPE),
        )
    return _CLIENT


def reset_databridges_client_for_tests() -> None:
    global _CLIENT
    _CLIENT = None


def _wire_bool(value: bool) -> str:
    return "true" if value else "false"


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _looks_like_secret_reference(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    return (
        ":latest" in lowered
        or lowered.startswith("projects/")
        or "/secrets/" in lowered
        or lowered.startswith("sm://")
    )


def _safe_token_error(response: Optional[requests.Response]) -> str:
    if response is None:
        return ""
    try:
        payload = response.json()
    except ValueError:
        text = getattr(response, "text", "")[:500]
        return text
    return _format_safe_token_payload(payload)


def _format_safe_token_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    safe: dict[str, Any] = {}
    for key in ("error", "error_description", "error_codes", "timestamp", "trace_id", "correlation_id"):
        if key in payload:
            safe[key] = payload[key]
    if not safe:
        return ""
    return "; ".join(f"{key}={value}" for key, value in safe.items())


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    raw = payload.get("items")
    if raw is None:
        raw = payload.get("Items")
    if raw is None:
        raw = payload.get("data")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _payload_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for key in ("totalItems", "total_items", "TotalItems", "total"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


_CLIENT: Optional[DataBridgesClient] = None
