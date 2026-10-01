import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine as create_sqlalchemy_engine
from sqlalchemy import inspect, text

from app.services.market_monitor.food_basket import BasketRole, SqlCountryFoodBasketRepository
from app.services.price_cache.config import load_price_cache_config
from app.services.price_cache.fixtures import seed_cache_snapshot
from app.services.price_cache.migrations import (
    MIGRATIONS_ROOT,
    _ensure_migration_table,
    _split_sql,
    apply_migrations,
)
from app.services.price_cache.sql_repository import SqlPriceCacheRepository, create_price_cache_engine


def _repo(tmp_path: Path) -> SqlPriceCacheRepository:
    config = load_price_cache_config(
        {
            "PRICE_CACHE_BACKEND": "sqlite",
            "PRICE_CACHE_SQLITE_PATH": str(tmp_path / "price_cache.sqlite3"),
        }
    )
    engine = create_price_cache_engine(config)
    apply_migrations(engine, config.backend)
    return SqlPriceCacheRepository(engine)


def test_config_defaults_to_local_sqlite(monkeypatch):
    for name in (
        "PRICE_CACHE_BACKEND",
        "PRICE_CACHE_SQLITE_PATH",
        "PRICE_CACHE_RETAIN_VERSIONS",
        "PRICE_CACHE_DATABASE_URL",
        "PRICE_CACHE_GCP_PROJECT",
        "PRICE_CACHE_GCP_REGION",
        "PRICE_CACHE_GCP_CLOUD_SQL_INSTANCE",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_price_cache_config()

    assert config.backend == "sqlite"
    assert config.sqlite_path == Path(".tmp/price_cache.sqlite3")
    assert config.retain_versions == 3
    assert not hasattr(config, "gcp_region")


def test_config_rejects_postgres_without_database_url(monkeypatch):
    monkeypatch.setenv("PRICE_CACHE_BACKEND", "cloud_sql_postgres")
    monkeypatch.delenv("PRICE_CACHE_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="PRICE_CACHE_DATABASE_URL"):
        load_price_cache_config()


def test_migrations_create_sqlite_schema(tmp_path):
    repo = _repo(tmp_path)
    inspector = inspect(repo.engine)

    tables = set(inspector.get_table_names())

    assert "price_cache_schema_migrations" in tables
    assert "price_cache_versions" in tables
    assert "price_cache_active_version" in tables
    assert "cached_countries" in tables
    assert "cached_commodities" in tables
    assert "cached_units" in tables
    assert "cached_markets" in tables
    assert "cached_currencies" in tables
    assert "cached_price_monthly" in tables
    assert "price_cache_country_active_versions" in tables
    assert "price_cache_version_countries" in tables
    assert "price_cache_refresh_locks" in tables
    assert "country_food_basket_current" in tables
    assert "country_food_basket_versions" in tables
    assert "country_food_basket_items" in tables
    assert "country_food_basket_regions" in tables


def test_migration_005_upgrades_populated_food_basket_schema_without_id_loss(tmp_path):
    config = load_price_cache_config(
        {
            "PRICE_CACHE_BACKEND": "sqlite",
            "PRICE_CACHE_SQLITE_PATH": str(tmp_path / "legacy_baskets.sqlite3"),
        }
    )
    engine = create_price_cache_engine(config)

    with engine.begin() as conn:
        _ensure_migration_table(conn, "sqlite")
        for path in sorted((MIGRATIONS_ROOT / "sqlite").glob("*.sql")):
            if path.name.split("_", 1)[0] > "004":
                continue
            for statement in _split_sql(path.read_text(encoding="utf-8")):
                conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO price_cache_schema_migrations(version) VALUES (:version)"),
                {"version": path.stem},
            )

        conn.execute(
            text(
                """
                INSERT INTO country_food_basket_versions (
                    basket_version_id,
                    country_iso3,
                    version_number,
                    status,
                    created_at,
                    created_by_user_id,
                    cache_version_id_at_creation,
                    change_note
                ) VALUES (
                    'legacy-basket-version',
                    'SSD',
                    7,
                    'active',
                    '2026-01-01T00:00:00+00:00',
                    'legacy-user',
                    'legacy-cache-version',
                    'legacy basket'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO country_food_basket_items (
                    basket_item_id,
                    basket_version_id,
                    commodity_id,
                    commodity_name_snapshot,
                    databridges_unit_id,
                    databridges_unit,
                    weight_quantity,
                    sort_order,
                    item_note
                ) VALUES (
                    'legacy-basket-item',
                    'legacy-basket-version',
                    1,
                    'Maize',
                    100,
                    'kg',
                    2,
                    1,
                    'legacy item'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO country_food_basket_current (
                    country_iso3,
                    active_basket_version_id,
                    updated_at,
                    updated_by_user_id
                ) VALUES (
                    'SSD',
                    'legacy-basket-version',
                    '2026-01-01T00:00:00+00:00',
                    'legacy-user'
                )
                """
            )
        )

    applied = apply_migrations(engine, config.backend)
    applied_again = apply_migrations(engine, config.backend)

    inspector = inspect(engine)
    with engine.begin() as conn:
        version = conn.execute(
            text("SELECT * FROM country_food_basket_versions WHERE basket_version_id = 'legacy-basket-version'")
        ).mappings().one()
        item = conn.execute(
            text("SELECT * FROM country_food_basket_items WHERE basket_item_id = 'legacy-basket-item'")
        ).mappings().one()
        current = conn.execute(
            text("SELECT * FROM country_food_basket_current WHERE country_iso3 = 'SSD'")
        ).mappings().one()
        foreign_keys = conn.exec_driver_sql(
            "PRAGMA foreign_key_list('country_food_basket_current')"
        ).mappings().all()

    assert applied == ["005_second_food_basket"]
    assert applied_again == []
    assert version["basket_version_id"] == "legacy-basket-version"
    assert version["basket_role"] == "primary"
    assert version["basket_name"] == "MEB"
    assert version["short_description"] == "Primary MEB reference basket configured by the Country Office."
    assert version["scope_type"] == "national"
    assert item["basket_item_id"] == "legacy-basket-item"
    assert item["basket_version_id"] == "legacy-basket-version"
    assert current["active_basket_version_id"] == "legacy-basket-version"
    assert current["basket_role"] == "primary"
    assert inspector.get_pk_constraint("country_food_basket_current")["constrained_columns"] == [
        "country_iso3",
        "basket_role",
    ]
    assert "country_food_basket_regions" in inspector.get_table_names()
    role_fk = sorted(
        (int(row["seq"]), str(row["from"]), str(row["to"]))
        for row in foreign_keys
    )
    assert role_fk == [
        (0, "country_iso3", "country_iso3"),
        (1, "basket_role", "basket_role"),
        (2, "active_basket_version_id", "basket_version_id"),
    ]


def test_postgres_migration_005_upgrade_and_role_aware_repository_contract():
    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")

    schema = f"basket_phase1_{uuid.uuid4().hex}"
    admin_engine = create_sqlalchemy_engine(database_url, future=True)
    with admin_engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

    engine = create_sqlalchemy_engine(
        database_url,
        future=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with engine.begin() as conn:
            _ensure_migration_table(conn, "postgres")
            for path in sorted((MIGRATIONS_ROOT / "postgres").glob("*.sql")):
                if path.name.split("_", 1)[0] > "004":
                    continue
                for statement in _split_sql(path.read_text(encoding="utf-8")):
                    conn.execute(text(statement))
                conn.execute(
                    text("INSERT INTO price_cache_schema_migrations(version) VALUES (:version)"),
                    {"version": path.stem},
                )
            conn.execute(
                text(
                    """
                    INSERT INTO country_food_basket_versions (
                        basket_version_id,
                        country_iso3,
                        version_number,
                        status,
                        created_at,
                        created_by_user_id
                    ) VALUES (
                        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                        'SSD',
                        1,
                        'active',
                        '2026-01-01T00:00:00+00:00',
                        'legacy-user'
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO country_food_basket_items (
                        basket_item_id,
                        basket_version_id,
                        commodity_id,
                        commodity_name_snapshot,
                        databridges_unit_id,
                        databridges_unit,
                        weight_quantity,
                        sort_order
                    ) VALUES (
                        'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                        1,
                        'Maize',
                        100,
                        'kg',
                        2,
                        1
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO country_food_basket_current (
                        country_iso3,
                        active_basket_version_id,
                        updated_at,
                        updated_by_user_id
                    ) VALUES (
                        'SSD',
                        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                        '2026-01-01T00:00:00+00:00',
                        'legacy-user'
                    )
                    """
                )
            )

        assert apply_migrations(engine, "postgres") == ["005_second_food_basket"]
        repo = SqlPriceCacheRepository(engine)
        seed_cache_snapshot(repo)
        basket_repo = SqlCountryFoodBasketRepository(engine)
        primary = basket_repo.get_active_basket("SSD", BasketRole.PRIMARY)
        secondary = basket_repo.save_basket(
            "SSD",
            role=BasketRole.SECONDARY,
            basket_name="Pastoral Basket",
            short_description="Pastoral household affordability proxy.",
            items=[{"commodity_id": 2, "weight_quantity": 3}],
        )

        assert primary is not None
        assert primary.basket_version_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        assert primary.basket_name == "MEB"
        assert primary.version_number == 1
        assert secondary.version_number == 2
        assert basket_repo.get_active_baskets("SSD")["primary"].basket_version_id == primary.basket_version_id
        assert basket_repo.get_active_baskets("SSD")["secondary"].basket_version_id == secondary.basket_version_id
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin_engine.dispose()


def test_migration_003_backfills_admin_metadata_on_legacy_databases(tmp_path):
    config = load_price_cache_config(
        {
            "PRICE_CACHE_BACKEND": "sqlite",
            "PRICE_CACHE_SQLITE_PATH": str(tmp_path / "legacy_cache.sqlite3"),
        }
    )
    engine = create_price_cache_engine(config)

    # Build a database at schema version 002 with rows shaped like the broken
    # deployment: price rows whose admin1/admin2 are NULL.
    with engine.begin() as conn:
        _ensure_migration_table(conn, "sqlite")
        for version in ("001_initial_cache", "002_phase3_refresh_worker"):
            for statement in _split_sql((MIGRATIONS_ROOT / "sqlite" / f"{version}.sql").read_text(encoding="utf-8")):
                conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO price_cache_schema_migrations(version) VALUES (:version)"),
                {"version": version},
            )
        conn.execute(
            text(
                """
                INSERT INTO price_cache_versions (cache_version_id, status, refresh_type, started_at)
                VALUES ('legacy-version', 'active', 'weekly_full', '2026-06-01T00:00:00+00:00')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO cached_markets (cache_version_id, country_iso3, market_id, market_name, admin1_name, admin2_name)
                VALUES ('legacy-version', 'SSD', 10, 'Juba', 'Central Equatoria', 'Juba County')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO cached_price_monthly (
                    cache_version_id, country_iso3, commodity_id, market_id, price_date, price,
                    price_type_name, price_flag, admin1_name, admin2_name, market_name
                ) VALUES (
                    'legacy-version', 'SSD', 1, 10, '2025-01-01', 20.0,
                    'Retail', 'actual', NULL, NULL, NULL
                )
                """
            )
        )

    applied = apply_migrations(engine, config.backend)

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT admin1_name, admin2_name, market_name FROM cached_price_monthly")
        ).mappings().first()
    assert "003_admin_backfill_and_price_key" in applied
    assert row["admin1_name"] == "Central Equatoria"
    assert row["admin2_name"] == "Juba County"
    assert row["market_name"] == "Juba"


