"""Pricing config + per-call cost computation.

Reads ``config/pricing.yaml`` once at startup. Exposes:
    pricing.compute_cost(provider, model, prompt_tokens, completion_tokens) → float | None

Returns None for unknown (provider, model) pairs so the caller can persist
``cost_usd = NULL`` rather than silently zero-cost a real spend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    input: float  # USD per million prompt tokens
    output: float  # USD per million completion tokens
    cache_read: float | None = None
    cache_write: float | None = None


class PricingTable:
    def __init__(self, prices: dict[str, ModelPrice]):
        self._prices = prices
        self._missing_warned: set[tuple[str, str]] = set()

    @classmethod
    def from_yaml(cls, path: str | Path) -> PricingTable:
        p = Path(path)
        if not p.exists():
            log.warning("pricing_yaml_missing path=%s", p)
            return cls({})
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log.warning("pricing_yaml_parse_failed err=%s", exc)
            return cls({})
        entries = raw.get("pricing") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return cls({})
        prices: dict[str, ModelPrice] = {}
        for key, body in entries.items():
            if not isinstance(body, dict):
                continue
            try:
                prices[str(key)] = ModelPrice(
                    input=float(body.get("input", 0)),
                    output=float(body.get("output", 0)),
                    cache_read=(float(body["cache_read"]) if "cache_read" in body else None),
                    cache_write=(float(body["cache_write"]) if "cache_write" in body else None),
                )
            except (TypeError, ValueError) as exc:
                log.warning("pricing_entry_invalid key=%s err=%s", key, exc)
        return cls(prices)

    @staticmethod
    def _key(provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def lookup(self, provider: str | None, model: str | None) -> ModelPrice | None:
        if not provider or not model:
            return None
        return self._prices.get(self._key(provider, model))

    def compute_cost(
        self,
        provider: str | None,
        model: str | None,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> float | None:
        """USD cost for one call. Returns None when pricing is unknown OR usage is missing.

        ``prompt_tokens`` is the FRESH (non-cached) input. Cache reads/writes are
        priced at their own tiers (cache_read / cache_write from pricing.yaml),
        falling back to the full ``input`` rate when a tier is unpriced so we
        never silently zero-cost cached volume.
        """
        price = self.lookup(provider, model)
        if price is None:
            if provider and model:
                pair = (provider, model)
                if pair not in self._missing_warned:
                    log.warning("pricing_unknown provider=%s model=%s", provider, model)
                    self._missing_warned.add(pair)
            return None
        if (
            prompt_tokens is None
            and completion_tokens is None
            and cache_read_tokens is None
            and cache_write_tokens is None
        ):
            return None
        read_rate = price.cache_read if price.cache_read is not None else price.input
        write_rate = price.cache_write if price.cache_write is not None else price.input
        prompt_cost = (prompt_tokens or 0) * price.input / 1_000_000
        cache_read_cost = (cache_read_tokens or 0) * read_rate / 1_000_000
        cache_write_cost = (cache_write_tokens or 0) * write_rate / 1_000_000
        completion_cost = (completion_tokens or 0) * price.output / 1_000_000
        return round(prompt_cost + cache_read_cost + cache_write_cost + completion_cost, 6)

    @property
    def size(self) -> int:
        return len(self._prices)
