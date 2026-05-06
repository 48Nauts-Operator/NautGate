"""Migration runner. Tracks applied migrations in `nautgate.schema_migrations`.

Filename convention: `NNN_*.sql` where NNN is the version (e.g. "001", "002").
The version is taken from the leading dot-separated token of the filename stem.

Idempotent: re-running the runner with all migrations applied is a no-op.
"""

from pathlib import Path

import asyncpg
import structlog

log = structlog.get_logger()

_BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS nautgate;

CREATE TABLE IF NOT EXISTS nautgate.schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def apply_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> None:
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        log.warning("migrations_dir_empty", path=str(migrations_dir))
        return

    async with pool.acquire() as conn:
        await conn.execute(_BOOTSTRAP_SQL)

        applied = {
            r["version"] for r in await conn.fetch("SELECT version FROM nautgate.schema_migrations")
        }

        # Backfill for DBs migrated before tracking existed: if 001's tables are
        # present but nothing is in schema_migrations, mark 001 as applied.
        if "001" not in applied:
            existing = await conn.fetchval("SELECT to_regclass('nautgate.api_keys')")
            if existing is not None:
                await conn.execute(
                    "INSERT INTO nautgate.schema_migrations (version) VALUES ('001') "
                    "ON CONFLICT DO NOTHING"
                )
                applied.add("001")
                log.info("migrations_backfilled", version="001")

        for f in files:
            version = f.stem.split("_", 1)[0]
            if version in applied:
                continue
            sql = f.read_text(encoding="utf-8")
            log.info("applying_migration", version=version, file=f.name, bytes=len(sql))
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO nautgate.schema_migrations (version) VALUES ($1)",
                    version,
                )

        log.info("migrations_complete", applied_count=len(applied))
