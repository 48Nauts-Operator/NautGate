import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.anthropic_oauth_forwarder import max_guard_pause_response
from app.max_guard import (
    MaxGuard,
    MaxGuardDecision,
    MaxGuardPolicy,
    _record_control_receipt,
    reconcile_durable,
    reserve_durable,
)


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_):
        return False


class _FakeConn:
    def __init__(
        self, *, fresh=0, reserved=0, project=0, five=0, week=0, paused=False, override=None
    ):
        self.values = iter([reserved, project, five, week])
        self.session = {
            "fresh_tokens": fresh,
            "request_count": 2,
            "paused": paused,
            "pause_reason": "manual" if paused else None,
        }
        self.executed = []
        self.override = override

    def transaction(self):
        return _Context(self)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchrow(self, sql, *args):
        if "max_guard_overrides" in sql:
            return self.override
        return self.session

    async def fetchval(self, sql, *args):
        return next(self.values)


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Context(self.conn)


def test_august_incident_replay_warns_before_one_million_and_pauses_before_five():
    guard = MaxGuard(
        MaxGuardPolicy(
            mode="pause",
            warn_fresh_tokens=1_000_000,
            pause_fresh_tokens=5_000_000,
            pause_request_tokens=999_999,
            cache_free_stable_warn_calls=99,
            cache_free_stable_pause_calls=100,
        )
    )
    warned_at = paused_at = None
    for call in range(1, 321):
        decision = guard.reconcile(
            "pi-session",
            fresh_tokens=46_550_000 // 320,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=100,
            prefix_hash=f"growing-{call}",
        )
        if decision.action == "warn" and warned_at is None:
            warned_at = decision.fresh_tokens
        if decision.action == "pause":
            paused_at = decision.fresh_tokens
            break
    assert warned_at is not None and warned_at < 1_200_000
    assert paused_at is not None and paused_at < 5_200_000


def test_three_large_cache_free_reuses_pause_only_that_session():
    guard = MaxGuard(
        MaxGuardPolicy(mode="pause", warn_fresh_tokens=99_000_000, pause_fresh_tokens=100_000_000)
    )
    actions = [
        guard.reconcile(
            "runaway",
            fresh_tokens=60_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            prefix_hash="same",
        ).action
        for _ in range(3)
    ]
    assert actions == ["observe", "warn", "pause"]
    assert guard.preflight("runaway", {}).action == "pause"
    assert guard.preflight("interactive", {}).action == "observe"


def test_observe_mode_never_blocks_but_reports_usage():
    guard = MaxGuard(MaxGuardPolicy(mode="observe", pause_fresh_tokens=10))
    decision = guard.reconcile(
        "s",
        fresh_tokens=20,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        prefix_hash=None,
    )
    assert decision.action == "observe"
    assert decision.fresh_tokens == 20


def test_pause_response_is_structured_and_explicitly_non_retryable():
    response = max_guard_pause_response(
        MaxGuardDecision("pause", "session_fresh_token_allowance", 5_100_000, 10, 36),
        native_session=True,
        pause_tokens=5_000_000,
    )
    body = json.loads(response.body)
    assert response.status_code == 403
    assert response.headers["x-nautgate-max-guard"] == "paused"
    assert body["error"]["type"] == "nautgate_max_guard_paused"
    assert body["error"]["retryable"] is False
    assert body["error"]["scope"] == "native_session"


async def test_durable_reservation_counts_inflight_requests_before_dispatch():
    conn = _FakeConn(fresh=4_700_000, reserved=200_000)
    decision, reservation = await reserve_durable(
        _FakePool(conn),
        identity="s",
        app="xnaut",
        project_id="p",
        native_session="s",
        estimated_tokens=150_000,
        policy=MaxGuardPolicy(mode="pause", pause_request_tokens=999_999),
    )
    assert decision.action == "pause"
    assert decision.reason == "session_fresh_token_allowance"
    assert reservation is None
    assert not any("INSERT INTO nautgate.max_guard_reservations" in sql for sql, _ in conn.executed)


async def test_durable_reservation_is_inserted_below_all_rolling_limits():
    conn = _FakeConn(fresh=100, reserved=0, project=200, five=300, week=400)
    decision, reservation = await reserve_durable(
        _FakePool(conn),
        identity="s",
        app="xnaut",
        project_id="p",
        native_session="s",
        estimated_tokens=50,
        policy=MaxGuardPolicy(mode="pause"),
    )
    assert decision.action == "observe"
    assert reservation is not None
    assert any("INSERT INTO nautgate.max_guard_reservations" in sql for sql, _ in conn.executed)


async def test_reconcile_is_idempotent_when_reservation_is_already_finished():
    conn = _FakeConn()
    conn.session = {"identity": "s", "status": "reconciled"}
    await reconcile_durable(
        _FakePool(conn),
        reservation_id=__import__("uuid").uuid4(),
        fresh_tokens=10,
        cache_read_tokens=1,
        cache_write_tokens=2,
        output_tokens=3,
    )
    assert not any("UPDATE nautgate.max_guard_sessions" in sql for sql, _ in conn.executed)


async def test_one_request_override_bypasses_pause_and_is_consumed_atomically():
    conn = _FakeConn(
        paused=True,
        override={
            "id": __import__("uuid").uuid4(),
            "extra_tokens": 5_000_000,
            "remaining_requests": 1,
        },
    )
    decision, reservation = await reserve_durable(
        _FakePool(conn),
        identity="s",
        app="xnaut",
        project_id="p",
        native_session="s",
        estimated_tokens=100,
        policy=MaxGuardPolicy(mode="pause"),
    )
    assert decision.action == "observe"
    assert reservation is not None
    assert any("remaining_requests=remaining_requests-1" in sql for sql, _ in conn.executed)


async def test_dashboard_endpoint_exposes_windows_policy_and_paused_sessions(monkeypatch):
    class DashboardPool:
        async def fetch(self, _sql):
            return [
                {
                    "identity": "s",
                    "app": "xnaut",
                    "project_id": "p",
                    "native_session": "native",
                    "fresh_tokens": 123,
                    "cache_read_tokens": 45,
                    "cache_write_tokens": 6,
                    "output_tokens": 7,
                    "request_count": 8,
                    "paused": True,
                    "pause_reason": "limit",
                    "updated_at": datetime.now(UTC),
                    "project_hour_tokens": Decimal("120"),
                }
            ]

        async def fetchrow(self, _sql):
            return {"five_hour_tokens": 1000, "week_tokens": 2000}

    async def authenticated(_pool, _request):
        return "operator"

    monkeypatch.setattr("app.routes.v1.authenticate", authenticated)
    from app.routes.v1 import max_guard_sessions

    guard = MaxGuard(MaxGuardPolicy(mode="pause"))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=DashboardPool(), max_guard=guard))
    )
    response = await max_guard_sessions(request)
    body = json.loads(response.body)
    assert body["windows"] == {"five_hour_tokens": 1000, "week_tokens": 2000}
    assert body["policy"]["mode"] == "pause"
    assert body["items"][0]["paused"] is True
    assert body["items"][0]["project_hour_tokens"] == 120


async def test_guard_control_receipt_enters_existing_signer_outbox():
    class ReceiptConn:
        def __init__(self):
            self.executed = []

        async def fetchrow(self, _sql, *_args):
            return {"created_at": datetime(2026, 8, 23, 12, 0, tzinfo=UTC)}

        async def fetchval(self, _sql, *_args):
            return 42

        async def execute(self, sql, *args):
            self.executed.append((sql, args))

    conn = ReceiptConn()
    receipt_id = await _record_control_receipt(
        conn,
        actor_agent_id="operator",
        identity="claude-session",
        action="authorize",
        details={"extra_tokens": 5_000_000, "remaining_requests": 1},
    )
    receipt_insert = next(args for sql, args in conn.executed if "audit_receipts" in sql)
    outbox_insert = next(args for sql, args in conn.executed if "audit_outbox" in sql)
    canonical = receipt_insert[5]
    digest = receipt_insert[6]
    assert str(receipt_id) in canonical.decode()
    assert b"dev.nautgate.max-guard-control-receipt/v1" in canonical
    assert len(digest) == 32
    assert outbox_insert == (receipt_id, 42)
