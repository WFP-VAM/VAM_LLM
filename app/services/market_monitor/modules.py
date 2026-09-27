"""The optional report modules: exchange rate, fuel and energy, livestock and animal products, labour market."""
from __future__ import annotations

import logging
import requests
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

import pandas as pd

from .i18n import (
    format_decimal_value,
    format_month_label,
    format_percent_value,
    prompt_base_context,
    t,
)
from .prompts import (
    render_prompt,
    _json_for_prompt,
    _report_month_for_prompt,
)
from .state import _state_language
from .text import _plain_or_localized_number, _validated_prose, format_pct
from .basket_context import optional_module_basket_relevance
from .qa import _correction_flags_json

logger = logging.getLogger(__name__)


CURRENCY_SYMBOLS = {
    "SDG": "USDSDG:CUR",
    "MMK": "USDMMK:CUR",
    "YER": "USDYER:CUR",
    "SYP": "USDSYP:CUR",
    "AFN": "USDAFN:CUR",
    "ETB": "USDETB:CUR",
    "NGN": "USDNGN:CUR",
    "PKR": "USDPKR:CUR",
    "BDT": "USDBDT:CUR",
    "KES": "USDKES:CUR",
    "UGX": "USDUGX:CUR",
    "TZS": "USDTZS:CUR",
    "ZMW": "USDZMW:CUR",
    "MWK": "USDMWK:CUR",
    "HTG": "USDHTG:CUR",
    "CDF": "USDCDF:CUR",
    "SOS": "USDSOS:CUR",
    "SSP": "USDSSP:CUR",
}


class ReportModule(ABC):
    """Interfaccia base per moduli opzionali del report."""
    
    @property
    @abstractmethod
    def module_id(self) -> str:
        pass
    
    @property
    @abstractmethod
    def display_name(self) -> str:
        pass
    
    @property
    @abstractmethod
    def required_inputs(self) -> List[str]:
        pass
    
    def validate_inputs(self, state: dict) -> bool:
        missing = [f for f in self.required_inputs if f not in state or state[f] is None]
        if missing:
            logger.warning(f"Module '{self.module_id}' missing inputs: {missing}")
            return False
        return True
    
    @abstractmethod
    def fetch_data(self, state: dict) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    def generate_section(self, state: dict, llm) -> Dict[str, Any]:
        pass


