"""
Market Monitor - Graph
======================
Workflow LangGraph per generazione Market Monitor Reports.

Struttura del grafo:
    data_agent → graph_designer → news_retrieval → event_mapper 
    → trend_analyst → module_orchestrator → highlights_drafter 
    → narrative_drafter → red_team → [loop/END]
"""
from __future__ import annotations

import io
import re
import json
import base64
import random
import logging
from datetime import datetime, timedelta
from typing import Literal, List, Dict, Any, Optional, Callable, Mapping

import pandas as pd
import numpy as np

from langgraph.graph import StateGraph, END

from app.shared.llm import (
    LLMCallError,
    log_llm_run_summary,
    tracing_run,
)
from app.shared.context.news import gather_context
from app.shared.context.retrievers import ReliefWebRetriever, SeeristRetriever

from .data_loader import (
    calculate_statistics_from_csv,
    resolve_report_price_data,
)
from .basket_calculation import BasketCalculationSpec
from .food_basket import get_active_basket_for_report
from .i18n import (
    format_currency_value,
    format_decimal_value,
    format_month_label,
    localize_axis_label,
    prompt_base_context,
    resolve_report_language,
    t,
)
from .prompts import (
    render_prompt,
    TERMINOLOGY_THRESHOLDS,
    _json_for_prompt,
    _report_month_for_prompt,
    event_extraction_prompt,
    trend_analysis_prompt,
)
from .state import MarketReportState, _state_currency_code, _state_language, create_initial_state
from .runtime import llm_client
from .text import _dedupe_text, _normalize_output_text, format_pct
from .basket_context import _mapping, build_basket_context, optional_module_basket_relevance
from .qa import (
    QA_MODULE_SECTIONS,
    _correction_flags_json,
    _correction_targets,
    _material_qa_flags,
    _normalized_qa_flags,
    _targeted,
    normalize_qa_review,
    qa_review_from_state,
)
from .modules import AVAILABLE_MODULES

logger = logging.getLogger(__name__)

OnStepCallback = Callable[[str, Dict[str, Any]], None]


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def _is_auxiliary_series(name: str) -> bool:
    s = str(name or "").strip().lower()
    if not s:
        return False
    patterns = [
        "exchange",
        "fx",
        "fuel",
        "petrol",
        "diesel",
        "gasoline",
        "wage",
        "salary",
        "labour",
        "labor",
        "animal products -",
        "livestock -",
        "purchasing power",
        "milling",
        "transport",
        "freight",
    ]
    return any(p in s for p in patterns)


def _categorize_commodity(name: str) -> str:
    s = str(name or "").strip().lower()
    if not s:
        return "Other"

    if any(x in s for x in ["sorghum", "maize", "wheat", "rice", "millet", "bread", "teff", "barley", "flour"]):
        return "Cereals"
    if any(x in s for x in ["beans", "lentil", "pea", "chickpea", "pulse", "cowpea", "groundnut"]):
        return "Pulses"
    if "oil" in s:
        return "Oil"
    if "sugar" in s:
        return "Sugar"
    if "salt" in s:
        return "Condiments"
    if any(x in s for x in ["cabbage", "tomato", "onion", "vegetable", "leaves", "sukuma", "pumpkin", "cassava", "okra", "spinach"]):
        return "Vegetables"
    if "livestock" in s or any(x in s for x in ["goat", "sheep", "cattle", "chicken", "camel", "beef", "mutton"]):
        return "Livestock"

    return "Other"


def _slugify(text: str) -> str:
    s = str(text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "other"


def _chunk_list(items: List[str], size: int) -> List[List[str]]:
    if size <= 0:
        return [list(items)]
    out: List[List[str]] = []
    for i in range(0, len(items), size):
        out.append(items[i : i + size])
    return out


def _commodity_importance_score(stats: Dict[str, Any], commodity: str) -> float:
    if not isinstance(stats, dict):
        return 0.0
    comm = stats.get("commodities") or {}
    if not isinstance(comm, dict):
        return 0.0
    s = comm.get(commodity) or {}
    if not isinstance(s, dict):
        return 0.0

    yoy = s.get("yoy_change_pct")
    mom = s.get("mom_change_pct")
    try:
        if yoy is not None:
            return abs(float(yoy))
    except Exception:
        pass
    try:
        if mom is not None:
            return abs(float(mom))
    except Exception:
        pass
    return 0.0


def _trend_with_basket_identity(value: Any, basket_context: Mapping[str, Any]) -> Dict[str, Any]:
    trend = _mapping(value)
    generated = _mapping(trend.get("basket_analysis"))
    basket_analysis: Dict[str, Any] = {"primary": None, "secondary": None}
    for role in ("primary", "secondary"):
        role_context = _mapping(basket_context.get(role))
        if not role_context:
            continue
        raw = _mapping(generated.get(role))
        basket_analysis[role] = {
            "role": role,
            "basket_name": role_context.get("basket_name"),
            "scope_type": role_context.get("scope_type"),
            "scope_label": role_context.get("scope_label"),
            "trajectory": str(raw.get("trajectory") or "unknown"),
            "movement_observations": [str(item) for item in (raw.get("movement_observations") or [])],
            "cost_composition_observations": [
                str(item) for item in (raw.get("cost_composition_observations") or [])
            ],
        }
    trend["basket_analysis"] = basket_analysis
    return trend


def _currency_axis_label(label: str, currency_code: str, language: str = "en") -> str:
    label_key = f"chart.axis.{str(label or '').strip().lower()}"
    try:
        localized_label = t(language, label_key)
    except KeyError:
        localized_label = str(label or "")
    return t(language, "chart.axis.currency", label=localized_label, currency=currency_code or "LCU")


def _fx_axis_label(currency_code: str, language: str = "en") -> str:
    code = str(currency_code or "LCU").strip().upper() or "LCU"
    return t(language, "chart.axis.fx", currency=code)


def _fuel_axis_label(currency_code: str, language: str = "en") -> str:
    code = str(currency_code or "LCU").strip().upper() or "LCU"
    return t(language, "chart.axis.fuel", currency=code)


def _animal_axis_label(data: Dict[str, Any], currency_code: str, language: str = "en") -> str:
    chart = data.get("chart") or {}
    axis = chart.get("axis_label")
    if axis:
        return localize_axis_label(axis, language)
    code = str(currency_code or "LCU").strip().upper() or "LCU"
    unit = chart.get("unit") or "unit"
    return t(language, "chart.axis.unit", currency=code, unit=unit)


def _labour_axis_label(data: Dict[str, Any], currency_code: str, language: str = "en") -> str:
    chart = data.get("chart") or {}
    axis = chart.get("axis_label")
    if axis:
        return localize_axis_label(axis, language)
    code = str(currency_code or "LCU").strip().upper() or "LCU"
    return t(language, "chart.axis.day", currency=code)


def _localized_category_name(category: str, language: str) -> str:
    key = f"commodity_category.{_slugify(category)}"
    try:
        return t(language, key)
    except KeyError:
        return str(category)


def _localized_page_suffix(category: str, page_idx: int, page_count: int, language: str) -> str:
    localized = _localized_category_name(category, language)
    if page_count <= 1:
        return localized
    return t(language, "chart.page_suffix", category=localized, page_idx=page_idx, page_count=page_count)


def _set_localized_numeric_axis(ax: Any, language: str) -> None:
    try:
        import matplotlib.ticker as mticker

        ax.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda value, _pos: format_decimal_value(value, language, decimals=0))
        )
    except Exception:
        return


def _set_localized_month_axis(ax: Any, language: str) -> None:
    try:
        import matplotlib.dates as mdates
        import matplotlib.ticker as mticker

        ax.xaxis.set_major_formatter(
            mticker.FuncFormatter(
                lambda value, _pos: format_month_label(mdates.num2date(value), language, width="abbrev")
            )
        )
    except Exception:
        return


def _normalise_time_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    out = out[out.index.notna()]
    return out.sort_index()


