"""Body retention must shrink the database without damaging the audit trail."""

from datetime import timedelta

import pytest

from app.retention import prune_bodies


class _Pool:
    def __init__(self, tags=("UPDATE 3", "UPDATE 2")):
        self.calls = []
        self._tags = list(tags)

    async def execute(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return self._tags.pop(0)


@pytest.mark.asyncio
async def test_prunes_both_tables_and_reports_counts():
    pool = _Pool()
    assert await prune_bodies(pool, retention_days=90) == {"decisions": 3, "outcomes": 2}
    assert len(pool.calls) == 2
    # Pin the TYPE, not just the value: asyncpg rejects a string for an
    # interval parameter, and the original fake-pool test passed anyway.
    for _, a in pool.calls:
        assert isinstance(a[0], timedelta), f"interval must be a timedelta, got {type(a[0])}"
        assert a[0] == timedelta(days=90)


@pytest.mark.asyncio
async def test_only_body_columns_are_touched():
    """The audit value is the metadata — cost, model, attestation must survive."""
    pool = _Pool()
    await prune_bodies(pool, retention_days=30)
    for sql, _ in pool.calls:
        assert "SET" in sql and "DELETE" not in sql, "must null columns, never delete rows"
        for keep in ("cost_usd", "actual_model", "actual_provider", "prompt_tokens", "status_code"):
            assert f"{keep} = NULL" not in sql, f"{keep} must not be nulled"


@pytest.mark.asyncio
async def test_a_nonpositive_window_prunes_nothing():
    """A misread config should keep too much, never destroy the audit trail."""
    for days in (0, -1, -9999):
        pool = _Pool()
        assert await prune_bodies(pool, retention_days=days) == {"decisions": 0, "outcomes": 0}
        assert pool.calls == []


@pytest.mark.asyncio
async def test_rows_already_pruned_are_skipped():
    pool = _Pool()
    await prune_bodies(pool, retention_days=90)
    assert "IS NOT NULL" in pool.calls[0][0]
    assert "IS NOT NULL" in pool.calls[1][0]


@pytest.mark.asyncio
async def test_unparseable_tag_counts_as_zero_rather_than_raising():
    pool = _Pool(tags=("weird", "UPDATE"))
    assert await prune_bodies(pool, retention_days=90) == {"decisions": 0, "outcomes": 0}
