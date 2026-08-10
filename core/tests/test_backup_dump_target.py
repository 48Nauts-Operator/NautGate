"""A backup must dump the database it was configured for.

The docker-exec fallback hardcoded `-d nautgate`, so a sandbox pointed at a
scratch database still dumped production: 8 GB files recorded against a 10 MB
DB, where production's retention could never see them to clean up.
"""

import pytest

from app import backup as bk


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgres://u:p@h:5432/nautgate_compliance", "nautgate_compliance"),
        ("postgresql://u:p@h:5432/nautgate", "nautgate"),
        ("postgres://u:p@h:5432/ng_test?sslmode=disable", "ng_test"),
    ],
)
def test_database_comes_from_the_configured_dsn(monkeypatch, dsn, expected):
    monkeypatch.setattr(bk, "_dsn", lambda: dsn)
    assert bk._dsn_database() == expected


def test_a_dsn_without_a_database_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(bk, "_dsn", lambda: "postgres://u:p@h:5432/")
    assert bk._dsn_database() == "nautgate"


def test_the_docker_fallback_passes_the_configured_database(monkeypatch):
    monkeypatch.setattr(bk, "_use_dsn", lambda: False)
    monkeypatch.setattr(bk, "_dsn", lambda: "postgres://u:p@h:5432/scratch_db")
    cmd = bk._dump_cmd()
    assert cmd[cmd.index("-d") + 1] == "scratch_db"
    assert "nautgate" not in cmd[cmd.index("-d") + 1]
