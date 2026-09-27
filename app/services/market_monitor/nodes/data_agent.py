"""Node data_agent: the price data, food basket statistics and national and regional series of the report."""
from __future__ import annotations

import json
import random
import logging
from typing import List, Dict, Any, Mapping

import pandas as pd
import numpy as np

from ..data_loader import calculate_statistics_from_csv, resolve_report_price_data
from ..basket_calculation import BasketCalculationSpec
from ..food_basket import get_active_basket_for_report
from ..i18n import t
from ..state import MarketReportState, _state_language
from ..text import _dedupe_text

logger = logging.getLogger(__name__)


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
