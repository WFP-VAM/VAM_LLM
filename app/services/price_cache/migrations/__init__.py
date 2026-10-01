from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine


MIGRATIONS_ROOT = Path(__file__).parent


def apply_migrations(engine: Engine, dialect: str) -> list[str]:
    normalized = _normalize_dialect(dialect)
    migration_dir = MIGRATIONS_ROOT / normalized
    if not migration_dir.exists():
        raise ValueError(f"No price cache migrations found for dialect {normalized!r}.")

    applied_now: list[str] = []
    with engine.begin() as conn:
        _acquire_migration_lock(conn, normalized)
        _ensure_migration_table(conn, normalized)
        applied = {
            str(row.version)
            for row in conn.execute(text("SELECT version FROM price_cache_schema_migrations"))
        }
        for path in sorted(migration_dir.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            for statement in _split_sql(path.read_text(encoding="utf-8")):
                conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO price_cache_schema_migrations(version) VALUES (:version)"),
                {"version": version},
            )
            applied_now.append(version)
    return applied_now


def _acquire_migration_lock(conn, dialect: str) -> None:
    if dialect != "postgres":
        return
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('price_cache_schema_migrations'))"))


def _ensure_migration_table(conn, dialect: str) -> None:
    if dialect == "postgres":
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS price_cache_schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )
        return
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS price_cache_schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _normalize_dialect(dialect: str) -> str:
    from app.shared.database import database_backend_alias
    value = database_backend_alias(dialect)
    if value in {"sqlite", "sqlite3"}:
        return "sqlite"
    if value in {"postgres", "postgresql"}:
        return "postgres"
    raise ValueError(f"Unsupported migration dialect {dialect!r}.")


def _split_sql(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        current.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements
