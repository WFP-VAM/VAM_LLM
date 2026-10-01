from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import POSTGRES_BACKEND, SQLITE_BACKEND, PriceCacheConfig
from .db_resilience import retry_disconnected_read
from .schemas import (
    CacheStatus,
    CacheRefreshSummary,
    CountryAvailability,
    CommodityRecord,
    CountryMetadata,
    CountryRecord,
    CountryRefreshStatus,
    CurrencyRecord,
    MarketRecord,
    MonthlyPriceRecord,
    UnitRecord,
)


MappingLike = Mapping[str, Any]


def _merge_libpq_options(database_url: str, extra: str) -> str:
    """Append libpq options to any already present in the URL query string.

    SQLAlchemy overrides URL-derived connect kwargs with ``connect_args``, so a
    bare ``options`` entry would silently drop e.g. ``options=-csearch_path=...``
    carried by the configured URL.
    """
    try:
        from sqlalchemy.engine import make_url

        existing = make_url(database_url).query.get("options")
    except Exception:
        existing = None
    if isinstance(existing, (tuple, list)):
        existing = existing[0] if existing else None
    if existing:
        return f"{existing} {extra}"
    return extra


def create_price_cache_engine(
    config: PriceCacheConfig,
    *,
    apply_statement_timeout: bool = True,
) -> Engine:
    if config.backend == SQLITE_BACKEND:
        sqlite_path = config.sqlite_path
        if not sqlite_path.is_absolute():
            sqlite_path = Path.cwd() / sqlite_path
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(f"sqlite:///{sqlite_path.as_posix()}", future=True)
    if config.backend == POSTGRES_BACKEND:
        if not config.database_url:
            raise ValueError("PRICE_CACHE_DATABASE_URL is required for the postgres price cache backend.")
        # Hosting networks may drop idle TCP flows; without these
        # libpq settings a checked-out dead socket blocks on TCP retransmission
        # for 15+ minutes instead of failing fast for retry_disconnected_read.
        connect_args: Dict[str, Any] = {
            "connect_timeout": config.connect_timeout_seconds,
            "keepalives": 1,
            "keepalives_idle": config.tcp_keepalives_idle_seconds,
            "keepalives_interval": config.tcp_keepalives_interval_seconds,
            "keepalives_count": config.tcp_keepalives_count,
            "tcp_user_timeout": config.tcp_user_timeout_ms,
        }
        if apply_statement_timeout:
            connect_args["options"] = _merge_libpq_options(
                config.database_url,
                f"-c statement_timeout={config.statement_timeout_ms}",
            )
        return create_engine(
            config.database_url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            pool_timeout=config.pool_timeout_seconds,
            connect_args=connect_args,
        )
    raise ValueError(f"Unsupported price cache backend {config.backend!r}.")


