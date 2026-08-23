"""Deterministic, content-free Max subscription consumption guard."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MaxGuardPolicy:
    mode: str = "observe"
    warn_fresh_tokens: int = 1_000_000
    pause_fresh_tokens: int = 5_000_000
    warn_request_tokens: int = 100_000
    pause_request_tokens: int = 300_000
    cache_free_stable_warn_calls: int = 2
    cache_free_stable_pause_calls: int = 3
    cache_free_min_tokens: int = 50_000
    project_hour_pause_tokens: int = 10_000_000
    lane_five_hour_pause_tokens: int = 25_000_000
    lane_week_pause_tokens: int = 100_000_000


@dataclass
class MaxGuardState:
    fresh_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    stable_cache_free_calls: dict[str, int] = field(default_factory=dict)
    paused: bool = False
    pause_reason: str | None = None
    updated_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class MaxGuardDecision:
    action: str
    reason: str
    fresh_tokens: int
    estimated_request_tokens: int
    requests: int


class MaxGuard:
    def __init__(self, policy: MaxGuardPolicy):
        if policy.mode not in {"observe", "warn", "pause"}:
            raise ValueError("Max Guard mode must be observe, warn, or pause")
        self.policy = policy
        self._states: dict[str, MaxGuardState] = {}

    def state(self, identity: str) -> MaxGuardState:
        return self._states.setdefault(identity, MaxGuardState())

    def set_pause(self, identity: str, paused: bool, reason: str | None = None) -> None:
        state = self.state(identity)
        state.paused = paused
        state.pause_reason = (reason or "operator_pause") if paused else None
        state.updated_at = time.monotonic()

    def preflight(self, identity: str, payload: dict | None) -> MaxGuardDecision:
        state = self.state(identity)
        estimate = estimate_input_tokens(payload)
        reason = "within_configured_allowance"
        requested_action = "observe"
        if state.paused:
            requested_action, reason = "pause", state.pause_reason or "session_paused"
        elif estimate >= self.policy.pause_request_tokens:
            requested_action, reason = "pause", "estimated_request_tokens"
        elif state.fresh_tokens + estimate >= self.policy.pause_fresh_tokens:
            requested_action, reason = "pause", "session_fresh_token_allowance"
        elif estimate >= self.policy.warn_request_tokens:
            requested_action, reason = "warn", "large_estimated_request"
        elif state.fresh_tokens + estimate >= self.policy.warn_fresh_tokens:
            requested_action, reason = "warn", "session_fresh_token_warning"

        action = self._effective_action(requested_action)
        if action == "pause":
            state.paused = True
            state.pause_reason = reason
        return MaxGuardDecision(action, reason, state.fresh_tokens, estimate, state.requests)

    def reconcile(
        self,
        identity: str,
        *,
        fresh_tokens: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
        output_tokens: int | None,
        prefix_hash: str | None,
    ) -> MaxGuardDecision:
        state = self.state(identity)
        fresh = max(0, int(fresh_tokens or 0))
        cache_read = max(0, int(cache_read_tokens or 0))
        state.fresh_tokens += fresh
        state.cache_read_tokens += cache_read
        state.cache_write_tokens += max(0, int(cache_write_tokens or 0))
        state.output_tokens += max(0, int(output_tokens or 0))
        state.requests += 1
        state.updated_at = time.monotonic()

        reason = "usage_reconciled"
        requested_action = "observe"
        if prefix_hash and fresh >= self.policy.cache_free_min_tokens and cache_read == 0:
            count = state.stable_cache_free_calls.get(prefix_hash, 0) + 1
            state.stable_cache_free_calls[prefix_hash] = count
            if count >= self.policy.cache_free_stable_pause_calls:
                requested_action, reason = "pause", "repeated_stable_prefix_without_cache"
            elif count >= self.policy.cache_free_stable_warn_calls:
                requested_action, reason = "warn", "stable_prefix_without_cache"
        if state.fresh_tokens >= self.policy.pause_fresh_tokens:
            requested_action, reason = "pause", "session_fresh_token_allowance"
        elif state.fresh_tokens >= self.policy.warn_fresh_tokens and requested_action == "observe":
            requested_action, reason = "warn", "session_fresh_token_warning"

        action = self._effective_action(requested_action)
        if action == "pause":
            state.paused = True
            state.pause_reason = reason
        return MaxGuardDecision(action, reason, state.fresh_tokens, 0, state.requests)

    def _effective_action(self, requested: str) -> str:
        if self.policy.mode == "observe":
            return "observe"
        if self.policy.mode == "warn" and requested == "pause":
            return "warn"
        return requested


def estimate_input_tokens(payload: dict | None) -> int:
    if not isinstance(payload, dict):
        return 0
    native = payload.get("_nautgate_anthropic_native")
    source = native if isinstance(native, dict) else payload
    # A deliberately conservative content-free estimate. Provider reconciliation
    # replaces it after the response; this exists to stop a single giant call.
    encoded = json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (len(encoded) + 2) // 3


async def reserve_durable(
    pool,
    *,
    identity: str,
    app: str,
    project_id: str,
    native_session: str,
    estimated_tokens: int,
    policy: MaxGuardPolicy,
) -> tuple[MaxGuardDecision, uuid.UUID | None]:
    """Atomically reserve Max capacity across session, project and lane windows."""
    reservation_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"max:{identity}")
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"max-project:{project_id}"
            )
            await conn.execute(
                """INSERT INTO nautgate.max_guard_sessions
                       (identity, app, project_id, native_session)
                     VALUES ($1, $2, $3, $4)
                     ON CONFLICT (identity) DO UPDATE SET
                       app = EXCLUDED.app, project_id = EXCLUDED.project_id,
                       native_session = EXCLUDED.native_session, updated_at = NOW()""",
                identity,
                app,
                project_id or None,
                native_session or None,
            )
            session = await conn.fetchrow(
                "SELECT fresh_tokens, request_count, paused, pause_reason "
                "FROM nautgate.max_guard_sessions WHERE identity = $1 FOR UPDATE",
                identity,
            )
            active_reserved = int(
                await conn.fetchval(
                    """SELECT COALESCE(SUM(estimated_fresh_tokens), 0)
                     FROM nautgate.max_guard_reservations
                    WHERE identity = $1 AND status = 'reserved'
                      AND created_at > NOW() - INTERVAL '15 minutes'""",
                    identity,
                )
                or 0
            )
            project_hour = int(
                await conn.fetchval(
                    """SELECT COALESCE(SUM(CASE WHEN status = 'reconciled'
                            THEN actual_fresh_tokens ELSE estimated_fresh_tokens END), 0)
                     FROM nautgate.max_guard_reservations
                    WHERE project_id = $1 AND status IN ('reserved', 'reconciled')
                      AND (status = 'reconciled' OR created_at > NOW() - INTERVAL '15 minutes')
                      AND created_at > NOW() - INTERVAL '1 hour'""",
                    project_id or None,
                )
                or 0
            )
            lane_five_hour = int(
                await conn.fetchval(
                    """SELECT COALESCE(SUM(CASE WHEN status = 'reconciled'
                            THEN actual_fresh_tokens ELSE estimated_fresh_tokens END), 0)
                     FROM nautgate.max_guard_reservations
                    WHERE status IN ('reserved', 'reconciled')
                      AND (status = 'reconciled' OR created_at > NOW() - INTERVAL '15 minutes')
                      AND created_at > NOW() - INTERVAL '5 hours'"""
                )
                or 0
            )
            lane_week = int(
                await conn.fetchval(
                    """SELECT COALESCE(SUM(CASE WHEN status = 'reconciled'
                            THEN actual_fresh_tokens ELSE estimated_fresh_tokens END), 0)
                     FROM nautgate.max_guard_reservations
                    WHERE status IN ('reserved', 'reconciled')
                      AND (status = 'reconciled' OR created_at > NOW() - INTERVAL '15 minutes')
                      AND created_at > NOW() - INTERVAL '7 days'"""
                )
                or 0
            )
            override = await conn.fetchrow(
                """SELECT id, extra_tokens, remaining_requests
                     FROM nautgate.max_guard_overrides
                    WHERE identity=$1 AND expires_at > NOW()
                      AND (remaining_requests IS NULL OR remaining_requests > 0)
                    ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED""",
                identity,
            )
            extra = int(override["extra_tokens"] or 0) if override else 0

            reason = "within_configured_allowance"
            requested = "observe"
            projected = int(session["fresh_tokens"] or 0) + active_reserved + estimated_tokens
            if session["paused"] and override is None:
                requested, reason = "pause", session["pause_reason"] or "session_paused"
            elif estimated_tokens >= policy.pause_request_tokens + extra:
                requested, reason = "pause", "estimated_request_tokens"
            elif projected >= policy.pause_fresh_tokens + extra:
                requested, reason = "pause", "session_fresh_token_allowance"
            elif project_hour + estimated_tokens >= policy.project_hour_pause_tokens + extra:
                requested, reason = "pause", "project_rolling_hour_allowance"
            elif lane_five_hour + estimated_tokens >= policy.lane_five_hour_pause_tokens + extra:
                requested, reason = "pause", "lane_rolling_five_hour_allowance"
            elif lane_week + estimated_tokens >= policy.lane_week_pause_tokens + extra:
                requested, reason = "pause", "lane_rolling_week_allowance"
            elif (
                projected >= policy.warn_fresh_tokens
                or estimated_tokens >= policy.warn_request_tokens
            ):
                requested, reason = "warn", "fresh_token_warning"

            effective = requested
            if policy.mode == "observe":
                effective = "observe"
            elif policy.mode == "warn" and requested == "pause":
                effective = "warn"
            decision = MaxGuardDecision(
                effective,
                reason,
                int(session["fresh_tokens"] or 0),
                estimated_tokens,
                int(session["request_count"] or 0),
            )
            if effective == "pause":
                await conn.execute(
                    "UPDATE nautgate.max_guard_sessions SET paused=TRUE, pause_reason=$2, updated_at=NOW() WHERE identity=$1",
                    identity,
                    reason,
                )
                return decision, None
            if override is not None and override["remaining_requests"] is not None:
                await conn.execute(
                    "UPDATE nautgate.max_guard_overrides SET remaining_requests=remaining_requests-1 WHERE id=$1",
                    override["id"],
                )
            await conn.execute(
                """INSERT INTO nautgate.max_guard_reservations
                       (id, identity, project_id, estimated_fresh_tokens)
                     VALUES ($1, $2, $3, $4)""",
                reservation_id,
                identity,
                project_id or None,
                estimated_tokens,
            )
    return decision, reservation_id


async def list_durable_states(pool) -> list[dict]:
    rows = await pool.fetch(
        """SELECT identity, app, project_id, native_session, fresh_tokens,
                  cache_read_tokens, cache_write_tokens, output_tokens,
                  request_count, paused, pause_reason, updated_at,
                  (SELECT COALESCE(SUM(CASE WHEN r.status='reconciled'
                              THEN r.actual_fresh_tokens ELSE r.estimated_fresh_tokens END), 0)
                     FROM nautgate.max_guard_reservations r
                    WHERE r.project_id=max_guard_sessions.project_id
                      AND r.status IN ('reserved','reconciled')
                      AND (r.status='reconciled' OR r.created_at > NOW()-INTERVAL '15 minutes')
                      AND r.created_at > NOW()-INTERVAL '1 hour') AS project_hour_tokens
             FROM nautgate.max_guard_sessions ORDER BY updated_at DESC"""
    )
    result = []
    for row in rows:
        item = dict(row)
        if item.get("updated_at") is not None:
            item["updated_at"] = item["updated_at"].isoformat()
        # PostgreSQL SUM over the reservation token columns is NUMERIC and
        # asyncpg therefore returns Decimal.  Keep the public API's token
        # counters consistently integer-valued and JSON serializable.
        item["project_hour_tokens"] = int(item.get("project_hour_tokens") or 0)
        result.append(item)
    return result


async def durable_window_summary(pool) -> dict:
    row = await pool.fetchrow(
        """SELECT
              COALESCE(SUM(CASE WHEN created_at > NOW()-INTERVAL '5 hours'
                THEN CASE WHEN status='reconciled' THEN actual_fresh_tokens
                          ELSE estimated_fresh_tokens END ELSE 0 END), 0) AS five_hour_tokens,
              COALESCE(SUM(CASE WHEN created_at > NOW()-INTERVAL '7 days'
                THEN CASE WHEN status='reconciled' THEN actual_fresh_tokens
                          ELSE estimated_fresh_tokens END ELSE 0 END), 0) AS week_tokens
             FROM nautgate.max_guard_reservations
            WHERE status IN ('reserved','reconciled')
              AND (status='reconciled' OR created_at > NOW()-INTERVAL '15 minutes')"""
    )
    return {
        "five_hour_tokens": int(row["five_hour_tokens"] or 0),
        "week_tokens": int(row["week_tokens"] or 0),
    }


async def set_durable_pause(
    pool,
    *,
    identity: str,
    paused: bool,
    actor_agent_id: str,
    reason: str | None = None,
) -> tuple[bool, uuid.UUID | None]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """UPDATE nautgate.max_guard_sessions SET paused=$2,
                      pause_reason=CASE WHEN $2 THEN $3 ELSE NULL END, updated_at=NOW()
                 WHERE identity=$1""",
                identity,
                paused,
                reason or "operator_pause",
            )
            if not result.endswith("1"):
                return False, None
            receipt_id = await _record_control_receipt(
                conn,
                actor_agent_id=actor_agent_id,
                identity=identity,
                action="pause" if paused else "resume",
                details={"reason": reason or "operator_pause"} if paused else {},
            )
    return True, receipt_id


async def create_durable_override(
    pool,
    *,
    identity: str,
    extra_tokens: int,
    remaining_requests: int | None,
    ttl_seconds: int,
    reason: str,
    actor_agent_id: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    override_id = uuid.uuid4()
    ttl = max(60, min(int(ttl_seconds), 86_400))
    extra = max(0, int(extra_tokens))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO nautgate.max_guard_overrides
                       (id, identity, extra_tokens, remaining_requests, reason, expires_at)
                     VALUES ($1, $2, $3, $4, $5, NOW() + make_interval(secs => $6))""",
                override_id,
                identity,
                extra,
                remaining_requests,
                reason,
                ttl,
            )
            receipt_id = await _record_control_receipt(
                conn,
                actor_agent_id=actor_agent_id,
                identity=identity,
                action="authorize",
                details={
                    "override_id": str(override_id),
                    "extra_tokens": extra,
                    "remaining_requests": remaining_requests,
                    "ttl_seconds": ttl,
                    "reason": reason,
                },
            )
    return override_id, receipt_id


