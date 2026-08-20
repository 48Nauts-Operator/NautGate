"""Anthropic subscription token for ordinary NautGate ``ng_`` requests.

``anthropic_oauth_forwarder`` only fires when the CLIENT already holds a
``sk-ant-oat01-`` token (Claude Code does). An xNAUT chat surface authenticates
with an ``ng_`` key, so it fell through to NautRouter's metered
``ANTHROPIC_API_KEY`` — which is why every ``claude-*`` call answered
``502 upstream_failed (400): Your credit balance is too low`` (NAUTGATE-36).

This supplies the operator's own subscription token as the ``anthropic``
provider key instead. NautRouter recognises the ``sk-ant-oat01-`` prefix and
switches to the Bearer + oauth-beta + Claude-Code-system-block lane.

Source order: ``NAUTGATE_ANTHROPIC_OAUTH_TOKEN`` (containers, non-macOS), then
the macOS keychain item Claude Code owns. Reading the keychain rather than
copying the token into ``.env`` means Claude Code's own refresh keeps us fresh;
a pasted token goes stale in hours.
"""

from __future__ import annotations

import json
import os
import subprocess

_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _from_env() -> str | None:
    value = os.environ.get("NAUTGATE_ANTHROPIC_OAUTH_TOKEN", "").strip()
    return value or None


def _from_keychain() -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        token = json.loads(out.stdout)["claudeAiOauth"]["accessToken"]
    except (ValueError, TypeError, KeyError):
        return None
    return token if isinstance(token, str) and token.startswith("sk-ant-oat01-") else None


def subscription_token() -> str | None:
    """The operator's Claude subscription access token, or None."""
    return _from_env() or _from_keychain()