class ExchangeRateModule(ReportModule):
    """Narrative module over DataBridges FX, with TradingEconomics fallback."""
    
    TE_API_BASE = "https://api.tradingeconomics.com"
    
    def __init__(self, api_key: Optional[str] = None):
        import os
        self.api_key = api_key or os.getenv("TE_API_KEY")
    
    @property
    def module_id(self) -> str:
        return "exchange_rate"
    
    @property
    def display_name(self) -> str:
        return "Exchange Rate Analysis"
    
    @property
    def required_inputs(self) -> List[str]:
        return ["currency_code", "country", "time_period"]
    
    def _get_symbol(self, currency_code: str) -> str:
        return CURRENCY_SYMBOLS.get(currency_code, f"USD{currency_code}:CUR")
    
    def _generate_mock_data(self, currency_code: str) -> Dict[str, Any]:
        raise RuntimeError("Mock exchange rate data generation is not allowed")

    def _fetch_historical_series(self, symbol: str, d1: str, d2: str) -> pd.DataFrame:
        url = f"{self.TE_API_BASE}/markets/historical/{symbol}"
        params = {"c": self.api_key, "d1": d1, "d2": d2, "f": "json"}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()

        payload = resp.json()
        if not isinstance(payload, list) or not payload:
            raise RuntimeError(f"TradingEconomics returned no historical data for symbol '{symbol}'")

        df = pd.DataFrame(payload)
        if "Date" not in df.columns or "Close" not in df.columns:
            raise RuntimeError("TradingEconomics response missing required fields 'Date' and/or 'Close'")

        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).sort_values("Date")
        if df.empty:
            raise RuntimeError(f"TradingEconomics returned only invalid rows for symbol '{symbol}'")

        df = df.set_index("Date")
        return df[["Close"]]

    def _pct_change(self, current: float, previous: Optional[float]) -> Optional[float]:
        if previous is None:
            return None
        try:
            prev = float(previous)
            curr = float(current)
        except Exception:
            return None
        if prev == 0:
            return None
        return (curr - prev) / prev * 100.0
    
    def fetch_data(self, state: dict) -> Dict[str, Any]:
        """Use DataBridges data if present, otherwise TradingEconomics fallback."""
        existing = state.get("exchange_rate_data") or {}
        if existing.get("current_rate") is not None:
            return {"exchange_rate_data": existing}
        if not self.api_key:
            raise RuntimeError("DataBridges exchange-rate data is unavailable and TE_API_KEY is not configured for fallback")

        logger.info(f"[ExchangeRateModule] Falling back to TradingEconomics for {state['currency_code']}")
        
        currency_code = state["currency_code"]
        symbol = self._get_symbol(currency_code)

        try:
            period_start = pd.to_datetime(state["time_period"] + "-01")
        except Exception as e:
            raise ValueError(f"Invalid time_period: {state.get('time_period')}") from e

        period_end = (period_start + pd.offsets.MonthEnd(0)).normalize()
        end_dt = period_end.to_pydatetime()

        d2 = end_dt.strftime("%Y-%m-%d")
        d1 = (period_end - pd.Timedelta(days=400)).strftime("%Y-%m-%d")

        df = self._fetch_historical_series(symbol=symbol, d1=d1, d2=d2)
        df_upto_end = df.loc[:period_end]
        if df_upto_end.empty:
            raise RuntimeError(f"No exchange rate data for {symbol} up to {d2}")

        current_close = float(df_upto_end.iloc[-1]["Close"])

        def close_on_or_before(ts: pd.Timestamp) -> Optional[float]:
            sub = df_upto_end.loc[:ts]
            if sub.empty:
                return None
            return float(sub.iloc[-1]["Close"])

        prev_day = close_on_or_before(period_end - pd.Timedelta(days=1))
        prev_week = close_on_or_before(period_end - pd.Timedelta(days=7))
        prev_month = close_on_or_before(period_end - pd.DateOffset(months=1))
        prev_year = close_on_or_before(period_end - pd.DateOffset(years=1))

        daily_change_pct = self._pct_change(current_close, prev_day)
        weekly_change_pct = self._pct_change(current_close, prev_week)
        monthly_change_pct = self._pct_change(current_close, prev_month)
        yearly_change_pct = self._pct_change(current_close, prev_year)

        yoy_for_trend = yearly_change_pct if yearly_change_pct is not None else 0.0
        if yoy_for_trend > 30:
            trend = "rapid_depreciation"
        elif yoy_for_trend > 10:
            trend = "depreciation"
        elif yoy_for_trend < -10:
            trend = "appreciation"
        else:
            trend = "stable"

        data = {
            "symbol": symbol,
            "currency_code": currency_code,
            "current_rate": round(current_close, 6),
            "unit": f"{currency_code} per 1 USD",
            "quotation": "local_currency_per_usd",
            "higher_value_indicates": "local_currency_depreciation",
            "daily_change_pct": None if daily_change_pct is None else round(daily_change_pct, 2),
            "weekly_change_pct": None if weekly_change_pct is None else round(weekly_change_pct, 2),
            "monthly_change_pct": None if monthly_change_pct is None else round(monthly_change_pct, 2),
            "yearly_change_pct": None if yearly_change_pct is None else round(yearly_change_pct, 2),
            "trend": trend,
            "last_update": end_dt.isoformat(),
            "historical_data_json": df.to_json(date_format='iso'),
            "is_mock": False,
            "source": "TradingEconomics fallback",
        }

        return {"exchange_rate_data": data}
    
    def generate_section(self, state: dict, llm) -> Dict[str, Any]:
        """Genera la sezione narrativa."""
        exchange_data = state.get("exchange_rate_data", {})
        language = _state_language(state)
        
        if not exchange_data or exchange_data.get("current_rate") is None:
            raise RuntimeError("Exchange rate data is unavailable (no mock fallback is permitted)")
        
        prompt_context = {
            **prompt_base_context(language),
            "country": state.get("country", "Unknown"),
            "currency_code": exchange_data.get("currency_code", "LCU"),
            "current_rate": exchange_data.get("current_rate", "N/A"),
            "current_rate_localized": format_decimal_value(exchange_data.get("current_rate"), language, decimals=1),
            "unit": exchange_data.get("unit") or "local currency per 1 USD",
            "monthly_change_pct": exchange_data.get("monthly_change_pct", "N/A"),
            "monthly_change_pct_localized": format_percent_value(exchange_data.get("monthly_change_pct"), language),
            "yearly_change_pct": exchange_data.get("yearly_change_pct", "N/A"),
            "yearly_change_pct_localized": format_percent_value(exchange_data.get("yearly_change_pct"), language),
            "trend": exchange_data.get("trend", "unknown"),
            "basket_relevance_json": _json_for_prompt(optional_module_basket_relevance(state, self.module_id)),
            "correction_flags_json": _correction_flags_json(state, "EXCHANGE_RATE_ANALYSIS"),
        }
        prompt = render_prompt("exchange_rate", language, prompt_context)
        
        traced = llm.generate_text(
            prompt=prompt,
            node="module_orchestrator",
            operation="market_monitor.exchange_rate_module.v1",
            artifact_type="module",
            artifact_id=self.module_id,
            correction_attempt=int(state.get("correction_attempts", 0) or 0),
            validator=lambda text: _validated_prose(text, state),
        )
        narrative = traced.value
        
        return {
            "section_title": t(language, "module.exchange_rate"),
            "narrative": narrative,
            "key_metrics": {
                "current_rate": exchange_data.get("current_rate"),
                "mom_change_pct": exchange_data.get("monthly_change_pct"),
                "yoy_change_pct": exchange_data.get("yearly_change_pct"),
                "trend": exchange_data.get("trend"),
            }
        }


