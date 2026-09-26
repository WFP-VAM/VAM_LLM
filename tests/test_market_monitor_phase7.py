from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.services.market_monitor import data_loader, food_basket, router
from app.services.market_monitor.features import (
    SECOND_BASKET_FEATURE_ENV,
    SecondBasketFeatureDisabled,
    market_monitor_second_basket_enabled,
    normalize_secondary_request,
)
from app.services.market_monitor.food_basket import BasketSaveInput
from app.services.market_monitor.schemas import GenerateReportInput
from app.services.price_cache.config import load_price_cache_config
from app.services.price_cache.fixtures import seed_cache_snapshot
from app.services.price_cache.migrations import MIGRATIONS_ROOT, _ensure_migration_table, _split_sql
from app.services.price_cache.sql_repository import SqlPriceCacheRepository, create_price_cache_engine
from app.shared.runs import executor, report_runs
from app.streamlit_backend import dispatcher
from scripts.phase7_second_basket_qa import (
    ReleaseQAFailure,
    generate_sample_reports,
    prepare_streamlit_fixture,
    record_visual_review,
    verify_sqlite_copy,
)


class FakeBasketSelection:
    def __init__(self, include_secondary: bool):
        self.primary_basket_version_id = "primary-v1"
        self.secondary_basket_version_id = "secondary-v1" if include_secondary else None
        self.secondary_basket_included = include_secondary

    def food_baskets_dict(self):
        return {
            "primary": {
                "basket_version_id": "primary-v1",
                "basket_role": "primary",
                "basket_name": "MEB",
                "items": [],
            },
            "secondary": (
                {
                    "basket_version_id": "secondary-v1",
                    "basket_role": "secondary",
                    "basket_name": "Pastoral basket",
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


def _launch_after_disabling_second_basket(work, *, name):
    """Run launched work at once, after the second basket was switched off."""
    import os

    os.environ[SECOND_BASKET_FEATURE_ENV] = "false"
    work()


def _api_client() -> TestClient:
    app = FastAPI()
    app.include_router(router.router)
    return TestClient(app)


def _generation_result(**kwargs):
    return {
        "run_id": "phase7-run",
        "country": kwargs.get("country", "South Sudan"),
        "time_period": kwargs.get("time_period", "2025-02"),
        "language": "en",
        "locale": "en_US",
        "report_draft_sections": {},
        "visualizations": {},
        "data_statistics": {"food_basket": {"current_price": 42}},
        "basket_statistics": {"primary": {"current_cost": 42}, "secondary": None},
        "basket_series_national": [],
        "basket_series_regional": [],
        "document_references": [],
        "warnings": [],
    }


def test_second_basket_flag_is_default_on_and_accepts_existing_false_values(monkeypatch):
    monkeypatch.delenv(SECOND_BASKET_FEATURE_ENV, raising=False)
    assert market_monitor_second_basket_enabled() is True
    for value in ("0", "false", "FALSE", " no ", "off"):
        monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, value)
        assert market_monitor_second_basket_enabled() is False
    monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "unexpected")
    assert market_monitor_second_basket_enabled() is True


def test_disabled_generation_normalizes_omitted_fields_but_rejects_explicit_use(monkeypatch):
    monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "false")
    omitted = GenerateReportInput(country="South Sudan", time_period="2025-02")
    assert normalize_secondary_request(omitted) == (False, None)

    explicit = GenerateReportInput(
        country="South Sudan",
        time_period="2025-02",
        include_secondary_basket=True,
    )
    with pytest.raises(SecondBasketFeatureDisabled):
        normalize_secondary_request(explicit)

    excluded = GenerateReportInput(
        country="South Sudan",
        time_period="2025-02",
        include_secondary_basket=False,
        secondary_basket_version_id="stale-secondary",
    )
    assert normalize_secondary_request(excluded) == (False, "stale-secondary")

    mock = GenerateReportInput(
        country="South Sudan",
        time_period="2025-02",
        include_secondary_basket=True,
        use_mock_data=True,
    )
    assert normalize_secondary_request(mock) == (False, None)


def test_generate_report_schema_removes_news_date_customization():
    assert "news_start_date" not in GenerateReportInput.model_fields
    assert "news_end_date" not in GenerateReportInput.model_fields

    parsed = GenerateReportInput(
        country="South Sudan",
        time_period="2025-02",
        news_start_date="1999-01-01",
        news_end_date="1999-01-31",
    )
    payload = parsed.model_dump()
    assert "news_start_date" not in payload
    assert "news_end_date" not in payload


def test_configuration_exposes_flag_and_secondary_mutations_are_gated(monkeypatch):
    class FakeRepo:
        def get_active_baskets(self, _iso3):
            return {"primary": None, "secondary": None}

        def save_basket(self, *_args, **_kwargs):
            raise AssertionError("disabled secondary save reached the repository")

    monkeypatch.setattr(food_basket, "create_food_basket_repository", lambda: FakeRepo())
    monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "false")

    response = food_basket.get_country_baskets_response("South Sudan")
    assert response["second_basket_enabled"] is False
    with pytest.raises(SecondBasketFeatureDisabled):
        food_basket.save_country_basket_role(
            "South Sudan",
            "secondary",
            BasketSaveInput(
                basket_role="secondary",
                basket_name="Pastoral",
                short_description="Pastoral basket.",
                items=[{"commodity_id": 1, "weight_quantity": 1}],
            ),
        )
    with pytest.raises(SecondBasketFeatureDisabled):
        food_basket.archive_country_secondary_basket("South Sudan")


