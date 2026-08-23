# Max Guard — Product and Engineering Specification

Date: 2026-08-24  
Status: Shipped in NautGate 0.5.0  
Incident reference: 2026-08-22 Claude Max depletion

## Purpose

Max Guard protects limited subscription capacity from accidental amplification by long-context agent workloads. It must answer, in real time:

1. Which app, project, native session, human turn, and agent tree is consuming capacity?
2. Is repeated context being served from prompt cache or charged as fresh input?
3. At the current velocity, when will the configured rolling allowance be exhausted?
4. Can NautGate pause the responsible workload without stopping unrelated work?

Max Guard is subscription-capacity protection, not merely dollar-cost control. A request with `cost_usd = 0` can still exhaust a Max plan.

## Incident-derived causal model

The August 22 traffic used the ordinary `ng_` lane:

```text
Claude-compatible client
  -> Anthropic Messages request
  -> NautGate Anthropic-to-OpenAI normalization
  -> NautRouter OpenAI-to-Anthropic reconstruction
  -> Anthropic using the operator's Max OAuth credential
```

The affected 703 Opus calls recorded 148,526,418 fresh input tokens and no cache reads or writes. Current code flattens Anthropic system blocks and reconstructs system/tool definitions without `cache_control`. This is a confirmed cache-semantics preservation defect in the translation path. A boundary-capture test is still required to establish whether the incident client supplied cache markers before translation.

## Feature 1 — Cache-safe Anthropic routing (P0)

### Requirements

- If an Anthropic request will ultimately use Anthropic, forward its native body without converting it through OpenAI Chat.
- Preserve `cache_control`, block ordering, tool definitions, tool choice, thinking configuration, metadata, and supported beta headers.
- If translation is unavoidable, represent cache metadata in the canonical request model and round-trip it losslessly.
- Never invent cache breakpoints silently. Provider-specific automatic cache policy must be explicit, versioned, configurable, and visible in the audit receipt.
- Record four independent facts per request:
  - cache markers received from the client;
  - cache markers sent upstream;
  - cache tokens written;
  - cache tokens read.
- Redact content while retaining marker locations and stable prefix hashes.

### Safety behavior

- Emit `cache_integrity_lost` if markers were received but not forwarded.
- Reject the request by default when loss occurs on an enforcement-enabled subscription lane.
- Emit `cache_expected_but_absent` when a stable, sufficiently large repeated prefix produces neither writes nor reads across the configured observation window.
- Never claim that caching was active solely because a prefix hash was stable.

### Acceptance criteria

- A Claude Code-shaped request containing cache controls reaches a capture upstream with identical controls and ordering.
- The second request with a stable test prefix reports a cache read against an Anthropic-compatible integration fixture.
- Removing one cache marker makes a release-blocking regression test fail.
- Requests without cache markers remain valid and are reported as `not_requested`, not `lost`.

## Feature 2 — Native workload identity

### Requirements

- Persist app ID, project ID, native session ID, human-turn ID, task ID, agent ID, parent-agent ID, process/run ID, authenticated key ID, and source instance.
- Prefer trusted client identifiers over inferred identifiers.
- Label inferred identifiers as heuristic and record their derivation version.
- Detect simultaneous incompatible histories mapped to one inferred session.

### Acceptance criteria

- Identical opening prompts in two projects cannot merge.
- The August 22 xNaut and website traffic resolves to separate native sessions.
- Every enforcement event identifies its scope and attribution confidence.

## Feature 3 — Subscription-capacity budgets

### Requirements

- Budget fresh input, cache writes, cache reads, output, requests, concurrency, and wall-clock runtime independently.
- Support scopes for account lane, key, app, project, native session, task, human turn, agent, and agent tree.
- Support rolling windows of 5 minutes, 1 hour, 5 hours, 1 day, and 7 days.
- Reserve estimated capacity atomically before dispatch and reconcile it with provider usage afterward.
- Keep subscription budgets independent of monetary budgets.
- Start with observe-only policies, then allow warn, approval, route-local, pause, and block actions.

### Initial configurable policy template

| Signal | Observe/warn | Approval/pause |
|---|---:|---:|
| Estimated fresh input per Opus request | 100K | 200K |
| Fresh input per human turn | 2M | 5M |
| Fresh input per project per rolling hour | 5M | 10M |
| Cache-free calls with stable prefix over 50K | 2 | 3 |
| Agentic calls per human turn | 20 | 50 |

These are bootstrap defaults, not claims about Anthropic's unpublished quota formula.

### Runtime configuration

The initial implementation is observe-only unless the operator explicitly changes
`NAUTGATE_MAX_GUARD_MODE` to `warn` or `pause`.

