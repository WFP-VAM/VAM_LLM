from datetime import date, datetime, timezone
from importlib import import_module

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.shared import async_runs
from app.services.market_monitor import data_loader
from app.services.price_cache.schemas import (
    CacheRefreshSummary,
    CacheStatus,
    CommodityRecord,
    CountryAvailability,
    CountryMetadata,
    CountryRecord,
    CountryRefreshStatus,
    CurrencyRecord,
    MarketRecord,
    UnitRecord,
)


market_router = import_module("app.services.market_monitor.router")


class FakeBasketSelection:
    def __init__(self, *, include_secondary=True):
        self.primary_basket_version_id = "primary-v1"
        self.secondary_basket_version_id = "secondary-v1" if include_secondary else None
        self.secondary_basket_included = include_secondary

    def food_baskets_dict(self):
        return {
            "primary": {
                "basket_version_id": self.primary_basket_version_id,
                "basket_role": "primary",
                "basket_name": "MEB",
                "items": [],
            },
            "secondary": (
                {
                    "basket_version_id": self.secondary_basket_version_id,
                    "basket_role": "secondary",
                    "basket_name": "Pastoral Basket",
                    "items": [],
                }
                if self.secondary_basket_included
                else None
            ),
        }

    def to_metadata(self):
        return {
            "primary_basket_version_id": self.primary_basket_version_id,
            "secondary_basket_version_id": self.secondary_basket_version_id,
            "secondary_basket_included": self.secondary_basket_included,
            "food_baskets": self.food_baskets_dict(),
        }


class FakeRepo:
    def get_cache_status(self):
        return CacheStatus(
            has_active_cache=True,
            active_version_id="11111111-1111-1111-1111-111111111111",
            status="partial_active",
            active_country_count=2,
            rows_prices=10,
            validation_summary={
                "warnings": [
                    {
                        "scope": "units",
                        "warning": "CommodityUnits/List could not be fetched; units were derived.",
                    }
                ]
            },
        )

    def get_active_version_id_for_country(self, country_iso3):
        if str(country_iso3).upper() == "SSD":
            return "11111111-1111-1111-1111-111111111111"
        return None

    def list_countries(self):
        return [
            CountryRecord(
                cache_version_id="11111111-1111-1111-1111-111111111111",
                country_iso3="SSD",
                country_name="South Sudan",
                currency_code="SSP",
                currency_name="South Sudanese Pound",
                latest_price_date=date(2025, 2, 1),
            )
        ]

    def get_country_availability(self, country_iso3):
        if str(country_iso3).upper() != "SSD":
            return None
        return CountryAvailability(
            cache_version_id="11111111-1111-1111-1111-111111111111",
            country_iso3="SSD",
            date_start=date(2025, 1, 1),
            date_end=date(2025, 2, 1),
            latest_price_date=date(2025, 2, 1),
            priced_commodity_ids=[1, 2],
            admin1_names=["Central Equatoria", "Western Bahr el Ghazal"],
            units=[
                UnitRecord(
                    cache_version_id="11111111-1111-1111-1111-111111111111",
                    commodity_unit_id=100,
                    commodity_unit_name="kg",
                )
            ],
        )

    def get_country_metadata(self, country_iso3):
        if str(country_iso3).upper() != "SSD":
            return None
        version_id = "11111111-1111-1111-1111-111111111111"
        return CountryMetadata(
            country=CountryRecord(
                cache_version_id=version_id,
                country_iso3="SSD",
                country_name="South Sudan",
                currency_code="SSP",
                currency_name="South Sudanese Pound",
                latest_price_date=date(2025, 2, 1),
            ),
            commodities=[
                CommodityRecord(version_id, "SSD", 1, "Maize", 100, "kg", "Cereals"),
                CommodityRecord(version_id, "SSD", 2, "Beans", 100, "kg", "Pulses"),
                CommodityRecord(version_id, "SSD", 3, "Rice", 100, "kg", "Cereals"),
            ],
            units=[UnitRecord(version_id, 100, "kg")],
            markets=[
                MarketRecord(version_id, "SSD", 10, "Juba", "Central Equatoria"),
                MarketRecord(version_id, "SSD", 11, "Wau", "Western Bahr el Ghazal"),
            ],
            currencies=[CurrencyRecord(version_id, 200, "SSP", "South Sudanese Pound")],
        )

    def list_cache_refreshes(self, *, limit=20):
        return [
            CacheRefreshSummary(
                cache_version_id="11111111-1111-1111-1111-111111111111",
                status="partial_active",
                refresh_type="weekly_full",
                started_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
                rows_prices=10,
            )
        ]

    def get_cache_refresh(self, cache_version_id):
        if cache_version_id == "missing":
            return None
        return CacheRefreshSummary(
            cache_version_id=cache_version_id,
            status="partial_active",
            refresh_type="weekly_full",
            started_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
            rows_prices=10,
            countries=[
                CountryRefreshStatus(
                    cache_version_id=cache_version_id,
                    country_iso3="SSD",
                    status="success",
                    rows_prices=10,
                )
            ],
        )