def test_fastapi_and_dispatcher_return_matching_disabled_errors_and_service_info(monkeypatch):
    monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "false")
    client = _api_client()

    api = client.post(
        "/generate",
        json={
            "country": "South Sudan",
            "time_period": "2025-02",
            "include_secondary_basket": True,
        },
    )
    local = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/generate",
        json_body={
            "country": "South Sudan",
            "time_period": "2025-02",
            "include_secondary_basket": True,
        },
    )

    assert api.status_code == local.status_code == 503
    assert api.json()["detail"]["code"] == "second_basket_disabled"
    assert local.json()["detail"]["code"] == "second_basket_disabled"
    assert client.get("/info").json()["features"]["second_food_basket"]["enabled"] is False
    assert dispatcher.dispatch_request("GET", "/market-monitor/info").json()["features"][
        "second_food_basket"
    ]["enabled"] is False

    api_save = client.post(
        "/countries/South%20Sudan/baskets/secondary",
        json={
            "basket_role": "secondary",
            "basket_name": "Pastoral",
            "short_description": "Pastoral basket.",
            "items": [{"commodity_id": 1, "weight_quantity": 1}],
        },
    )
    local_archive = dispatcher.dispatch_request(
        "DELETE", "/market-monitor/countries/South%20Sudan/baskets/secondary"
    )
    assert api_save.status_code == local_archive.status_code == 503
    assert api_save.json()["detail"]["code"] == local_archive.json()["detail"]["code"]


def test_primary_only_generation_and_reportability_remain_available_when_disabled(monkeypatch):
    monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "false")
    captured = []

    def fake_resolve(_country, **kwargs):
        captured.append(kwargs)
        return FakeBasketSelection(include_secondary=False)

    monkeypatch.setattr(router, "resolve_baskets_for_report", fake_resolve)
    monkeypatch.setattr(router, "run_report_generation", _generation_result)
    monkeypatch.setattr(
        data_loader,
        "get_reportable_months",
        lambda country, **kwargs: {"country": country, "reportable_months": ["2025-02"], **kwargs},
    )

    api = _api_client().post("/generate", json={"country": "South Sudan", "time_period": "2025-02"})
    local = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/generate",
        json_body={"country": "South Sudan", "time_period": "2025-02"},
    )
    assert api.status_code == local.status_code == 200
    assert captured[0]["include_secondary_basket"] is False
    assert captured[1]["include_secondary_basket"] is False
    assert api.json()["secondary_basket_included"] is False
    assert local.json()["secondary_basket_included"] is False

    reportable = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/reportable-months",
        params={"include_secondary_basket": "false"},
    )
    blocked = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/reportable-months",
        params={"include_secondary_basket": "true"},
    )
    assert reportable.status_code == 200
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "second_basket_disabled"


def test_accepted_async_run_continues_after_gate_is_disabled(monkeypatch):
    monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "true")
    monkeypatch.setattr(executor, "launch", _launch_after_disabling_second_basket)
    calls = []

    def fake_resolve(_country, **kwargs):
        calls.append(kwargs)
        return FakeBasketSelection(include_secondary=True)

    monkeypatch.setattr(router, "resolve_baskets_for_report", fake_resolve)
    monkeypatch.setattr(router, "run_report_generation", _generation_result)
    response = dispatcher.dispatch_request(
        "POST", "/market-monitor/generate-async", json_body={"country": "South Sudan", "time_period": "2025-02"}
    )
    run = report_runs.get_run(response.json()["run_id"])

    assert run is not None and run.status == "completed"
    assert len(calls) == 2
    assert calls[0]["include_secondary_basket"] is True
    assert calls[1]["include_secondary_basket"] is True
    assert run.metadata["feature_flags"]["second_food_basket_enabled"] is True
    assert run.result["secondary_basket_included"] is True


def test_fastapi_accepted_async_run_uses_submission_gate_snapshot(monkeypatch, immediate_launch):
    monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "true")
    calls = []

    def fake_resolve(_country, **kwargs):
        calls.append(kwargs)
        selection = FakeBasketSelection(include_secondary=True)
        if len(calls) == 1:
            monkeypatch.setenv(SECOND_BASKET_FEATURE_ENV, "false")
        return selection

    monkeypatch.setattr(router, "resolve_baskets_for_report", fake_resolve)
    monkeypatch.setattr(router, "run_report_generation", _generation_result)
    response = _api_client().post(
        "/generate-async",
        json={"country": "South Sudan", "time_period": "2025-02"},
    )
    run = report_runs.get_run(response.json()["run_id"])

    assert response.status_code == 200
    assert run is not None and run.status == "completed"
    assert len(calls) == 2
    assert run.metadata["feature_flags"]["second_food_basket_enabled"] is True
    assert run.result["secondary_basket_included"] is True


