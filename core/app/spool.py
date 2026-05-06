"""Day 4d — durable-spool fallback for route_outcomes (Tech Paper §9).

If write_outcome to Postgres fails (DB down, network blip), the outcome row is
appended as NDJSON to a local spool file. On startup we attempt to drain the
spool back into Postgres, line by line. On the first failure during drain we
stop and rewrite the spool with the unprocessed tail intact so retries continue
on the next pass.

Lines that fail JSON parse are moved to ``<spool>.bad`` and never retried.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger()


def _json_default(o: Any):
    if isinstance(o, UUID):
        return str(o)
    raise TypeError(f"unserializable: {type(o).__name__}")


def _coerce_kwargs_for_db(kw: dict) -> dict:
    """Convert spool-reloaded kwargs back into the types write_outcome expects."""
    out = dict(kw)
    if isinstance(out.get("decision_id"), str):
        out["decision_id"] = UUID(out["decision_id"])
    return out


@dataclass
class DrainResult:
    drained: int
    skipped_bad: int
    pending: int  # lines left in the spool after this drain pass


class OutcomeSpool:
    """Append-only NDJSON spool with atomic-rewrite drain.

    Not thread-safe across processes, but a single uvicorn worker is the only
    writer in v1. Multi-worker deployments need filelock or per-worker spools.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.bad_path = self.path.with_suffix(self.path.suffix + ".bad")

    def ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kwargs: dict) -> None:
        self.ensure_parent()
        line = json.dumps(kwargs, default=_json_default, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def is_empty(self) -> bool:
        return not self.path.exists() or self.path.stat().st_size == 0

    async def drain(self, write_fn, pool) -> DrainResult:
        """Replay every spooled line through write_fn(pool, **kwargs).

        On the first DB failure, rewrites the spool with all unprocessed lines
        (including the one that failed) so the next attempt retries them.
        """
        if self.is_empty():
            return DrainResult(drained=0, skipped_bad=0, pending=0)

        with self.path.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        drained = 0
        skipped_bad = 0
        remaining: list[str] = []
        bad: list[str] = []
        first_failure_index: int | None = None

        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                bad.append(raw)
                skipped_bad += 1
                continue

            if first_failure_index is not None:
                # Already failed on a prior line; skip the rest, will rewrite.
                remaining.append(raw)
                continue

            try:
                await write_fn(pool, **_coerce_kwargs_for_db(parsed))
                drained += 1
            except Exception as exc:
                log.warning(
                    "spool_drain_db_failure",
                    line_index=i,
                    error=str(exc),
                )
                first_failure_index = i
                remaining.append(raw)

        # Rewrite the spool atomically — only what's left to retry.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        if remaining:
            tmp.write_text("".join(remaining), encoding="utf-8")
            tmp.replace(self.path)
        else:
            self.path.unlink(missing_ok=True)

        if bad:
            with self.bad_path.open("a", encoding="utf-8") as bf:
                bf.writelines(bad)

        log.info(
            "spool_drain_complete",
            drained=drained,
            skipped_bad=skipped_bad,
            pending=len(remaining),
        )
        return DrainResult(drained=drained, skipped_bad=skipped_bad, pending=len(remaining))