def _client(monkeypatch):
    app = FastAPI()
    app.include_router(market_router.router)
    monkeypatch.setattr(market_router, "_get_price_cache_repository", lambda: FakeRepo())
    monkeypatch.setattr(data_loader, "_get_price_cache_repository", lambda: FakeRepo())
    data_loader.reset_market_monitor_caches_for_tests()
    return TestClient(app)


def test_cache_status_endpoint(monkeypatch):
    client = _client(monkeypatch)

    response = client.get("/cache/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial_active"
    assert payload["active_country_count"] == 2
    assert payload["warnings"] == []
    assert payload["operator_warnings"][0].startswith("CommodityUnits/List")


def test_info_endpoint_lists_fuel_energy_module(monkeypatch):
    client = _client(monkeypatch)

    response = client.get("/info")

    assert response.status_code == 200
    payload = response.json()
    module_ids = {item["id"] for item in payload["available_modules"]}
    assert {"exchange_rate", "fuel_energy"}.issubset(module_ids)
    enabled_modules = next(item for item in payload["inputs"] if item["name"] == "enabled_modules")
    assert "fuel_energy" in enabled_modules["options"]
    assert payload["basket_visualizations"] == {
        "canonical": [
            "food_basket_trend_primary",
            "food_basket_trend_secondary",
            "regional_comparison_primary",
            "regional_comparison_secondary",
        ],
        "primary_aliases": {
            "food_basket_trend": "food_basket_trend_primary",
            "regional_comparison": "regional_comparison_primary",
        },
        "combined_chart": False,
    }
    assert "qa_review" in payload["outputs"]


def test_cache_refresh_history_endpoints(monkeypatch):
    client = _client(monkeypatch)

    list_response = client.get("/cache/refreshes")
    detail_response = client.get("/cache/refreshes/11111111-1111-1111-1111-111111111111")
    missing_response = client.get("/cache/refreshes/missing")

    assert list_response.status_code == 200
    assert list_response.json()["refreshes"][0]["status"] == "partial_active"
    assert detail_response.status_code == 200
    assert detail_response.json()["countries"][0]["country_iso3"] == "SSD"
    assert missing_response.status_code == 404


def test_countries_endpoint_reads_cached_countries(monkeypatch):
    client = _client(monkeypatch)

    response = client.get("/countries")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cache_status"]["source"] == "PriceCache"
    assert payload["warnings"] == []
    assert payload["operator_warnings"][0].startswith("CommodityUnits/List")
    assert payload["countries"] == [
        {
            "name": "South Sudan",
            "iso3": "SSD",
            "currency_code": "SSP",
            "currency_name": "South Sudanese Pound",
            "has_data": True,
            "source": "PriceCache",
            "cache_version_id": "11111111-1111-1111-1111-111111111111",
            "latest_cached_date": "2025-02-01",
            "date_range": {"start": "2025-01-01", "end": "2025-02-01"},
        }
    ]


def test_country_metadata_endpoint_reads_cache_without_databridges(monkeypatch):
    def fail_if_live_client_is_used():
        raise AssertionError("DataBridges should not be called by cache-first metadata.")

    monkeypatch.setattr(data_loader, "DataBridgesClientAdapter", fail_if_live_client_is_used)
    client = _client(monkeypatch)

    response = client.get("/countries/South%20Sudan/metadata")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "PriceCache"
    assert payload["cache_version_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["latest_cached_date"] == "2025-02-01"
    assert payload["date_range"] == {"start": "2025-01-01", "end": "2025-02-01"}
    assert [item["name"] for item in payload["commodities"]] == ["Beans", "Maize"]
    assert all(item["priced"] is True for item in payload["commodities"])
    assert payload["unpriced_commodities"] == [
        {
            "id": 3,
            "name": "Rice",
            "category": "Cereals",
            "unit_id": 100,
            "unit": "kg",
            "unit_name": "kg",
            "priced": False,
        }
    ]
    assert payload["units"] == [
        {"id": 100, "name": "kg", "conversion_to_kg_l": None, "source": "cached_units"}
    ]
    assert payload["markets"][0]["market_name"] == "Juba"


def test_country_reportable_months_endpoint(monkeypatch):
    monkeypatch.setattr(
        data_loader,
        "get_reportable_months",
        lambda country: {
            "country": country,
            "iso3": "SSD",
            "cache_version_id": "cache-v1",
            "basket_version_id": "basket-v1",
            "reportable_months": ["2025-01", "2025-02"],
            "latest_reportable_month": "2025-02",
            "latest_cached_month": "2025-03",
            "latest_cached_real_month": "2025-02",
            "missing_by_month": {"2025-03": ["Beans"]},
            "warnings": [],
        },
    )
    client = _client(monkeypatch)

    response = client.get("/countries/South%20Sudan/reportable-months")

    assert response.status_code == 200
    assert response.json()["latest_reportable_month"] == "2025-02"
    assert response.json()["missing_by_month"] == {"2025-03": ["Beans"]}


def test_country_reportable_months_forwards_two_basket_region_selection(monkeypatch):
    captured = {}

    def fake_reportable(country, **kwargs):
        captured.update({"country": country, **kwargs})
        return {
            "country": country,
            "iso3": "SSD",
            "reportable_months": ["2025-01"],
            "latest_reportable_month": "2025-01",
            "primary_reportable_months": ["2025-01", "2025-02"],
            "secondary_reportable_months": ["2025-01"],
            "joint_reportable_months": ["2025-01"],
        }

    monkeypatch.setattr(data_loader, "get_reportable_months", fake_reportable)
    client = _client(monkeypatch)

    response = client.get(
        "/countries/South%20Sudan/reportable-months",
        params=[
            ("primary_basket_version_id", "primary-v1"),
            ("include_secondary_basket", "true"),
            ("secondary_basket_version_id", "secondary-v1"),
            ("admin1_list", "Juba"),
            ("admin1_list", "Wau"),
        ],
    )

    assert response.status_code == 200
    assert captured == {
        "country": "South Sudan",
        "basket_version_id": None,
        "primary_basket_version_id": "primary-v1",
        "include_secondary_basket": True,
        "secondary_basket_version_id": "secondary-v1",
        "admin1_list": ["Juba", "Wau"],
    }
    assert response.json()["joint_reportable_months"] == ["2025-01"]


def test_country_reportable_months_rejects_conflicting_primary_aliases(monkeypatch):
    client = _client(monkeypatch)

    response = client.get(
        "/countries/South%20Sudan/reportable-months",
        params={"basket_version_id": "old", "primary_basket_version_id": "new"},
    )

    assert response.status_code == 422
    assert "must reference the same" in str(response.json()["detail"])


def test_country_reportable_months_refresh_endpoint(monkeypatch):
    captured = {}

    def fake_refresh(country, *, basket_version_id=None):
        captured["country"] = country
        captured["basket_version_id"] = basket_version_id
        return {
            "country": country,
            "iso3": "SSD",
            "status": "updated",
            "source_cache_version_id": "cache-v1",
            "new_cache_version_id": "cache-v2",
            "checked_start_month": "2025-03",
            "checked_end_month": "2025-03",
            "months_checked": ["2025-03"],
            "latest_reportable_month_before": "2025-02",
            "latest_reportable_month_after": "2025-03",
            "new_reportable_months": ["2025-03"],
            "rows_fetched": 2,
            "rows_real": 2,
            "rows_saved": 2,
            "rows_skipped_existing": 0,
            "excluded_non_real_rows": 0,
            "excluded_future_rows": 0,
            "deduplicated_rows": 0,
            "missing_by_month": {},
            "warnings": [],
        }

    monkeypatch.setattr(data_loader, "refresh_reportable_months_from_databridges", fake_refresh)
    client = _client(monkeypatch)

    response = client.post(
        "/countries/South%20Sudan/reportable-months/refresh",
        json={"basket_version_id": "basket-v1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert captured == {"country": "South Sudan", "basket_version_id": "basket-v1"}


def test_country_reportable_months_refresh_forwards_role_and_region_selection(monkeypatch):
    captured = {}

    def fake_refresh(country, **kwargs):
        captured.update({"country": country, **kwargs})
        return {"country": country, "iso3": "SSD", "status": "no_update", "warnings": []}

    monkeypatch.setattr(data_loader, "refresh_reportable_months_from_databridges", fake_refresh)
    client = _client(monkeypatch)

    response = client.post(
        "/countries/South%20Sudan/reportable-months/refresh",
        json={
            "primary_basket_version_id": "primary-v1",
            "include_secondary_basket": True,
            "secondary_basket_version_id": "secondary-v1",
            "admin1_list": ["Juba"],
        },
    )

    assert response.status_code == 200
    assert captured == {
        "country": "South Sudan",
        "basket_version_id": None,
        "primary_basket_version_id": "primary-v1",
        "include_secondary_basket": True,
        "secondary_basket_version_id": "secondary-v1",
        "admin1_list": ["Juba"],
    }


def test_country_reportable_months_refresh_endpoint_returns_stale_basket_conflict(monkeypatch):
    def fake_refresh(_country, *, basket_version_id=None):
        raise market_router.BasketVersionConflict("refresh basket")

    monkeypatch.setattr(data_loader, "refresh_reportable_months_from_databridges", fake_refresh)
    client = _client(monkeypatch)

    response = client.post(
        "/countries/South%20Sudan/reportable-months/refresh",
        json={"basket_version_id": "old"},
    )

    assert response.status_code == 409
    assert "refresh basket" in response.json()["detail"]


def test_country_basket_endpoint_returns_needs_setup(monkeypatch):
    monkeypatch.setattr(
        market_router,
        "get_country_basket_response",
        lambda country: {
            "country": country,
            "iso3": "SSD",
            "needs_setup": True,
            "active_basket": None,
        },
    )
    client = _client(monkeypatch)

    response = client.get("/countries/South%20Sudan/basket")

    assert response.status_code == 200
    assert response.json()["needs_setup"] is True


def test_country_basket_save_endpoint_validates_payload_shape(monkeypatch):
    captured = {}

    def fake_save(country, input_data):
        captured["country"] = country
        captured["commodity_id"] = input_data.items[0].commodity_id
        captured["weight_quantity"] = input_data.items[0].weight_quantity
        captured["created_by_user_id"] = input_data.created_by_user_id
        return {
            "country": country,
            "iso3": "SSD",
            "needs_setup": False,
            "active_basket": {
                "basket_version_id": "new-version",
                "version_number": 1,
                "items": [],
            },
        }

    monkeypatch.setattr(market_router, "save_country_basket", fake_save)
    client = _client(monkeypatch)

    response = client.post(
        "/countries/South%20Sudan/basket",
        json={
            "items": [{"commodity_id": 1, "weight_quantity": 2}],
            "created_by_user_id": "tester",
        },
    )

    assert response.status_code == 200
    assert response.json()["active_basket"]["basket_version_id"] == "new-version"
    assert captured == {
        "country": "South Sudan",
        "commodity_id": 1,
        "weight_quantity": 2.0,
        "created_by_user_id": "tester",
    }


def test_country_basket_history_endpoint_returns_newest_first(monkeypatch):
    monkeypatch.setattr(
        market_router,
        "list_country_basket_history",
        lambda country, limit=20: {
            "country": country,
            "iso3": "SSD",
            "versions": [
                {"basket_version_id": "v2", "version_number": 2},
                {"basket_version_id": "v1", "version_number": 1},
            ],
        },
    )
    client = _client(monkeypatch)

    response = client.get("/countries/South%20Sudan/basket/history?limit=2")

    assert response.status_code == 200
    assert [item["version_number"] for item in response.json()["versions"]] == [2, 1]


def test_plural_basket_configuration_endpoint_returns_both_roles(monkeypatch):
    monkeypatch.setattr(
        market_router,
        "get_country_baskets_response",
        lambda country: {
            "country": country,
            "iso3": "SSD",
            "primary": {"basket_version_id": "primary-v1", "basket_role": "primary"},
            "secondary": {"basket_version_id": "secondary-v1", "basket_role": "secondary"},
            "needs_primary_setup": False,
            "has_secondary": True,
        },
    )
    client = _client(monkeypatch)

    response = client.get("/countries/South%20Sudan/baskets")

    assert response.status_code == 200
    assert response.json()["primary"]["basket_version_id"] == "primary-v1"
    assert response.json()["secondary"]["basket_version_id"] == "secondary-v1"
    assert response.json()["has_secondary"] is True


def test_plural_basket_mutation_history_and_archive_endpoints(monkeypatch):
    captured = []

    def fake_save(country, role, input_data):
        captured.append(("save", country, role.value, input_data.basket_name))
        return {
            "country": country,
            "iso3": "SSD",
            "primary": {"basket_version_id": "primary-v1"},
            "secondary": {"basket_version_id": "secondary-v1"},
            "needs_primary_setup": False,
            "has_secondary": True,
        }

    def fake_history(country, role, limit=20):
        captured.append(("history", country, role, limit))
        return {
            "country": country,
            "iso3": "SSD",
            "basket_role": role,
            "versions": [{"basket_version_id": "secondary-v1", "version_number": 2}],
        }

    monkeypatch.setattr(market_router, "save_country_basket_role", fake_save)
    monkeypatch.setattr(market_router, "list_country_basket_role_history", fake_history)
    monkeypatch.setattr(
        market_router,
        "archive_country_secondary_basket",
        lambda country: {
            "country": country,
            "iso3": "SSD",
            "primary": {"basket_version_id": "primary-v1"},
            "secondary": None,
            "needs_primary_setup": False,
            "has_secondary": False,
            "archived_secondary": {"basket_version_id": "secondary-v1", "status": "archived"},
        },
    )
    client = _client(monkeypatch)

    save_response = client.post(
        "/countries/South%20Sudan/baskets/secondary",
        json={
            "basket_name": "Pastoral Basket",
            "short_description": "Pastoral household affordability proxy.",
            "items": [{"commodity_id": 2, "weight_quantity": 3}],
        },
    )
    history_response = client.get(
        "/countries/South%20Sudan/baskets/secondary/history?limit=5"
    )
    archive_response = client.delete("/countries/South%20Sudan/baskets/secondary")

    assert save_response.status_code == 200
    assert history_response.status_code == 200
    assert archive_response.status_code == 200
    assert archive_response.json()["archived_secondary"]["status"] == "archived"
    assert captured == [
        ("save", "South Sudan", "secondary", "Pastoral Basket"),
        ("history", "South Sudan", "secondary", 5),
    ]


def test_plural_basket_endpoint_rejects_explicit_role_mismatch(monkeypatch):
    def reject_mismatch(_country, _role, _input_data):
        raise market_router.BasketValidationError("does not match the secondary endpoint")

    monkeypatch.setattr(market_router, "save_country_basket_role", reject_mismatch)
    client = _client(monkeypatch)

    response = client.post(
        "/countries/South%20Sudan/baskets/secondary",
        json={
            "basket_role": "primary",
            "basket_name": "Pastoral Basket",
            "short_description": "Pastoral household affordability proxy.",
            "items": [{"commodity_id": 2, "weight_quantity": 3}],
        },
    )

    assert response.status_code == 400
    assert "does not match" in response.json()["detail"]


def test_generate_async_returns_conflict_for_stale_basket_version(monkeypatch):
    def stale_basket(_country, **_kwargs):
        raise market_router.BasketVersionConflict("refresh basket")

    monkeypatch.setattr(market_router, "resolve_baskets_for_report", stale_basket)
    client = _client(monkeypatch)

    response = client.post(
        "/generate-async",
        json={
            "country": "South Sudan",
            "time_period": "2025-02",
            "commodity_list": ["Maize"],
            "admin1_list": [],
            "currency_code": "SSP",
            "enabled_modules": [],
            "basket_version_id": "old-version",
        },
    )

    assert response.status_code == 409
    assert "refresh basket" in response.json()["detail"]


def test_generate_rejects_conflicting_primary_version_aliases(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/generate",
        json={
            "country": "South Sudan",
            "time_period": "2025-02",
            "basket_version_id": "primary-v1",
            "primary_basket_version_id": "primary-v2",
        },
    )

    assert response.status_code == 422
    assert "must reference the same primary basket version" in response.text


def test_generate_resolves_selection_and_returns_two_basket_contract(monkeypatch):
    captured = {}
    selection = FakeBasketSelection(include_secondary=True)

    def fake_resolve(country, **kwargs):
        captured["resolve"] = {"country": country, **kwargs}
        return selection

    def fake_generate(**kwargs):
        captured["graph"] = kwargs
        return {
            "run_id": "run-sync",
            "country": kwargs["country"],
            "time_period": kwargs["time_period"],
            "report_draft_sections": {
                "HIGHLIGHTS": "Highlights.",
                "REGIONAL_HIGHLIGHTS": "Regional. [INSERT GRAPH: regional_comparison]",
            },
            "visualizations": {
                "food_basket_trend": "primary-trend",
                "food_basket_trend_primary": "primary-trend",
                "food_basket_trend_secondary": "secondary-trend",
                "regional_comparison": "primary-regional",
                "regional_comparison_primary": "primary-regional",
                "regional_comparison_secondary": "secondary-regional",
            },
            "data_statistics": {"food_basket": {"current_price": 42, "current_cost": 42}},
            "basket_statistics": {
                "primary": {"current_price": 42, "current_cost": 42},
                "secondary": {"current_price": 21, "current_cost": 21},
            },
            "basket_series_national": [
                {"Date": "2025-02-01", "BasketRole": "primary", "Cost": 42, "Complete": True},
                {"Date": "2025-02-01", "BasketRole": "secondary", "Cost": 21, "Complete": True},
            ],
            "basket_series_regional": [],
            "qa_review": {"status": "passed", "correction_attempts": 1, "flags": []},
            "warnings": [],
        }

    monkeypatch.setattr(market_router, "resolve_baskets_for_report", fake_resolve)
    monkeypatch.setattr(market_router, "run_report_generation", fake_generate)
    client = _client(monkeypatch)

    response = client.post(
        "/generate",
        json={
            "country": "South Sudan",
            "time_period": "2025-02",
            "basket_version_id": "primary-v1",
            "secondary_basket_version_id": "secondary-v1",
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert captured["resolve"] == {
        "country": "South Sudan",
        "primary_basket_version_id": "primary-v1",
        "include_secondary_basket": True,
        "secondary_basket_version_id": "secondary-v1",
    }
    assert captured["graph"]["basket_version_id"] == "primary-v1"
    assert captured["graph"]["basket_selection"] is selection
    assert payload["food_basket"]["basket_version_id"] == "primary-v1"
    assert payload["food_baskets"]["secondary"]["basket_version_id"] == "secondary-v1"
    assert payload["basket_statistics"] == {
        "primary": {"current_price": 42, "current_cost": 42},
        "secondary": {"current_price": 21, "current_cost": 21},
    }
    assert [row["BasketRole"] for row in payload["basket_series_national"]] == ["primary", "secondary"]
    assert payload["secondary_basket_included"] is True
    assert payload["qa_review"] == {"status": "passed", "correction_attempts": 1, "flags": []}
    assert payload["visualizations"]["food_basket_trend"] == payload["visualizations"]["food_basket_trend_primary"]
    assert [
        block["figure_id"]
        for block in payload["report_blocks"]
        if block["type"] == "figure"
    ] == [
        "food_basket_trend_primary",
        "food_basket_trend_secondary",
        "regional_comparison_primary",
        "regional_comparison_secondary",
    ]
    assert next(block for block in payload["report_blocks"] if block["type"] == "table")["meta"]["table_kind"] == (
        "basket_definitions"
    )


def test_generate_mock_data_returns_empty_basket_selection(monkeypatch):
    monkeypatch.setattr(
        market_router,
        "resolve_baskets_for_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("resolver should not run")),
    )
    monkeypatch.setattr(
        market_router,
        "run_report_generation",
        lambda **kwargs: {
            "run_id": "run-mock",
            "country": kwargs["country"],
            "time_period": kwargs["time_period"],
            "report_draft_sections": {},
            "visualizations": {},
            "data_statistics": {},
            "warnings": [],
        },
    )
    client = _client(monkeypatch)

    response = client.post(
        "/generate",
        json={
            "country": "South Sudan",
            "time_period": "2025-02",
            "use_mock_data": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["food_baskets"] == {"primary": None, "secondary": None}
    assert response.json()["secondary_basket_included"] is False
    assert response.json()["qa_review"]["status"] == "not_recorded"


def test_generate_async_persists_and_returns_revalidated_basket_selection(monkeypatch):
    monkeypatch.setattr(async_runs, "_BACKEND", "memory")
    async_runs._RUNS.clear()
    async_runs._RUN_ARTIFACTS.clear()
    selection = FakeBasketSelection(include_secondary=True)
    revalidated_selection = FakeBasketSelection(include_secondary=True)
    resolve_calls = []
    graph_calls = []

    def fake_resolve(country, **kwargs):
        resolve_calls.append({"country": country, **kwargs})
        return selection if len(resolve_calls) == 1 else revalidated_selection

    def fake_generate(**kwargs):
        graph_calls.append(kwargs)
        return {
            "run_id": "graph-run",
            "country": kwargs["country"],
            "time_period": kwargs["time_period"],
            "report_draft_sections": {},
            "visualizations": {},
            "data_statistics": {"food_basket": {"current_price": 42}},
            "basket_statistics": {
                "primary": {"current_cost": 42},
                "secondary": {"current_cost": 21},
            },
            "basket_series_national": [
                {"Date": "2025-02-01", "BasketRole": "primary", "Cost": 42},
            ],
            "basket_series_regional": [],
            "cache_metadata": {
                "cache_version_id": "cache-v1",
                "basket_calculation_specs": [{"basket_role": "primary"}],
                "basket_applicable_regions": {"primary": []},
                "basket_coverage": {"primary": {"current_complete": True}},
            },
            "qa_review": {
                "status": "completed_with_warnings",
                "correction_attempts": 3,
                "flags": [
                    {
                        "section": "GLOBAL",
                        "claim": "Unresolved basket claim",
                        "issue_type": "basket_identity_error",
                        "severity": "high",
                        "details": "Values remain swapped.",
                        "recommendation": "Review before publication.",
                    }
                ],
            },
            "warnings": [],
        }

    monkeypatch.setattr(market_router, "resolve_baskets_for_report", fake_resolve)
    monkeypatch.setattr(market_router, "run_report_generation", fake_generate)
    client = _client(monkeypatch)

    start_response = client.post(
        "/generate-async",
        json={
            "country": "South Sudan",
            "time_period": "2025-02",
            "primary_basket_version_id": "primary-v1",
            "secondary_basket_version_id": "secondary-v1",
        },
    )
    run_id = start_response.json()["run_id"]
    run = async_runs.get_run(run_id)
    result_response = client.get(f"/result/{run_id}")

    assert start_response.status_code == 200
    assert run is not None
    assert run.status == "completed"
    assert len(resolve_calls) == 2
    assert resolve_calls[1]["primary_basket_version_id"] == "primary-v1"
    assert resolve_calls[1]["secondary_basket_version_id"] == "secondary-v1"
    assert graph_calls[0]["basket_selection"] is selection
    assert run.metadata["basket_selection"]["secondary_basket_included"] is True
    assert run.metadata["basket_calculation"]["cache_version_id"] == "cache-v1"
    assert run.metadata["basket_calculation"]["series_national"][0]["Cost"] == 42
    assert run.metadata["basket_calculation"]["statistics"]["secondary"]["current_cost"] == 21
    assert run.metadata["qa_review"]["status"] == "completed_with_warnings"
    assert run.result["food_baskets"]["secondary"]["basket_version_id"] == "secondary-v1"
    assert result_response.status_code == 200
    assert result_response.json()["secondary_basket_included"] is True
    assert result_response.json()["basket_statistics"]["secondary"]["current_cost"] == 21
    assert result_response.json()["qa_review"]["correction_attempts"] == 3
