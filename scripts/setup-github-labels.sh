#!/usr/bin/env bash
# Apply the 48Nauts standard label set to the GitHub repo.
# GitHub analog of the Forgejo baseline's .forgejo/labels.yaml.
# Idempotent: --force updates a label if it already exists (incl. GitHub defaults).
#
# Requires: gh CLI, authenticated (`gh auth status`) with repo access.
# Usage:  ./scripts/setup-github-labels.sh [owner/repo]   (defaults to the repo's origin)
set -euo pipefail

REPO="${1:-}"
GH=(gh label create)
[ -n "$REPO" ] && GH+=(--repo "$REPO")

label() { "${GH[@]}" "$1" --color "$2" --description "$3" --force; }

# ── Type ────────────────────────────────────────────────────────
label "bug"             d73a4a "Something is broken"
label "feature"         0e8a16 "New capability"
label "incident"        b60205 "Production / shared infra broken"
label "enhancement"     a2eeef "Improvement to an existing feature"
label "documentation"   0075ca "Docs only"
label "chore"           cccccc "Maintenance / housekeeping"
label "refactor"        bfdadc "Internal restructure, no behavior change"
label "ci"              fbca04 "CI / build infrastructure"
label "security"        b60205 "Security-relevant"
label "dependencies"    0366d6 "Dependency updates"

# ── Priority ────────────────────────────────────────────────────
label "priority/high"   b60205 "Urgent"
label "priority/medium" fbca04 "Normal"
label "priority/low"    0e8a16 "Whenever"

# ── Status ──────────────────────────────────────────────────────
label "needs-triage"    ededed "Awaiting initial review"
label "blocked"         000000 "Blocked on something"
label "in-progress"     1d76db "Being worked on"

# ── Community ───────────────────────────────────────────────────
label "good first issue" 7057ff "Good entry point for new contributors"
label "help wanted"      008672 "Extra attention / help welcome"

echo "✓ labels applied"