async def _record_control_receipt(
    conn,
    *,
    actor_agent_id: str,
    identity: str,
    action: str,
    details: dict,
) -> uuid.UUID:
    """Write a guard event and signer-outbox receipt in the caller's transaction."""
    from datetime import UTC

    from app.audit_evidence import CONTROL_RECEIPT_SCHEMA, canonical_json, receipt_hash
    from app.version import get_version

    event_id, receipt_id = uuid.uuid4(), uuid.uuid4()
    event_row = await conn.fetchrow(
        """INSERT INTO nautgate.max_guard_control_events
               (id, actor_agent_id, identity, action, details)
             VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING created_at""",
        event_id,
        actor_agent_id,
        identity,
        action,
        json.dumps(details, ensure_ascii=False, separators=(",", ":")),
    )
    sequence = await conn.fetchval(
        """UPDATE nautgate.audit_state SET next_sequence=next_sequence+1
             WHERE singleton=TRUE RETURNING next_sequence-1"""
    )
    created_at = event_row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    receipt = {
        "schema": CONTROL_RECEIPT_SCHEMA,
        "receipt_id": str(receipt_id),
        "guard_event_id": str(event_id),
        "sequence": int(sequence),
        "created_at": created_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "actor": {"agent_id": actor_agent_id},
        "scope": {"type": "max_native_session", "identity": identity},
        "control": {"action": action, "details": details},
        "runtime": {"nautgate_version": get_version()},
    }
    canonical = canonical_json(receipt)
    digest = receipt_hash(receipt)
    await conn.execute(
        """INSERT INTO nautgate.audit_receipts
               (receipt_id, guard_event_id, evidence_sequence, schema_version,
                canonical_receipt, canonical_bytes, receipt_hash)
             VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)""",
        receipt_id,
        event_id,
        sequence,
        receipt["schema"],
        json.dumps(receipt, ensure_ascii=False, separators=(",", ":")),
        canonical,
        digest,
    )
    await conn.execute(
        "INSERT INTO nautgate.audit_outbox (receipt_id, evidence_sequence) VALUES ($1, $2)",
        receipt_id,
        sequence,
    )
    return receipt_id


