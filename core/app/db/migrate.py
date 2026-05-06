from pathlib import Path

import asyncpg
import structlog

log = structlog.get_logger()


async def apply_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> None:
    """Apply *.sql files in migrations_dir if the `nautgate` schema is missing.

    Day-1 simplification: single migration file, no migrations table.
    Add a migrations table when the second migration ships.
    """
    async with pool.acquire() as conn:
        schema_oid = await conn.fetchval("SELECT to_regnamespace('nautgate')")
        if schema_oid is not None:
            log.info("migrations_skipped", reason="schema_exists")
            return

        files = sorted(migrations_dir.glob("*.sql"))
        if not files:
            log.warning("migrations_dir_empty", path=str(migrations_dir))
            return

        async with conn.transaction():
            for f in files:
                sql = f.read_text(encoding="utf-8")
                log.info("applying_migration", file=f.name, bytes=len(sql))
                await conn.execute(sql)

        log.info("migrations_complete", count=len(files))