| Environment variable | Default |
|---|---:|
| `NAUTGATE_MAX_GUARD_MODE` | `observe` |
| `NAUTGATE_MAX_GUARD_WARN_FRESH_TOKENS` | `1000000` |
| `NAUTGATE_MAX_GUARD_PAUSE_FRESH_TOKENS` | `5000000` |
| `NAUTGATE_MAX_GUARD_WARN_REQUEST_TOKENS` | `100000` |
| `NAUTGATE_MAX_GUARD_PAUSE_REQUEST_TOKENS` | `300000` |
| `NAUTGATE_MAX_GUARD_CACHE_FREE_WARN_CALLS` | `2` |
| `NAUTGATE_MAX_GUARD_CACHE_FREE_PAUSE_CALLS` | `3` |
| `NAUTGATE_MAX_GUARD_CACHE_FREE_MIN_TOKENS` | `50000` |
| `NAUTGATE_MAX_GUARD_PROJECT_HOUR_PAUSE_TOKENS` | `10000000` |
| `NAUTGATE_MAX_GUARD_LANE_FIVE_HOUR_PAUSE_TOKENS` | `25000000` |
| `NAUTGATE_MAX_GUARD_LANE_WEEK_PAUSE_TOKENS` | `100000000` |

`pause` returns an Anthropic-shaped HTTP 403 error with
`error.type = nautgate_max_guard_paused` and `retryable = false`. It never falls
back to metered Anthropic credits. With PostgreSQL configured, session pause
state, reconciled usage, and in-flight reservations survive NautGate restarts.
The in-memory guard remains as a process-local fallback when the database is
unavailable. Dashboard controls and signed override receipts belong to the next
control-plane increment.

### Operator control API

All endpoints require an ordinary authenticated NautGate key. NautGate currently
uses its existing single-operator administration model, so an authenticated key
can operate any Max Guard session.

| Operation | Endpoint |
|---|---|
| List state | `GET /v1/max-guard/sessions` |
| Pause one identity | `POST /v1/max-guard/sessions/{identity}/pause` |
| Resume one identity | `POST /v1/max-guard/sessions/{identity}/resume` |
| Authorize one or more requests | `POST /v1/max-guard/sessions/{identity}/authorize` |

The authorization body accepts `extra_tokens`, `remaining_requests`,
`ttl_seconds`, and `reason`. The default grants one request up to five million
tokens for fifteen minutes. Passing `remaining_requests: null` creates a
time-limited capacity increase. One-request grants are locked and consumed in
the same transaction as the request reservation.

Pause, resume, and authorize mutate control state and create a canonical
`dev.nautgate.max-guard-control-receipt/v1` in one transaction. The receipt uses
a distinct hash domain, receives the shared gapless evidence sequence, and enters
the existing audit outbox. It is included in the normal Merkle checkpoint and
signature workflow and can be exported through the Verified Audit Trail APIs.
Dashboard action feedback displays the receipt ID.

When xNaut Agent Space sees `nautgate_max_guard_paused`, it terminates the exact
zellij session named in the launch response and presents a Max Guard explanation
in the conversation. It does not leave the background Claude process retrying.

## Feature 4 — Cache and context anomaly engine

### Requirements

- Track context size, fresh-token velocity, cache-hit ratio, calls per human turn, input/output ratio, concurrent branches, and repeated tool cycles.
- Distinguish sustained interactive work from autonomous no-human-progress activity.
- Link tool-result continuations to the external human turn that initiated them.
- Detect continued requests after cancellation and repeated retries of non-retryable guardrail responses.
- Produce deterministic evidence fields for every anomaly; optional model-generated explanations may summarize but never establish the finding.

## Feature 5 — Pause and cancellation control plane

### Requirements

- Pause by subscription lane, key, app, project, session, task, agent, or agent tree.
- Reject subsequent requests for paused identities with a structured non-retryable response.
- Expose APIs and events that xNaut can use to propagate cancellation to process groups.
- Support one-request authorization and time-bound/amount-bound overrides.
- Preserve unrelated sessions when a narrower scope can be identified safely.

## Feature 6 — Max Guard UI and incident explanation

### Requirements

- Show fresh, cache-write, cache-read, and output tokens separately.
- Display cache status as `requested`, `forwarded`, `written`, `hit`, `not requested`, or `lost`.
- Drill down account lane -> app -> project -> native session -> human turn -> agent/tool calls.
- Show rolling five-hour and seven-day consumption estimates, clearly labeled as estimates.
- Provide stop, pause, route-local, compact/restart, and authorize actions.
- Generate an HTML incident report containing facts, inferences, confidence, and unresolved evidence gaps.

## Audit and privacy requirements

- Store marker topology and hashes without requiring full prompt retention.
- Never store OAuth credentials or authorization headers.
- Sign guardrail events and policy overrides through the existing audit-receipt system.
- Record the policy version and exact measured values behind each enforcement decision.

## Explicit non-goals

- Reverse-engineering or claiming Anthropic's exact Max quota formula.
- Treating every old-session resume or long agent run as an error.
- Using prompt caching as the only runaway-work defense.
- Allowing NautGate to kill local processes without an explicit client-side cancellation integration.
