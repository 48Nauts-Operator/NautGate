"""NAUTGATE-36 — an ng_ client asking for claude-* must not hit the metered key."""

import json
import subprocess

from app import anthropic_subscription as subs

_KEYCHAIN_BLOB = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-abc"}}).encode()


def _fake_security(returncode=0, stdout=_KEYCHAIN_BLOB):
    def run(cmd, **kwargs):
        assert cmd[0] == "security"
        return subprocess.CompletedProcess(cmd, returncode, stdout, b"")

    return run


def test_env_token_wins(monkeypatch):
    monkeypatch.setenv("NAUTGATE_ANTHROPIC_OAUTH_TOKEN", " sk-ant-oat01-env ")
    monkeypatch.setattr(subs.subprocess, "run", _fake_security())
    assert subs.subscription_token() == "sk-ant-oat01-env"


def test_falls_back_to_keychain(monkeypatch):
    monkeypatch.delenv("NAUTGATE_ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(subs.subprocess, "run", _fake_security())
    assert subs.subscription_token() == "sk-ant-oat01-abc"


def test_rejects_a_metered_key_in_the_keychain(monkeypatch):
    """Only an OAuth token belongs here; a metered key would defeat the point."""
    monkeypatch.delenv("NAUTGATE_ANTHROPIC_OAUTH_TOKEN", raising=False)
    blob = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-api03-metered"}}).encode()
    monkeypatch.setattr(subs.subprocess, "run", _fake_security(stdout=blob))
    assert subs.subscription_token() is None


def test_no_keychain_entry(monkeypatch):
    monkeypatch.delenv("NAUTGATE_ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(subs.subprocess, "run", _fake_security(returncode=44, stdout=b""))
    assert subs.subscription_token() is None
