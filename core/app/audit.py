"""Lighthouse-style privacy audit aggregation.

Given a list of recent decisions (with optional prompt_body), produces the
Lighthouse-shaped report: overall score, per-category scores, exposure
matrices by host (agent_id) and by finding type with sample matched text.

Mirrors ClawProxy's report shape so the SecondBrain dashboard's audit tab
and NautGate's Privacy tab present comparable numbers.
"""

from __future__ import annotations

from typing import Any

from app.classify import scan_for_findings
from app.findings import (
    CATEGORY,
    DESCRIPTION,
    DISPLAY,
    REMEDIATION,
    SEVERITY,
    category_score,
    overall_score,
    verdict_for,
)


def _empty_counts() -> dict[str, int]:
    return {"critical": 0, "warning": 0, "info": 0}


def _empty_cat_counts() -> dict[str, dict[str, int]]:
    return {cat: _empty_counts() for cat in ("credentials", "secrets", "pii", "infrastructure")}


def build_audit(decisions: list[dict]) -> dict[str, Any]:
    """Aggregate a list of decisions into the Lighthouse audit report.

    Each decision row should carry: decision_id, ts (str ISO), agent_id,
    classified_sensitivity, classified_signals (list[dict] or None), prompt_body
    (str or None), decision_model.

    For rows where prompt_body is present (capture policy didn't suppress), we
    re-scan the body to recover matched_text samples. For suppressed rows
    (sensitivity = secret) we fall back to the stored aggregated signals — no
    matched text, but counts and types are still attributed to the right
    category and severity.
    """
    cat_counts = _empty_cat_counts()
    host_matrix: dict[str, dict] = {}
    type_matrix: dict[str, dict] = {}

    def _bump_host(agent_id: str, cat: str, ts: str | None) -> None:
        h = host_matrix.setdefault(
            agent_id,
            {
                "credentials": 0,
                "secrets": 0,
                "pii": 0,
                "infrastructure": 0,
                "total": 0,
                "lastSeen": ts,
            },
        )
        h[cat] = h.get(cat, 0) + 1
        h["total"] += 1
        if ts and (h["lastSeen"] is None or ts > h["lastSeen"]):
            h["lastSeen"] = ts

    def _bump_type(
        rule_id: str, severity: str, agent_id: str, ts: str | None, sample: str | None
    ) -> None:
        t = type_matrix.setdefault(
            rule_id,
            {
                "rule_id": rule_id,
                "display": DISPLAY.get(rule_id, rule_id),
                "category": CATEGORY.get(rule_id, "secrets"),
                "severity": severity,
                "count": 0,
                "agents": set(),
                "lastSeen": ts,
                "samples": [],
            },
        )
        t["count"] += 1
        t["agents"].add(agent_id)
        if ts and (t["lastSeen"] is None or ts > t["lastSeen"]):
            t["lastSeen"] = ts
        if sample and len(t["samples"]) < 5 and sample not in t["samples"]:
            t["samples"].append(sample)
        # Severity escalation (critical > warning > info).
        order = {"critical": 2, "warning": 1, "info": 0}
        if order.get(severity, 0) > order.get(t["severity"], 0):
            t["severity"] = severity

    for d in decisions:
        agent_id = d.get("agent_id") or "unknown"
        ts = d.get("ts")
        body = d.get("prompt_body")

        # Prefer rescanning the body — gives us matched samples.
        findings = scan_for_findings(body) if body else []

        if not findings:
            # Fall back to stored signals (no samples, but counts still attributed).
            for s in d.get("classified_signals") or []:
                if not isinstance(s, dict):
                    continue
                rid = s.get("rule_id")
                if not rid:
                    continue
                count = int(s.get("count", 1))
                severity = s.get("severity") or SEVERITY.get(rid, "info")
                cat = CATEGORY.get(rid, "secrets")
                # Record `count` repetitions so the matrix totals match what was seen.
                for _ in range(count):
                    cat_counts[cat][severity] = cat_counts[cat].get(severity, 0) + 1
                    _bump_host(agent_id, cat, ts)
                    _bump_type(rid, severity, agent_id, ts, None)
            continue

        for f in findings:
            rid = f["rule_id"]
            severity = f["severity"]
            cat = CATEGORY.get(rid, "secrets")
            cat_counts[cat][severity] = cat_counts[cat].get(severity, 0) + 1
            _bump_host(agent_id, cat, ts)
            _bump_type(rid, severity, agent_id, ts, f.get("matched_text"))

    # Convert sets to sorted lists for JSON.
    type_list = []
    for rid, t in type_matrix.items():
        type_list.append(
            {
                **t,
                "agents": sorted(t["agents"]),
                "description": DESCRIPTION.get(rid, "Sensitive data pattern detected."),
                "remediation": REMEDIATION.get(rid, "Review and rotate any exposed credentials."),
            }
        )
    type_list.sort(key=lambda x: (x["count"], x["severity"]), reverse=True)

    cat_scores = {cat: category_score(counts) for cat, counts in cat_counts.items()}
    overall = overall_score(cat_scores)
    verdict, explain = verdict_for(overall)

    return {
        "overall": overall,
        "verdict": verdict,
        "verdict_explain": explain,
        "cat_scores": cat_scores,
        "cat_counts": cat_counts,
        "host_matrix": [
            {"agent_id": agent, **counts}
            for agent, counts in sorted(
                host_matrix.items(), key=lambda kv: kv[1]["total"], reverse=True
            )
        ],
        "type_matrix": type_list,
        "scanned_count": len(decisions),
    }
