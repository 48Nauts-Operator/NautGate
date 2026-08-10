"""Backup + restore for the nautgate Postgres schema.

Invokes pg_dump / psql. Two strategies, picked automatically (NAUTGATE-7):

- **DSN** (default) — connect straight to the database with ``NAUTGATE_DB_URL``.
  Works both inside the published container (pg_dump ships in the image, reaches
  the db service over the network) and on any host that has pg_dump installed.
- **docker exec** (fallback) — exec pg_dump inside the db container. Used only
  when the client binaries aren't on PATH. This is what dev-on-the-host used, but
  it cannot work from *inside* a container (no docker CLI/socket), which is why
  the DSN path is now the default.

Files land under NAUTGATE_BACKUP_DIR (default ~/.nautgate/backups/) as gzipped
SQL, named ``nautgate-<YYYYMMDD-HHMMSS>-<via>.sql.gz``.

Scheduling: ``run_scheduler(pool)`` is a long-running asyncio task started
in the FastAPI lifespan. It checks ``backup_config`` every minute and
fires a scheduled backup when ``now >= next_run_at``. Retention is
enforced after each successful backup (oldest are deleted to stay at
``retention_count``).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import subprocess
import time as _time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import structlog

from app.settings import get_settings

log = structlog.get_logger()

# Container name to exec into (docker-exec fallback only). Override with
# NAUTGATE_DB_CONTAINER if you named the service differently in docker-compose.
DEFAULT_DB_CONTAINER = "nautgate-db"
DEFAULT_BACKUP_DIR = Path.home() / ".nautgate" / "backups"


def _backup_dir() -> Path:
    p = Path(os.environ.get("NAUTGATE_BACKUP_DIR") or DEFAULT_BACKUP_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db_container() -> str:
    return os.environ.get("NAUTGATE_DB_CONTAINER") or DEFAULT_DB_CONTAINER


def _use_dsn() -> bool:
    """Prefer a direct connection whenever pg_dump/psql are on PATH."""
    return bool(shutil.which("pg_dump") and shutil.which("psql"))


def _dsn() -> str:
    dsn = get_settings().nautgate_db_url
    if not dsn:
        raise RuntimeError("NAUTGATE_DB_URL is not set — cannot run a backup")
    return dsn


def _dsn_database() -> str:
    """Database name from NAUTGATE_DB_URL.

    The docker-exec fallback used to hardcode ``-d nautgate``. That meant an
    instance configured against any other database still dumped ``nautgate`` —
    so a sandbox pointed at a 10 MB scratch DB produced 8 GB dumps of production
    and filed the rows in the scratch DB, where production's retention could
    never see them to clean up.
    """
    # The docker-exec fallback is reachable with no DSN configured at all, so
    # this must never raise the way _dsn() does — fall back to the historical
    # name rather than breaking the backup entirely.
    try:
        dsn = _dsn()
    except RuntimeError:
        return "nautgate"
    return urlparse(dsn).path.lstrip("/") or "nautgate"


def _dump_cmd() -> list[str]:
    """pg_dump argv. DSN form when the client is available, else docker exec."""
    args = ["--schema=nautgate", "--no-owner", "--no-privileges"]
    if _use_dsn():
        return ["pg_dump", _dsn(), *args]
    return [
        "docker",
        "exec",
        _db_container(),
        "pg_dump",
        "-U",
        "nautgate",
        "-d",
        _dsn_database(),
        *args,
    ]


def _psql_cmd(extra: list[str]) -> list[str]:
    """psql argv for restore. DSN form when available, else docker exec."""
    if _use_dsn():
        return ["psql", _dsn(), *extra]
    return [
        "docker",
        "exec",
        "-i",
        _db_container(),
        "psql",
        "-U",
        "nautgate",
        "-d",
        "nautgate",
        *extra,
    ]


def _backup_filename(now: datetime, via: str) -> str:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return f"nautgate-{stamp}-{via}.sql.gz"


# ── Core operations ───────────────────────────────────────────────────────


async def create_backup(pool: asyncpg.Pool, *, via: str = "manual") -> dict:
    """Dump the nautgate schema to a gzipped SQL file, record the metadata.

    Returns the inserted backup row as a dict.
    """
    if via not in ("manual", "scheduled"):
        raise ValueError(f"via must be manual|scheduled, got {via!r}")

    now = datetime.now(UTC)
    backup_dir = _backup_dir()
    filename = _backup_filename(now, via)
    file_path = backup_dir / filename

    # Pre-insert an in_progress row so the UI can show it immediately.
    bid = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.backups (id, ts, file_path, size_bytes,
                                          created_via, status)
            VALUES ($1, $2, $3, 0, $4, 'in_progress')
            """,
            bid,
            now,
            str(file_path),
            via,
        )

    try:
        # pg_dump → stdout, we gzip into the target file. --no-owner
        # --no-privileges keeps the dump portable across users.
        cmd = _dump_cmd()

        # Run in a thread so we don't block the event loop on big dumps.
        def _run():
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with gzip.open(file_path, "wb") as out:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            stderr = proc.stderr.read()
            rc = proc.wait()
            return rc, stderr.decode("utf-8", errors="replace")

        rc, stderr = await asyncio.to_thread(_run)
        if rc != 0:
            raise RuntimeError(f"pg_dump exited {rc}: {stderr.strip()[:500]}")

        size = file_path.stat().st_size
        counts = await _table_counts(pool)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nautgate.backups
                   SET size_bytes=$2, table_counts=$3::jsonb, status='ok'
                 WHERE id=$1
                """,
                bid,
                size,
                json.dumps(counts),
            )

        log.info("backup_created", backup_id=str(bid), size=size, via=via)
        await _enforce_retention(pool)

    except Exception as exc:
        # Mark the row as failed; leave a partial file in place for forensics
        # but record the error so the UI can show it.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nautgate.backups
                   SET status='failed', error_message=$2
                 WHERE id=$1
                """,
                bid,
                str(exc)[:1000],
            )
        log.error(
            "backup_failed",
            backup_id=str(bid),
            error=str(exc) or repr(exc),
            error_type=type(exc).__name__,
        )
        # Re-raise for the caller (API handler) to return a 500.
        raise

    return await _get_backup(pool, bid)


