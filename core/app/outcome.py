"""Day 4d — outcome writer with spool fallback.

The route handlers call ``persist_outcome(pool, spool, **kwargs)``. We try the DB
first; on any failure (connection refused, query timeout, etc.) we append the
kwargs to the spool so they get retried on the next drain.

The spool is the durable side of the contract: route_outcomes is *eventually*
written, even when Postgres is down.
"""

from __future__ import annotations

import structlog

from app.db import queries
from app.spool import OutcomeSpool

log = structlog.get_logger()


def _strip_nul_bytes(s: str | None) -> str | None:
    """Postgres TEXT columns reject \\x00 — strip NUL bytes from any
    response body before insert. SSE responses from Anthropic/OpenAI
    occasionally contain stray NULs inside tool-call argument JSON or
    binary-ish chunks; without this every such call would silently lose
    its outcome row. Cheap (.replace) and idempotent.
    """
    if s is None:
        return None
    return s.replace("\x00", "") if "\x00" in s else s


async def persist_outcome(pool, spool: OutcomeSpool | None, **kwargs) -> None:
    """Try the DB; spool the kwargs on failure if a spool is configured.

    If the spool itself is unwritable we log and swallow — the audit log already
    has route_decisions (synchronous), so we never block the request path.
    """
    # Sanitize the only TEXT column that's bytes-derived. tool_calls_made
    # is jsonb (Postgres allows \\x00 in jsonb strings via json escapes),
    # everything else is integer/bool/numeric.
    if "response_body" in kwargs:
        kwargs["response_body"] = _strip_nul_bytes(kwargs["response_body"])

    try:
        await queries.write_outcome(pool, **kwargs)
        return
    except Exception as exc:
        log.warning(
            "outcome_write_db_failure",
            error=str(exc),
            decision_id=str(kwargs.get("decision_id")),
        )

    if spool is None:
        log.error(
            "outcome_lost_no_spool",
            decision_id=str(kwargs.get("decision_id")),
        )
        return

    try:
        spool.append(kwargs)
    except Exception as exc:
        log.error(
            "outcome_spool_write_failed",
            error=str(exc),
            decision_id=str(kwargs.get("decision_id")),
        )
