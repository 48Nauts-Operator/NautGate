"""Retention only ever deleted rows it knew about.

A dump that failed, was killed, or whose row was removed leaves its file behind,
invisible to row-based retention forever. On a real instance that reached 31 GB
of unreferenced dumps against 49 GB of free disk.
"""

import time

import pytest

from app import backup as bk


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return self._rows


def _mk(dirpath, name, *, age_s=0, size=16):
    f = dirpath / name
    f.write_bytes(b"x" * size)
    if age_s:
        old = time.time() - age_s
        import os

        os.utime(f, (old, old))
    return f


@pytest.mark.asyncio
async def test_unreferenced_file_is_reaped(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "_backup_dir", lambda: tmp_path)
    keep = _mk(tmp_path, "nautgate-1-scheduled.sql.gz", age_s=7200)
    orphan = _mk(tmp_path, "nautgate-2-scheduled.sql.gz", age_s=7200)
    await bk._reap_orphan_files(_FakePool([{"file_path": str(keep)}]))
    assert keep.exists()
    assert not orphan.exists()


@pytest.mark.asyncio
async def test_recent_file_is_left_alone(tmp_path, monkeypatch):
    """A dump in flight has no completed row yet — deleting it would be fatal."""
    monkeypatch.setattr(bk, "_backup_dir", lambda: tmp_path)
    inflight = _mk(tmp_path, "nautgate-3-scheduled.sql.gz", age_s=0)
    await bk._reap_orphan_files(_FakePool([]))
    assert inflight.exists()


@pytest.mark.asyncio
async def test_only_our_own_naming_pattern_is_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "_backup_dir", lambda: tmp_path)
    someone_elses = _mk(tmp_path, "important.sql.gz", age_s=7200)
    notes = _mk(tmp_path, "README.md", age_s=7200)
    await bk._reap_orphan_files(_FakePool([]))
    assert someone_elses.exists()
    assert notes.exists()


@pytest.mark.asyncio
async def test_a_failing_query_never_deletes_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "_backup_dir", lambda: tmp_path)
    f = _mk(tmp_path, "nautgate-4-scheduled.sql.gz", age_s=7200)

    class _Boom:
        async def fetch(self, *_a, **_k):
            raise RuntimeError("db down")

    await bk._reap_orphan_files(_Boom())
    assert f.exists(), "must not reap when the reference list could not be read"
