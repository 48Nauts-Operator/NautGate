"""Shared data structures for detection and redaction."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Detection:
    """A detected PII span.

    `value` is intentionally kept internal to the local process. Public audit
    reports should use `to_public_dict()` so raw PII does not leave the boundary.
    """

    start: int
    end: int
    label: str
    value: str
    source: str
    rule_id: str
    normalized: str | None = None
    confidence: float = 1.0

    def overlaps(self, other: Detection) -> bool:
        return self.start < other.end and other.start < self.end

    def to_public_dict(self, replacement: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "source": self.source,
            "rule_id": self.rule_id,
            "confidence": self.confidence,
        }
        if replacement is not None:
            payload["replacement"] = replacement
        return payload


@dataclass(frozen=True)
class DetectionConflict:
    kept: Detection
    dropped: Detection
    reason: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "kept": self.kept.to_public_dict(),
            "dropped": self.dropped.to_public_dict(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PlaceholderEntry:
    label: str
    value: str
    source: str
    rule_id: str
    normalized: str | None = None


@dataclass(frozen=True)
class RedactionResult:
    redacted: str
    spans: list[dict[str, object]]
    placeholder_map: dict[str, PlaceholderEntry]
    audit: dict[str, object] = field(default_factory=dict)