def _disable_statement_timeout_for_transaction(conn: Any) -> None:
    """Lift the session statement_timeout for one known-long write transaction."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("SET LOCAL statement_timeout = 0"))


class SqlPriceCacheRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @retry_disconnected_read
    def get_cache_status(self) -> CacheStatus:
        with self.engine.begin() as conn:
            active = conn.execute(
                text(
                    """
                    SELECT v.*
                    FROM price_cache_active_version av
                    JOIN price_cache_versions v
                        ON v.cache_version_id = av.cache_version_id
                    WHERE av.singleton_id = 1
                    """
                )
            ).mappings().first()
            active_country_count = conn.execute(
                text("SELECT COUNT(*) AS count FROM price_cache_country_active_versions")
            ).mappings().first()
        if active is None:
            return CacheStatus(
                has_active_cache=False,
                active_country_count=int((active_country_count or {}).get("count") or 0),
            )
        return CacheStatus(
            has_active_cache=True,
            active_version_id=str(active["cache_version_id"]),
            status=_optional_str(active.get("status")),
            activated_at=_to_datetime(active.get("activated_at")),
            completed_at=_to_datetime(active.get("completed_at")),
            rows_prices=int(active.get("rows_prices") or 0),
            rows_commodities=int(active.get("rows_commodities") or 0),
            rows_markets=int(active.get("rows_markets") or 0),
            rows_countries=int(active.get("rows_countries") or 0),
            active_country_count=int((active_country_count or {}).get("count") or 0),
            validation_summary=_json_dict(active.get("validation_summary_json")),
            error_message=_optional_str(active.get("error_message")),
        )

    @retry_disconnected_read
    def get_active_version_id(self) -> Optional[str]:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT cache_version_id
                    FROM price_cache_active_version
                    WHERE singleton_id = 1
                    """
                )
            ).mappings().first()
        if row is None:
            return None
        return str(row["cache_version_id"])

    @retry_disconnected_read
    def get_active_version_id_for_country(self, country_iso3: str) -> Optional[str]:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT cache_version_id
                    FROM price_cache_country_active_versions
                    WHERE country_iso3 = :country_iso3
                    """
                ),
                {"country_iso3": country_iso3.upper()},
            ).mappings().first()
        if row is not None:
            return str(row["cache_version_id"])
        return self.get_active_version_id()

    @retry_disconnected_read
    def list_countries(self) -> List[CountryRecord]:
        with self.engine.begin() as conn:
            country_active_count = conn.execute(
                text("SELECT COUNT(*) AS count FROM price_cache_country_active_versions")
            ).mappings().first()
            if int((country_active_count or {}).get("count") or 0) > 0:
                rows = conn.execute(
                    text(
                        """
                        SELECT c.*
                        FROM price_cache_country_active_versions av
                        JOIN cached_countries c
                          ON c.country_iso3 = av.country_iso3
                         AND c.cache_version_id = av.cache_version_id
                        ORDER BY c.country_name
                        """
                    )
                ).mappings().all()
                return [_country_from_row(row) for row in rows]

            active_version_id = self.get_active_version_id()
            if active_version_id is None:
                return []
            rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM cached_countries
                    WHERE cache_version_id = :cache_version_id
                    ORDER BY country_name
                    """
                ),
                {"cache_version_id": active_version_id},
            ).mappings().all()
        return [_country_from_row(row) for row in rows]

    @retry_disconnected_read
    def get_country_metadata(self, country_iso3: str) -> Optional[CountryMetadata]:
        active_version_id = self.get_active_version_id_for_country(country_iso3)
        if active_version_id is None:
            return None
        params = {
            "cache_version_id": active_version_id,
            "country_iso3": country_iso3.upper(),
        }
        with self.engine.begin() as conn:
            country_row = conn.execute(
                text(
                    """
                    SELECT *
                    FROM cached_countries
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                params,
            ).mappings().first()
            if country_row is None:
                return None

            commodity_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM cached_commodities
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                      AND active = :active
                    ORDER BY commodity_name
                    """
                ),
                {**params, "active": _active_param(conn)},
            ).mappings().all()
            unit_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM cached_units
                    WHERE cache_version_id = :cache_version_id
                      AND active = :active
                    ORDER BY commodity_unit_name
                    """
                ),
                {"cache_version_id": active_version_id, "active": _active_param(conn)},
            ).mappings().all()
            market_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM cached_markets
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                      AND active = :active
                    ORDER BY market_name
                    """
                ),
                {**params, "active": _active_param(conn)},
            ).mappings().all()
            currency_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM cached_currencies
                    WHERE cache_version_id = :cache_version_id
                    ORDER BY currency_name
                    """
                ),
                {"cache_version_id": active_version_id},
            ).mappings().all()

        return CountryMetadata(
            country=_country_from_row(country_row),
            commodities=[_commodity_from_row(row) for row in commodity_rows],
            units=[_unit_from_row(row) for row in unit_rows],
            markets=[_market_from_row(row) for row in market_rows],
            currencies=[_currency_from_row(row) for row in currency_rows],
        )

    @retry_disconnected_read
    def get_country_availability(self, country_iso3: str) -> Optional[CountryAvailability]:
        active_version_id = self.get_active_version_id_for_country(country_iso3)
        if active_version_id is None:
            return None
        country = country_iso3.upper()
        params = {"cache_version_id": active_version_id, "country_iso3": country}
        with self.engine.begin() as conn:
            country_row = conn.execute(
                text(
                    """
                    SELECT latest_price_date
                    FROM cached_countries
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                params,
            ).mappings().first()
            if country_row is None:
                return None

            date_row = conn.execute(
                text(
                    """
                    SELECT
                        MIN(price_date) AS date_start,
                        MAX(price_date) AS date_end
                    FROM cached_price_monthly
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                params,
            ).mappings().first()
            commodity_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT commodity_id
                    FROM cached_price_monthly
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    ORDER BY commodity_id
                    """
                ),
                params,
            ).mappings().all()
            admin1_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        COALESCE(NULLIF(p.admin1_name, ''), m.admin1_name) AS admin1_name
                    FROM cached_price_monthly p
                    LEFT JOIN cached_markets m
                      ON m.cache_version_id = p.cache_version_id
                     AND m.country_iso3 = p.country_iso3
                     AND m.market_id = p.market_id
                    WHERE p.cache_version_id = :cache_version_id
                      AND p.country_iso3 = :country_iso3
                      AND COALESCE(NULLIF(p.admin1_name, ''), m.admin1_name) IS NOT NULL
                    ORDER BY admin1_name
                    """
                ),
                params,
            ).mappings().all()
            unit_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        commodity_unit_id,
                        commodity_unit_name
                    FROM cached_price_monthly
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                      AND commodity_unit_id IS NOT NULL
                      AND commodity_unit_name IS NOT NULL
                      AND commodity_unit_name <> ''
                    ORDER BY commodity_unit_name
                    """
                ),
                params,
            ).mappings().all()

        date_start = _to_date((date_row or {}).get("date_start"))
        date_end = _to_date((date_row or {}).get("date_end"))
        latest_price_date = date_end or _to_date(country_row.get("latest_price_date"))
        return CountryAvailability(
            cache_version_id=active_version_id,
            country_iso3=country,
            date_start=date_start,
            date_end=date_end,
            latest_price_date=latest_price_date,
            priced_commodity_ids=[
                int(row["commodity_id"])
                for row in commodity_rows
                if row.get("commodity_id") is not None
            ],
            admin1_names=[
                str(row["admin1_name"])
                for row in admin1_rows
                if row.get("admin1_name") not in (None, "")
            ],
            units=[
                UnitRecord(
                    cache_version_id=active_version_id,
                    commodity_unit_id=int(row["commodity_unit_id"]),
                    commodity_unit_name=str(row["commodity_unit_name"]),
                    active=True,
                )
                for row in unit_rows
                if row.get("commodity_unit_id") is not None
                and row.get("commodity_unit_name") not in (None, "")
            ],
        )

    @retry_disconnected_read
    def get_price_window(
        self,
        country_iso3: str,
        start_date: date | str,
        end_date: date | str,
        *,
        commodity_ids: Optional[Sequence[int]] = None,
        market_ids: Optional[Sequence[int]] = None,
        admin1_names: Optional[Sequence[str]] = None,
    ) -> List[MonthlyPriceRecord]:
        active_version_id = self.get_active_version_id_for_country(country_iso3)
        if active_version_id is None:
            return []

        clauses = [
            "p.cache_version_id = :cache_version_id",
            "p.country_iso3 = :country_iso3",
            "p.price_date >= :start_date",
            "p.price_date <= :end_date",
        ]
        params: Dict[str, Any] = {
            "cache_version_id": active_version_id,
            "country_iso3": country_iso3.upper(),
            "start_date": _date_wire(start_date),
            "end_date": _date_wire(end_date),
        }
        admin1_expr = "COALESCE(NULLIF(p.admin1_name, ''), m.admin1_name)"
        _add_in_clause(clauses, params, "p.commodity_id", commodity_ids, "commodity_id")
        _add_in_clause(clauses, params, "p.market_id", market_ids, "market_id")
        _add_in_clause(clauses, params, admin1_expr, admin1_names, "admin1_name")

        # The PriceMonthly payload has no admin fields, so older cache versions can
        # hold NULL admin1/admin2 on price rows; market metadata fills the gap.
        query = f"""
            SELECT
                p.cache_version_id,
                p.country_iso3,
                p.commodity_id,
                p.market_id,
                p.price_date,
                p.price,
                p.currency_id,
                p.currency_code,
                p.currency_name,
                p.commodity_unit_id,
                p.commodity_unit_name,
                p.price_type_id,
                p.price_type_name,
                p.price_flag,
                p.original_frequency,
                p.observations,
                p.source_payload_hash,
                {admin1_expr} AS admin1_name,
                COALESCE(NULLIF(p.admin2_name, ''), m.admin2_name) AS admin2_name,
                COALESCE(NULLIF(p.market_name, ''), m.market_name) AS market_name,
                p.commodity_name,
                p.data_source
            FROM cached_price_monthly p
            LEFT JOIN cached_markets m
              ON m.cache_version_id = p.cache_version_id
             AND m.country_iso3 = p.country_iso3
             AND m.market_id = p.market_id
            WHERE {' AND '.join(clauses)}
            ORDER BY p.price_date, admin1_name, market_name, p.commodity_name
        """
        with self.engine.begin() as conn:
            rows = conn.execute(text(query), params).mappings().all()
        return [_monthly_price_from_row(row) for row in rows]

    @retry_disconnected_read
    def get_commodity_price_units(
        self,
        country_iso3: str,
        *,
        commodity_ids: Optional[Sequence[int]] = None,
    ) -> Dict[int, Tuple[Optional[int], str]]:
        """Return one (unit_id, unit_name) per commodity derived from price rows.

        Aggregates in SQL so callers never materialize the country's full price
        history just to recover unit metadata.
        """
        active_version_id = self.get_active_version_id_for_country(country_iso3)
        if active_version_id is None:
            return {}

        clauses = [
            "cache_version_id = :cache_version_id",
            "country_iso3 = :country_iso3",
            "commodity_unit_name IS NOT NULL",
            "commodity_unit_name <> ''",
        ]
        params: Dict[str, Any] = {
            "cache_version_id": active_version_id,
            "country_iso3": country_iso3.upper(),
        }
        _add_in_clause(clauses, params, "commodity_id", commodity_ids, "commodity_id")
        query = f"""
            SELECT commodity_id, commodity_unit_id, commodity_unit_name
            FROM cached_price_monthly
            WHERE {' AND '.join(clauses)}
            GROUP BY commodity_id, commodity_unit_id, commodity_unit_name
        """
        with self.engine.begin() as conn:
            rows = conn.execute(text(query), params).mappings().all()

        # Deterministic pick per commodity, sorted in Python because NULL
        # ordering differs between sqlite and postgres.
        units: Dict[int, Tuple[Optional[int], str]] = {}
        def _sort_key(row: Mapping[str, Any]) -> Tuple[int, str, int, Any]:
            unit_id = row.get("commodity_unit_id")
            return (
                int(row["commodity_id"]),
                str(row["commodity_unit_name"]),
                1 if unit_id is None else 0,
                unit_id if unit_id is not None else 0,
            )

        for row in sorted(rows, key=_sort_key):
            commodity_id = int(row["commodity_id"])
            if commodity_id in units:
                continue
            unit_id = row.get("commodity_unit_id")
            units[commodity_id] = (
                int(unit_id) if unit_id is not None else None,
                str(row["commodity_unit_name"]),
            )
        return units

    def copy_country_snapshot(
        self,
        *,
        source_cache_version_id: str,
        target_cache_version_id: str,
        country_iso3: str,
    ) -> None:
        country = country_iso3.upper()
        params = {
            "source_cache_version_id": source_cache_version_id,
            "target_cache_version_id": target_cache_version_id,
            "country_iso3": country,
        }
        with self.engine.begin() as conn:
            _disable_statement_timeout_for_transaction(conn)
            for table in ("cached_price_monthly", "cached_markets", "cached_commodities", "cached_countries"):
                conn.execute(
                    text(
                        f"""
                        DELETE FROM {table}
                        WHERE cache_version_id = :target_cache_version_id
                          AND country_iso3 = :country_iso3
                        """
                    ),
                    params,
                )
            for table in ("cached_units", "cached_currencies"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE cache_version_id = :target_cache_version_id"),
                    params,
                )

            conn.execute(
                text(
                    """
                    INSERT INTO cached_units (
                        cache_version_id,
                        commodity_unit_id,
                        commodity_unit_name,
                        conversion_to_kg_l,
                        active
                    )
                    SELECT
                        :target_cache_version_id,
                        commodity_unit_id,
                        commodity_unit_name,
                        conversion_to_kg_l,
                        active
                    FROM cached_units
                    WHERE cache_version_id = :source_cache_version_id
                    """
                ),
                params,
            )
            conn.execute(
                text(
                    """
                    INSERT INTO cached_currencies (
                        cache_version_id,
                        currency_id,
                        currency_code,
                        currency_name
                    )
                    SELECT
                        :target_cache_version_id,
                        currency_id,
                        currency_code,
                        currency_name
                    FROM cached_currencies
                    WHERE cache_version_id = :source_cache_version_id
                    """
                ),
                params,
            )
            conn.execute(
                text(
                    """
                    INSERT INTO cached_countries (
                        cache_version_id,
                        country_iso3,
                        country_name,
                        currency_code,
                        currency_name,
                        latest_price_date
                    )
                    SELECT
                        :target_cache_version_id,
                        country_iso3,
                        country_name,
                        currency_code,
                        currency_name,
                        latest_price_date
                    FROM cached_countries
                    WHERE cache_version_id = :source_cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                params,
            )
            conn.execute(
                text(
                    """
                    INSERT INTO cached_commodities (
                        cache_version_id,
                        country_iso3,
                        commodity_id,
                        commodity_name,
                        commodity_unit_id,
                        commodity_unit_name,
                        category_name,
                        active
                    )
                    SELECT
                        :target_cache_version_id,
                        country_iso3,
                        commodity_id,
                        commodity_name,
                        commodity_unit_id,
                        commodity_unit_name,
                        category_name,
                        active
                    FROM cached_commodities
                    WHERE cache_version_id = :source_cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                params,
            )
            conn.execute(
                text(
                    """
                    INSERT INTO cached_markets (
                        cache_version_id,
                        country_iso3,
                        market_id,
                        market_name,
                        admin1_name,
                        admin2_name,
                        latitude,
                        longitude,
                        active
                    )
                    SELECT
                        :target_cache_version_id,
                        country_iso3,
                        market_id,
                        market_name,
                        admin1_name,
                        admin2_name,
                        latitude,
                        longitude,
                        active
                    FROM cached_markets
                    WHERE cache_version_id = :source_cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                params,
            )
            conn.execute(
                text(
                    """
                    INSERT INTO cached_price_monthly (
                        cache_version_id,
                        country_iso3,
                        commodity_id,
                        market_id,
                        price_date,
                        price,
                        currency_id,
                        currency_code,
                        currency_name,
                        commodity_unit_id,
                        commodity_unit_name,
                        price_type_id,
                        price_type_name,
                        price_flag,
                        original_frequency,
                        observations,
                        source_payload_hash,
                        admin1_name,
                        admin2_name,
                        market_name,
                        commodity_name,
                        data_source
                    )
                    SELECT
                        :target_cache_version_id,
                        country_iso3,
                        commodity_id,
                        market_id,
                        price_date,
                        price,
                        currency_id,
                        currency_code,
                        currency_name,
                        commodity_unit_id,
                        commodity_unit_name,
                        price_type_id,
                        price_type_name,
                        price_flag,
                        original_frequency,
                        observations,
                        source_payload_hash,
                        admin1_name,
                        admin2_name,
                        market_name,
                        commodity_name,
                        data_source
                    FROM cached_price_monthly
                    WHERE cache_version_id = :source_cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                params,
            )

    def upsert_country_metadata(
        self,
        *,
        cache_version_id: str,
        country_iso3: str,
        commodities: Sequence[MappingLike],
        markets: Sequence[MappingLike],
    ) -> None:
        country = country_iso3.upper()
        commodity_rows = [
            {
                "cache_version_id": cache_version_id,
                "country_iso3": country,
                "commodity_id": _required_int(row, "commodity_id"),
                "commodity_name": str(row.get("commodity_name") or ""),
                "commodity_unit_id": _optional_int(row.get("commodity_unit_id")),
                "commodity_unit_name": _optional_str(row.get("commodity_unit_name")),
                "category_name": _optional_str(row.get("category_name")),
                "active": _bool_param_value(row.get("active", True)),
            }
            for row in commodities
            if row.get("commodity_id") not in (None, "")
        ]
        market_rows = [
            {
                "cache_version_id": cache_version_id,
                "country_iso3": country,
                "market_id": _required_int(row, "market_id"),
                "market_name": str(row.get("market_name") or ""),
                "admin1_name": _optional_str(row.get("admin1_name")),
                "admin2_name": _optional_str(row.get("admin2_name")),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "active": _bool_param_value(row.get("active", True)),
            }
            for row in markets
            if row.get("market_id") not in (None, "")
        ]
        with self.engine.begin() as conn:
            _disable_statement_timeout_for_transaction(conn)
            if commodity_rows:
                _delete_by_ids(
                    conn,
                    "cached_commodities",
                    "commodity_id",
                    [row["commodity_id"] for row in commodity_rows],
                    cache_version_id=cache_version_id,
                    country_iso3=country,
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO cached_commodities (
                            cache_version_id,
                            country_iso3,
                            commodity_id,
                            commodity_name,
                            commodity_unit_id,
                            commodity_unit_name,
                            category_name,
                            active
                        ) VALUES (
                            :cache_version_id,
                            :country_iso3,
                            :commodity_id,
                            :commodity_name,
                            :commodity_unit_id,
                            :commodity_unit_name,
                            :category_name,
                            :active
                        )
                        """
                    ),
                    commodity_rows,
                )
            if market_rows:
                _delete_by_ids(
                    conn,
                    "cached_markets",
                    "market_id",
                    [row["market_id"] for row in market_rows],
                    cache_version_id=cache_version_id,
                    country_iso3=country,
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO cached_markets (
                            cache_version_id,
                            country_iso3,
                            market_id,
                            market_name,
                            admin1_name,
                            admin2_name,
                            latitude,
                            longitude,
                            active
                        ) VALUES (
                            :cache_version_id,
                            :country_iso3,
                            :market_id,
                            :market_name,
                            :admin1_name,
                            :admin2_name,
                            :latitude,
                            :longitude,
                            :active
                        )
                        """
                    ),
                    market_rows,
                )

    def upsert_monthly_prices(
        self,
        *,
        cache_version_id: str,
        country_iso3: str,
        prices: Sequence[MappingLike],
    ) -> int:
        country = country_iso3.upper()
        price_rows = [_price_insert_row(cache_version_id, country, row) for row in prices]
        if not price_rows:
            return 0
        existing_keys = self.get_country_price_keys(cache_version_id=cache_version_id, country_iso3=country)
        insert_rows = [
            row for row in price_rows if _canonical_key_from_price_row(row) not in existing_keys
        ]
        if not insert_rows:
            return 0
        with self.engine.begin() as conn:
            _disable_statement_timeout_for_transaction(conn)
            conn.execute(
                text(
                    """
                    INSERT INTO cached_price_monthly (
                        cache_version_id,
                        country_iso3,
                        commodity_id,
                        market_id,
                        price_date,
                        price,
                        currency_id,
                        currency_code,
                        currency_name,
                        commodity_unit_id,
                        commodity_unit_name,
                        price_type_id,
                        price_type_name,
                        price_flag,
                        original_frequency,
                        observations,
                        source_payload_hash,
                        admin1_name,
                        admin2_name,
                        market_name,
                        commodity_name,
                        data_source
                    ) VALUES (
                        :cache_version_id,
                        :country_iso3,
                        :commodity_id,
                        :market_id,
                        :price_date,
                        :price,
                        :currency_id,
                        :currency_code,
                        :currency_name,
                        :commodity_unit_id,
                        :commodity_unit_name,
                        :price_type_id,
                        :price_type_name,
                        :price_flag,
                        :original_frequency,
                        :observations,
                        :source_payload_hash,
                        :admin1_name,
                        :admin2_name,
                        :market_name,
                        :commodity_name,
                        :data_source
                    )
                    """
                ),
                insert_rows,
            )
            latest = conn.execute(
                text(
                    """
                    SELECT MAX(price_date) AS latest_price_date
                    FROM cached_price_monthly
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                {"cache_version_id": cache_version_id, "country_iso3": country},
            ).mappings().first()
            conn.execute(
                text(
                    """
                    UPDATE cached_countries
                    SET latest_price_date = :latest_price_date
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                {
                    "cache_version_id": cache_version_id,
                    "country_iso3": country,
                    "latest_price_date": (latest or {}).get("latest_price_date"),
                },
            )
        return len(insert_rows)

    @retry_disconnected_read
    def get_country_price_keys(self, *, cache_version_id: str, country_iso3: str) -> set[tuple[Any, ...]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cache_version_id,
                        country_iso3,
                        commodity_id,
                        market_id,
                        price_date,
                        price_type_name,
                        price_flag,
                        COALESCE(price_type_id, -1) AS price_type_id,
                        COALESCE(currency_id, -1) AS currency_id,
                        COALESCE(commodity_unit_id, -1) AS commodity_unit_id
                    FROM cached_price_monthly
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                {"cache_version_id": cache_version_id, "country_iso3": country_iso3.upper()},
            ).mappings().all()
        return {_canonical_key_from_price_row(row) for row in rows}

    @retry_disconnected_read
    def count_country_price_rows(self, *, cache_version_id: str, country_iso3: str) -> int:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT COUNT(*) AS count
                    FROM cached_price_monthly
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                {"cache_version_id": cache_version_id, "country_iso3": country_iso3.upper()},
            ).mappings().first()
        return int((row or {}).get("count") or 0)

    def create_cache_version(
        self,
        *,
        cache_version_id: Optional[str] = None,
        refresh_type: str = "weekly_full",
        triggered_by: str = "worker",
        source_host: Optional[str] = None,
        source_env: Optional[str] = None,
    ) -> str:
        version_id = cache_version_id or str(uuid.uuid4())
        now = _now_wire()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO price_cache_versions (
                        cache_version_id,
                        status,
                        refresh_type,
                        started_at,
                        triggered_by,
                        source_host,
                        source_env
                    ) VALUES (
                        :cache_version_id,
                        'building',
                        :refresh_type,
                        :started_at,
                        :triggered_by,
                        :source_host,
                        :source_env
                    )
                    """
                ),
                {
                    "cache_version_id": version_id,
                    "refresh_type": refresh_type,
                    "started_at": now,
                    "triggered_by": triggered_by,
                    "source_host": source_host,
                    "source_env": source_env,
                },
            )
        return version_id

    def insert_units(self, cache_version_id: str, rows: Sequence[MappingLike]) -> None:
        normalized_by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            unit_id = _optional_int(row.get("commodity_unit_id"))
            unit_name = _optional_str(row.get("commodity_unit_name"))
            if unit_id is None or not unit_name:
                continue
            normalized_by_id[unit_id] = {
                "cache_version_id": cache_version_id,
                "commodity_unit_id": unit_id,
                "commodity_unit_name": unit_name,
                "conversion_to_kg_l": row.get("conversion_to_kg_l"),
                "active": _bool_param_value(row.get("active", True)),
            }
        normalized = list(normalized_by_id.values())
        if not normalized:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO cached_units (
                        cache_version_id,
                        commodity_unit_id,
                        commodity_unit_name,
                        conversion_to_kg_l,
                        active
                    ) VALUES (
                        :cache_version_id,
                        :commodity_unit_id,
                        :commodity_unit_name,
                        :conversion_to_kg_l,
                        :active
                    )
                    """
                ),
                normalized,
            )

    def insert_currencies(self, cache_version_id: str, rows: Sequence[MappingLike]) -> None:
        normalized = [
            {
                "cache_version_id": cache_version_id,
                "currency_id": _required_int(row, "currency_id"),
                "currency_code": _optional_str(row.get("currency_code")),
                "currency_name": str(row.get("currency_name") or ""),
            }
            for row in rows
        ]
        if not normalized:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO cached_currencies (
                        cache_version_id,
                        currency_id,
                        currency_code,
                        currency_name
                    ) VALUES (
                        :cache_version_id,
                        :currency_id,
                        :currency_code,
                        :currency_name
                    )
                    """
                ),
                normalized,
            )

    def insert_country_snapshot(
        self,
        *,
        cache_version_id: str,
        country_iso3: str,
        country_name: str,
        commodities: Sequence[MappingLike],
        markets: Sequence[MappingLike],
        prices: Sequence[MappingLike],
        latest_price_date: Optional[date | str] = None,
        currency_code: Optional[str] = None,
        currency_name: Optional[str] = None,
    ) -> None:
        country = country_iso3.upper()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO cached_countries (
                        cache_version_id,
                        country_iso3,
                        country_name,
                        currency_code,
                        currency_name,
                        latest_price_date
                    ) VALUES (
                        :cache_version_id,
                        :country_iso3,
                        :country_name,
                        :currency_code,
                        :currency_name,
                        :latest_price_date
                    )
                    """
                ),
                {
                    "cache_version_id": cache_version_id,
                    "country_iso3": country,
                    "country_name": country_name,
                    "currency_code": currency_code,
                    "currency_name": currency_name,
                    "latest_price_date": _optional_date_wire(latest_price_date),
                },
            )

            commodity_rows = [
                {
                    "cache_version_id": cache_version_id,
                    "country_iso3": country,
                    "commodity_id": _required_int(row, "commodity_id"),
                    "commodity_name": str(row.get("commodity_name") or ""),
                    "commodity_unit_id": _optional_int(row.get("commodity_unit_id")),
                    "commodity_unit_name": _optional_str(row.get("commodity_unit_name")),
                    "category_name": _optional_str(row.get("category_name")),
                    "active": _bool_param_value(row.get("active", True)),
                }
                for row in commodities
            ]
            if commodity_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO cached_commodities (
                            cache_version_id,
                            country_iso3,
                            commodity_id,
                            commodity_name,
                            commodity_unit_id,
                            commodity_unit_name,
                            category_name,
                            active
                        ) VALUES (
                            :cache_version_id,
                            :country_iso3,
                            :commodity_id,
                            :commodity_name,
                            :commodity_unit_id,
                            :commodity_unit_name,
                            :category_name,
                            :active
                        )
                        """
                    ),
                    commodity_rows,
                )

            market_rows = [
                {
                    "cache_version_id": cache_version_id,
                    "country_iso3": country,
                    "market_id": _required_int(row, "market_id"),
                    "market_name": str(row.get("market_name") or ""),
                    "admin1_name": _optional_str(row.get("admin1_name")),
                    "admin2_name": _optional_str(row.get("admin2_name")),
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                    "active": _bool_param_value(row.get("active", True)),
                }
                for row in markets
            ]
            if market_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO cached_markets (
                            cache_version_id,
                            country_iso3,
                            market_id,
                            market_name,
                            admin1_name,
                            admin2_name,
                            latitude,
                            longitude,
                            active
                        ) VALUES (
                            :cache_version_id,
                            :country_iso3,
                            :market_id,
                            :market_name,
                            :admin1_name,
                            :admin2_name,
                            :latitude,
                            :longitude,
                            :active
                        )
                        """
                    ),
                    market_rows,
                )

            price_rows = [
                {
                    "cache_version_id": cache_version_id,
                    "country_iso3": country,
                    "commodity_id": _required_int(row, "commodity_id"),
                    "market_id": _required_int(row, "market_id"),
                    "price_date": _date_wire(row.get("price_date")),
                    "price": row.get("price"),
                    "currency_id": _optional_int(row.get("currency_id")),
                    "currency_code": _optional_str(row.get("currency_code")),
                    "currency_name": _optional_str(row.get("currency_name")),
                    "commodity_unit_id": _optional_int(row.get("commodity_unit_id")),
                    "commodity_unit_name": _optional_str(row.get("commodity_unit_name")),
                    "price_type_id": _optional_int(row.get("price_type_id")),
                    "price_type_name": str(row.get("price_type_name") or ""),
                    "price_flag": str(row.get("price_flag") or ""),
                    "original_frequency": _optional_str(row.get("original_frequency")),
                    "observations": _optional_int(row.get("observations")),
                    "source_payload_hash": _optional_str(row.get("source_payload_hash")),
                    "admin1_name": _optional_str(row.get("admin1_name")),
                    "admin2_name": _optional_str(row.get("admin2_name")),
                    "market_name": _optional_str(row.get("market_name")),
                    "commodity_name": _optional_str(row.get("commodity_name")),
                    "data_source": _optional_str(row.get("data_source")),
                }
                for row in prices
            ]
            if price_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO cached_price_monthly (
                            cache_version_id,
                            country_iso3,
                            commodity_id,
                            market_id,
                            price_date,
                            price,
                            currency_id,
                            currency_code,
                            currency_name,
                            commodity_unit_id,
                            commodity_unit_name,
                            price_type_id,
                            price_type_name,
                            price_flag,
                            original_frequency,
                            observations,
                            source_payload_hash,
                            admin1_name,
                            admin2_name,
                            market_name,
                            commodity_name,
                            data_source
                        ) VALUES (
                            :cache_version_id,
                            :country_iso3,
                            :commodity_id,
                            :market_id,
                            :price_date,
                            :price,
                            :currency_id,
                            :currency_code,
                            :currency_name,
                            :commodity_unit_id,
                            :commodity_unit_name,
                            :price_type_id,
                            :price_type_name,
                            :price_flag,
                            :original_frequency,
                            :observations,
                            :source_payload_hash,
                            :admin1_name,
                            :admin2_name,
                            :market_name,
                            :commodity_name,
                            :data_source
                        )
                        """
                    ),
                    price_rows,
                )

    def record_country_result(
        self,
        *,
        cache_version_id: str,
        country_iso3: str,
        status: str,
        rows_prices: int = 0,
        rows_commodities: int = 0,
        rows_markets: int = 0,
        latest_price_date: Optional[date | str] = None,
        validation_summary: Optional[dict[str, Any]] = None,
        error_message: Optional[str] = None,
        started_at: Optional[datetime | str] = None,
        completed_at: Optional[datetime | str] = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM price_cache_version_countries
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                {"cache_version_id": cache_version_id, "country_iso3": country_iso3.upper()},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO price_cache_version_countries (
                        cache_version_id,
                        country_iso3,
                        status,
                        rows_prices,
                        rows_commodities,
                        rows_markets,
                        latest_price_date,
                        validation_summary_json,
                        error_message,
                        started_at,
                        completed_at
                    ) VALUES (
                        :cache_version_id,
                        :country_iso3,
                        :status,
                        :rows_prices,
                        :rows_commodities,
                        :rows_markets,
                        :latest_price_date,
                        :validation_summary_json,
                        :error_message,
                        :started_at,
                        :completed_at
                    )
                    """
                ),
                {
                    "cache_version_id": cache_version_id,
                    "country_iso3": country_iso3.upper(),
                    "status": status,
                    "rows_prices": rows_prices,
                    "rows_commodities": rows_commodities,
                    "rows_markets": rows_markets,
                    "latest_price_date": _optional_date_wire(latest_price_date),
                    "validation_summary_json": _json_wire(validation_summary or {}),
                    "error_message": error_message,
                    "started_at": _datetime_wire(started_at) if started_at is not None else None,
                    "completed_at": _datetime_wire(completed_at) if completed_at is not None else _now_wire(),
                },
            )

    def publish_cache_version(
        self,
        cache_version_id: str,
        *,
        country_iso3s: Sequence[str],
        status: str,
        validation_summary: Optional[dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        countries = sorted({str(country).upper() for country in country_iso3s if country})
        activate = bool(countries) and status in {"active", "partial_active"}
        now = _now_wire()
        with self.engine.begin() as conn:
            counts = conn.execute(
                text(
                    """
                    SELECT
                        COALESCE(SUM(rows_prices), 0) AS rows_prices,
                        COALESCE(SUM(rows_commodities), 0) AS rows_commodities,
                        COALESCE(SUM(rows_markets), 0) AS rows_markets,
                        COUNT(*) AS rows_countries
                    FROM price_cache_version_countries
                    WHERE cache_version_id = :cache_version_id
                      AND status = 'success'
                    """
                ),
                {"cache_version_id": cache_version_id},
            ).mappings().first()

            if activate:
                for country in countries:
                    conn.execute(
                        text(
                            """
                            DELETE FROM price_cache_country_active_versions
                            WHERE country_iso3 = :country_iso3
                            """
                        ),
                        {"country_iso3": country},
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO price_cache_country_active_versions (
                                country_iso3,
                                cache_version_id,
                                activated_at
                            ) VALUES (
                                :country_iso3,
                                :cache_version_id,
                                :activated_at
                            )
                            """
                        ),
                        {
                            "country_iso3": country,
                            "cache_version_id": cache_version_id,
                            "activated_at": now,
                        },
                    )
                conn.execute(text("DELETE FROM price_cache_active_version WHERE singleton_id = 1"))
                conn.execute(
                    text(
                        """
                        INSERT INTO price_cache_active_version (
                            singleton_id,
                            cache_version_id,
                            activated_at
                        ) VALUES (1, :cache_version_id, :activated_at)
                        """
                    ),
                    {"cache_version_id": cache_version_id, "activated_at": now},
                )

            conn.execute(
                text(
                    """
                    UPDATE price_cache_versions
                    SET status = :status,
                        completed_at = :completed_at,
                        activated_at = :activated_at,
                        rows_prices = :rows_prices,
                        rows_commodities = :rows_commodities,
                        rows_markets = :rows_markets,
                        rows_countries = :rows_countries,
                        validation_summary_json = :validation_summary_json,
                        error_message = :error_message
                    WHERE cache_version_id = :cache_version_id
                    """
                ),
                {
                    "cache_version_id": cache_version_id,
                    "status": status,
                    "completed_at": now,
                    "activated_at": now if activate else None,
                    "rows_prices": int((counts or {}).get("rows_prices") or 0),
                    "rows_commodities": int((counts or {}).get("rows_commodities") or 0),
                    "rows_markets": int((counts or {}).get("rows_markets") or 0),
                    "rows_countries": int((counts or {}).get("rows_countries") or 0),
                    "validation_summary_json": _json_wire(validation_summary or {}),
                    "error_message": error_message,
                },
            )

    def promote_countries(self, cache_version_id: str, country_iso3s: Sequence[str]) -> None:
        countries = sorted({str(country).upper() for country in country_iso3s if country})
        if not countries:
            return
        now = _now_wire()
        with self.engine.begin() as conn:
            for country in countries:
                conn.execute(
                    text(
                        """
                        DELETE FROM price_cache_country_active_versions
                        WHERE country_iso3 = :country_iso3
                        """
                    ),
                    {"country_iso3": country},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO price_cache_country_active_versions (
                            country_iso3,
                            cache_version_id,
                            activated_at
                        ) VALUES (
                            :country_iso3,
                            :cache_version_id,
                            :activated_at
                        )
                        """
                    ),
                    {
                        "country_iso3": country,
                        "cache_version_id": cache_version_id,
                        "activated_at": now,
                    },
                )
            conn.execute(text("DELETE FROM price_cache_active_version WHERE singleton_id = 1"))
            conn.execute(
                text(
                    """
                    INSERT INTO price_cache_active_version (
                        singleton_id,
                        cache_version_id,
                        activated_at
                    ) VALUES (1, :cache_version_id, :activated_at)
                    """
                ),
                {"cache_version_id": cache_version_id, "activated_at": now},
            )

    def set_country_active_version(self, country_iso3: str, cache_version_id: str) -> None:
        country = country_iso3.upper()
        now = _now_wire()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM price_cache_country_active_versions
                    WHERE country_iso3 = :country_iso3
                    """
                ),
                {"country_iso3": country},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO price_cache_country_active_versions (
                        country_iso3,
                        cache_version_id,
                        activated_at
                    ) VALUES (
                        :country_iso3,
                        :cache_version_id,
                        :activated_at
                    )
                    """
                ),
                {
                    "country_iso3": country,
                    "cache_version_id": cache_version_id,
                    "activated_at": now,
                },
            )
            conn.execute(
                text(
                    """
                    UPDATE price_cache_versions
                    SET activated_at = :activated_at
                    WHERE cache_version_id = :cache_version_id
                    """
                ),
                {"cache_version_id": cache_version_id, "activated_at": now},
            )

    def finalize_cache_version(
        self,
        cache_version_id: str,
        *,
        status: str,
        validation_summary: Optional[dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with self.engine.begin() as conn:
            counts = conn.execute(
                text(
                    """
                    SELECT
                        COALESCE(SUM(rows_prices), 0) AS rows_prices,
                        COALESCE(SUM(rows_commodities), 0) AS rows_commodities,
                        COALESCE(SUM(rows_markets), 0) AS rows_markets,
                        COUNT(*) AS rows_countries
                    FROM price_cache_version_countries
                    WHERE cache_version_id = :cache_version_id
                      AND status = 'success'
                    """
                ),
                {"cache_version_id": cache_version_id},
            ).mappings().first()
            now = _now_wire()
            conn.execute(
                text(
                    """
                    UPDATE price_cache_versions
                    SET status = :status,
                        completed_at = :completed_at,
                        activated_at = :activated_at,
                        rows_prices = :rows_prices,
                        rows_commodities = :rows_commodities,
                        rows_markets = :rows_markets,
                        rows_countries = :rows_countries,
                        validation_summary_json = :validation_summary_json,
                        error_message = :error_message
                    WHERE cache_version_id = :cache_version_id
                    """
                ),
                {
                    "cache_version_id": cache_version_id,
                    "status": status,
                    "completed_at": now,
                    "activated_at": now if status in {"active", "partial_active"} else None,
                    "rows_prices": int((counts or {}).get("rows_prices") or 0),
                    "rows_commodities": int((counts or {}).get("rows_commodities") or 0),
                    "rows_markets": int((counts or {}).get("rows_markets") or 0),
                    "rows_countries": int((counts or {}).get("rows_countries") or 0),
                    "validation_summary_json": _json_wire(validation_summary or {}),
                    "error_message": error_message,
                },
            )

    @retry_disconnected_read
    def count_active_country_price_rows(self, country_iso3: str) -> int:
        active_version_id = self.get_active_version_id_for_country(country_iso3)
        if active_version_id is None:
            return 0
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT COUNT(*) AS count
                    FROM cached_price_monthly
                    WHERE cache_version_id = :cache_version_id
                      AND country_iso3 = :country_iso3
                    """
                ),
                {
                    "cache_version_id": active_version_id,
                    "country_iso3": country_iso3.upper(),
                },
            ).mappings().first()
        return int((row or {}).get("count") or 0)

    def acquire_refresh_lock(self, lock_name: str, owner: str, timeout_minutes: int) -> bool:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=timeout_minutes)
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        DELETE FROM price_cache_refresh_locks
                        WHERE lock_name = :lock_name
                          AND expires_at <= :now
                        """
                    ),
                    {"lock_name": lock_name, "now": now.isoformat()},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO price_cache_refresh_locks (
                            lock_name,
                            owner,
                            acquired_at,
                            expires_at
                        ) VALUES (
                            :lock_name,
                            :owner,
                            :acquired_at,
                            :expires_at
                        )
                        """
                    ),
                    {
                        "lock_name": lock_name,
                        "owner": owner,
                        "acquired_at": now.isoformat(),
                        "expires_at": expires.isoformat(),
                    },
                )
        except IntegrityError:
            return False
        return True

    def release_refresh_lock(self, lock_name: str, owner: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM price_cache_refresh_locks
                    WHERE lock_name = :lock_name
                      AND owner = :owner
                    """
                ),
                {"lock_name": lock_name, "owner": owner},
            )

    @retry_disconnected_read
    def list_cache_refreshes(self, *, limit: int = 20) -> List[CacheRefreshSummary]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM price_cache_versions
                    ORDER BY started_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().all()
        return [_refresh_summary_from_row(row) for row in rows]

    @retry_disconnected_read
    def get_cache_refresh(self, cache_version_id: str) -> Optional[CacheRefreshSummary]:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT *
                    FROM price_cache_versions
                    WHERE cache_version_id = :cache_version_id
                    """
                ),
                {"cache_version_id": cache_version_id},
            ).mappings().first()
            if row is None:
                return None
            country_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM price_cache_version_countries
                    WHERE cache_version_id = :cache_version_id
                    ORDER BY country_iso3
                    """
                ),
                {"cache_version_id": cache_version_id},
            ).mappings().all()
        return _refresh_summary_from_row(
            row,
            countries=[_country_refresh_from_row(country_row) for country_row in country_rows],
        )

    def cleanup_old_versions(self, *, retain_versions: int) -> list[str]:
        with self.engine.begin() as conn:
            referenced = {
                str(row["cache_version_id"])
                for row in conn.execute(
                    text(
                        """
                        SELECT cache_version_id FROM price_cache_country_active_versions
                        UNION
                        SELECT cache_version_id FROM price_cache_active_version
                        """
                    )
                ).mappings().all()
            }
            version_rows = conn.execute(
                text(
                    """
                    SELECT cache_version_id
                    FROM price_cache_versions
                    ORDER BY COALESCE(completed_at, started_at) DESC
                    """
                )
            ).mappings().all()
            keep = {str(row["cache_version_id"]) for row in version_rows[:retain_versions]} | referenced
            delete_ids = [
                str(row["cache_version_id"])
                for row in version_rows
                if str(row["cache_version_id"]) not in keep
            ]
            for version_id in delete_ids:
                for table in (
                    "cached_price_monthly",
                    "cached_currencies",
                    "cached_markets",
                    "cached_commodities",
                    "cached_units",
                    "cached_countries",
                    "price_cache_version_countries",
                ):
                    conn.execute(
                        text(f"DELETE FROM {table} WHERE cache_version_id = :cache_version_id"),
                        {"cache_version_id": version_id},
                    )
                conn.execute(
                    text("DELETE FROM price_cache_versions WHERE cache_version_id = :cache_version_id"),
                    {"cache_version_id": version_id},
                )
        return delete_ids


