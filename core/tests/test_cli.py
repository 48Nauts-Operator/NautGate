"""The `nautgate` command — what a native (non-Docker) install runs.

Deliberately invoked as a module: pyproject sets `[tool.uv] package = false` and
the Dockerfile installs with `--no-install-project`, so declaring an entry point
would have changed how every contributor's sync and the image build behave, for
one wrapper the Homebrew formula can provide itself.
"""

import sys

import pytest

from app import cli


def test_version_is_reported(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert "nautgate" in capsys.readouterr().out


def test_no_command_prints_help_and_fails(capsys):
    assert cli.main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_default_bind_is_loopback_not_every_interface():
    """A native install has no container boundary around it. Binding 0.0.0.0 by
    default is exactly how the dashboard ended up readable from a whole tailnet."""
    assert cli.DEFAULT_HOST == "127.0.0.1"


def test_status_reports_the_address_it_probed(capsys):
    rc = cli.main(["status", "--port", "9"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "127.0.0.1:9" in err, "must name the address probed, not just 'not running'"


def test_serve_warns_when_binding_every_interface(monkeypatch, capsys):
    called = {}

    class _FakeUvicorn:
        @staticmethod
        def run(*_a, **kw):
            called.update(kw)

    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn)
    cli.main(["serve", "--host", "0.0.0.0"])
    assert "exposes the dashboard" in capsys.readouterr().err
    assert called["host"] == "0.0.0.0"


def test_serve_honours_env_when_no_flag_given(monkeypatch):
    called = {}

    class _FakeUvicorn:
        @staticmethod
        def run(*_a, **kw):
            called.update(kw)

    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setenv("NAUTGATE_PORT", "18123")
    cli.main(["serve"])
    assert called["port"] == 18123
