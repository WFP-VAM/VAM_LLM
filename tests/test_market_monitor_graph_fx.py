import io

import pandas as pd
import pytest
from types import SimpleNamespace

from app.shared.llm import LLMCallError, LLMClient, market_monitor_profile
from app.services.market_monitor.report_blocks import build_market_monitor_report_blocks
from app.services.market_monitor import graph as market_graph


def _state_with_frames(df_national, df_history=None, df_regional=None):
    return {
        "country": "South Sudan",
        "time_period": "2026-02",
        "currency_code": "SSP",
        "cache_metadata": {"currency_code": "SSP"},
        "time_series_data_national": df_national.to_json(date_format="iso"),
        "time_series_history_national": (
            df_history.to_json(date_format="iso") if df_history is not None else None
        ),
        "time_series_data_regional": (
            df_regional.to_json(date_format="iso") if df_regional is not None else None
        ),
        "data_statistics": {"commodities": {"Maize": {"yoy_change_pct": 10}}},
    }


def test_currency_axis_label_uses_resolved_code():
    assert market_graph._currency_axis_label("Cost", "CDF") == "Cost (CDF)"
    assert market_graph._fx_axis_label("CDF") == "CDF per 1 USD"
    assert market_graph._fuel_axis_label("CDF") == "CDF/Litre"


def test_history_overlay_values_skip_gracefully_with_short_history():
    target = pd.date_range("2026-01-01", periods=2, freq="MS")
    history = pd.DataFrame({"FoodBasket": [10.0]}, index=pd.DatetimeIndex(["2025-01-01"]))

    prior, five_year_mean, low, high, counts = market_graph._history_overlay_values(history, target, "FoodBasket")

    assert prior.loc[pd.Timestamp("2026-01-01")] == 10.0
    assert pd.isna(prior.loc[pd.Timestamp("2026-02-01")])
    assert pd.isna(five_year_mean.loc[pd.Timestamp("2026-01-01")])
    assert pd.isna(low.loc[pd.Timestamp("2026-01-01")])
    assert pd.isna(high.loc[pd.Timestamp("2026-01-01")])
    assert counts.loc[pd.Timestamp("2026-01-01")] == 1


def test_history_overlay_values_require_five_prior_same_month_values_for_five_year_band():
    target = pd.date_range("2026-01-01", periods=1, freq="MS")
    history = pd.DataFrame(
        {"FoodBasket": [10.0, 11.0, 12.0, 13.0, 14.0]},
        index=pd.DatetimeIndex(["2021-01-01", "2022-01-01", "2023-01-01", "2024-01-01", "2025-01-01"]),
    )

    prior, five_year_mean, low, high, counts = market_graph._history_overlay_values(history, target, "FoodBasket")

    assert prior.loc[pd.Timestamp("2026-01-01")] == 14.0
    assert five_year_mean.loc[pd.Timestamp("2026-01-01")] == 12.0
    assert low.loc[pd.Timestamp("2026-01-01")] == 10.0
    assert high.loc[pd.Timestamp("2026-01-01")] == 14.0
    assert counts.loc[pd.Timestamp("2026-01-01")] == 5


def test_graph_designer_renders_fx_chart_and_skips_blank_regional_chart():
    dates = pd.date_range("2025-02-01", periods=13, freq="MS")
    df_national = pd.DataFrame(
        {
            "FoodBasket": range(100, 113),
            "Maize": range(10, 23),
            "ExchangeRate": range(1000, 1013),
            "ExchangeRateUnofficial": range(1100, 1113),
        },
        index=dates,
    )
    history_dates = pd.date_range("2021-03-01", periods=60, freq="MS")
    df_history = pd.DataFrame(
        {
            "FoodBasket": range(60),
            "Maize": range(100, 160),
        },
        index=history_dates,
    )
    df_regional = pd.DataFrame(
        {
            "Date": [dates[-1], dates[-1]],
            "Region": ["No Data", "Zero"],
            "FoodBasket": [None, 0],
        }
    )

    result = market_graph.node_graph_designer(_state_with_frames(df_national, df_history, df_regional))

    assert "food_basket_trend" in result["visualizations"]
    assert "commodity_trends" in result["visualizations"]
    assert "exchange_rate_trend" in result["visualizations"]
    assert "regional_comparison" not in result["visualizations"]