def _add_in_clause(
    clauses: list[str],
    params: Dict[str, Any],
    column: str,
    values: Optional[Sequence[Any]],
    prefix: str,
) -> None:
    cleaned = [value for value in values or [] if value not in (None, "")]
    if not cleaned:
        return
    placeholders = []
    for index, value in enumerate(cleaned):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        params[key] = value
    clauses.append(f"{column} IN ({', '.join(placeholders)})")


def _delete_by_ids(
    conn,
    table: str,
    id_column: str,
    values: Sequence[Any],
    *,
    cache_version_id: str,
    country_iso3: str,
) -> None:
    cleaned = [_optional_int(value) for value in values if value not in (None, "")]
    ids = [value for value in cleaned if value is not None]
    if not ids:
        return
    params: Dict[str, Any] = {
        "cache_version_id": cache_version_id,
        "country_iso3": country_iso3.upper(),
    }
    placeholders = []
    for index, value in enumerate(ids):
        key = f"id_{index}"
        placeholders.append(f":{key}")
        params[key] = value
    conn.execute(
        text(
            f"""
            DELETE FROM {table}
            WHERE cache_version_id = :cache_version_id
              AND country_iso3 = :country_iso3
              AND {id_column} IN ({', '.join(placeholders)})
            """
        ),
        params,
    )