def test_empty_cache_returns_clear_empty_status(tmp_path):
    repo = _repo(tmp_path)

    status = repo.get_cache_status()

    assert status.has_active_cache is False
    assert status.active_version_id is None
    assert repo.get_active_version_id() is None
    assert repo.list_countries() == []
    assert repo.get_country_metadata("SSD") is None
    assert repo.get_price_window("SSD", "2025-01-01", "2025-02-01") == []


def test_fixture_snapshot_seeds_country_metadata(tmp_path):
    repo = _repo(tmp_path)
    version_id = seed_cache_snapshot(repo)

    status = repo.get_cache_status()
    countries = repo.list_countries()
    metadata = repo.get_country_metadata("SSD")
    availability = repo.get_country_availability("SSD")

    assert status.has_active_cache is True
    assert status.active_version_id == version_id
    assert status.rows_prices == 4
    assert countries[0].country_name == "South Sudan"
    assert metadata is not None
    assert availability is not None
    assert availability.cache_version_id == version_id
    assert availability.date_start.isoformat() == "2025-01-01"
    assert availability.date_end.isoformat() == "2025-02-01"
    assert availability.latest_price_date.isoformat() == "2025-02-01"
    assert availability.priced_commodity_ids == [1, 2]
    assert availability.admin1_names == ["Central Equatoria", "Western Bahr el Ghazal"]
    assert [unit.commodity_unit_name for unit in availability.units] == ["kg"]
    assert metadata.country.country_iso3 == "SSD"
    assert [item.commodity_name for item in metadata.commodities] == ["Beans", "Maize"]
    assert [item.market_name for item in metadata.markets] == ["Juba", "Wau"]
    assert [item.commodity_unit_name for item in metadata.units] == ["kg"]
    assert [item.currency_code for item in metadata.currencies] == ["SSP"]