class FuelEnergyModule(ReportModule):
    """Narrative module over resolved transport fuel retail prices."""

    @property
    def module_id(self) -> str:
        return "fuel_energy"

    @property
    def display_name(self) -> str:
        return "Fuel & Energy"

    @property
    def required_inputs(self) -> List[str]:
        return ["country", "time_period", "fuel_energy_data"]

    def fetch_data(self, state: dict) -> Dict[str, Any]:
        existing = state.get("fuel_energy_data") or {}
        if existing.get("available") and existing.get("series"):
            return {"fuel_energy_data": existing}
        raise RuntimeError("resolved transport fuel price data is unavailable")

    def generate_section(self, state: dict, llm) -> Dict[str, Any]:
        fuel_data = state.get("fuel_energy_data") or {}
        series = fuel_data.get("series") or []
        language = _state_language(state)
        if not fuel_data.get("available") or not series:
            raise RuntimeError("Fuel & Energy data is unavailable")

        prompt = render_prompt(
            "fuel_energy",
            language,
            {
                **prompt_base_context(language),
                "country": state.get("country", "Unknown"),
                "time_period": state.get("time_period", "Unknown"),
                "report_month_localized": _report_month_for_prompt(state),
                "fuel_data_json": _json_for_prompt(fuel_data),
                "trend_analysis_json": _json_for_prompt(state.get("trend_analysis") or {}),
                "basket_relevance_json": _json_for_prompt(optional_module_basket_relevance(state, self.module_id)),
                "correction_flags_json": _correction_flags_json(state, "FUEL_ENERGY_ANALYSIS"),
            },
        )

        traced = llm.generate_text(
            prompt=prompt,
            node="module_orchestrator",
            operation="market_monitor.fuel_energy_module.v1",
            artifact_type="module",
            artifact_id=self.module_id,
            correction_attempt=int(state.get("correction_attempts", 0) or 0),
            validator=lambda text: _validated_prose(text, state),
        )
        narrative = traced.value

        return {
            "section_title": t(language, "module.fuel_energy"),
            "narrative": str(narrative).strip(),
            "key_metrics": {
                "series": series,
                "latest_month": fuel_data.get("latest_month"),
                "unit": fuel_data.get("unit"),
            },
        }

    def _fallback_narrative(self, state: dict, fuel_data: dict[str, Any]) -> str:
        language = _state_language(state)
        series = fuel_data.get("series") or []
        unit = fuel_data.get("unit") or "LCU/Litre"
        by_kind = {item.get("kind"): item for item in series if isinstance(item, dict)}
        diesel = by_kind.get("diesel") or series[0]
        petrol = by_kind.get("petrol_gasoline")

        def metric_sentence(item: dict[str, Any]) -> str:
            label = str(item.get("label") or "Fuel").lower()
            current = _plain_or_localized_number(item.get("current_price"), language, decimals=1)
            month = format_month_label(item.get("latest_month"), language) if language != "en" else item.get("latest_month")
            mom = format_pct(item.get("mom_change_pct"), language)
            yoy_raw = item.get("yoy_change_pct")
            yoy = "" if yoy_raw is None else t(language, "fallback.yoy", value=format_pct(yoy_raw, language))
            return t(
                language,
                "fallback.fuel.metric",
                label=label,
                current=current,
                unit=unit,
                month=month,
                mom=mom,
                yoy=yoy,
            )

        first = metric_sentence(diesel)
        if petrol:
            first = f"{first}; {metric_sentence(petrol)}."
        else:
            first = f"{first}."
        driver = fuel_data.get("driver_hint") or (
            t(language, "fallback.fuel.driver")
        )
        implication = t(language, "fallback.fuel.implication")
        regional = ""
        disparities = fuel_data.get("regional_disparities") or []
        if disparities:
            item = disparities[0]
            regional = t(
                language,
                "fallback.fuel.regional",
                highest=item.get("highest_region"),
                lowest=item.get("lowest_region"),
                label=str(item.get("label") or "fuel").lower(),
            )
        return f"{first} {driver} {implication}{regional}"