def _price_insert_row(cache_version_id: str, country_iso3: str, row: MappingLike) -> dict[str, Any]:
    return {
        "cache_version_id": cache_version_id,
        "country_iso3": country_iso3.upper(),
        "commodity_id": _required_int(row, "commodity_id"),
        "market_id": _required_int(row, "market_id"),
        "price_date": _date_wire(row.get("price_date")),
        "price": row.get("price"),
        "currency_id": _optional_int(row.get("currency_id")),
        "currency_code": _optional_str(row.get("currency_code")),
        "currency_name": _optional_str(row.get("currency_name")),
        "commodity_unit_id": _optional_int(row.get("commodity_unit_id")),
        "commodity_unit_name": _optional_str(row.get("commodity_unit_name")),
        "price_type_id": _optional_int(row.get("price_type_id")),
        "price_type_name": str(row.get("price_type_name") or ""),
        "price_flag": str(row.get("price_flag") or ""),
        "original_frequency": _optional_str(row.get("original_frequency")),
        "observations": _optional_int(row.get("observations")),
        "source_payload_hash": _optional_str(row.get("source_payload_hash")),
        "admin1_name": _optional_str(row.get("admin1_name")),
        "admin2_name": _optional_str(row.get("admin2_name")),
        "market_name": _optional_str(row.get("market_name")),
        "commodity_name": _optional_str(row.get("commodity_name")),
        "data_source": _optional_str(row.get("data_source")),
    }