def test_price_window_filters_by_country_date_commodity_market_and_admin1(tmp_path):
    repo = _repo(tmp_path)
    seed_cache_snapshot(repo)

    all_rows = repo.get_price_window("SSD", "2025-01-01", "2025-02-01")
    maize_rows = repo.get_price_window("SSD", "2025-01-01", "2025-02-01", commodity_ids=[1])
    wau_rows = repo.get_price_window("SSD", "2025-01-01", "2025-02-01", market_ids=[11])
    juba_rows = repo.get_price_window(
        "SSD",
        "2025-01-01",
        "2025-02-01",
        admin1_names=["Central Equatoria"],
    )
    feb_rows = repo.get_price_window("SSD", "2025-02-01", "2025-02-01")

    assert len(all_rows) == 4
    assert {row.commodity_name for row in maize_rows} == {"Maize"}
    assert {row.market_name for row in wau_rows} == {"Wau"}
    assert {row.admin1_name for row in juba_rows} == {"Central Equatoria"}
    assert len(feb_rows) == 2
    assert {row.price for row in feb_rows} == {6.0, 12.0}


def test_active_version_pointer_controls_visible_rows(tmp_path):
    repo = _repo(tmp_path)
    old_version = seed_cache_snapshot(
        repo,
        cache_version_id="11111111-1111-1111-1111-111111111111",
        price_offset=0,
    )
    old_rows = repo.get_price_window("SSD", "2025-01-01", "2025-01-01", commodity_ids=[1])

    new_version = seed_cache_snapshot(
        repo,
        cache_version_id="22222222-2222-2222-2222-222222222222",
        price_offset=100,
    )
    new_rows = repo.get_price_window("SSD", "2025-01-01", "2025-01-01", commodity_ids=[1])

    assert old_version != new_version
    assert repo.get_active_version_id() == new_version
    assert old_rows[0].price == 10.0
    assert new_rows[0].price == 110.0