def _phase5_basket_state(*, secondary_scope="national", included=True):
    dates = pd.date_range("2026-01-01", periods=2, freq="MS")
    national = pd.DataFrame({"FoodBasket": [100.0, 110.0]}, index=dates)
    basket_national = []
    for date, primary, secondary in zip(dates, (100.0, 110.0), (50.0, 55.0)):
        basket_national.append(
            {
                "Date": date.strftime("%Y-%m-%d"),
                "BasketRole": "primary",
                "BasketVersionId": "primary-v1",
                "BasketName": "MEB",
                "ScopeType": "national",
                "ScopeLabel": "National",
                "Region": None,
                "Cost": primary,
                "Complete": True,
            }
        )
        if secondary_scope == "national":
            basket_national.append(
                {
                    "Date": date.strftime("%Y-%m-%d"),
                    "BasketRole": "secondary",
                    "BasketVersionId": "secondary-v1",
                    "BasketName": "Pastoral basket",
                    "ScopeType": "national",
                    "ScopeLabel": "National",
                    "Region": None,
                    "Cost": secondary,
                    "Complete": True,
                }
            )
    basket_regional = []
    for date in dates:
        for region, primary, secondary in (("Region A", 90.0, 40.0), ("Region B", 120.0, 60.0)):
            basket_regional.append(
                {
                    "Date": date.strftime("%Y-%m-%d"),
                    "BasketRole": "primary",
                    "BasketVersionId": "primary-v1",
                    "BasketName": "MEB",
                    "ScopeType": "national",
                    "ScopeLabel": region,
                    "Region": region,
                    "Cost": primary,
                    "Complete": True,
                }
            )
            if included:
                basket_regional.append(
                    {
                        "Date": date.strftime("%Y-%m-%d"),
                        "BasketRole": "secondary",
                        "BasketVersionId": "secondary-v1",
                        "BasketName": "Pastoral basket",
                        "ScopeType": secondary_scope,
                        "ScopeLabel": region,
                        "Region": region,
                        "Cost": secondary,
                        "Complete": True,
                    }
                )
    return {
        **_state_with_frames(national),
        "language": "en",
        "include_secondary_basket": included,
        "food_basket": {},
        "food_baskets": {
            "primary": {
                "basket_role": "primary",
                "basket_version_id": "primary-v1",
                "basket_name": "MEB",
                "scope_type": "national",
                "regions": [],
            },
            "secondary": (
                {
                    "basket_role": "secondary",
                    "basket_version_id": "secondary-v1",
                    "basket_name": "Pastoral basket",
                    "scope_type": secondary_scope,
                    "regions": ["Region B", "Region A"] if secondary_scope == "selected_regions" else [],
                }
                if included
                else None
            ),
        },
        "basket_series_national": basket_national,
        "basket_series_regional": basket_regional,
        "basket_statistics": {
            "primary": {"applicable_regions": ["Region A", "Region B"]},
            "secondary": {"applicable_regions": ["Region B", "Region A"]} if included else None,
        },
    }


def test_graph_designer_emits_separate_role_charts_and_exact_primary_aliases(monkeypatch):
    encoded = []

    def fake_encode(plt):
        value = f"image-{len(encoded) + 1}"
        encoded.append(value)
        plt.close()
        return value

    monkeypatch.setattr(market_graph, "_encode_matplotlib_figure", fake_encode)
    result = market_graph.node_graph_designer(_phase5_basket_state())
    figures = result["visualizations"]

    assert figures["food_basket_trend"] == figures["food_basket_trend_primary"]
    assert figures["regional_comparison"] == figures["regional_comparison_primary"]
    assert "food_basket_trend_secondary" in figures
    assert "regional_comparison_secondary" in figures
    assert "food_basket_trend_combined" not in figures