def _index_to_first_observation(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    valid = values.dropna()
    if valid.empty:
        return values
    base = float(valid.iloc[0])
    if base == 0:
        return values * np.nan
    return (values / base * 100.0).round(2)


def _history_overlay_values(
    history: pd.DataFrame,
    target_index: pd.DatetimeIndex,
    column: str,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    empty = pd.Series(index=target_index, dtype=float)
    empty_count = pd.Series(index=target_index, dtype=int)
    if history is None or history.empty or column not in history.columns:
        return empty, empty, empty, empty, empty_count
    hist = _normalise_time_index(history)
    if hist.empty:
        return empty, empty, empty, empty, empty_count
    values = pd.to_numeric(hist[column], errors="coerce")
    prior_values = []
    five_year_mean = []
    five_year_low = []
    five_year_high = []
    five_year_counts = []
    for ts in target_index:
        current_ts = pd.Timestamp(ts)
        prior_values.append(values.get(current_ts - pd.DateOffset(years=1), np.nan))
        window_start = current_ts - pd.DateOffset(years=5)
        candidates = values[
            (values.index < current_ts)
            & (values.index >= window_start)
            & (values.index.month == current_ts.month)
        ].dropna()
        five_year_counts.append(int(len(candidates)))
        if len(candidates) < 5:
            five_year_mean.append(np.nan)
            five_year_low.append(np.nan)
            five_year_high.append(np.nan)
        else:
            five_year_mean.append(float(candidates.mean()))
            five_year_low.append(float(candidates.min()))
            five_year_high.append(float(candidates.max()))
    return (
        pd.Series(prior_values, index=target_index, dtype=float),
        pd.Series(five_year_mean, index=target_index, dtype=float),
        pd.Series(five_year_low, index=target_index, dtype=float),
        pd.Series(five_year_high, index=target_index, dtype=float),
        pd.Series(five_year_counts, index=target_index, dtype=int),
    )


def _plot_history_overlays(
    ax: Any,
    history: pd.DataFrame,
    target_index: pd.DatetimeIndex,
    column: str,
    *,
    color: str,
    label_prefix: str = "",
) -> None:
    prior, five_year_mean, five_year_low, five_year_high, _counts = _history_overlay_values(
        history,
        target_index,
        column,
    )
    prefix = f"{label_prefix} " if label_prefix else ""
    if prior.notna().any():
        ax.plot(target_index, prior, linestyle="--", linewidth=1.5, color=color, alpha=0.65, label=f"{prefix}prior year")
    if five_year_mean.notna().any():
        ax.plot(target_index, five_year_mean, linestyle=":", linewidth=1.5, color=color, alpha=0.75, label=f"{prefix}5-year avg")
    if five_year_low.notna().any() and five_year_high.notna().any():
        ax.fill_between(
            target_index,
            five_year_low.to_numpy(dtype=float),
            five_year_high.to_numpy(dtype=float),
            color=color,
            alpha=0.08,
            label=f"{prefix}5-year range",
        )


def _has_currency_depreciation_driver(drivers: Any) -> bool:
    if not drivers:
        return False
    try:
        for d in drivers:
            s = str(d or "").lower()
            if "currency" in s and (
                "depreciat" in s
                or "devalu" in s
                or "weak" in s
                or "collapse" in s
            ):
                return True
            if "fx" in s and "depreciat" in s:
                return True
    except Exception:
        return False
    return False


# NODE: DATA AGENT
# ============================================================================

def generate_mock_time_series(
    country: str, 
    time_period: str, 
    commodities: List[str], 
    admin1s: List[str]
) -> tuple:
    """Genera serie temporali mock (13 mesi)."""
    try:
        end_date = pd.to_datetime(time_period + "-01")
    except:
        end_date = pd.to_datetime("2025-01-01")
    
    dates = pd.date_range(end=end_date, periods=13, freq='MS')
    
    # National data
    national_data = []
    base_prices = {c: random.uniform(500, 2000) for c in commodities}
    base_prices["FoodBasket"] = random.uniform(10000, 30000)
    base_prices["ExchangeRate"] = random.uniform(1000, 5000)
    base_prices["FuelPrice"] = random.uniform(50, 500)
    
    for date in dates:
        row = {"Date": date}
        for item, base in base_prices.items():
            trend = (date.to_julian_date() - dates[0].to_julian_date()) / 365 * 0.15
            seasonality = np.sin((date.month - 3) * np.pi / 6) * 0.1
            shock = random.uniform(0.05, 0.20) if random.random() > 0.9 else 0
            price = base * (1 + trend + seasonality + shock)
            row[item] = round(price, 2)
        national_data.append(row)
    
    df_national = pd.DataFrame(national_data).set_index("Date")
    
    # Regional data
    regional_data = []
    for date in dates:
        for i, region in enumerate(admin1s):
            national_fb = df_national.loc[date, "FoodBasket"]
            regional_factor = 1.0 + i * 0.05 + random.uniform(-0.05, 0.15)
            price = national_fb * regional_factor
            regional_data.append({"Date": date, "Region": region, "FoodBasket": round(price, 2)})
    
    df_regional = pd.DataFrame(regional_data)
    
    return df_national, df_regional


def calculate_statistics(df: pd.DataFrame) -> Dict[str, Any]:
    """Calcola MoM e YoY dai dati."""
    stats = {"food_basket": {}, "commodities": {}, "auxiliary": {}}
    
    if df.empty or len(df) < 13:
        return stats

    current = df.iloc[-1]
    mom = df.iloc[-2]
    yoy = df.iloc[0]

    for col in df.columns:
        current_val = current[col]
        mom_val = mom[col]
        yoy_val = yoy[col]

        mom_pct = round(((current_val - mom_val) / mom_val * 100) if mom_val else 0, 1)
        yoy_pct = round(((current_val - yoy_val) / yoy_val * 100) if yoy_val else 0, 1)

        data = {
            "current_price": round(current_val, 2),
            "mom_change_pct": mom_pct,
            "yoy_change_pct": yoy_pct
        }

        if col == "FoodBasket":
            stats["food_basket"] = data
        elif col in ["ExchangeRate", "FuelPrice"]:
            stats["auxiliary"][col] = data
        else:
            stats["commodities"][col] = data

    return stats


def _select_default_commodities(available: List[str], max_items: int = 6) -> List[str]:
    """Select default food basket commodities from available list."""
    defaults = []

    priority_patterns = [
        "sorghum", "maize", "wheat", "rice",
        "beans", "lentil",
        "oil",
        "salt",
        "sugar",
    ]

    for pattern in priority_patterns:
        for commodity in available:
            if pattern in commodity.lower() and commodity not in defaults:
                defaults.append(commodity)
                break
        if len(defaults) >= max_items:
            break

    return defaults


def node_data_agent(state: MarketReportState) -> dict:
    """
    Nodo: Recupera e processa i dati.
    
    Supports two modes:
    - use_mock_data=True: Uses generated mock data for explicit testing only
    - use_mock_data=False: Loads cached PriceCache price data

    PriceCache failures are raised so users see actionable errors instead of
    silently receiving generated data.
    """
    logger.info(f"[DataAgent] Processing data for {state['country']}")

    country = state["country"]
    use_mock = state.get("use_mock_data", False)
    language = _state_language(state)
    requested_commodities = state.get("commodity_list", []) or []
    commodity_list = _dedupe_text(requested_commodities)

    warnings = []
    databridges_rows: List[Dict[str, Any]] = []
    cache_metadata: Dict[str, Any] = {}
    food_basket: Dict[str, Any] = dict(state.get("food_basket") or {})
    food_baskets: Dict[str, Any] = dict(state.get("food_baskets") or {})
    basket_series_national: List[Dict[str, Any]] = []
    basket_series_regional: List[Dict[str, Any]] = []
    basket_statistics: Dict[str, Any] = {"primary": None, "secondary": None}
    
    if use_mock:
        # =====================================================================
        # MOCK DATA MODE (Original behavior)
        # =====================================================================
        logger.info("[DataAgent] Using MOCK data generation")
        df_national, df_regional = generate_mock_time_series(
            state["country"],
            state["time_period"],
            commodity_list,
            state["admin1_list"]
        )
        stats = calculate_statistics(df_national)
        
    else:
        # =====================================================================
        # PRICECACHE DATA MODE
        # =====================================================================
        logger.info("[DataAgent] Loading data from PriceCache")
        
        try:
            if not food_baskets.get("primary"):
                food_basket = get_active_basket_for_report(
                    state["country"],
                    basket_version_id=state.get("primary_basket_version_id") or state.get("basket_version_id"),
                )
                food_baskets = {"primary": food_basket, "secondary": None}
            else:
                food_basket = dict(food_baskets.get("primary") or {})
            basket_specs = [BasketCalculationSpec.from_snapshot(food_basket)]
            secondary_snapshot = food_baskets.get("secondary")
            if state.get("include_secondary_basket") and isinstance(secondary_snapshot, Mapping):
                basket_specs.append(BasketCalculationSpec.from_snapshot(secondary_snapshot))
            basket_items = list(food_basket.get("items") or [])
            basket_commodities = [
                item.commodity_name
                for spec in basket_specs
                for item in spec.items
            ]
            commodity_list = _dedupe_text(basket_commodities + commodity_list)

            result = resolve_report_price_data(
                country=state["country"],
                time_period=state["time_period"],
                commodities=commodity_list,
                admin1_list=state["admin1_list"],
                currency_code=state.get("currency_code"),
                basket_items=basket_items,
                basket_specs=basket_specs,
                enabled_modules=state.get("enabled_modules", []),
            )
            df_national = result.df_national
            df_regional = result.df_regional
            df_history_national = result.df_history_national
            df_raw = result.raw_rows
            cache_metadata = result.cache_metadata
            warnings.extend(result.warnings)
            databridges_rows = json.loads(df_raw.to_json(orient="records", date_format="iso"))
            basket_series_national = result.basket_series_national.copy()
            if not basket_series_national.empty:
                basket_series_national["Date"] = pd.to_datetime(
                    basket_series_national["Date"], errors="coerce"
                ).dt.strftime("%Y-%m-%d")
                basket_series_national = json.loads(basket_series_national.to_json(orient="records"))
            else:
                basket_series_national = []
            basket_series_regional = result.basket_series_regional.copy()
            if not basket_series_regional.empty:
                basket_series_regional["Date"] = pd.to_datetime(
                    basket_series_regional["Date"], errors="coerce"
                ).dt.strftime("%Y-%m-%d")
                basket_series_regional = json.loads(basket_series_regional.to_json(orient="records"))
            else:
                basket_series_regional = []
            basket_statistics = dict(result.basket_statistics or basket_statistics)
            
            # Calculate statistics using the existing report statistics contract
            stats = calculate_statistics_from_csv(
                df_national, 
                commodity_list,
                food_basket_components=basket_items,
                currency_code=cache_metadata.get("currency_code") or state.get("currency_code"),
            )
            if basket_statistics.get("primary"):
                stats["food_basket"] = basket_statistics["primary"]
            if result.fuel_energy_data:
                stats["fuel_energy"] = result.fuel_energy_data
            if result.livestock_animal_products_data:
                stats["livestock_animal_products"] = result.livestock_animal_products_data
            if result.labour_market_data:
                stats["labour_market"] = result.labour_market_data
            basket_metadata = {
                "basket_version_id": food_basket.get("basket_version_id"),
                "basket_version_number": food_basket.get("version_number"),
                "basket_created_at": food_basket.get("created_at"),
                "basket_created_by_user_id": food_basket.get("created_by_user_id"),
                "basket_cache_version_id_at_creation": food_basket.get("cache_version_id_at_creation"),
                "basket_change_note": food_basket.get("change_note"),
                "basket_items": basket_items,
                "basket_calculation_specs": result.basket_calculation_specs,
                "basket_applicable_regions": result.basket_applicable_regions,
                "basket_coverage": {
                    role: {
                        "current_complete": (payload or {}).get("current_complete"),
                        "missing_component_names": (payload or {}).get("missing_component_names") or [],
                    }
                    for role, payload in basket_statistics.items()
                    if payload is not None
                },
            }
            cache_metadata = {**cache_metadata, **basket_metadata}
            food_basket_stats = stats.get("food_basket", {}) if isinstance(stats, dict) else {}
            missing_latest_components = food_basket_stats.get("missing_latest_component_names") or []
            selected_component_count = food_basket_stats.get("selected_component_count")
            latest_component_count = food_basket_stats.get("latest_component_count")
            latest_component_names = food_basket_stats.get("latest_component_names") or []
            if missing_latest_components and selected_component_count:
                warnings.append(
                    t(
                        language,
                        "warning.partial_basket",
                        period=state["time_period"],
                        latest_count=latest_component_count,
                        selected_count=selected_component_count,
                        latest_names=", ".join(latest_component_names) or "none",
                        missing_names=", ".join(missing_latest_components),
                    )
                )
            
            logger.info(
                f"[DataAgent] Successfully loaded {len(df_national)} months of data "
                f"with {len(df_national.columns)} columns"
            )
            
        except Exception as e:
            logger.exception(f"[DataAgent] Failed to load PriceCache price data: {e}")
            raise
    
    # =========================================================================
    # RETURN STATE UPDATE
    # =========================================================================
    return {
        "commodity_list": commodity_list,
        "time_series_data_national": df_national.to_json(date_format='iso'),
        "time_series_data_regional": df_regional.to_json(date_format='iso'),
        "time_series_history_national": (
            df_history_national.to_json(date_format='iso')
            if "df_history_national" in locals() and isinstance(df_history_national, pd.DataFrame)
            else None
        ),
        "data_statistics": stats,
        "databridges_rows": databridges_rows,
        "cache_metadata": cache_metadata,
        "food_basket": food_basket,
        "food_baskets": food_baskets if not use_mock else {"primary": None, "secondary": None},
        "basket_series_national": basket_series_national,
        "basket_series_regional": basket_series_regional,
        "basket_statistics": basket_statistics,
        "secondary_basket_included": bool(
            not use_mock and state.get("include_secondary_basket") and food_baskets.get("secondary")
        ),
        "exchange_rate_data": result.exchange_rate_data if not use_mock and "result" in locals() else None,
        "fuel_energy_data": result.fuel_energy_data if not use_mock and "result" in locals() else None,
        "livestock_animal_products_data": (
            result.livestock_animal_products_data if not use_mock and "result" in locals() else None
        ),
        "labour_market_data": result.labour_market_data if not use_mock and "result" in locals() else None,
        "warnings": warnings,
        "current_node": "data_agent"
    }


# ============================================================================
# NODE: GRAPH DESIGNER
# ============================================================================

_BASKET_ROLE_COLORS = {
    "primary": ["#1f77b4", "#4c91c3", "#79abd2", "#a6c5e1", "#d3e2f0"],
    "secondary": ["#ff7f0e", "#ff9b3d", "#ffb66b", "#ffd09a", "#ffe7cc"],
}


def _basket_snapshot_for_chart(state: Mapping[str, Any], role: str) -> Dict[str, Any]:
    baskets = state.get("food_baskets") or {}
    snapshot = baskets.get(role) if isinstance(baskets, Mapping) else None
    if role == "primary" and not isinstance(snapshot, Mapping):
        snapshot = state.get("food_basket")
    if role == "secondary" and not bool(state.get("include_secondary_basket")):
        return {}
    return dict(snapshot) if isinstance(snapshot, Mapping) else {}


def _basket_series_frame(records: Any) -> pd.DataFrame:
    if not isinstance(records, list) or not records:
        return pd.DataFrame()
    frame = pd.DataFrame([item for item in records if isinstance(item, Mapping)])
    if frame.empty or "Date" not in frame.columns:
        return pd.DataFrame()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.to_period("M").dt.to_timestamp()
    frame["Cost"] = pd.to_numeric(frame.get("Cost"), errors="coerce")
    if "Complete" in frame.columns:
        frame["Complete"] = frame["Complete"].map(
            lambda value: value is True or str(value).strip().lower() in {"true", "1", "yes"}
        )
        frame.loc[~frame["Complete"], "Cost"] = np.nan
    else:
        frame["Complete"] = frame["Cost"].notna()
    return frame[frame["Date"].notna()].sort_values(["Date", "Region"] if "Region" in frame.columns else ["Date"])


def _basket_chart_identity(
    state: Mapping[str, Any],
    role: str,
    national: pd.DataFrame,
    regional: pd.DataFrame,
) -> tuple[str, str, List[str]]:
    snapshot = _basket_snapshot_for_chart(state, role)
    role_frames = []
    for frame in (national, regional):
        if not frame.empty and "BasketRole" in frame.columns:
            role_frames.append(frame[frame["BasketRole"].astype(str).str.lower() == role])
    role_rows = pd.concat(role_frames, ignore_index=True) if role_frames else pd.DataFrame()
    name = str(snapshot.get("basket_name") or "").strip()
    if not name and not role_rows.empty and "BasketName" in role_rows.columns:
        names = role_rows["BasketName"].dropna().astype(str)
        name = names.iloc[0].strip() if not names.empty else ""
    name = name or ("MEB" if role == "primary" else "Secondary basket")
    scope_type = str(snapshot.get("scope_type") or "").strip().lower()
    if not scope_type and not role_rows.empty and "ScopeType" in role_rows.columns:
        scopes = role_rows["ScopeType"].dropna().astype(str)
        scope_type = scopes.iloc[0].strip().lower() if not scopes.empty else ""
    scope_type = scope_type or "national"
    statistics = state.get("basket_statistics") or {}
    role_stats = statistics.get(role) if isinstance(statistics, Mapping) else None
    ordered_regions = list(role_stats.get("applicable_regions") or []) if isinstance(role_stats, Mapping) else []
    if not ordered_regions:
        ordered_regions = list(snapshot.get("regions") or [])
    return name, scope_type, _dedupe_text(ordered_regions)


def _localized_basket_scope(scope_type: str, regions: List[str], language: str) -> str:
    if scope_type == "national":
        return t(language, "basket.scope.national")
    if regions:
        return t(language, "basket.scope.selected_regions_named", regions=", ".join(regions))
    return t(language, "basket.scope.selected_regions")


def _basket_trend_chart_data(
    state: Mapping[str, Any],
    role: str,
    df_national: pd.DataFrame,
    basket_national: pd.DataFrame,
    basket_regional: pd.DataFrame,
) -> Dict[str, Any]:
    name, scope_type, ordered_regions = _basket_chart_identity(
        state,
        role,
        basket_national,
        basket_regional,
    )
    if role == "secondary" and not _basket_snapshot_for_chart(state, role):
        return {}
    if scope_type == "national":
        if role == "primary" and "FoodBasket" in df_national.columns:
            series = pd.to_numeric(df_national["FoodBasket"], errors="coerce")
        else:
            rows = basket_national
            if not rows.empty and "BasketRole" in rows.columns:
                rows = rows[rows["BasketRole"].astype(str).str.lower() == role]
            series = (
                rows.drop_duplicates("Date", keep="last").set_index("Date")["Cost"].sort_index()
                if not rows.empty
                else pd.Series(dtype=float)
            )
        if series.dropna().empty:
            return {}
        return {
            "name": name,
            "scope_type": scope_type,
            "regions": ordered_regions,
            "series": [(t(_state_language(dict(state)), "chart.label.current"), series)],
        }

    rows = basket_regional
    if rows.empty or "BasketRole" not in rows.columns or "Region" not in rows.columns:
        return {}
    rows = rows[rows["BasketRole"].astype(str).str.lower() == role]
    if rows.empty:
        return {}
    observed = _dedupe_text(rows["Region"].dropna().astype(str).tolist())
    region_lookup = {region.casefold(): region for region in observed}
    regions = [region_lookup.get(str(region).casefold()) for region in ordered_regions]
    regions = [region for region in regions if region]
    regions.extend(region for region in observed if region not in regions)
    series_items: List[tuple[str, pd.Series]] = []
    for region in regions:
        region_rows = rows[rows["Region"].astype(str).str.casefold() == region.casefold()]
        series = region_rows.drop_duplicates("Date", keep="last").set_index("Date")["Cost"].sort_index()
        if series.notna().any():
            series_items.append((region, series))
    if not series_items:
        return {}
    return {
        "name": name,
        "scope_type": scope_type,
        "regions": regions,
        "series": series_items,
    }


def _basket_regional_target_data(
    state: Mapping[str, Any],
    role: str,
    basket_regional: pd.DataFrame,
) -> Dict[str, Any]:
    if basket_regional.empty or "BasketRole" not in basket_regional.columns or "Region" not in basket_regional.columns:
        return {}
    name, scope_type, ordered_regions = _basket_chart_identity(
        state,
        role,
        pd.DataFrame(),
        basket_regional,
    )
    if role == "secondary" and not _basket_snapshot_for_chart(state, role):
        return {}
    target = pd.to_datetime(f"{state.get('time_period')}-01", errors="coerce")
    if pd.isna(target):
        return {}
    rows = basket_regional[
        (basket_regional["BasketRole"].astype(str).str.lower() == role)
        & (basket_regional["Date"] == pd.Timestamp(target).to_period("M").to_timestamp())
        & basket_regional["Complete"].astype(bool)
        & basket_regional["Cost"].notna()
    ].copy()
    if rows.empty:
        return {}
    observed = _dedupe_text(rows["Region"].dropna().astype(str).tolist())
    region_lookup = {region.casefold(): region for region in observed}
    regions = [region_lookup.get(str(region).casefold()) for region in ordered_regions]
    regions = [region for region in regions if region]
    regions.extend(region for region in observed if region not in regions)
    rows["_region_order"] = rows["Region"].map({region: index for index, region in enumerate(regions)})
    rows = rows.sort_values("_region_order", na_position="last").drop_duplicates("Region", keep="last")
    return {
        "name": name,
        "scope_type": scope_type,
        "scope_regions": ordered_regions,
        "regions": rows["Region"].astype(str).tolist(),
        "costs": rows["Cost"].astype(float).tolist(),
    }


def _legacy_primary_regional_target_data(state: Mapping[str, Any], df_regional: pd.DataFrame) -> Dict[str, Any]:
    if df_regional.empty or "Date" not in df_regional.columns or "Region" not in df_regional.columns:
        return {}
    target = pd.to_datetime(f"{state.get('time_period')}-01", errors="coerce")
    if pd.isna(target) or "FoodBasket" not in df_regional.columns:
        return {}
    rows = df_regional.copy()
    rows["Date"] = pd.to_datetime(rows["Date"], errors="coerce").dt.to_period("M").dt.to_timestamp()
    rows["FoodBasket"] = pd.to_numeric(rows["FoodBasket"], errors="coerce")
    rows = rows[
        (rows["Date"] == pd.Timestamp(target).to_period("M").to_timestamp())
        & rows["FoodBasket"].notna()
        & (rows["FoodBasket"] > 0)
    ]
    if rows.empty:
        return {}
    return {
        "name": str((_basket_snapshot_for_chart(state, "primary") or {}).get("basket_name") or "MEB"),
        "scope_type": str((_basket_snapshot_for_chart(state, "primary") or {}).get("scope_type") or "national"),
        "scope_regions": [],
        "regions": rows["Region"].astype(str).tolist(),
        "costs": rows["FoodBasket"].astype(float).tolist(),
    }


def _encode_matplotlib_figure(plt: Any) -> str:
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")

def node_graph_designer(state: MarketReportState) -> dict:
    """Nodo: Genera visualizzazioni."""
    logger.info("[GraphDesigner] Generating visualizations")
    
    visualizations = {}
    
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        
        # Parse data
        df_national = pd.read_json(io.StringIO(state["time_series_data_national"]))
        df_national = _normalise_time_index(df_national)
        history_json = state.get("time_series_history_national")
        df_history = pd.DataFrame()
        if history_json:
            df_history = _normalise_time_index(pd.read_json(io.StringIO(history_json)))
        currency_code = _state_currency_code(state)
        language = _state_language(state)
        basket_national = _basket_series_frame(state.get("basket_series_national"))
        basket_regional = _basket_series_frame(state.get("basket_series_regional"))
        
        # 1. Food Basket Trend
        for role in ("primary", "secondary"):
            chart = _basket_trend_chart_data(
                state,
                role,
                df_national,
                basket_national,
                basket_regional,
            )
            if not chart:
                continue
            fig, ax = plt.subplots(figsize=(10, 5))
            colors = _BASKET_ROLE_COLORS[role]
            plotted_index = pd.DatetimeIndex([])
            for index, (label, series) in enumerate(chart["series"]):
                series = pd.to_numeric(series, errors="coerce").sort_index()
                plotted_index = pd.DatetimeIndex(series.index)
                ax.plot(
                    series.index,
                    series,
                    marker="o",
                    linewidth=2,
                    color=colors[index % len(colors)],
                    label=label,
                )
            if role == "primary" and chart["scope_type"] == "national" and len(plotted_index):
                _plot_history_overlays(
                    ax,
                    df_history,
                    plotted_index,
                    "FoodBasket",
                    color=colors[0],
                )
            scope_label = _localized_basket_scope(
                chart["scope_type"],
                list(chart.get("scope_regions") or chart.get("regions") or []),
                language,
            )
            ax.set_title(
                t(
                    language,
                    "chart.title.basket_trend_role",
                    basket=chart["name"],
                    scope=scope_label,
                    country=state["country"],
                ),
                fontweight="bold",
            )
            ax.set_ylabel(_currency_axis_label("Cost", currency_code, language))
            _set_localized_numeric_axis(ax, language)
            ax.legend(loc="upper left")
            _set_localized_month_axis(ax, language)
            plt.xticks(rotation=45)
            plt.tight_layout()
            encoded = _encode_matplotlib_figure(plt)
            visualizations[f"food_basket_trend_{role}"] = encoded
            if role == "primary":
                visualizations["food_basket_trend"] = encoded
        
        # 2. Commodity Trends (Grouped)
        stats = state.get("data_statistics", {}) or {}
        commodity_cols = []
        for c in df_national.columns:
            if c == "FoodBasket":
                continue
            if _is_auxiliary_series(c):
                continue
            try:
                if df_national[c].dropna().empty:
                    continue
            except Exception:
                pass
            commodity_cols.append(c)

        if commodity_cols:
            grouped: Dict[str, List[str]] = {}
            for c in commodity_cols:
                cat = _categorize_commodity(c)
                grouped.setdefault(cat, []).append(c)

            category_order = ["Cereals", "Pulses", "Oil", "Sugar", "Condiments", "Vegetables", "Livestock", "Other"]
            ordered_categories = [c for c in category_order if c in grouped]
            for extra in sorted([c for c in grouped.keys() if c not in ordered_categories]):
                ordered_categories.append(extra)

            max_lines_per_chart = 6
            for cat in ordered_categories:
                cols = grouped.get(cat) or []
                cols = sorted(
                    cols,
                    key=lambda x: (-_commodity_importance_score(stats, x), str(x).lower()),
                )

                pages = _chunk_list(cols, max_lines_per_chart)
                cat_slug = _slugify(cat)
                for page_idx, page_cols in enumerate(pages, start=1):
                    fig, ax = plt.subplots(figsize=(12, 6))
                    show_history_overlays = len(page_cols) == 1
                    for col in page_cols:
                        line = ax.plot(df_national.index, df_national[col], marker='o', label=col)[0]
                        if show_history_overlays:
                            _plot_history_overlays(
                                ax,
                                df_history,
                                pd.DatetimeIndex(df_national.index),
                                col,
                                color=line.get_color(),
                                label_prefix=col,
                            )
                    title_suffix = _localized_page_suffix(cat, page_idx, len(pages), language)
                    ax.set_title(
                        t(language, "chart.title.commodity", country=state["country"], title_suffix=title_suffix),
                        fontweight='bold',
                    )
                    ax.set_ylabel(_currency_axis_label("Price", currency_code, language))
                    _set_localized_numeric_axis(ax, language)
                    ax.legend(loc='upper left')
                    _set_localized_month_axis(ax, language)
                    plt.xticks(rotation=45)
                    plt.tight_layout()

                    buf = io.BytesIO()
                    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
                    plt.close()
                    buf.seek(0)
                    fig_id = f"commodity_trends_{cat_slug}_p{page_idx}"
                    fig_b64 = base64.b64encode(buf.read()).decode('utf-8')
                    visualizations[fig_id] = fig_b64
                    if "commodity_trends" not in visualizations:
                        visualizations["commodity_trends"] = fig_b64

        # 3. Exchange Rate Trend
        fx_cols = [
            ("ExchangeRate", t(language, "chart.label.official"), "#6f42c1"),
            ("ExchangeRateUnofficial", t(language, "chart.label.unofficial"), "#d35400"),
        ]
        if any(col in df_national.columns and df_national[col].dropna().any() for col, _label, _color in fx_cols):
            fig, ax = plt.subplots(figsize=(10, 5))
            for col, label, color in fx_cols:
                if col in df_national.columns and df_national[col].dropna().any():
                    ax.plot(df_national.index, df_national[col], marker='o', linewidth=2, color=color, label=label)
            ax.set_title(t(language, "chart.title.exchange_rate", country=state["country"]), fontweight='bold')
            ax.set_ylabel(_fx_axis_label(currency_code, language))
            _set_localized_numeric_axis(ax, language)
            ax.legend(loc='upper left')
            _set_localized_month_axis(ax, language)
            plt.xticks(rotation=45)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            visualizations["exchange_rate_trend"] = base64.b64encode(buf.read()).decode('utf-8')

        # 4. Fuel & Energy Trend
        fuel_data = state.get("fuel_energy_data") or {}
        fuel_series = []
        for item in fuel_data.get("series") or []:
            if not isinstance(item, dict):
                continue
            column = str(item.get("column_name") or "").strip()
            label = str(item.get("label") or column).strip()
            if column and column in df_national.columns and df_national[column].dropna().any():
                fuel_series.append((column, label))

        if fuel_series:
            fig, ax = plt.subplots(figsize=(10, 5))
            show_history_overlays = len(fuel_series) <= 2
            for column, label in fuel_series:
                line = ax.plot(df_national.index, df_national[column], marker='o', linewidth=2, label=label)[0]
                if show_history_overlays:
                    _plot_history_overlays(
                        ax,
                        df_history,
                        pd.DatetimeIndex(df_national.index),
                        column,
                        color=line.get_color(),
                        label_prefix=label,
                    )
            ax.set_title(t(language, "chart.title.fuel", country=state["country"]), fontweight='bold')
            ax.set_ylabel(_fuel_axis_label(currency_code, language))
            _set_localized_numeric_axis(ax, language)
            ax.legend(loc='upper left')
            _set_localized_month_axis(ax, language)
            plt.xticks(rotation=45)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            visualizations["fuel_prices"] = base64.b64encode(buf.read()).decode('utf-8')

        # 5. Livestock & Animal Products Trend
        animal_data = state.get("livestock_animal_products_data") or {}
        animal_chart = animal_data.get("chart") or {}
        animal_series = []
        for item in animal_chart.get("series") or []:
            if not isinstance(item, dict):
                continue
            column = str(item.get("column_name") or "").strip()
            label = str(item.get("label") or column).strip()
            if column and column in df_national.columns and df_national[column].dropna().any():
                animal_series.append((column, label))

        if animal_series:
            fig, ax = plt.subplots(figsize=(10, 5))
            chart_mode = str(animal_chart.get("mode") or "absolute")
            show_history_overlays = chart_mode == "absolute" and len(animal_series) <= 2
            for column, label in animal_series:
                values = df_national[column]
                if chart_mode == "indexed":
                    values = _index_to_first_observation(values)
                line = ax.plot(df_national.index, values, marker='o', linewidth=2, label=label)[0]
                if show_history_overlays:
                    _plot_history_overlays(
                        ax,
                        df_history,
                        pd.DatetimeIndex(df_national.index),
                        column,
                        color=line.get_color(),
                        label_prefix=label,
                    )
            ax.set_title(t(language, "chart.title.livestock", country=state["country"]), fontweight='bold')
            ax.set_ylabel(_animal_axis_label(animal_data, currency_code, language))
            _set_localized_numeric_axis(ax, language)
            ax.legend(loc='upper left')
            _set_localized_month_axis(ax, language)
            plt.xticks(rotation=45)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            visualizations["livestock_animal_products"] = base64.b64encode(buf.read()).decode('utf-8')

        # 6. Labour Market Trend
        labour_data = state.get("labour_market_data") or {}
        labour_chart = labour_data.get("chart") or {}
        labour_series = []
        for item in labour_chart.get("series") or []:
            if not isinstance(item, dict):
                continue
            column = str(item.get("column_name") or "").strip()
            label = str(item.get("label") or column).strip()
            axis_name = str(item.get("axis") or "primary")
            if column and column in df_national.columns and df_national[column].dropna().any():
                labour_series.append((column, label, axis_name, item))

        if labour_series:
            fig, ax = plt.subplots(figsize=(10, 5))
            secondary_ax = None
            plotted_for_overlays = []
            for column, label, axis_name, item in labour_series:
                target_ax = ax
                if axis_name == "secondary":
                    secondary_ax = secondary_ax or ax.twinx()
                    target_ax = secondary_ax
                line = target_ax.plot(df_national.index, df_national[column], marker='o', linewidth=2, label=label)[0]
                if axis_name != "secondary":
                    plotted_for_overlays.append((column, label, line.get_color()))
            show_history_overlays = len(labour_series) <= 2
            if show_history_overlays:
                for column, label, color in plotted_for_overlays:
                    _plot_history_overlays(
                        ax,
                        df_history,
                        pd.DatetimeIndex(df_national.index),
                        column,
                        color=color,
                        label_prefix=label,
                    )
            ax.set_title(t(language, "chart.title.labour", country=state["country"]), fontweight='bold')
            ax.set_ylabel(_labour_axis_label(labour_data, currency_code, language))
            _set_localized_numeric_axis(ax, language)
            if secondary_ax is not None:
                secondary_label = next(
                    (
                        localize_axis_label(item.get("axis_label"), language)
                        for _c, _l, axis_name, item in labour_series
                        if axis_name == "secondary" and item.get("axis_label")
                    ),
                    _currency_axis_label("Wage", currency_code, language),
                )
                secondary_ax.set_ylabel(secondary_label)
                _set_localized_numeric_axis(secondary_ax, language)
                lines = ax.get_lines() + secondary_ax.get_lines()
                labels = [line.get_label() for line in lines]
                ax.legend(lines, labels, loc='upper left')
            else:
                ax.legend(loc='upper left')
            _set_localized_month_axis(ax, language)
            plt.xticks(rotation=45)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            visualizations["labour_market"] = base64.b64encode(buf.read()).decode('utf-8')
        
        # 7. Regional Comparison (if data available)
        legacy_regional = pd.DataFrame()
        if state.get("time_series_data_regional"):
            legacy_regional = pd.read_json(io.StringIO(state["time_series_data_regional"]))
        for role in ("primary", "secondary"):
            chart = _basket_regional_target_data(state, role, basket_regional)
            if role == "primary" and not chart:
                chart = _legacy_primary_regional_target_data(state, legacy_regional)
            if not chart:
                continue
            fig, ax = plt.subplots(figsize=(10, 6))
            ax.barh(
                chart["regions"],
                chart["costs"],
                color=_BASKET_ROLE_COLORS[role][0],
            )
            period_label = format_month_label(state.get("time_period"), language)
            scope_label = _localized_basket_scope(
                chart["scope_type"],
                list(chart.get("scope_regions") or chart.get("regions") or []),
                language,
            )
            ax.set_title(
                t(
                    language,
                    "chart.title.basket_regional_role",
                    basket=chart["name"],
                    scope=scope_label,
                    period=period_label,
                ),
                fontweight="bold",
            )
            ax.set_xlabel(_currency_axis_label("Cost", currency_code, language))
            try:
                import matplotlib.ticker as mticker

                ax.xaxis.set_major_formatter(
                    mticker.FuncFormatter(lambda value, _pos: format_decimal_value(value, language, decimals=0))
                )
            except Exception:
                pass
            plt.tight_layout()
            encoded = _encode_matplotlib_figure(plt)
            visualizations[f"regional_comparison_{role}"] = encoded
            if role == "primary":
                visualizations["regional_comparison"] = encoded
    
    except Exception as e:
        logger.error(f"Error generating visualizations: {e}")
    
    return {
        "visualizations": visualizations,
        "current_node": "graph_designer"
    }


# ============================================================================
# NODE: NEWS RETRIEVAL 
# ============================================================================

def node_news_retrieval(state: MarketReportState) -> dict:
    """Nodo: Recupera notizie (mock per ora)."""
    logger.info(f"[NewsRetrieval] Fetching news for {state['country']}")

    warnings: List[str] = []

    country = state.get("country", "")
    time_period = state.get("time_period", "")

    try:
        start_dt = datetime.strptime(time_period + "-01", "%Y-%m-%d")
    except Exception:
        start_dt = datetime.utcnow().replace(day=1)

    end_dt = (start_dt + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    prev_month_start = (start_dt - timedelta(days=1)).replace(day=1)

    context = gather_context(
        ReliefWebRetriever(verbose=False),
        SeeristRetriever(verbose=False),
        country=country,
        start_date=prev_month_start.strftime("%Y-%m-%d"),
        end_date=end_dt.strftime("%Y-%m-%d"),
        reliefweb_terms=["food security", "supply", "shortage", "subsidy"],
        seerist_terms=["food security", "wheat", "sorghum", "rice", "cooking oil"],
        seerist_focus=["market", "food security", "inflation", "currency", "availability"],
        limit=10,
    )
    if context.error("Seerist"):
        warnings.append(f"Seerist retrieval unavailable for {country}: {context.error('Seerist')}")

    updates = {
        "documents": context.documents,
        "document_references": context.references(),
        "seerist_documents": list(context.seerist),
        "reliefweb_documents": list(context.reliefweb),
        "news_counts": context.counts(),
        "retriever_traces": context.traces,
        "current_node": "news_retrieval",
    }
    if warnings:
        updates["warnings"] = warnings
    return updates


# ============================================================================
# NODE: EVENT MAPPER
# ============================================================================

def node_event_mapper(state: MarketReportState) -> dict:
    """Nodo: Estrae eventi dai documenti."""
    logger.info("[EventMapper] Extracting events")
    
    documents = state.get("documents", [])
    llm = llm_client(state)
    trace = llm.tracer
    
    if not documents:
        trace.record_skip(
            node="event_mapper",
            operation="market_monitor.event_extraction.v1",
            reason="no_contextual_documents",
        )
        # Fallback events
        events = [{
            "event_id": "evt_fallback",
            "category": "economic",
            "statement": f"Ongoing price increases in {state['country']} due to economic factors.",
            "location": state["country"],
            "date": state["time_period"] + "-01",
            "source_ids": []
        }]
        return {
            "events": events,
            "llm_diagnostics": trace.snapshot(),
            "current_node": "event_mapper",
        }
    
    # Prepare context
    context = "\n\n".join([
        f"[{d['doc_id']}] {d['date']}: {d['content'][:500]}"
        for d in documents[:5]
    ])
    
    prompt = event_extraction_prompt(state['country'], context)
    
    def _validate_events(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        events = result.get("events")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise ValueError(f"events[{index}] must be an object")
            for field in ("event_id", "category", "statement", "location", "date"):
                if not str(event.get(field) or "").strip():
                    raise ValueError(f"events[{index}].{field} is required")
            if not isinstance(event.get("source_ids"), list):
                raise ValueError(f"events[{index}].source_ids must be a list")
        return events

    traced = llm.generate_json(
        prompt=prompt,
        node="event_mapper",
        operation="market_monitor.event_extraction.v1",
        artifact_type="context",
        artifact_id="events",
        validator=_validate_events,
    )
    events = traced.value
    llm_calls = 1
    
    if not events:
        events = [{
            "event_id": "evt_fallback",
            "category": "economic",
            "statement": f"Market conditions in {state['country']} remain challenging.",
            "location": state["country"],
            "date": state["time_period"] + "-01",
            "source_ids": []
        }]
    
    return {
        "events": events,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "event_mapper"
    }


# ============================================================================
# NODE: TREND ANALYST
# ============================================================================

def node_trend_analyst(state: MarketReportState) -> dict:
    """Nodo: Analizza i trend."""
    logger.info("[TrendAnalyst] Analyzing trends")
    
    stats = state.get("data_statistics", {})
    events = state.get("events", [])
    basket_context = build_basket_context(state)
    llm = llm_client(state)
    trace = llm.tracer
    
    prompt = trend_analysis_prompt(stats, events, basket_context)
    
    def _validate_trend(result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("trajectory") not in {
            "increasing_prices",
            "decreasing_prices",
            "stable",
            "volatile",
        }:
            raise ValueError("trajectory is missing or invalid")
        for field in ("key_market_drivers",):
            if not isinstance(result.get(field), list):
                raise ValueError(f"{field} must be a list")
        for field in ("commodity_analysis", "regional_analysis", "basket_analysis"):
            if not isinstance(result.get(field), dict):
                raise ValueError(f"{field} must be an object")
        if not isinstance(result.get("outlook"), str) or not result["outlook"].strip():
            raise ValueError("outlook must be non-empty text")
        return result

    traced = llm.generate_json(
        prompt=prompt,
        node="trend_analyst",
        operation="market_monitor.trend_analysis.v1",
        artifact_type="analysis",
        artifact_id="trend_analysis",
        validator=_validate_trend,
    )
    trend_analysis = traced.value
    llm_calls = 1
    trend_analysis = _trend_with_basket_identity(trend_analysis, basket_context)
    
    return {
        "trend_analysis": trend_analysis,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "trend_analyst"
    }


# ============================================================================
# NODE: MODULE ORCHESTRATOR
# ============================================================================

def node_module_orchestrator(state: MarketReportState) -> dict:
    """Nodo: Esegue i moduli opzionali."""
    logger.info("[ModuleOrchestrator] Running optional modules")
    
    enabled_modules = state.get("enabled_modules", [])
    llm = llm_client(state)
    trace = llm.tracer
    language = _state_language(state)
    targets = set(state.get("correction_targets") or [])
    correction_mode = bool(targets)

    if not correction_mode:
        operation_ids = {
            "exchange_rate": "market_monitor.exchange_rate_module.v1",
            "fuel_energy": "market_monitor.fuel_energy_module.v1",
            "livestock_animal_products": "market_monitor.livestock_module.v1",
            "labour_market": "market_monitor.labour_module.v1",
        }
        for module_id, operation in operation_ids.items():
            if module_id not in enabled_modules:
                trace.record_skip(
                    node="module_orchestrator",
                    operation=operation,
                    reason="optional_module_disabled",
                    artifact_type="module",
                    artifact_id=module_id,
                )

    if correction_mode and "GLOBAL" not in targets:
        targeted_modules = {
            module_id
            for section, module_id in QA_MODULE_SECTIONS.items()
            if section in targets
        }
        enabled_modules = [module_id for module_id in enabled_modules if module_id in targeted_modules]
    
    if not enabled_modules:
        return {
            "current_node": "module_orchestrator",
            "llm_diagnostics": trace.snapshot(),
        }
    
    module_sections = dict(state.get("module_sections") or {})
    updates = {}
    llm_calls = 0
    warnings: List[str] = []
    
    for module_id in enabled_modules:
        if module_id not in AVAILABLE_MODULES:
            logger.warning(f"Unknown module: {module_id}")
            continue

        if module_id == "exchange_rate":
            currency_code = str(state.get("currency_code") or "").strip().upper()
            if not currency_code or currency_code == "USD":
                warnings.append(t(language, "warning.skip_exchange_usd"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.exchange_rate_module.v1",
                    reason="currency_is_usd_or_missing",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        if module_id == "fuel_energy":
            fuel_data = state.get("fuel_energy_data") or {}
            if not fuel_data.get("available") or not fuel_data.get("series"):
                warnings.append(t(language, "warning.skip_fuel_missing"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.fuel_energy_module.v1",
                    reason="module_data_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        if module_id == "livestock_animal_products":
            animal_data = state.get("livestock_animal_products_data") or {}
            if not animal_data.get("available") or not animal_data.get("series"):
                warnings.append(t(language, "warning.skip_livestock_missing"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.livestock_module.v1",
                    reason="module_data_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        if module_id == "labour_market":
            labour_data = state.get("labour_market_data") or {}
            if not labour_data.get("available") or not labour_data.get("series"):
                warnings.append(t(language, "warning.skip_labour_missing"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.labour_module.v1",
                    reason="module_data_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        
        try:
            module_class = AVAILABLE_MODULES[module_id]
            module = module_class()
            
            if not module.validate_inputs(state):
                operation = {
                    "exchange_rate": "market_monitor.exchange_rate_module.v1",
                    "fuel_energy": "market_monitor.fuel_energy_module.v1",
                    "livestock_animal_products": "market_monitor.livestock_module.v1",
                    "labour_market": "market_monitor.labour_module.v1",
                }.get(module_id, f"market_monitor.{module_id}_module.v1")
                trace.record_skip(
                    node="module_orchestrator",
                    operation=operation,
                    reason="required_inputs_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                if module_id == "exchange_rate":
                    missing = [
                        f
                        for f in getattr(module, "required_inputs", [])
                        if f not in state or state[f] is None
                    ]
                    warnings.append(t(language, "warning.skip_exchange_required", missing=missing))
                    continue
                continue
            
            # Corrections reuse the immutable, already-fetched module inputs.
            if not correction_mode:
                data_update = module.fetch_data(state)
                updates.update(data_update)
                state.update(data_update)
            
            # Generate section
            output = module.generate_section(state, llm)
            module_sections[module_id] = output.get("narrative", "")
            llm_calls += 1
            
            logger.info(f"Module '{module_id}' completed successfully")
            
        except LLMCallError:
            raise
        except Exception as e:
            logger.error(f"Module '{module_id}' failed: {e}")
            if module_id == "exchange_rate":
                warnings.append(t(language, "warning.skip_exchange_source", error=e))
            elif module_id == "fuel_energy":
                warnings.append(t(language, "warning.skip_fuel_error", error=e))
            elif module_id == "livestock_animal_products":
                warnings.append(t(language, "warning.skip_livestock_error", error=e))
            elif module_id == "labour_market":
                warnings.append(t(language, "warning.skip_labour_error", error=e))
            continue
    
    updates["module_sections"] = module_sections
    if correction_mode:
        sections = dict(state.get("report_draft_sections") or {})
        for module_id, section_text in module_sections.items():
            sections[f"{module_id.upper()}_ANALYSIS"] = section_text
        updates["report_draft_sections"] = sections
    if warnings:
        updates["warnings"] = warnings
    updates["llm_calls"] = state.get("llm_calls", 0) + llm_calls
    updates["llm_diagnostics"] = trace.snapshot()
    updates["current_node"] = "module_orchestrator"
    
    return updates


# ============================================================================
# NODE: HIGHLIGHTS DRAFTER
# ============================================================================

def node_highlights_drafter(state: MarketReportState) -> dict:
    """Nodo: Genera la sezione Highlights."""
    logger.info("[HighlightsDrafter] Generating highlights")

    if state.get("correction_targets") and not _targeted(state, "HIGHLIGHTS"):
        return {"current_node": "highlights_drafter"}
    
    language = _state_language(state)
    stats = state.get("data_statistics", {})
    trend = state.get("trend_analysis", {})
    exchange_data = state.get("exchange_rate_data", {}) or {}
    currency_code = _state_currency_code(state)
    basket_context = build_basket_context(state)
    llm = llm_client(state)
    trace = llm.tracer
 
    validation_warnings: List[str] = []
    if exchange_data and exchange_data.get("trend") == "stable":
        drivers = (trend or {}).get("key_market_drivers") or []
        if _has_currency_depreciation_driver(drivers):
            validation_warnings.append(
                t(language, "warning.exchange_stable_driver")
            )
     
    # Format statistics with arrows
    formatted_stats = {}
    if stats.get("food_basket"):
        fb = stats["food_basket"]
        formatted_stats["food_basket"] = {
            "current_price": format_currency_value(fb.get("current_price"), currency_code, language),
            "mom_change": format_pct(fb.get("mom_change_pct"), language),
            "yoy_change": format_pct(fb.get("yoy_change_pct"), language),
        }
    
    for name, data in stats.get("commodities", {}).items():
        formatted_stats[name] = {
            "current_price": format_currency_value(data.get("current_price"), currency_code, language),
            "mom_change": format_pct(data.get("mom_change_pct"), language),
            "yoy_change": format_pct(data.get("yoy_change_pct"), language),
        }
    
    prompt = render_prompt(
        "highlights",
        language,
        {
            **prompt_base_context(language),
            "country": state["country"],
            "time_period": state["time_period"],
            "report_month_localized": _report_month_for_prompt(state),
            "formatted_stats_json": _json_for_prompt(formatted_stats),
            "exchange_data_json": _json_for_prompt(exchange_data) if exchange_data else "None",
            "trend_json": _json_for_prompt(trend),
            "terminology_thresholds_json": _json_for_prompt(TERMINOLOGY_THRESHOLDS),
            "validation_warnings_json": _json_for_prompt(validation_warnings),
            "basket_context_json": _json_for_prompt(basket_context),
            "correction_flags_json": _correction_flags_json(state, "HIGHLIGHTS"),
        },
    )
    
    def _validate_highlights(result: Dict[str, Any]) -> Dict[str, Any]:
        value = result.get("HIGHLIGHTS")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("HIGHLIGHTS must be non-empty text")
        normalized, warnings = _normalize_output_text(value, state)
        if not normalized.strip():
            raise ValueError("HIGHLIGHTS is empty after normalization")
        return {"text": normalized, "warnings": warnings}

    traced = llm.generate_json(
        prompt=prompt,
        node="highlights_drafter",
        operation="market_monitor.highlights_drafting.v1",
        artifact_type="report_section",
        artifact_id="HIGHLIGHTS",
        correction_attempt=int(state.get("correction_attempts", 0) or 0),
        validator=_validate_highlights,
    )
    highlights = traced.value["text"]
    validation_warnings.extend(traced.value["warnings"])
    llm_calls = 1
    
    sections = dict(state.get("report_draft_sections") or {})
    sections["HIGHLIGHTS"] = highlights
    
    updates: Dict[str, Any] = {
        "report_draft_sections": sections,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "highlights_drafter"
    }
    if validation_warnings:
        updates["warnings"] = validation_warnings
    return updates


# ============================================================================
# NODE: NARRATIVE DRAFTER
# ============================================================================

def node_narrative_drafter(state: MarketReportState) -> dict:
    """Nodo: Genera le sezioni narrative."""
    logger.info("[NarrativeDrafter] Generating narrative sections")

    language = _state_language(state)
    trend = state.get("trend_analysis", {})
    events = state.get("events", [])
    module_sections = dict(state.get("module_sections") or {})
    basket_context = build_basket_context(state)
    core_sections = ["MARKET_OVERVIEW", "COMMODITY_ANALYSIS", "REGIONAL_HIGHLIGHTS"]
    targets = set(state.get("correction_targets") or [])
    if targets and "GLOBAL" not in targets:
        sections_to_generate = [section for section in core_sections if section in targets]
    else:
        sections_to_generate = list(core_sections)
    two_baskets = bool(basket_context.get("secondary_included"))
    word_ranges = (
        {
            "MARKET_OVERVIEW": "250-325 words",
            "COMMODITY_ANALYSIS": "250-350 words",
            "REGIONAL_HIGHLIGHTS": "200-275 words",
        }
        if two_baskets
        else {
            "MARKET_OVERVIEW": "200-250 words",
            "COMMODITY_ANALYSIS": "200-300 words",
            "REGIONAL_HIGHLIGHTS": "150-200 words",
        }
    )
    correction_flags = _normalized_qa_flags(state.get("skeptic_flags") or [])
    correction_flags = [
        flag for flag in correction_flags if flag["section"] in set(sections_to_generate) | {"GLOBAL"}
    ]
    result: Dict[str, Any] = {}
    llm_calls = 0
    normalization_warnings: List[str] = []
    llm = llm_client(state)
    trace = llm.tracer
    if sections_to_generate:
        prompt = render_prompt(
            "narrative",
            language,
            {
                **prompt_base_context(language),
                "country": state["country"],
                "time_period": state["time_period"],
                "report_month_localized": _report_month_for_prompt(state),
                "trend_json": _json_for_prompt(trend),
                "events_json": _json_for_prompt(events),
                "module_sections_json": _json_for_prompt(module_sections) if module_sections else "None",
                "basket_context_json": _json_for_prompt(basket_context),
                "sections_to_generate_json": _json_for_prompt(sections_to_generate),
                "section_word_ranges_json": _json_for_prompt(word_ranges),
                "correction_flags_json": _json_for_prompt(correction_flags),
            },
        )
        def _validate_sections(payload: Dict[str, Any]) -> Dict[str, Any]:
            normalized_sections: Dict[str, str] = {}
            warnings: List[str] = []
            for section in sections_to_generate:
                value = payload.get(section)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{section} must be non-empty text")
                normalized, section_warnings = _normalize_output_text(value, state)
                if not normalized.strip():
                    raise ValueError(f"{section} is empty after normalization")
                normalized_sections[section] = normalized
                warnings.extend(section_warnings)
            return {"sections": normalized_sections, "warnings": warnings}

        traced = llm.generate_json(
            prompt=prompt,
            node="narrative_drafter",
            operation="market_monitor.narrative_drafting.v1",
            artifact_type="report",
            artifact_id="core_sections",
            correction_attempt=int(state.get("correction_attempts", 0) or 0),
            validator=_validate_sections,
        )
        result = traced.value["sections"]
        normalization_warnings.extend(traced.value["warnings"])
        llm_calls = 1

    sections = dict(state.get("report_draft_sections") or {})
    if result:
        for key, value in result.items():
            if key not in sections_to_generate:
                continue
            if isinstance(value, str):
                normalized, warnings = _normalize_output_text(value, state)
                sections[key] = normalized
                normalization_warnings.extend(warnings)
            else:
                sections[key] = value
    
    # Add module sections
    for module_id, section_text in module_sections.items():
        section_key = f"{module_id.upper()}_ANALYSIS"
        sections[section_key] = section_text
    
    document_references = state.get("document_references", []) or []
    if document_references:
        lines = ["REFERENCES"]
        for ref in document_references:
            doc_id = ref.get("doc_id", "")
            source = ref.get("source", "")
            date = ref.get("date", "")
            title = ref.get("title", "")
            url = ref.get("url", "")
            lines.append(f"[{doc_id}] {source} ({date}) {title}")
            if url:
                lines.append(url)
            lines.append("")
        sections["REFERENCES"] = "\n".join(lines).strip()
    
    updates = {
        "report_draft_sections": sections,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "narrative_drafter"
    }
    if normalization_warnings:
        updates["warnings"] = normalization_warnings
    return updates

# NODE: RED TEAM (QA)
# ============================================================================

def node_red_team(state: MarketReportState) -> dict:
    """Nodo: Quality Assurance - verifica il draft."""
    logger.info("[RedTeam] Fact-checking draft")
    
    llm = llm_client(state)
    trace = llm.tracer
    language = _state_language(state)
    sections = state.get("report_draft_sections", {})
    stats = state.get("data_statistics", {})
    trend = state.get("trend_analysis", {}) or {}
    exchange_data = state.get("exchange_rate_data", {}) or {}
    basket_context = build_basket_context(state)
    module_relevance = {
        module_id: optional_module_basket_relevance(state, module_id)
        for module_id in AVAILABLE_MODULES
    }
     
    if not sections:
        trace.record_skip(
            node="red_team",
            operation="market_monitor.red_team_review.v1",
            reason="no_report_sections",
        )
        review = qa_review_from_state({**dict(state), "skeptic_flags": []})
        return {
            "skeptic_flags": [],
            "qa_review": review,
            "correction_targets": [],
            "llm_diagnostics": trace.snapshot(),
            "current_node": "red_team",
        }
     
    draft_text = "\n\n".join([f"== {k} ==\n{v}" for k, v in sections.items()])
     
    prompt = render_prompt(
        "red_team",
        language,
        {
            **prompt_base_context(language),
            "stats_json": _json_for_prompt(stats),
            "exchange_mom": exchange_data.get("monthly_change_pct"),
            "exchange_yoy": exchange_data.get("yearly_change_pct"),
            "exchange_trend": exchange_data.get("trend"),
            "exchange_data_json": _json_for_prompt(exchange_data) if exchange_data else "None",
            "trend_json": _json_for_prompt(trend),
            "terminology_thresholds_json": _json_for_prompt(TERMINOLOGY_THRESHOLDS),
            "basket_context_json": _json_for_prompt(basket_context),
            "module_basket_relevance_json": _json_for_prompt(module_relevance),
            "draft_text": draft_text,
        },
    )

     
    def _validate_qa(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        flags = result.get("flags")
        if not isinstance(flags, list):
            raise ValueError("flags must be a list")
        required = {
            "section",
            "claim",
            "issue_type",
            "severity",
            "details",
            "recommendation",
        }
        for index, flag in enumerate(flags):
            if not isinstance(flag, dict):
                raise ValueError("every QA flag must be an object")
            missing = sorted(required - set(flag))
            if missing:
                raise ValueError(f"flags[{index}] is missing: {', '.join(missing)}")
            if flag.get("severity") not in {"high", "medium", "low"}:
                raise ValueError(f"flags[{index}].severity is invalid")
            for field in required - {"severity"}:
                if not isinstance(flag.get(field), str):
                    raise ValueError(f"flags[{index}].{field} must be text")
        return _normalized_qa_flags(flags)

    traced = llm.generate_json(
        prompt=prompt,
        node="red_team",
        operation="market_monitor.red_team_review.v1",
        artifact_type="global",
        artifact_id="qa_review",
        correction_attempt=int(state.get("correction_attempts", 0) or 0),
        validator=_validate_qa,
    )
    flags = traced.value
    llm_calls = 1

    review = qa_review_from_state({**dict(state), "skeptic_flags": flags})
    return {
        "skeptic_flags": flags,
        "qa_review": review,
        "correction_targets": [],
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "red_team"
    }


# ============================================================================
# ROUTING & GRAPH BUILDER
# ============================================================================

MAX_CORRECTION_ATTEMPTS = 3


def node_prepare_correction(state: MarketReportState) -> dict:
    """Capture material QA targets without clearing the flags that explain them."""
    targets = _correction_targets(state.get("skeptic_flags") or [])
    return {
        "correction_targets": targets,
        "correction_attempts": int(state.get("correction_attempts") or 0) + 1,
        "current_node": "prepare_correction",
    }


def should_correct(state: MarketReportState) -> Literal["correct", "finish"]:
    """Determina se servono correzioni."""
    flags = _material_qa_flags(state.get("skeptic_flags", []))
    attempts = state.get("correction_attempts", 0)
    
    if flags and attempts < MAX_CORRECTION_ATTEMPTS:
        return "correct"
    return "finish"


def build_graph(on_step: Optional[OnStepCallback] = None):
    """Costruisce il grafo LangGraph per Market Monitor."""
    
    def wrap_node(node_name: str, fn):
        def wrapped(state: MarketReportState):
            state_dict = dict(state)
            if on_step is not None:
                on_step(node_name, state_dict)

            updates = fn(state)

            if on_step is not None:
                merged = dict(state_dict)
                if isinstance(updates, dict):
                    merged.update(updates)
                on_step(node_name, merged)

            return updates

        return wrapped

    graph = StateGraph(MarketReportState)
    
    # Add nodes
    graph.add_node("data_agent", wrap_node("data_agent", node_data_agent))
    graph.add_node("graph_designer", wrap_node("graph_designer", node_graph_designer))
    graph.add_node("news_retrieval", wrap_node("news_retrieval", node_news_retrieval))
    graph.add_node("event_mapper", wrap_node("event_mapper", node_event_mapper))
    graph.add_node("trend_analyst", wrap_node("trend_analyst", node_trend_analyst))
    graph.add_node("module_orchestrator", wrap_node("module_orchestrator", node_module_orchestrator))
    graph.add_node("highlights_drafter", wrap_node("highlights_drafter", node_highlights_drafter))
    graph.add_node("narrative_drafter", wrap_node("narrative_drafter", node_narrative_drafter))
    graph.add_node("red_team", wrap_node("red_team", node_red_team))
    graph.add_node("prepare_correction", wrap_node("prepare_correction", node_prepare_correction))
    
    # Set entry point
    graph.set_entry_point("data_agent")
    
    # Linear flow
    graph.add_edge("data_agent", "graph_designer")
    graph.add_edge("graph_designer", "news_retrieval")
    graph.add_edge("news_retrieval", "event_mapper")
    graph.add_edge("event_mapper", "trend_analyst")
    graph.add_edge("trend_analyst", "module_orchestrator")
    graph.add_edge("module_orchestrator", "highlights_drafter")
    graph.add_edge("highlights_drafter", "narrative_drafter")
    graph.add_edge("narrative_drafter", "red_team")
    graph.add_edge("prepare_correction", "module_orchestrator")
    
    # QA Loop
    graph.add_conditional_edges(
        "red_team",
        should_correct,
        {
            "correct": "prepare_correction",
            "finish": END
        }
    )
    
    return graph.compile()


# ============================================================================
# PUBLIC API
# ============================================================================

def run_report_generation(
    country: str,
    time_period: str,
    commodity_list: List[str],
    admin1_list: List[str],
    currency_code: str = "USD",
    enabled_modules: List[str] = None,
    basket_version_id: Optional[str] = None,
    basket_selection: Optional[Any] = None,
    previous_report_text: str = "",
    use_mock_data: bool = False,
    language: str = "auto",
    on_step: Optional[OnStepCallback] = None,
    run_id: Optional[str] = None,
    llm_trace_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> dict:
    """
    Entry point per la generazione del Market Monitor.
    
    Returns:
        Stato finale con report completo
    """
    if enabled_modules is None:
        enabled_modules = ["exchange_rate"]
    language_info = resolve_report_language(country, language)
    if basket_selection is not None and hasattr(basket_selection, "to_metadata"):
        basket_selection_payload = basket_selection.to_metadata()
    elif isinstance(basket_selection, Mapping):
        basket_selection_payload = dict(basket_selection)
    else:
        basket_selection_payload = None
    
    initial_state = create_initial_state(
        country=country,
        time_period=time_period,
        commodity_list=commodity_list,
        admin1_list=admin1_list,
        currency_code=currency_code,
        enabled_modules=enabled_modules,
        basket_version_id=basket_version_id,
        basket_selection=basket_selection_payload,
        previous_report_text=previous_report_text,
        use_mock_data=use_mock_data,
        language=language_info["language"],
        locale=language_info["locale"],
        language_source=language_info["language_source"],
        run_id=run_id,
    )
    
    agent = build_graph(on_step=on_step)
    with tracing_run(
        service="market-monitor",
        run_id=initial_state["run_id"],
        initial=initial_state.get("llm_diagnostics"),
        live=llm_trace_sink,
    ) as trace:
        try:
            result = agent.invoke(initial_state)
        except Exception:
            log_llm_run_summary(trace.snapshot())
            raise
        result["llm_diagnostics"] = trace.snapshot()
        log_llm_run_summary(result["llm_diagnostics"])
    for key in ("databridges_rows", "seerist_documents", "reliefweb_documents"):
        result.pop(key, None)
    result["language"] = language_info["language"]
    result["locale"] = language_info["locale"]
    result["language_source"] = language_info["language_source"]
    result["qa_review"] = normalize_qa_review(result)
    if result["qa_review"]["status"] == "completed_with_warnings":
        warning = t(
            language_info["language"],
            "warning.qa_unresolved",
            count=len(_material_qa_flags(result["qa_review"]["flags"])),
        )
        existing_warnings = list(result.get("warnings") or [])
        if warning not in existing_warnings:
            existing_warnings.append(warning)
        result["warnings"] = existing_warnings
    return result