def test_country_active_pointer_preserves_failed_country_fallback(tmp_path):
    repo = _repo(tmp_path)
    old_version = seed_cache_snapshot(
        repo,
        cache_version_id="11111111-1111-1111-1111-111111111111",
        country_iso3="SSD",
        country_name="South Sudan",
        price_offset=0,
    )
    new_version = seed_cache_snapshot(
        repo,
        cache_version_id="22222222-2222-2222-2222-222222222222",
        country_iso3="ETH",
        country_name="Ethiopia",
        price_offset=100,
    )

    countries = repo.list_countries()
    ssd_rows = repo.get_price_window("SSD", "2025-01-01", "2025-01-01", commodity_ids=[1])
    eth_rows = repo.get_price_window("ETH", "2025-01-01", "2025-01-01", commodity_ids=[1])

    assert repo.get_active_version_id() == new_version
    assert repo.get_active_version_id_for_country("SSD") == old_version
    assert repo.get_active_version_id_for_country("ETH") == new_version
    assert {country.country_iso3 for country in countries} == {"ETH", "SSD"}
    assert ssd_rows[0].price == 10.0
    assert eth_rows[0].price == 110.0


def test_country_incremental_snapshot_promotes_only_selected_country(tmp_path):
    repo = _repo(tmp_path)
    source_version = seed_cache_snapshot(
        repo,
        cache_version_id="11111111-1111-1111-1111-111111111111",
        country_iso3="SSD",
        country_name="South Sudan",
    )
    target_version = repo.create_cache_version(
        cache_version_id="33333333-3333-3333-3333-333333333333",
        refresh_type="manual_country_latest",
        triggered_by="pytest",
    )

    repo.copy_country_snapshot(
        source_cache_version_id=source_version,
        target_cache_version_id=target_version,
        country_iso3="SSD",
    )
    inserted = repo.upsert_monthly_prices(
        cache_version_id=target_version,
        country_iso3="SSD",
        prices=[
            {
                "country_iso3": "SSD",
                "commodity_id": 1,
                "commodity_name": "Maize",
                "market_id": 10,
                "market_name": "Juba",
                "admin1_name": "Central Equatoria",
                "price_date": "2025-02-01",
                "price": 12,
                "currency_id": 200,
                "currency_code": "SSP",
                "currency_name": "South Sudanese Pound",
                "commodity_unit_id": 100,
                "commodity_unit_name": "kg",
                "price_type_id": 1,
                "price_type_name": "Retail",
                "price_flag": "actual",
            },
            {
                "country_iso3": "SSD",
                "commodity_id": 1,
                "commodity_name": "Maize",
                "market_id": 10,
                "market_name": "Juba",
                "admin1_name": "Central Equatoria",
                "price_date": "2025-03-01",
                "price": 14,
                "currency_id": 200,
                "currency_code": "SSP",
                "currency_name": "South Sudanese Pound",
                "commodity_unit_id": 100,
                "commodity_unit_name": "kg",
                "price_type_id": 1,
                "price_type_name": "Retail",
                "price_flag": "actual",
            },
        ],
    )
    repo.set_country_active_version("SSD", target_version)

    rows = repo.get_price_window("SSD", "2025-03-01", "2025-03-01", commodity_ids=[1])
    metadata = repo.get_country_metadata("SSD")
    availability = repo.get_country_availability("SSD")

    assert inserted == 1
    assert repo.get_active_version_id() == source_version
    assert repo.get_active_version_id_for_country("SSD") == target_version
    assert rows[0].price == 14.0
    assert metadata is not None
    assert [item.commodity_name for item in metadata.commodities] == ["Beans", "Maize"]
    assert availability is not None
    assert availability.date_end.isoformat() == "2025-03-01"