def test_selected_region_trend_keeps_individual_lines_and_target_region_order():
    state = _phase5_basket_state(secondary_scope="selected_regions")
    for row in state["basket_series_regional"]:
        if row["BasketRole"] == "secondary" and row["Region"] == "Region A" and row["Date"] == "2026-01-01":
            row["Complete"] = False
            row["Cost"] = 999.0
    state["basket_series_regional"].append(
        {
            "Date": "2026-03-01",
            "BasketRole": "secondary",
            "BasketVersionId": "secondary-v1",
            "BasketName": "Pastoral basket",
            "ScopeType": "selected_regions",
            "ScopeLabel": "Region B",
            "Region": "Region B",
            "Cost": 999.0,
            "Complete": True,
        }
    )
    national = pd.read_json(io.StringIO(state["time_series_data_national"]))
    national = market_graph._normalise_time_index(national)
    basket_national = market_graph._basket_series_frame(state["basket_series_national"])
    basket_regional = market_graph._basket_series_frame(state["basket_series_regional"])
    trend = market_graph._basket_trend_chart_data(
        state,
        "secondary",
        national,
        basket_national,
        basket_regional,
    )
    target = market_graph._basket_regional_target_data(state, "secondary", basket_regional)

    assert trend["scope_type"] == "selected_regions"
    assert [label for label, _series in trend["series"]] == ["Region B", "Region A"]
    assert pd.isna(dict(trend["series"])["Region A"].loc[pd.Timestamp("2026-01-01")])
    assert target["regions"] == ["Region B", "Region A"]
    assert target["costs"] == [60.0, 40.0]


def test_excluded_secondary_never_emits_secondary_chart_data():
    state = _phase5_basket_state(included=False)
    national = market_graph._normalise_time_index(pd.read_json(io.StringIO(state["time_series_data_national"])))

    assert market_graph._basket_trend_chart_data(
        state,
        "secondary",
        national,
        market_graph._basket_series_frame(state["basket_series_national"]),
        market_graph._basket_series_frame(state["basket_series_regional"]),
    ) == {}