class LivestockAnimalProductsModule(ReportModule):
    """Narrative module over resolved livestock and animal-source food prices."""

    @property
    def module_id(self) -> str:
        return "livestock_animal_products"

    @property
    def display_name(self) -> str:
        return "Livestock & Animal Products"

    @property
    def required_inputs(self) -> List[str]:
        return ["country", "time_period", "livestock_animal_products_data"]

    def fetch_data(self, state: dict) -> Dict[str, Any]:
        existing = state.get("livestock_animal_products_data") or {}
        if existing.get("available") and existing.get("series"):
            return {"livestock_animal_products_data": existing}
        raise RuntimeError("resolved livestock and animal product price data is unavailable")

    def generate_section(self, state: dict, llm) -> Dict[str, Any]:
        data = state.get("livestock_animal_products_data") or {}
        series = data.get("series") or []
        language = _state_language(state)
        if not data.get("available") or not series:
            raise RuntimeError("Livestock & Animal Products data is unavailable")

        prompt = render_prompt(
            "livestock_animal_products",
            language,
            {
                **prompt_base_context(language),
                "country": state.get("country", "Unknown"),
                "time_period": state.get("time_period", "Unknown"),
                "report_month_localized": _report_month_for_prompt(state),
                "livestock_data_json": _json_for_prompt(data),
                "trend_analysis_json": _json_for_prompt(state.get("trend_analysis") or {}),
                "basket_relevance_json": _json_for_prompt(optional_module_basket_relevance(state, self.module_id)),
                "correction_flags_json": _correction_flags_json(state, "LIVESTOCK_ANIMAL_PRODUCTS_ANALYSIS"),
            },
        )

        traced = llm.generate_text(
            prompt=prompt,
            node="module_orchestrator",
            operation="market_monitor.livestock_module.v1",
            artifact_type="module",
            artifact_id=self.module_id,
            correction_attempt=int(state.get("correction_attempts", 0) or 0),
            validator=lambda text: _validated_prose(text, state),
        )
        narrative = traced.value

        return {
            "section_title": t(language, "module.livestock_animal_products"),
            "narrative": str(narrative).strip(),
            "key_metrics": {
                "series": series,
                "latest_month": data.get("latest_month"),
                "chart": data.get("chart"),
            },
        }

    def _fallback_narrative(self, state: dict, data: dict[str, Any]) -> str:
        language = _state_language(state)
        series = [item for item in data.get("series") or [] if isinstance(item, dict)]
        parts = []
        for item in series[:4]:
            yoy = (
                ""
                if item.get("yoy_change_pct") is None
                else t(language, "fallback.yoy", value=format_pct(item.get("yoy_change_pct"), language))
            )
            parts.append(
                t(
                    language,
                    "fallback.livestock.metric",
                    label=item.get("label"),
                    current=_plain_or_localized_number(item.get("current_price"), language, decimals=1),
                    unit=item.get("axis_unit"),
                    month=(
                        format_month_label(item.get("latest_month"), language)
                        if language != "en"
                        else item.get("latest_month")
                    ),
                    mom=format_pct(item.get("mom_change_pct"), language),
                    yoy=yoy,
                )
            )
        first = "; ".join(parts).rstrip() + "."
        driver = data.get("driver_hint") or (
            t(language, "fallback.livestock.driver")
        )
        implication = t(language, "fallback.livestock.implication")
        if any(item.get("group") == "live_animal" for item in series):
            implication += t(language, "fallback.livestock.live_animal")
        regional = ""
        disparities = data.get("regional_disparities") or []
        if disparities:
            item = disparities[0]
            regional = t(
                language,
                "fallback.livestock.regional",
                highest=item.get("highest_region"),
                lowest=item.get("lowest_region"),
                label=str(item.get("label") or "animal products").lower(),
            )
        return f"{first} {driver} {implication}{regional}"


