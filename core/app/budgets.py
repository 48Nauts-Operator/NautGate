"""Cost budgets — per-project, per-agent, per-model-family.

Three scopes, one table (``nautgate.budgets``). For each incoming request
we resolve which scopes apply (the agent, that agent's project, the model
family), compute their current period spend (daily or monthly, in
Europe/Berlin so days align with the dashboard), and compare to the cap.

  • spend < warn_at_pct  → green, no header
  • warn ≤ spend < 100%  → warning chip + ``X-Nautgate-Budget-Warning`` header
  • spend ≥ 100%         → block with HTTP 429 (unless caller opts into
                           "warn-only" mode by setting enabled=false on the
                           specific budget row)

Spend is cached for 5 seconds per scope. This keeps the per-request check
to a single dict lookup the vast majority of the time, and bounds the
worst-case staleness so a sudden spike still gets caught fast.

The model-family resolver is hard-coded — extending it doesn't require a
migration, just a code change.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import asyncpg
import structlog

log = structlog.get_logger()


# ── Model family detection ─────────────────────────────────────────────────
# Order matters: first match wins, so put the more specific patterns first.
# Patterns are matched against the *lowercased* model id.

_FAMILY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("claude-opus", re.compile(r"claude[-_]opus")),
    ("claude-sonnet", re.compile(r"claude[-_]sonnet|claude-3-7-sonnet|claude-3-5-sonnet")),
    ("claude-haiku", re.compile(r"claude[-_]haiku|claude-3-haiku|claude-3-5-haiku")),
    ("gpt-5", re.compile(r"\bgpt[-_]?5\b")),
    ("gpt-4", re.compile(r"\bgpt[-_]?4\b")),
    ("gpt-3.5", re.compile(r"\bgpt[-_]?3\.?5\b")),
    ("o1", re.compile(r"\bo1[-_]?")),
    ("o3", re.compile(r"\bo3[-_]?")),
    ("gemini-pro", re.compile(r"gemini[-_].*pro")),
    ("gemini-flash", re.compile(r"gemini[-_].*flash")),
    ("gemini", re.compile(r"gemini")),
    ("deepseek", re.compile(r"deepseek")),
    ("kimi", re.compile(r"kimi|moonshot")),
    ("qwen", re.compile(r"qwen")),
    ("llama", re.compile(r"llama")),
    ("mistral", re.compile(r"mistral|mixtral")),
]


def model_family(model: str | None) -> str | None:
    """Map a model id (``claude-sonnet-4-6``, ``openrouter/anthropic/claude-sonnet``,
    ``gpt-4o-mini``…) to its family bucket. Returns None if the model is
    unrecognised — we don't want to silently coerce unknowns into 'other'
    and then surprise the operator with a budget that catches things they
    didn't expect.
    """
    if not model:
        return None
    m = model.lower()
    for fam, pat in _FAMILY_PATTERNS:
        if pat.search(m):
            return fam
    return None


# ── Spend cache ────────────────────────────────────────────────────────────
# Keyed by (scope_type, scope_id, period). Short TTL so a request burst is
# caught within a few seconds rather than at the next minute boundary.

_SPEND_TTL_SEC = 5.0
_spend_cache: dict[tuple[str, str, str], tuple[float, float]] = {}


def spend_cache_clear() -> None:
    _spend_cache.clear()


_PERIOD_SQL = {
    "daily": "ts >= (date_trunc('day', NOW() AT TIME ZONE 'Europe/Berlin')) AT TIME ZONE 'Europe/Berlin'",
    "monthly": "ts >= (date_trunc('month', NOW() AT TIME ZONE 'Europe/Berlin')) AT TIME ZONE 'Europe/Berlin'",
}

# SQL fragments per scope. Each must take exactly one parameter ($1 = scope_id).
_SCOPE_WHERE = {
    # The model_family case scans by LIKE pattern; the resolver runs in Python
    # before the DB hit so we know which model_family substring to look for.
    "project": "d.project_id = $1",
    "agent": "d.agent_id = $1",
}


async def _compute_spend_db(
    pool: asyncpg.Pool,
    *,
    scope_type: str,
    scope_id: str,
    period: str,
) -> float:
    if period not in _PERIOD_SQL:
        return 0.0
    period_clause = _PERIOD_SQL[period]
    if scope_type == "model_family":
        # We rely on the family pattern matching ``decision_model`` LIKE
        # using the family name as the substring. Cheap, indexed enough for
        # 1000s of rows; if this gets slow we add a generated column.
        sql = f"""
            SELECT COALESCE(SUM(o.cost_usd), 0)::FLOAT AS s
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON o.decision_id = d.id
             WHERE d.{period_clause.split("ts")[0].strip()}ts >= (date_trunc('{period.replace("ly", "")}', NOW() AT TIME ZONE 'Europe/Berlin')) AT TIME ZONE 'Europe/Berlin'
               AND LOWER(d.decision_model) ~ $1
        """
        # Build the family regex from the scope_id directly. scope_id is a
        # family name like "claude-sonnet" → matches when the model name
        # contains that token.
        # NOTE: above SQL got jumbled when I tried to inline period — rewrite cleanly:
        sql = f"""
            SELECT COALESCE(SUM(o.cost_usd), 0)::FLOAT AS s
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON o.decision_id = d.id
             WHERE d.{period_clause}
               AND LOWER(d.decision_model) ~ $1
        """
        param = re.escape(scope_id)
    else:
        where = _SCOPE_WHERE.get(scope_type)
        if not where:
            return 0.0
        sql = f"""
            SELECT COALESCE(SUM(o.cost_usd), 0)::FLOAT AS s
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON o.decision_id = d.id
             WHERE d.{period_clause}
               AND {where}
        """
        param = scope_id
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, param)
        return float((row or {}).get("s") or 0.0)
    except Exception as exc:
        log.warning(
            "budget_spend_query_failed", scope_type=scope_type, scope_id=scope_id, error=str(exc)
        )
        return 0.0


async def get_spend(
    pool: asyncpg.Pool,
    *,
    scope_type: str,
    scope_id: str,
    period: str,
) -> float:
    key = (scope_type, scope_id, period)
    now = time.monotonic()
    cached = _spend_cache.get(key)
    if cached is not None and (now - cached[0]) < _SPEND_TTL_SEC:
        return cached[1]
    value = await _compute_spend_db(
        pool,
        scope_type=scope_type,
        scope_id=scope_id,
        period=period,
    )
    _spend_cache[key] = (now, value)
    return value


def _bump_spend(scope_type: str, scope_id: str, period: str, delta: float) -> None:
    """Optimistically increment the cached spend after a successful call so
    a burst doesn't blow past the cap before the cache TTL expires."""
    key = (scope_type, scope_id, period)
    cur = _spend_cache.get(key)
    if cur is None:
        return  # nothing cached yet — let the next read pull fresh
    _spend_cache[key] = (cur[0], cur[1] + max(delta, 0.0))