def test_data_agent_uses_captured_two_basket_snapshots_and_emits_role_results(monkeypatch):
    captured = {}
    dates = pd.date_range("2024-02-01", periods=13, freq="MS")
    national = pd.DataFrame({"Maize": [10.0] * 13, "Beans": [5.0] * 13, "FoodBasket": [20.0] * 13}, index=dates)
    regional = pd.DataFrame({"Date": [dates[-1]], "Region": ["Juba"], "FoodBasket": [20.0]})
    basket_series_national = pd.DataFrame(
        {
            "Date": [dates[-1]],
            "BasketRole": ["primary"],
            "BasketVersionId": ["primary-v1"],
            "BasketName": ["MEB"],
            "ScopeType": ["national"],
            "ScopeLabel": ["National"],
            "Region": [None],
            "Cost": [20.0],
            "SelectedComponentCount": [1],
            "AvailableComponentCount": [1],
            "MissingComponentNames": [[]],
            "Complete": [True],
        }
    )
    basket_series_regional = basket_series_national.assign(
        BasketRole="secondary",
        BasketVersionId="secondary-v1",
        BasketName="Urban basket",
        ScopeType="selected_regions",
        ScopeLabel="Juba",
        Region="Juba",
        Cost=5.0,
    )
    role_stats = {
        "primary": {"current_cost": 20.0, "current_price": 20.0, "current_complete": True},
        "secondary": {"current_cost": 5.0, "current_price": 5.0, "current_complete": True},
    }

    def fake_resolve(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            df_national=national,
            df_regional=regional,
            df_history_national=pd.DataFrame(),
            raw_rows=pd.DataFrame(),
            cache_metadata={"cache_version_id": "cache-v1", "currency_code": "SSP"},
            warnings=[],
            exchange_rate_data=None,
            fuel_energy_data=None,
            livestock_animal_products_data=None,
            labour_market_data=None,
            basket_series_national=basket_series_national,
            basket_series_regional=basket_series_regional,
            basket_statistics=role_stats,
            basket_calculation_specs=[{"basket_role": "primary"}, {"basket_role": "secondary"}],
            basket_applicable_regions={"primary": [], "secondary": ["Juba"]},
        )

    monkeypatch.setattr(market_graph, "resolve_report_price_data", fake_resolve)
    state = {
        "country": "South Sudan",
        "time_period": "2025-02",
        "language": "en",
        "commodity_list": ["Salt"],
        "admin1_list": ["Juba"],
        "currency_code": "SSP",
        "enabled_modules": [],
        "use_mock_data": False,
        "include_secondary_basket": True,
        "food_baskets": {
            "primary": {
                "basket_version_id": "primary-v1",
                "basket_role": "primary",
                "basket_name": "MEB",
                "scope_type": "national",
                "items": [{"commodity_id": 1, "commodity_name": "Maize", "weight_quantity": 2}],
            },
            "secondary": {
                "basket_version_id": "secondary-v1",
                "basket_role": "secondary",
                "basket_name": "Urban basket",
                "scope_type": "selected_regions",
                "regions": ["Juba"],
                "items": [{"commodity_id": 2, "commodity_name": "Beans", "weight_quantity": 1}],
            },
        },
        "food_basket": {},
    }

    result = market_graph.node_data_agent(state)

    assert [spec.role for spec in captured["basket_specs"]] == ["primary", "secondary"]
    assert captured["commodities"] == ["Maize", "Beans", "Salt"]
    assert result["basket_statistics"] == role_stats
    assert result["data_statistics"]["food_basket"] == role_stats["primary"]
    assert result["basket_series_national"][0]["BasketRole"] == "primary"
    assert result["basket_series_regional"][0]["BasketRole"] == "secondary"


def test_graph_designer_applies_commodity_overlays_only_to_single_commodity_pages(monkeypatch):
    calls = []

    def fake_overlay(_ax, _history, _target_index, column, **kwargs):
        calls.append((column, kwargs.get("label_prefix")))

    monkeypatch.setattr(market_graph, "_plot_history_overlays", fake_overlay)
    dates = pd.date_range("2025-02-01", periods=13, freq="MS")
    history_dates = pd.date_range("2020-02-01", periods=73, freq="MS")
    df_history = pd.DataFrame(
        {
            "FoodBasket": range(73),
            "Maize": range(100, 173),
            "Sorghum": range(200, 273),
        },
        index=history_dates,
    )
    df_multi = pd.DataFrame(
        {
            "FoodBasket": range(100, 113),
            "Maize": range(10, 23),
            "Sorghum": range(20, 33),
        },
        index=dates,
    )

    market_graph.node_graph_designer(_state_with_frames(df_multi, df_history))

    assert calls == [("FoodBasket", None)]

    calls.clear()
    df_single = df_multi.drop(columns=["Sorghum"])

    market_graph.node_graph_designer(_state_with_frames(df_single, df_history))

    assert calls == [("FoodBasket", None), ("Maize", "Maize")]


def test_graph_designer_renders_fuel_chart_and_applies_overlays_for_two_or_fewer_series(monkeypatch):
    calls = []

    def fake_overlay(_ax, _history, _target_index, column, **kwargs):
        calls.append((column, kwargs.get("label_prefix")))

    monkeypatch.setattr(market_graph, "_plot_history_overlays", fake_overlay)
    dates = pd.date_range("2025-06-01", periods=13, freq="MS")
    df_national = pd.DataFrame(
        {
            "FoodBasket": range(100, 113),
            "Fuel (diesel)": range(200, 213),
            "Fuel (petrol/gasoline)": range(300, 313),
        },
        index=dates,
    )
    df_history = pd.DataFrame(
        {
            "FoodBasket": range(73),
            "Fuel (diesel)": range(1000, 1073),
            "Fuel (petrol/gasoline)": range(2000, 2073),
        },
        index=pd.date_range("2020-06-01", periods=73, freq="MS"),
    )
    state = _state_with_frames(df_national, df_history)
    state["fuel_energy_data"] = {
        "available": True,
        "series": [
            {"kind": "diesel", "label": "Diesel", "column_name": "Fuel (diesel)"},
            {"kind": "petrol_gasoline", "label": "Petrol/Gasoline", "column_name": "Fuel (petrol/gasoline)"},
        ],
    }

    result = market_graph.node_graph_designer(state)

    assert "fuel_prices" in result["visualizations"]
    assert ("Fuel (diesel)", "Diesel") in calls
    assert ("Fuel (petrol/gasoline)", "Petrol/Gasoline") in calls