def _canonical_key_from_price_row(row: MappingLike) -> tuple[Any, ...]:
    return (
        str(row.get("cache_version_id") or ""),
        str(row.get("country_iso3") or "").upper(),
        _optional_int(row.get("commodity_id")),
        _optional_int(row.get("market_id")),
        _date_wire(row.get("price_date")),
        str(row.get("price_type_name") or ""),
        str(row.get("price_flag") or ""),
        _optional_int(row.get("price_type_id")) if _optional_int(row.get("price_type_id")) is not None else -1,
        _optional_int(row.get("currency_id")) if _optional_int(row.get("currency_id")) is not None else -1,
        _optional_int(row.get("commodity_unit_id")) if _optional_int(row.get("commodity_unit_id")) is not None else -1,
    )


def _active_param(conn) -> Any:
    return True if conn.dialect.name != "sqlite" else 1


def _country_from_row(row: Any) -> CountryRecord:
    return CountryRecord(
        cache_version_id=str(row["cache_version_id"]),
        country_iso3=str(row["country_iso3"]),
        country_name=str(row["country_name"]),
        currency_code=_optional_str(row.get("currency_code")),
        currency_name=_optional_str(row.get("currency_name")),
        latest_price_date=_to_date(row.get("latest_price_date")),
    )


def _commodity_from_row(row: Any) -> CommodityRecord:
    return CommodityRecord(
        cache_version_id=str(row["cache_version_id"]),
        country_iso3=str(row["country_iso3"]),
        commodity_id=int(row["commodity_id"]),
        commodity_name=str(row["commodity_name"]),
        commodity_unit_id=_optional_int(row.get("commodity_unit_id")),
        commodity_unit_name=_optional_str(row.get("commodity_unit_name")),
        category_name=_optional_str(row.get("category_name")),
        active=_to_bool(row.get("active")),
    )


