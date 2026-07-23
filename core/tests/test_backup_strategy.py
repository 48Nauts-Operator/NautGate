"""backup.py picks the DSN command form when pg_dump/psql are on PATH, and
falls back to docker-exec otherwise (NAUTGATE-7)."""
from unittest import mock

import app.backup as backup


def test_dump_prefers_dsn_when_pg_tools_present():
    with mock.patch("shutil.which", return_value="/usr/bin/pg_dump"), \
         mock.patch.object(backup, "_dsn", return_value="postgres://u:p@h:5432/d"):
        cmd = backup._dump_cmd()
        assert cmd[0] == "pg_dump"
        assert cmd[1] == "postgres://u:p@h:5432/d"
        assert "--schema=nautgate" in cmd
        assert "docker" not in cmd

        psql = backup._psql_cmd(["-c", "SELECT 1;"])
        assert psql[0] == "psql"
        assert psql[-2:] == ["-c", "SELECT 1;"]


def test_falls_back_to_docker_exec_without_pg_tools():
    with mock.patch("shutil.which", return_value=None):
        assert backup._dump_cmd()[:2] == ["docker", "exec"]
        assert backup._psql_cmd([])[:2] == ["docker", "exec"]