def test_graph_designer_skips_fuel_overlays_when_chart_has_more_than_two_series(monkeypatch):
    calls = []

    def fake_overlay(_ax, _history, _target_index, column, **kwargs):
        calls.append((column, kwargs.get("label_prefix")))

    monkeypatch.setattr(market_graph, "_plot_history_overlays", fake_overlay)
    dates = pd.date_range("2025-06-01", periods=13, freq="MS")
    df_national = pd.DataFrame(
        {
            "FoodBasket": range(100, 113),
            "Fuel (diesel)": range(200, 213),
            "Fuel (petrol/gasoline)": range(300, 313),
            "Fuel (other)": range(400, 413),
        },
        index=dates,
    )
    df_history = pd.DataFrame({"FoodBasket": range(73)}, index=pd.date_range("2020-06-01", periods=73, freq="MS"))
    state = _state_with_frames(df_national, df_history)
    state["fuel_energy_data"] = {
        "available": True,
        "series": [
            {"kind": "diesel", "label": "Diesel", "column_name": "Fuel (diesel)"},
            {"kind": "petrol_gasoline", "label": "Petrol/Gasoline", "column_name": "Fuel (petrol/gasoline)"},
            {"kind": "other", "label": "Other", "column_name": "Fuel (other)"},
        ],
    }

    result = market_graph.node_graph_designer(state)

    assert "fuel_prices" in result["visualizations"]
    assert all(not column.startswith("Fuel") for column, _label in calls)


def test_graph_designer_renders_livestock_chart_and_applies_absolute_overlays(monkeypatch):
    calls = []

    def fake_overlay(_ax, _history, _target_index, column, **kwargs):
        calls.append((column, kwargs.get("label_prefix")))

    monkeypatch.setattr(market_graph, "_plot_history_overlays", fake_overlay)
    dates = pd.date_range("2025-06-01", periods=13, freq="MS")
    df_national = pd.DataFrame(
        {
            "FoodBasket": range(100, 113),
            "Animal Products - Meat (beef)": range(200, 213),
            "Animal Products - Meat (pork)": range(300, 313),
        },
        index=dates,
    )
    df_history = pd.DataFrame(
        {
            "FoodBasket": range(73),
            "Animal Products - Meat (beef)": range(1000, 1073),
            "Animal Products - Meat (pork)": range(2000, 2073),
        },
        index=pd.date_range("2020-06-01", periods=73, freq="MS"),
    )
    state = _state_with_frames(df_national, df_history)
    state["livestock_animal_products_data"] = {
        "available": True,
        "chart": {
            "mode": "absolute",
            "axis_label": "CDF/KG",
            "series": [
                {"column_name": "Animal Products - Meat (beef)", "label": "Meat (beef)", "unit": "KG"},
                {"column_name": "Animal Products - Meat (pork)", "label": "Meat (pork)", "unit": "KG"},
            ],
        },
        "series": [],
    }

    result = market_graph.node_graph_designer(state)

    assert "livestock_animal_products" in result["visualizations"]
    assert ("Animal Products - Meat (beef)", "Meat (beef)") in calls
    assert ("Animal Products - Meat (pork)", "Meat (pork)") in calls