class LabourMarketModule(ReportModule):
    """Narrative module over daily wages and food purchasing power."""

    @property
    def module_id(self) -> str:
        return "labour_market"

    @property
    def display_name(self) -> str:
        return "Labour Market"

    @property
    def required_inputs(self) -> List[str]:
        return ["country", "time_period", "labour_market_data"]

    def fetch_data(self, state: dict) -> Dict[str, Any]:
        existing = state.get("labour_market_data") or {}
        if existing.get("available") and existing.get("series"):
            return {"labour_market_data": existing}
        raise RuntimeError("resolved labour market data is unavailable")

    def generate_section(self, state: dict, llm) -> Dict[str, Any]:
        data = state.get("labour_market_data") or {}
        series = data.get("series") or []
        language = _state_language(state)
        if not data.get("available") or not series:
            raise RuntimeError("Labour Market data is unavailable")

        prompt = render_prompt(
            "labour_market",
            language,
            {
                **prompt_base_context(language),
                "country": state.get("country", "Unknown"),
                "time_period": state.get("time_period", "Unknown"),
                "report_month_localized": _report_month_for_prompt(state),
                "labour_data_json": _json_for_prompt(data),
                "trend_analysis_json": _json_for_prompt(state.get("trend_analysis") or {}),
                "basket_relevance_json": _json_for_prompt(optional_module_basket_relevance(state, self.module_id)),
                "correction_flags_json": _correction_flags_json(state, "LABOUR_MARKET_ANALYSIS"),
            },
        )

        traced = llm.generate_text(
            prompt=prompt,
            node="module_orchestrator",
            operation="market_monitor.labour_module.v1",
            artifact_type="module",
            artifact_id=self.module_id,
            correction_attempt=int(state.get("correction_attempts", 0) or 0),
            validator=lambda text: _validated_prose(text, state),
        )
        narrative = traced.value

        return {
            "section_title": t(language, "module.labour_market"),
            "narrative": str(narrative).strip(),
            "key_metrics": {
                "series": series,
                "purchasing_power": data.get("purchasing_power"),
                "latest_month": data.get("latest_month"),
            },
        }

    def _fallback_narrative(self, state: dict, data: dict[str, Any]) -> str:
        language = _state_language(state)
        series = [item for item in data.get("series") or [] if isinstance(item, dict)]
        primary = next((item for item in series if item.get("kind") == "casual_unskilled"), series[0])
        yoy = (
            ""
            if primary.get("yoy_change_pct") is None
            else t(language, "fallback.yoy", value=format_pct(primary.get("yoy_change_pct"), language))
        )
        first = t(
            language,
            "fallback.labour.metric",
            label=primary.get("label"),
            current=_plain_or_localized_number(primary.get("current_wage"), language, decimals=1),
            unit=primary.get("axis_unit"),
            month=(
                format_month_label(primary.get("latest_month"), language)
                if language != "en"
                else primary.get("latest_month")
            ),
            mom=format_pct(primary.get("mom_change_pct"), language),
            yoy=yoy,
        )
        skilled = next((item for item in series if item is not primary and item.get("kind") == "skilled_qualified"), None)
        if skilled:
            first = first + t(
                language,
                "fallback.labour.skilled",
                label=skilled.get("label"),
                current=_plain_or_localized_number(skilled.get("current_wage"), language, decimals=1),
                unit=skilled.get("axis_unit"),
            )
        availability = ""
        if data.get("availability"):
            availability = t(language, "fallback.labour.availability", availability=data.get("availability"))
        pp = data.get("purchasing_power")
        purchasing = ""
        if isinstance(pp, dict):
            purchasing = t(
                language,
                "fallback.labour.purchasing_power",
                kg=_plain_or_localized_number(pp.get("current_kg"), language, decimals=2),
                staple=pp.get("staple_name"),
                month=(
                    format_month_label(pp.get("latest_month"), language)
                    if language != "en"
                    else pp.get("latest_month")
                ),
                mom=format_pct(pp.get("mom_change_pct"), language),
            )
        driver = data.get("driver_hint") or (
            t(language, "fallback.labour.driver")
        )
        return f"{first}{availability} {purchasing} {driver}".strip()


# Registry moduli disponibili
AVAILABLE_MODULES: Dict[str, type] = {
    "exchange_rate": ExchangeRateModule,
    "fuel_energy": FuelEnergyModule,
    "livestock_animal_products": LivestockAnimalProductsModule,
    "labour_market": LabourMarketModule,
}