def _unit_from_row(row: Any) -> UnitRecord:
    return UnitRecord(
        cache_version_id=str(row["cache_version_id"]),
        commodity_unit_id=int(row["commodity_unit_id"]),
        commodity_unit_name=str(row["commodity_unit_name"]),
        conversion_to_kg_l=_optional_float(row.get("conversion_to_kg_l")),
        active=_to_bool(row.get("active")),
    )


def _market_from_row(row: Any) -> MarketRecord:
    return MarketRecord(
        cache_version_id=str(row["cache_version_id"]),
        country_iso3=str(row["country_iso3"]),
        market_id=int(row["market_id"]),
        market_name=str(row["market_name"]),
        admin1_name=_optional_str(row.get("admin1_name")),
        admin2_name=_optional_str(row.get("admin2_name")),
        latitude=_optional_float(row.get("latitude")),
        longitude=_optional_float(row.get("longitude")),
        active=_to_bool(row.get("active")),
    )


def _currency_from_row(row: Any) -> CurrencyRecord:
    return CurrencyRecord(
        cache_version_id=str(row["cache_version_id"]),
        currency_id=int(row["currency_id"]),
        currency_code=_optional_str(row.get("currency_code")),
        currency_name=str(row["currency_name"]),
    )


def _monthly_price_from_row(row: Any) -> MonthlyPriceRecord:
    return MonthlyPriceRecord(
        cache_version_id=str(row["cache_version_id"]),
        country_iso3=str(row["country_iso3"]),
        commodity_id=int(row["commodity_id"]),
        market_id=int(row["market_id"]),
        price_date=_to_date(row["price_date"]) or date.min,
        price=float(row["price"]),
        currency_id=_optional_int(row.get("currency_id")),
        currency_code=_optional_str(row.get("currency_code")),
        currency_name=_optional_str(row.get("currency_name")),
        commodity_unit_id=_optional_int(row.get("commodity_unit_id")),
        commodity_unit_name=_optional_str(row.get("commodity_unit_name")),
        price_type_id=_optional_int(row.get("price_type_id")),
        price_type_name=str(row.get("price_type_name") or ""),
        price_flag=str(row.get("price_flag") or ""),
        original_frequency=_optional_str(row.get("original_frequency")),
        observations=_optional_int(row.get("observations")),
        source_payload_hash=_optional_str(row.get("source_payload_hash")),
        admin1_name=_optional_str(row.get("admin1_name")),
        admin2_name=_optional_str(row.get("admin2_name")),
        market_name=_optional_str(row.get("market_name")),
        commodity_name=_optional_str(row.get("commodity_name")),
        data_source=_optional_str(row.get("data_source")),
    )