def test_graph_designer_renders_livestock_indexed_chart_without_mixed_unit_overlays(monkeypatch):
    calls = []

    def fake_overlay(_ax, _history, _target_index, column, **kwargs):
        calls.append((column, kwargs.get("label_prefix")))

    monkeypatch.setattr(market_graph, "_plot_history_overlays", fake_overlay)
    dates = pd.date_range("2025-06-01", periods=13, freq="MS")
    df_national = pd.DataFrame(
        {
            "FoodBasket": range(100, 113),
            "Livestock - Livestock (Goat)": range(1000, 1013),
            "Animal Products - Milk (camel)": range(50, 63),
            "Animal Products - Meat (goat)": range(200, 213),
        },
        index=dates,
    )
    state = _state_with_frames(df_national, pd.DataFrame({"FoodBasket": range(73)}, index=pd.date_range("2020-06-01", periods=73, freq="MS")))
    state["livestock_animal_products_data"] = {
        "available": True,
        "chart": {
            "mode": "indexed",
            "axis_label": "Index (first month = 100)",
            "series": [
                {"column_name": "Livestock - Livestock (Goat)", "label": "Livestock (Goat)", "unit": "Head"},
                {"column_name": "Animal Products - Milk (camel)", "label": "Milk (camel)", "unit": "L"},
                {"column_name": "Animal Products - Meat (goat)", "label": "Meat (goat)", "unit": "KG"},
            ],
        },
        "series": [],
    }

    result = market_graph.node_graph_designer(state)

    assert "livestock_animal_products" in result["visualizations"]
    assert all(not column.startswith("Animal Products") and not column.startswith("Livestock") for column, _ in calls)


def test_graph_designer_renders_labour_market_chart(monkeypatch):
    calls = []

    def fake_overlay(_ax, _history, _target_index, column, **kwargs):
        calls.append((column, kwargs.get("label_prefix")))

    monkeypatch.setattr(market_graph, "_plot_history_overlays", fake_overlay)
    dates = pd.date_range("2025-06-01", periods=13, freq="MS")
    df_national = pd.DataFrame(
        {
            "FoodBasket": range(100, 113),
            "Labour - Purchasing power (Maize)": [20 + i * 0.1 for i in range(13)],
            "Labour - Casual wage": range(300, 313),
        },
        index=dates,
    )
    df_history = pd.DataFrame(
        {
            "FoodBasket": range(73),
            "Labour - Purchasing power (Maize)": [10 + i * 0.1 for i in range(73)],
            "Labour - Casual wage": range(1000, 1073),
        },
        index=pd.date_range("2020-06-01", periods=73, freq="MS"),
    )
    state = _state_with_frames(df_national, df_history)
    state["labour_market_data"] = {
        "available": True,
        "chart": {
            "mode": "purchasing_power",
            "axis_label": "kg of Maize per day's wage",
            "series": [
                {"column_name": "Labour - Purchasing power (Maize)", "label": "Purchasing power", "axis": "primary"},
                {"column_name": "Labour - Casual wage", "label": "Casual wage", "axis": "secondary", "axis_label": "AFN/day"},
            ],
        },
        "series": [],
    }

    result = market_graph.node_graph_designer(state)

    assert "labour_market" in result["visualizations"]
    assert ("Labour - Purchasing power (Maize)", "Purchasing power") in calls


def test_market_monitor_report_blocks_use_human_module_heading():
    blocks = build_market_monitor_report_blocks(
        {
            "country": "South Sudan",
            "time_period": "2026-02",
            "module_sections": {
                "exchange_rate": "Exchange rate narrative.",
                "fuel_energy": "Fuel narrative.",
                "livestock_animal_products": "Animal narrative.",
                "labour_market": "Labour narrative.",
            },
            "visualizations": {
                "fuel_prices": "base64",
                "livestock_animal_products": "base64",
                "labour_market": "base64",
            },
        }
    )

    headings = [block.text for block in blocks if block.type == "heading"]
    figures = [block.figure_id for block in blocks if block.type == "figure"]

    assert "Exchange Rate Analysis" in headings
    assert "Fuel & Energy" in headings
    assert "Livestock & Animal Products" in headings
    assert "Labour Market" in headings
    assert "EXCHANGE_RATE Analysis" not in headings
    assert "FUEL_ENERGY Analysis" not in headings
    assert "fuel_prices" in figures
    assert "livestock_animal_products" in figures
    assert "labour_market" in figures