def record_spend_increment(
    *,
    agent_id: str | None,
    project_id: str | None,
    decision_model: str | None,
    cost_usd: float | None,
) -> None:
    """Called from the post-outcome path to keep cached spend numbers honest
    in between TTL refreshes. Cheap, idempotent, never fails."""
    if not cost_usd or cost_usd <= 0:
        return
    for period in ("daily", "monthly"):
        if project_id:
            _bump_spend("project", project_id, period, cost_usd)
        if agent_id:
            _bump_spend("agent", agent_id, period, cost_usd)
        fam = model_family(decision_model)
        if fam:
            _bump_spend("model_family", fam, period, cost_usd)


# ── Budget rows ────────────────────────────────────────────────────────────


@dataclass
class BudgetRow:
    scope_type: str
    scope_id: str
    period: str
    cap_usd: float
    warn_at_pct: float
    enabled: bool
    note: str | None


async def list_budgets(pool: asyncpg.Pool) -> list[BudgetRow]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT scope_type, scope_id, period, cap_usd, warn_at_pct, enabled, note "
            "FROM nautgate.budgets ORDER BY scope_type, scope_id, period"
        )
    return [
        BudgetRow(
            scope_type=r["scope_type"],
            scope_id=r["scope_id"],
            period=r["period"],
            cap_usd=float(r["cap_usd"]),
            warn_at_pct=float(r["warn_at_pct"]),
            enabled=bool(r["enabled"]),
            note=r["note"],
        )
        for r in rows
    ]


