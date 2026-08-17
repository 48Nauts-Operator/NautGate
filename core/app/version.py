"""NautGate version, read from the one authoritative project value."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path


@cache
def get_version() -> str:
    """Return the build override or the version from ``core/pyproject.toml``."""
    env = os.environ.get("NAUTGATE_VERSION", "").strip()
    if env:
        return env
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return tomllib.loads(pyproject.read_text())["project"]["version"]
    except Exception:
        return "dev"