def test_refresh_lock_blocks_concurrent_refreshes(tmp_path):
    repo = _repo(tmp_path)

    assert repo.acquire_refresh_lock("weekly_full_refresh", "owner-a", 30) is True
    assert repo.acquire_refresh_lock("weekly_full_refresh", "owner-b", 30) is False

    repo.release_refresh_lock("weekly_full_refresh", "owner-a")

    assert repo.acquire_refresh_lock("weekly_full_refresh", "owner-b", 30) is True


def test_publish_cache_version_is_atomic_when_final_update_fails(tmp_path):
    repo = _repo(tmp_path)
    old_version = seed_cache_snapshot(
        repo,
        cache_version_id="11111111-1111-1111-1111-111111111111",
        country_iso3="SSD",
        country_name="South Sudan",
    )
    new_version = repo.create_cache_version(cache_version_id="22222222-2222-2222-2222-222222222222")
    repo.insert_country_snapshot(
        cache_version_id=new_version,
        country_iso3="SSD",
        country_name="South Sudan",
        commodities=[
            {
                "commodity_id": 1,
                "commodity_name": "Maize",
                "commodity_unit_id": 100,
                "commodity_unit_name": "kg",
            }
        ],
        markets=[{"market_id": 10, "market_name": "Juba", "admin1_name": "Central Equatoria"}],
        prices=[
            {
                "country_iso3": "SSD",
                "commodity_id": 1,
                "commodity_name": "Maize",
                "market_id": 10,
                "market_name": "Juba",
                "admin1_name": "Central Equatoria",
                "price_date": "2025-03-01",
                "price": 99,
                "commodity_unit_id": 100,
                "commodity_unit_name": "kg",
                "price_flag": "actual",
            }
        ],
        latest_price_date="2025-03-01",
        currency_code="SSP",
        currency_name="South Sudanese Pound",
    )
    repo.record_country_result(
        cache_version_id=new_version,
        country_iso3="SSD",
        status="success",
        rows_prices=1,
        rows_commodities=1,
        rows_markets=1,
        latest_price_date="2025-03-01",
    )
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TRIGGER force_publish_failure
                BEFORE UPDATE ON price_cache_versions
                WHEN NEW.cache_version_id = '22222222-2222-2222-2222-222222222222'
                BEGIN
                    SELECT RAISE(ABORT, 'forced publish failure');
                END
                """
            )
        )

    with pytest.raises(Exception, match="forced publish failure"):
        repo.publish_cache_version(
            new_version,
            country_iso3s=["SSD"],
            status="active",
            validation_summary={"test": True},
        )

    assert repo.get_active_version_id() == old_version
    assert repo.get_active_version_id_for_country("SSD") == old_version
    assert repo.get_cache_refresh(new_version).status == "building"


def test_get_commodity_price_units_aggregates_per_commodity(tmp_path):
    repo = _repo(tmp_path)
    seed_cache_snapshot(repo)

    units = repo.get_commodity_price_units("SSD")

    assert units == {1: (100, "kg"), 2: (100, "kg")}


def test_get_commodity_price_units_narrows_to_requested_ids(tmp_path):
    repo = _repo(tmp_path)
    seed_cache_snapshot(repo)

    units = repo.get_commodity_price_units("SSD", commodity_ids=[1])

    assert units == {1: (100, "kg")}


def test_get_commodity_price_units_skips_blank_unit_rows(tmp_path):
    repo = _repo(tmp_path)
    cache_version_id = seed_cache_snapshot(repo)
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE cached_price_monthly
                SET commodity_unit_name = '',
                    commodity_unit_id = NULL
                WHERE cache_version_id = :cache_version_id
                  AND commodity_id = 2
                """
            ),
            {"cache_version_id": cache_version_id},
        )

    units = repo.get_commodity_price_units("SSD")

    assert units == {1: (100, "kg")}


def test_get_commodity_price_units_returns_empty_for_unknown_country(tmp_path):
    repo = _repo(tmp_path)
    seed_cache_snapshot(repo)

    assert repo.get_commodity_price_units("XXX") == {}
