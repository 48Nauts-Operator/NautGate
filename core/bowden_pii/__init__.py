"""Vendored deterministic detector from bowden-pii.

Only the dependency-free rule runtime is shipped here. Neural training and
document tooling remain in the upstream Bowden repository.
"""

from bowden_pii.rules import rule_detections as detect

__all__ = ["detect"]
