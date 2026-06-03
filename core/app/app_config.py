"""Runtime-tunable config stored in nautgate.app_config (single row).

Operator can flip these from the Dashboard without editing .env + restart.
For settings that shouldn't be exposed (passwords, secrets), keep them in
env vars; this table is for things you'd happily display on a Settings
page.

DB row is checked first; env vars are the fallback when the DB has no
value for a key (or when running without a DB pool at all — e.g. during
tests).
"""

from __future__ import annotations

import json
import os
from typing import Any

import asyncpg

_DEFAULTS: dict[str, Any] = {
    "sb_ingest": {
        "enabled": False,
        "host": "100.71.163.122",
        "port": 5433,
        "database": "agents_memory",
        "user": "agents",
    },
    "quality_eval": {
        "enabled": True,
        "sample_rate": 0.10,
        "daily_cost_cap_usd": 5.00,
        # OpenRouter is the default: same gpt-4o-mini judge, but billed via
        # the OPENROUTER_API_KEY you already have set, and the UI's model
        # selector pulls the full live catalog so you can switch to a cheaper
        # model (DeepSeek, Gemini Flash, …) without touching code.
        "judge_provider": "openrouter",
        "judge_model": "openai/gpt-4o-mini",
        "judge_base_url": "https://openrouter.ai/api",
    },
}


# Per-provider config defaults. Used by quality_eval_config() to fill in
# base_url + api-key env var when the operator picks a provider in the UI.
QUALITY_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "api_key_env": "OPENAI_API_KEY",
    },
    "lmstudio": {
        # Match what's already used by NautRouter for LMStudio. Strip a
        # trailing /v1 if present; quality_eval._call_judge re-appends.
        "base_url": (os.environ.get("LMSTUDIO_BASE_URL", "http://host.docker.internal:1234")
                     .rstrip("/").removesuffix("/v1")),
        "api_key_env": "",  # LMStudio doesn't need a key
    },
}


async def get_settings(pool: asyncpg.Pool | None) -> dict[str, Any]:
    """Read the single-row app_config. Returns defaults merged with whatever
    is stored. Never raises — DB unavailability falls back to defaults.
    """
    if pool is None:
        return dict(_DEFAULTS)
    try:
        row = await pool.fetchrow("SELECT settings FROM nautgate.app_config WHERE id = 1")
    except Exception:
        return dict(_DEFAULTS)
    if row is None:
        return dict(_DEFAULTS)
    stored = row["settings"]
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except (ValueError, TypeError):
            stored = {}
    if not isinstance(stored, dict):
        stored = {}
    # Merge defaults under stored values so newly-added keys are present
    # even on rows written before that key existed.
    out: dict[str, Any] = {}
    for k, v in _DEFAULTS.items():
        if isinstance(v, dict) and isinstance(stored.get(k), dict):
            out[k] = {**v, **stored[k]}
        else:
            out[k] = stored.get(k, v)
    return out


async def update_settings(pool: asyncpg.Pool, patch: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``patch`` into the stored settings JSON. Returns the
    result of get_settings after the update.
    """
    current = await get_settings(pool)
    # Shallow merge at the top level; one level deep for known sections.
    merged: dict[str, Any] = dict(current)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    await pool.execute(
        """
        INSERT INTO nautgate.app_config (id, settings, updated_at)
        VALUES (1, $1::jsonb, now())
        ON CONFLICT (id) DO UPDATE SET settings = EXCLUDED.settings, updated_at = now()
        """,
        json.dumps(merged),
    )
    return merged


async def sb_ingest_config(pool: asyncpg.Pool | None) -> dict[str, Any]:
    """Resolve the SB ingest config: DB > env > defaults.

    Returns a dict with ``enabled, host, port, database, user, password``.
    Password is always read from env (MEMORY_DB_PASSWORD) for safety —
    never stored in the DB.
    """
    settings = await get_settings(pool)
    sb = dict(settings.get("sb_ingest") or {})
    # Env overrides for anything the DB doesn't specify; lets ops still
    # set things via .env if they prefer that path.
    for key, env_name in (
        ("enabled", "NAUTGATE_SB_INGEST"),
        ("host", "MEMORY_DB_HOST"),
        ("port", "MEMORY_DB_PORT"),
        ("database", "MEMORY_DB_NAME"),
        ("user", "MEMORY_DB_USER"),
    ):
        env_val = os.environ.get(env_name)
        if env_val and sb.get(key) in (None, "", _DEFAULTS["sb_ingest"].get(key)):
            if key == "enabled":
                sb[key] = env_val.lower() in ("1", "true", "yes")
            elif key == "port":
                try:
                    sb[key] = int(env_val)
                except ValueError:
                    pass
            else:
                sb[key] = env_val
    sb["password"] = os.environ.get("MEMORY_DB_PASSWORD", "agents_secure_2026")
    return sb


async def quality_eval_config(pool: asyncpg.Pool | None) -> dict[str, Any]:
    """Resolve the Quality Eval config: DB > env > defaults.

    Returns a dict with ``enabled, sample_rate, daily_cost_cap_usd,
    judge_provider, judge_model, judge_base_url, api_key, api_key_env``.

    The API key env-var name is provider-derived by default (openrouter →
    ``OPENROUTER_API_KEY``, openai → ``OPENAI_API_KEY``, lmstudio → none),
    but can be explicitly overridden via ``judge_api_key_env`` in the DB
    row. The key itself is read from that env var at call time — never
    stored in the DB.
    """
    settings = await get_settings(pool)
    qe = dict(settings.get("quality_eval") or {})
    for key, env_name in (
        ("enabled", "NAUTGATE_QUALITY_EVAL"),
        ("sample_rate", "NAUTGATE_QUALITY_EVAL_SAMPLE_RATE"),
        ("daily_cost_cap_usd", "NAUTGATE_QUALITY_EVAL_DAILY_CAP_USD"),
        ("judge_provider", "NAUTGATE_QUALITY_EVAL_PROVIDER"),
        ("judge_model", "NAUTGATE_QUALITY_EVAL_MODEL"),
        ("judge_base_url", "NAUTGATE_QUALITY_EVAL_BASE_URL"),
    ):
        env_val = os.environ.get(env_name)
        default_val = _DEFAULTS["quality_eval"].get(key)
        if env_val and qe.get(key) in (None, "", default_val):
            if key == "enabled":
                qe[key] = env_val.lower() in ("1", "true", "yes")
            elif key in ("sample_rate", "daily_cost_cap_usd"):
                try:
                    qe[key] = float(env_val)
                except ValueError:
                    pass
            else:
                qe[key] = env_val

    # Fill in provider preset defaults when the user picked a known provider
    # but didn't set a base_url. Lets the UI offer a clean provider dropdown.
    provider = (qe.get("judge_provider") or "openrouter").lower()
    preset = QUALITY_PROVIDER_PRESETS.get(provider, {})
    if not qe.get("judge_base_url") and preset.get("base_url"):
        qe["judge_base_url"] = preset["base_url"]

    api_key_env = qe.get("judge_api_key_env") or preset.get("api_key_env") or ""
    qe["api_key_env"] = api_key_env
    qe["api_key"] = os.environ.get(api_key_env, "") if api_key_env else ""
    return qe
