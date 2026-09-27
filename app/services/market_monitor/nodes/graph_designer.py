"""Node graph_designer: the report's charts (matplotlib), with the axis, overlay and basket-chart helpers only it
uses."""
from __future__ import annotations

import io
import re
import base64
import logging
from typing import List, Dict, Any, Mapping

import pandas as pd
import numpy as np

from ..i18n import format_decimal_value, format_month_label, localize_axis_label, t
from ..state import MarketReportState, _state_currency_code, _state_language
from ..text import _dedupe_text

logger = logging.getLogger(__name__)


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