@pytest.mark.parametrize(
    ("module", "state"),
    [
        (
            market_graph.FuelEnergyModule(),
            {
                "fuel_energy_data": {
                    "available": True,
                    "unit": "SOS/Litre",
                    "series": [{"kind": "diesel", "label": "Diesel", "current_price": 1120}],
                    "regional_disparities": [],
                }
            },
        ),
        (
            market_graph.LivestockAnimalProductsModule(),
            {
                "livestock_animal_products_data": {
                    "available": True,
                    "series": [{"group": "live_animal", "label": "Livestock (Goat)", "current_price": 1200}],
                    "regional_disparities": [],
                }
            },
        ),
        (
            market_graph.LabourMarketModule(),
            {
                "labour_market_data": {
                    "available": True,
                    "series": [{"kind": "casual_unskilled", "label": "Casual wage", "current_wage": 420}],
                    "purchasing_power": {"current_kg": 19.09, "staple_name": "Maize"},
                }
            },
        ),
    ],
)
def test_optional_module_llm_failure_interrupts_instead_of_using_fallback(module, state):
    class FailingProvider:
        def generate(self, *_args, **_kwargs):
            raise RuntimeError("offline")

    call_state = {
        "run_id": "market-module-failure",
        "country": "Testland",
        "time_period": "2026-06",
        **state,
    }
    with pytest.raises(LLMCallError) as caught:
        module.generate_section(call_state, LLMClient(market_monitor_profile(), provider=FailingProvider()))
    assert caught.value.failure_code == "llm_transport_error"
    assert caught.value.node == "module_orchestrator"


def test_exchange_rate_module_uses_existing_databridges_data_without_te(monkeypatch):
    module = market_graph.ExchangeRateModule(api_key=None)

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("TradingEconomics should not be called when DataBridges FX exists")

    monkeypatch.setattr(module, "_fetch_historical_series", fail_fetch)
    existing = {
        "currency_code": "SSP",
        "current_rate": 1000,
        "monthly_change_pct": 1.0,
        "yearly_change_pct": 5.0,
        "trend": "stable",
    }

    assert module.fetch_data({"exchange_rate_data": existing}) == {"exchange_rate_data": existing}


def test_exchange_rate_module_falls_back_to_te_when_state_has_no_fx(monkeypatch):
    module = market_graph.ExchangeRateModule(api_key="key")
    dates = pd.date_range("2024-01-01", "2025-02-28", freq="D")
    te_df = pd.DataFrame({"Close": range(1, len(dates) + 1)}, index=dates)
    monkeypatch.setattr(module, "_fetch_historical_series", lambda **_kwargs: te_df)

    result = module.fetch_data(
        {
            "exchange_rate_data": None,
            "currency_code": "SSP",
            "country": "South Sudan",
            "time_period": "2025-02",
        }
    )

    assert result["exchange_rate_data"]["current_rate"] == float(len(dates))
    assert result["exchange_rate_data"]["currency_code"] == "SSP"


def test_exchange_rate_module_skip_warning_when_no_databridges_fx_or_te(monkeypatch):
    monkeypatch.delenv("TE_API_KEY", raising=False)

    result = market_graph.node_module_orchestrator(
        {
            "enabled_modules": ["exchange_rate"],
            "exchange_rate_data": None,
            "currency_code": "SSP",
            "country": "South Sudan",
            "time_period": "2026-02",
            "llm_calls": 0,
        }
    )

    assert result["module_sections"] == {}
    assert any("no exchange-rate source was available" in warning for warning in result["warnings"])