async def upsert_budget(
    pool: asyncpg.Pool,
    *,
    scope_type: str,
    scope_id: str,
    period: str,
    cap_usd: float,
    warn_at_pct: float = 80.0,
    enabled: bool = True,
    note: str | None = None,
) -> BudgetRow:
    if scope_type not in ("project", "agent", "model_family"):
        raise ValueError("scope_type must be project|agent|model_family")
    if period not in ("daily", "monthly"):
        raise ValueError("period must be daily|monthly")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.budgets
                (scope_type, scope_id, period, cap_usd, warn_at_pct, enabled, note, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, now())
            ON CONFLICT (scope_type, scope_id, period) DO UPDATE
              SET cap_usd     = EXCLUDED.cap_usd,
                  warn_at_pct = EXCLUDED.warn_at_pct,
                  enabled     = EXCLUDED.enabled,
                  note        = EXCLUDED.note,
                  updated_at  = now()
            """,
            scope_type,
            scope_id,
            period,
            cap_usd,
            warn_at_pct,
            enabled,
            note,
        )
    spend_cache_clear()  # cap change may flip warn/exceed state immediately
    return BudgetRow(scope_type, scope_id, period, cap_usd, warn_at_pct, enabled, note)


async def delete_budget(
    pool: asyncpg.Pool,
    *,
    scope_type: str,
    scope_id: str,
    period: str,
) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM nautgate.budgets WHERE scope_type = $1 AND scope_id = $2 AND period = $3",
            scope_type,
            scope_id,
            period,
        )
    spend_cache_clear()
    return result.endswith("1")


# ── Enforcement ────────────────────────────────────────────────────────────


@dataclass
class BudgetEvaluation:
    """The outcome of evaluating every applicable budget for one request."""

    blocked: bool  # any budget at or over 100% → True
    block_reason: str | None  # 'scope:scope_id:cap=X spent=Y' or None
    warnings: list[str]  # 'scope:scope_id:80%-100%' for each over warn
    spends: dict[tuple[str, str, str], tuple[float, float, float]]
    # spends[(scope, id, period)] = (cap, spent, pct)


_NO_BUDGETS = BudgetEvaluation(
    blocked=False,
    block_reason=None,
    warnings=[],
    spends={},
)


def _candidate_scopes(
    *,
    agent_id: str | None,
    project_id: str | None,
    decision_model: str | None,
) -> list[tuple[str, str]]:
    """Which (scope_type, scope_id) keys could possibly match a budget row?"""
    out: list[tuple[str, str]] = []
    if project_id:
        out.append(("project", project_id))
    if agent_id:
        out.append(("agent", agent_id))
    fam = model_family(decision_model)
    if fam:
        out.append(("model_family", fam))
    return out


async def evaluate(
    pool: asyncpg.Pool,
    *,
    agent_id: str | None,
    project_id: str | None,
    decision_model: str | None,
) -> BudgetEvaluation:
    """Look up every budget that applies to this request, compute current
    spend, and tally warnings + the blocking budget (if any).

    Returns an empty BudgetEvaluation when no budgets exist or pool is None.
    """
    if pool is None:
        return _NO_BUDGETS
    candidates = _candidate_scopes(
        agent_id=agent_id,
        project_id=project_id,
        decision_model=decision_model,
    )
    if not candidates:
        return _NO_BUDGETS

    # Pull rows that match any candidate scope. One round trip.
    types_ids = list({(t, i) for t, i in candidates})
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT scope_type, scope_id, period, cap_usd, warn_at_pct, enabled
                  FROM nautgate.budgets
                 WHERE (scope_type, scope_id) = ANY($1::record[])
                """,
                types_ids,
            )
    except Exception as exc:
        # asyncpg may not accept the array-of-record cast for empty arrays
        # or unexpected types; fall back to per-row lookups.
        log.debug("budget_bulk_lookup_failed", error=str(exc))
        rows = []
        try:
            async with pool.acquire() as conn:
                for st, sid in types_ids:
                    rs = await conn.fetch(
                        "SELECT scope_type, scope_id, period, cap_usd, warn_at_pct, enabled "
                        "FROM nautgate.budgets WHERE scope_type = $1 AND scope_id = $2",
                        st,
                        sid,
                    )
                    rows.extend(rs)
        except Exception as exc2:
            log.warning("budget_lookup_failed", error=str(exc2))
            return _NO_BUDGETS

    if not rows:
        return _NO_BUDGETS

    spends: dict[tuple[str, str, str], tuple[float, float, float]] = {}
    warnings: list[str] = []
    block_reason: str | None = None

    for r in rows:
        cap = float(r["cap_usd"])
        if cap <= 0:
            continue
        spent = await get_spend(
            pool,
            scope_type=r["scope_type"],
            scope_id=r["scope_id"],
            period=r["period"],
        )
        pct = (spent / cap * 100.0) if cap > 0 else 0.0
        key = (r["scope_type"], r["scope_id"], r["period"])
        spends[key] = (cap, spent, pct)
        if pct >= 100.0 and r["enabled"]:
            reason = (
                f"{r['scope_type']}:{r['scope_id']}:{r['period']}:cap=${cap:.2f}:spent=${spent:.2f}"
            )
            # Take the first blocker, but keep looking so the response lists
            # all warnings/over-caps as well.
            if block_reason is None:
                block_reason = reason
        elif pct >= float(r["warn_at_pct"]):
            warnings.append(f"{r['scope_type']}:{r['scope_id']}:{r['period']}:{pct:.0f}%")

    return BudgetEvaluation(
        blocked=block_reason is not None,
        block_reason=block_reason,
        warnings=warnings,
        spends=spends,
    )