def _date_wire(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _optional_date_wire(value: Any) -> Optional[str]:
    return _date_wire(value)


def _datetime_wire(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _now_wire() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _to_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text_value = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        return None


def _optional_str(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def _required_int(row: MappingLike, key: str) -> int:
    value = _optional_int(row.get(key))
    if value is None:
        raise ValueError(f"Missing required integer field {key!r}.")
    return value


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes"}


def _bool_param_value(value: Any) -> bool:
    return _to_bool(value)


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_wire(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _country_refresh_from_row(row: Any) -> CountryRefreshStatus:
    return CountryRefreshStatus(
        cache_version_id=str(row["cache_version_id"]),
        country_iso3=str(row["country_iso3"]),
        status=str(row["status"]),
        rows_prices=int(row.get("rows_prices") or 0),
        rows_commodities=int(row.get("rows_commodities") or 0),
        rows_markets=int(row.get("rows_markets") or 0),
        latest_price_date=_to_date(row.get("latest_price_date")),
        validation_summary=_json_dict(row.get("validation_summary_json")),
        error_message=_optional_str(row.get("error_message")),
        started_at=_to_datetime(row.get("started_at")),
        completed_at=_to_datetime(row.get("completed_at")),
    )


def _refresh_summary_from_row(
    row: Any,
    *,
    countries: Optional[list[CountryRefreshStatus]] = None,
) -> CacheRefreshSummary:
    started_at = _to_datetime(row.get("started_at")) or datetime.min
    return CacheRefreshSummary(
        cache_version_id=str(row["cache_version_id"]),
        status=str(row["status"]),
        refresh_type=str(row["refresh_type"]),
        started_at=started_at,
        completed_at=_to_datetime(row.get("completed_at")),
        activated_at=_to_datetime(row.get("activated_at")),
        triggered_by=_optional_str(row.get("triggered_by")),
        source_host=_optional_str(row.get("source_host")),
        source_env=_optional_str(row.get("source_env")),
        rows_prices=int(row.get("rows_prices") or 0),
        rows_commodities=int(row.get("rows_commodities") or 0),
        rows_markets=int(row.get("rows_markets") or 0),
        rows_countries=int(row.get("rows_countries") or 0),
        validation_summary=_json_dict(row.get("validation_summary_json")),
        error_message=_optional_str(row.get("error_message")),
        countries=list(countries or []),
    )