def _build_populated_v004_sqlite(path: Path) -> None:
    config = load_price_cache_config(
        {"PRICE_CACHE_BACKEND": "sqlite", "PRICE_CACHE_SQLITE_PATH": str(path)}
    )
    engine = create_price_cache_engine(config)
    with engine.begin() as conn:
        _ensure_migration_table(conn, "sqlite")
        for migration in sorted((MIGRATIONS_ROOT / "sqlite").glob("*.sql")):
            if migration.name.split("_", 1)[0] > "004":
                continue
            for statement in _split_sql(migration.read_text(encoding="utf-8")):
                conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO price_cache_schema_migrations(version) VALUES (:version)"),
                {"version": migration.stem},
            )
    seed_cache_snapshot(SqlPriceCacheRepository(engine))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO country_food_basket_versions (
                    basket_version_id, country_iso3, version_number, status, created_at,
                    created_by_user_id, cache_version_id_at_creation, change_note
                ) VALUES (
                    'legacy-primary-v1', 'SSD', 1, 'active', '2026-01-01T00:00:00+00:00',
                    'legacy-user', 'fixture-cache-version', 'legacy basket'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO country_food_basket_items (
                    basket_item_id, basket_version_id, commodity_id, commodity_name_snapshot,
                    databridges_unit_id, databridges_unit, weight_quantity, sort_order, item_note
                ) VALUES (
                    'legacy-item-v1', 'legacy-primary-v1', 1, 'Maize', 100, 'kg', 2, 1, NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO country_food_basket_current (
                    country_iso3, active_basket_version_id, updated_at, updated_by_user_id
                ) VALUES ('SSD', 'legacy-primary-v1', '2026-01-01T00:00:00+00:00', 'legacy-user')
                """
            )
        )
    engine.dispose()


def test_release_qa_copies_and_verifies_populated_sqlite_without_mutating_source(tmp_path):
    source = tmp_path / "source-v004.sqlite3"
    _build_populated_v004_sqlite(source)
    audit = verify_sqlite_copy(source, tmp_path / "audit")

    assert audit["status"] == "passed"
    assert audit["legacy"]["identifiers_preserved"] is True
    assert audit["schema"]["valid"] is True
    assert audit["repository_smoke"]["archive_idempotent"] is True

    config = load_price_cache_config(
        {"PRICE_CACHE_BACKEND": "sqlite", "PRICE_CACHE_SQLITE_PATH": str(source)}
    )
    engine = create_price_cache_engine(config)
    with engine.begin() as conn:
        migrations = {row[0] for row in conn.execute(text("SELECT version FROM price_cache_schema_migrations"))}
        version_count = conn.execute(text("SELECT COUNT(*) FROM country_food_basket_versions")).scalar_one()
    engine.dispose()
    assert "005_second_food_basket" not in migrations
    assert version_count == 1


def test_release_qa_refuses_unpopulated_sqlite(tmp_path):
    source = tmp_path / "empty.sqlite3"
    _build_populated_v004_sqlite(source)
    config = load_price_cache_config(
        {"PRICE_CACHE_BACKEND": "sqlite", "PRICE_CACHE_SQLITE_PATH": str(source)}
    )
    engine = create_price_cache_engine(config)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM country_food_basket_current"))
        conn.execute(text("DELETE FROM country_food_basket_items"))
        conn.execute(text("DELETE FROM country_food_basket_versions"))
    engine.dispose()
    with pytest.raises(ReleaseQAFailure, match="must contain legacy"):
        verify_sqlite_copy(source, tmp_path / "audit")


def test_release_qa_generates_and_records_three_multilingual_samples(tmp_path):
    manifest = generate_sample_reports(tmp_path)

    assert [item["language"] for item in manifest["scenarios"]] == ["en", "fr", "es"]
    assert all(item["structural"]["passed"] for item in manifest["scenarios"])
    assert all((tmp_path / item["docx"]).is_file() for item in manifest["scenarios"])
    reviewed = record_visual_review(tmp_path, "Phase 7 automated test", "passed", "Fixture review.")
    assert reviewed["status"] == "passed"
    assert all(item["visual_review"]["status"] == "passed" for item in reviewed["scenarios"])


def test_release_qa_prepares_synthetic_two_basket_streamlit_fixture(tmp_path):
    summary = prepare_streamlit_fixture(tmp_path)

    assert summary["synthetic_data"] is True
    assert (tmp_path / summary["database"]).is_file()
    config = load_price_cache_config(
        {"PRICE_CACHE_BACKEND": "sqlite", "PRICE_CACHE_SQLITE_PATH": str(tmp_path / summary["database"])}
    )
    engine = create_price_cache_engine(config)
    try:
        active = food_basket.SqlCountryFoodBasketRepository(engine).get_active_baskets("SSD")
    finally:
        engine.dispose()
    assert active["primary"] is not None
    assert active["secondary"] is not None
    assert active["secondary"].regions == ["Central Equatoria"]