async def reconcile_durable(
    pool,
    *,
    reservation_id: uuid.UUID,
    fresh_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    output_tokens: int | None,
) -> None:
    """Replace one reservation with provider-reported usage exactly once."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            reservation = await conn.fetchrow(
                "SELECT identity, status FROM nautgate.max_guard_reservations WHERE id=$1 FOR UPDATE",
                reservation_id,
            )
            if reservation is None or reservation["status"] != "reserved":
                return
            fresh = max(0, int(fresh_tokens or 0))
            cache_read = max(0, int(cache_read_tokens or 0))
            cache_write = max(0, int(cache_write_tokens or 0))
            output = max(0, int(output_tokens or 0))
            await conn.execute(
                """UPDATE nautgate.max_guard_reservations SET status='reconciled',
                       actual_fresh_tokens=$2, cache_read_tokens=$3, cache_write_tokens=$4,
                       output_tokens=$5, reconciled_at=NOW() WHERE id=$1""",
                reservation_id,
                fresh,
                cache_read,
                cache_write,
                output,
            )
            await conn.execute(
                """UPDATE nautgate.max_guard_sessions SET
                       fresh_tokens=fresh_tokens+$2, cache_read_tokens=cache_read_tokens+$3,
                       cache_write_tokens=cache_write_tokens+$4, output_tokens=output_tokens+$5,
                       request_count=request_count+1, updated_at=NOW() WHERE identity=$1""",
                reservation["identity"],
                fresh,
                cache_read,
                cache_write,
                output,
            )