async def restore_backup(pool: asyncpg.Pool, backup_id: uuid.UUID) -> None:
    """Restore the nautgate schema from a backup file.

    DESTRUCTIVE: drops the existing nautgate schema and loads the dump.
    The caller (API handler) is responsible for requiring a confirmation
    flag from the user before invoking this.
    """
    row = await _get_backup(pool, backup_id)
    if row is None:
        raise FileNotFoundError(f"backup {backup_id} not found")
    file_path = Path(row["file_path"])
    if not file_path.exists():
        raise FileNotFoundError(f"backup file missing on disk: {file_path}")

    def _run():
        # First drop the schema (CASCADE wipes everything), then load.
        drop = subprocess.run(
            _psql_cmd(["-c", "DROP SCHEMA IF EXISTS nautgate CASCADE;"]),
            capture_output=True,
        )
        if drop.returncode != 0:
            raise RuntimeError(f"DROP SCHEMA failed: {drop.stderr.decode()[:500]}")
        # Stream the decompressed dump into psql in chunks rather than reading
        # the whole SQL into memory — a large DB's dump can be many GB.
        proc = subprocess.Popen(_psql_cmd([]), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with gzip.open(file_path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)  # 1 MiB
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
            proc.stdin.close()
        except BrokenPipeError:
            pass  # psql died early — its stderr/returncode below explains why
        stderr = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(f"psql restore failed: {stderr.decode()[:500]}")

    await asyncio.to_thread(_run)
    log.warning("backup_restored", backup_id=str(backup_id), file=str(file_path))


async def list_backups(pool: asyncpg.Pool, *, limit: int = 100) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id::text                       AS id,
               ts,
               file_path,
               size_bytes,
               created_via,
               table_counts,
               status,
               error_message,
               notes
          FROM nautgate.backups
         ORDER BY ts DESC
         LIMIT $1
        """,
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        if d.get("ts"):
            d["ts"] = d["ts"].isoformat()
        if isinstance(d.get("table_counts"), str):
            try:
                d["table_counts"] = json.loads(d["table_counts"])
            except (ValueError, TypeError):
                d["table_counts"] = None
        d["exists_on_disk"] = Path(d["file_path"]).exists()
        out.append(d)
    return out


async def delete_backup(pool: asyncpg.Pool, backup_id: uuid.UUID) -> bool:
    row = await _get_backup(pool, backup_id)
    if row is None:
        return False
    file_path = Path(row["file_path"])
    if file_path.exists():
        try:
            file_path.unlink()
        except OSError as e:
            log.warning("backup_file_delete_failed", path=str(file_path), error=str(e))
    await pool.execute("DELETE FROM nautgate.backups WHERE id=$1", backup_id)
    log.info("backup_deleted", backup_id=str(backup_id))
    return True


# ── Configuration ─────────────────────────────────────────────────────────


async def get_config(pool: asyncpg.Pool) -> dict:
    row = await pool.fetchrow(
        """
        SELECT enabled, interval_hours, retention_count,
               last_run_at, next_run_at, updated_at
          FROM nautgate.backup_config WHERE id=1
        """
    )
    if row is None:
        return {
            "enabled": True,
            "interval_hours": 3,
            "retention_count": 20,
            "last_run_at": None,
            "next_run_at": None,
        }
    d = dict(row)
    for k in ("last_run_at", "next_run_at", "updated_at"):
        v = d.get(k)
        if v is not None:
            d[k] = v.isoformat()
    return d


async def update_config(
    pool: asyncpg.Pool,
    *,
    enabled: bool | None = None,
    interval_hours: int | None = None,
    retention_count: int | None = None,
) -> dict:
    # Only update fields that were provided.
    fields = []
    params: list = []
    if enabled is not None:
        params.append(enabled)
        fields.append(f"enabled = ${len(params)}")
    if interval_hours is not None:
        if interval_hours < 1 or interval_hours > 168:
            raise ValueError("interval_hours must be 1..168")
        params.append(interval_hours)
        fields.append(f"interval_hours = ${len(params)}")
    if retention_count is not None:
        if retention_count < 1 or retention_count > 500:
            raise ValueError("retention_count must be 1..500")
        params.append(retention_count)
        fields.append(f"retention_count = ${len(params)}")
    if not fields:
        return await get_config(pool)
    fields.append("updated_at = now()")
    # When interval changes, reset next_run_at so the new cadence kicks in.
    if interval_hours is not None:
        fields.append("next_run_at = NULL")
    await pool.execute(
        f"UPDATE nautgate.backup_config SET {', '.join(fields)} WHERE id=1",
        *params,
    )
    return await get_config(pool)


# ── Scheduler ─────────────────────────────────────────────────────────────


async def run_scheduler(pool: asyncpg.Pool, *, tick_seconds: int = 60) -> None:
    """Long-running task: every ``tick_seconds``, check whether a scheduled
    backup is due. If so, fire it. Exits cleanly on cancellation.
    """
    log.info("backup_scheduler_started", tick_seconds=tick_seconds)
    while True:
        try:
            cfg = await pool.fetchrow(
                "SELECT enabled, interval_hours, last_run_at, next_run_at "
                "FROM nautgate.backup_config WHERE id=1"
            )
            if cfg is None or not cfg["enabled"]:
                await asyncio.sleep(tick_seconds)
                continue

            now = datetime.now(UTC)
            next_run_at = cfg["next_run_at"]
            if next_run_at is None:
                # First boot, or interval just changed: schedule the next run.
                from datetime import timedelta

                planned = now + timedelta(hours=cfg["interval_hours"])
                await pool.execute(
                    "UPDATE nautgate.backup_config SET next_run_at=$1 WHERE id=1",
                    planned,
                )
                await asyncio.sleep(tick_seconds)
                continue

            if now >= next_run_at:
                try:
                    await create_backup(pool, via="scheduled")
                except Exception as exc:
                    log.warning(
                        "scheduled_backup_failed",
                        error=str(exc) or repr(exc),
                        error_type=type(exc).__name__,
                    )
                # Whether it succeeded or not, advance the schedule so we
                # don't tight-loop. Update last_run_at + next_run_at.
                from datetime import timedelta

                planned = now + timedelta(hours=cfg["interval_hours"])
                await pool.execute(
                    "UPDATE nautgate.backup_config SET last_run_at=$1, next_run_at=$2 WHERE id=1",
                    now,
                    planned,
                )
        except asyncio.CancelledError:
            log.info("backup_scheduler_cancelled")
            raise
        except Exception as exc:
            log.error(
                "backup_scheduler_iteration_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(tick_seconds)


# ── Internals ─────────────────────────────────────────────────────────────


async def _table_counts(pool: asyncpg.Pool) -> dict[str, int]:
    """Snapshot row counts for the major tables — saved with each backup so
    the UI can show "this backup has 1,243 decisions, 87 incidents, …"
    """
    tables = (
        "api_keys",
        "route_decisions",
        "route_outcomes",
        "model_scorecard",
        "model_incidents",
        "model_baselines",
        "model_anomalies",
        "drift_alerts",
    )
    out: dict[str, int] = {}
    for t in tables:
        try:
            out[t] = await pool.fetchval(f"SELECT COUNT(*) FROM nautgate.{t}")
        except Exception:
            # Table may not exist on older schemas; skip.
            pass
    return out


async def _get_backup(pool: asyncpg.Pool, backup_id) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, ts, file_path, size_bytes, created_via, status FROM nautgate.backups WHERE id=$1",
        backup_id,
    )
    if row is None:
        return None
    d = dict(row)
    # JSONResponse can't serialize UUID / datetime — normalize here so any
    # caller that hands this dict to JSONResponse just works.
    if d.get("id") is not None:
        d["id"] = str(d["id"])
    if d.get("ts") is not None:
        d["ts"] = d["ts"].isoformat()
    return d


async def _enforce_retention(pool: asyncpg.Pool) -> None:
    """Delete oldest successful backups beyond retention_count."""
    cfg = await pool.fetchrow("SELECT retention_count FROM nautgate.backup_config WHERE id=1")
    if cfg is None:
        return
    keep = int(cfg["retention_count"])
    # Find IDs of successful backups beyond the cutoff.
    rows = await pool.fetch(
        """
        SELECT id FROM nautgate.backups
         WHERE status='ok'
         ORDER BY ts DESC
         OFFSET $1
        """,
        keep,
    )
    for r in rows:
        await delete_backup(pool, r["id"])

    await _reap_orphan_files(pool)


# A dump that failed, was killed, or whose row was removed leaves its file
# behind, and row-based retention can never see it again. On a real instance
# that reached 31 GB of unreferenced dumps against 49 GB of free disk.
_ORPHAN_MIN_AGE_S = 3600


async def _reap_orphan_files(pool: asyncpg.Pool) -> None:
    """Delete backup files in the backup dir that no row references.

    Only touches files matching our own naming pattern, and only those older
    than an hour — a dump in flight has no completed row yet and must not be
    deleted out from under itself.
    """
    directory = _backup_dir()
    if not directory.is_dir():
        return
    try:
        referenced = {
            Path(r["file_path"]).name
            for r in await pool.fetch(
                "SELECT file_path FROM nautgate.backups WHERE file_path IS NOT NULL"
            )
        }
    except Exception as exc:
        log.warning(
            "backup_orphan_scan_failed", error=str(exc) or repr(exc), error_type=type(exc).__name__
        )
        return

    now = _time.time()
    removed = 0
    freed = 0
    for f in directory.glob("nautgate-*.sql.gz"):
        if f.name in referenced:
            continue
        try:
            st = f.stat()
            if now - st.st_mtime < _ORPHAN_MIN_AGE_S:
                continue  # possibly still being written
            size = st.st_size
            f.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            log.warning("backup_orphan_unlink_failed", path=str(f), error=str(exc) or repr(exc))
    if removed:
        log.info("backup_orphans_reaped", files=removed, freed_bytes=freed)
