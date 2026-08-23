# NautGate Max Guardrails Update Plan

Date: 2026-08-24  
Status: v0.5.0 baseline shipped; remaining phases retained as roadmap  
Incident window: 2026-08-22, Europe/Zurich

Product requirements: [Max Guard feature specification](features/max-guard.md)  
Forensic report: [22 August incident report](reports/nautgate-max-usage-forensics-2026-08-22.html)

## Evidence status

- **Confirmed:** the affected ordinary `ng_` traffic used the Anthropic-to-OpenAI-to-Anthropic translation path.
- **Confirmed:** that path flattens system blocks and reconstructs system/tools without preserving `cache_control`.
- **Confirmed:** 703 affected Opus calls recorded 148,526,418 fresh input tokens and zero cache reads/writes.
- **Still to prove:** whether Claude Code 2.1.239 supplied cache markers at NautGate's inbound boundary during the historical incident. The retained audit representation cannot answer this because it stores the normalized form.

This distinction must survive into code comments, tests, UI language, and the incident report: the translation defect is proven; the exact historical inbound marker state requires a controlled boundary capture.

## Corrected incident conclusion

NautGate initially made two independent Claude sessions appear to be one session. The affected audit ID `fc6a2477dfbc4dfc` contained:

- An xNaut implementation session that executed four requested features.
- A simultaneous `andrewolke.me` website session.

The streams can be separated by their stable prefix hashes and confirmed against Claude's native local transcripts.

| Corrected project | Opus calls | Fresh input | Share of 22 Aug Opus input |
|---|---:|---:|---:|
| xNaut | 367 | 101,972,471 | 68.7% |
| andrewolke.me | 320 | 46,553,947 | 31.3% |

The xNaut four-feature instruction itself caused 55 calls and 7,368,526 fresh input tokens between 13:58 and 14:12. Its native transcript shows coherent implementation: a drift linter, flow context packet, SessionStart brief, artifact-to-skill bindings, test repairs, 561 passing Rust tests, and a clean clippy run.

The 320-call stream was not an autonomous xNaut child agent. It was the concurrently active `andrewolke.me` Claude session. It received 72 human messages, performed website edits, read project documentation, built pages, and ran verification. It appeared autonomous in NautGate because tool-result messages had empty prompt excerpts and both projects collided under NautGate's heuristic session ID.

## Why a small website consumed 46.55M tokens

Project size did not determine usage. Conversation size and cache behavior did.

| Website-session metric | Value |
|---|---:|
| Human messages | 72 |
| Opus calls | 320 |
| Calls per human message | 4.4 |
| Starting fresh input per call | 35,795 |
| Average fresh input per call | 145,481 |
| Final fresh input per call | 273,886 |
| Message count growth | 19 → 662 |
| Request-body growth | 94 KB → 751 KB |
| Total output | 152,194 |
| Fresh input per output token | 305.9 |
| Cache reads / writes | 0 / 0 |

Every small copy, CSS, or content change resent the accumulated conversation, tool results, file reads, build logs, screenshots, and tool schemas. With no cache hits, all repeated context counted as fresh input.

## Confirmed product failures

### 1. Session identity collision

`compute_session_id()` hashes only `agent_id + first 200 characters of the first user message`. This is not a reliable session identity for xNaut handoffs or clients that reuse an initial context message. Independent projects can therefore merge in analytics.

### 2. Missing project attribution

Both expensive streams were stored with `project_id = NULL`. The shared `pi` identity was insufficient to attribute usage to an application, project, native session, agent, or task.

### 3. Max traffic bypasses cost budgets

Subscription traffic records `cost_usd = 0`. Existing dollar budgets therefore cannot protect a limited Max subscription.

### 4. No effective Anthropic prompt caching

The ordinary `ng_` Max lane translates requests but does not preserve or create Anthropic cache breakpoints. Audit outcomes recorded zero cache reads and writes.

### 5. No context-growth or velocity circuit breaker

NautGate forwarded requests as they grew beyond 100K, 200K, and 500K fresh input tokens without warning, pause, downgrade, or approval.

### 6. Misleading prompt semantics

Tool-result turns often have an empty `prompt_excerpt`. NautGate interpreted those as calls without human prompts, hiding the relationship between a human instruction and its subsequent tool loop.

### 7. No account-quota telemetry

NautGate does not capture or estimate Claude Max five-hour and weekly-plan impact, so it could not warn that the current burn rate was exceptional.

## Previously proposed safeguards

1. Maximum tool/model turns per assignment.
2. Maximum runtime and cumulative input tokens per agent task.
3. Cancellation propagation to child and background agents.
4. Detection of tool-result loops without meaningful progress.
5. Explicit completion criteria for spawned agents.
6. A visible list of active agents and their usage.
7. Parent/child agent identifiers on every request.
8. Per-agent and per-project kill switches.
9. Fresh-input budgets per call, session, agent, project, hour, day, and rolling five-hour window.
10. Context ceilings with warnings, approval gates, and hard stops.
11. Broken-cache detection for repeated large prefixes with zero cache hits.
12. Calls-per-human-turn and token-velocity circuit breakers.
13. Required project, app, task, key, native session, agent, and process attribution.
14. Explicit per-key opt-in before using subscription credentials.
15. Configurable fallback to a local model after a circuit breaker trips.
16. Dashboard notifications with stop, compact, route-local, and authorize actions.

## NautGate 0.5 release plan — Max Guard

The v0.5.0 release ships native xNaut launch identity, durable Max Guard
reservations and reconciliation, rolling capacity counters, pause/resume and
temporary authorization controls, cache usage telemetry, the Max Guard UI, and
the Observatory. Items below that are not represented in shipped code remain
the post-0.5 roadmap rather than implied release claims.

### Phase 0 — Prove and repair cache integrity (P0)

Goal: remove the known amplification path before adding broader controls.

Implementation tasks:

1. Add a redacted boundary-capture fixture around `/v1/messages` containing marker topology, body hash, system block count, tool count, and cache-marker locations.
2. Reproduce a Claude Code-shaped request against a local capture upstream; do not spend subscription capacity for the regression test.
3. Add an Anthropic-native forwarding path for an `ng_` request whose selected provider remains Anthropic.
4. Preserve supported headers and the original Anthropic body while replacing only the authorized model/credential fields required by policy.
5. Extend the canonical representation only for routes that genuinely require cross-provider translation; retain cache metadata explicitly.
6. Add `cache_markers_received`, `cache_markers_forwarded`, `cache_status`, and `cache_integrity_reason` audit fields through a migration.
7. Fail closed on `received > forwarded` when subscription cache enforcement is enabled.
8. Add a stable-prefix anomaly detector for repeated large requests with neither cache writes nor cache reads.

Primary implementation areas:

- `core/app/routes/v1.py`
- `core/app/formats/anthropic.py`
- `core/app/anthropic_oauth_forwarder.py` or a shared native Anthropic forwarder
- `vendor/NautRouter/src/index.ts`
- `core/app/usage.py`
- `core/app/db/migrations/`
- `core/tests/test_anthropic_format.py`
- new end-to-end cache-integrity tests

Acceptance criteria:

- Cache controls survive byte-structural comparison at the local upstream capture boundary.
- A marker-loss mutation makes the test suite fail.
- The audit record distinguishes `not_requested` from `lost`, `written`, and `hit`.
- Existing OAuth-native forwarding and non-Anthropic routing remain unchanged.

### Phase 1 — Correct identity and attribution

Goal: every call must be attributable before usage enforcement is trusted.

- Accept and persist trusted headers:
  - `X-NautGate-App-Id`
  - `X-NautGate-Project-Id`
  - `X-NautGate-Native-Session-Id`
  - `X-NautGate-Agent-Id`
  - `X-NautGate-Parent-Agent-Id`
  - `X-NautGate-Task-Id`
  - `X-NautGate-Process-Id`
- Persist the authenticated API-key ID and name on each decision.
- Prefer the native session ID when provided.
- Replace the first-message heuristic with a scoped fallback hash containing key ID, project, app, source instance, and a client conversation identifier.
- Mark fallback identities as `heuristic` in the dashboard.
- Add a collision detector when one session ID exhibits concurrent incompatible prefix hashes or message-count trajectories.
- Backfill the August 22 incident with corrected project attribution where evidence is sufficient.

Acceptance criteria:

- Concurrent xNaut and website sessions never merge.
- The audit drawer can name app, project, key, native session, task, agent, and parent agent.
- Collision tests cover identical initial user messages across projects.

### Phase 2 — Preserve Anthropic cache semantics

Goal: large stable context must not be billed as fresh on every tool turn.

- Inspect the final Anthropic payload after translation.
- Preserve inbound cache controls when present.
- Add safe cache breakpoints to stable system and tool-definition blocks.
- Add a conversation-prefix breakpoint compatible with Anthropic limits and semantics.
- Record cache eligibility, cache markers sent, cache reads, cache writes, and hit ratio.
- Add regression tests using Claude Code-shaped tool loops and growing histories.
- Do not treat cache implementation as a substitute for limits.

Acceptance criteria:

- Repeated calls with a stable 100K-token prefix produce cache reads after the first request.
- The dashboard distinguishes fresh, cache-read, and cache-write tokens accurately.
- A missing-cache regression blocks release.

### Phase 3 — Subscription token budgets

Goal: protect Max plans independently from monetary cost.

- Add budget units: fresh input, cached input, output, calls, concurrency, and context bytes.
- Add scopes: key, app, project, native session, task, agent, parent-agent tree, model, and subscription account.
- Add rolling windows: 5 minutes, 1 hour, 5 hours, 1 day, and 7 days.
- Calculate preflight estimates before forwarding and reconcile with provider-reported usage afterward.
- Reserve budget before upstream dispatch to avoid concurrency overshoot.
- Make subscription use opt-in per key and model family.

Recommended initial defaults for Opus subscription traffic:

- Warn above 100K estimated fresh input per request.
- Require approval above 200K.
- Block or route local above 300K.
- Warn at 2M fresh tokens per task.
- Pause at 5M fresh tokens per task unless explicitly authorized.
- Pause at 10M fresh tokens per project per rolling hour.

Defaults must be configurable and introduced in observe-only mode before enforcement.

### Phase 4 — Runaway and inefficiency detection

Goal: stop accidental spend even when the application behaves incorrectly.

- Track calls and tokens per human turn, not just per raw message.
- Link tool-result continuations to the last external human prompt.
- Detect:
  - excessive calls per human turn;
  - repeated tool call/result cycles;
  - context growth without proportionate output;
  - repeated large prefixes with zero cache hits;
  - concurrent branches under one task;
  - continued activity after cancellation;
  - high input-to-output ratios;
  - repeated corrections to the same files or failing tests.
- Use a staged response: observe → warn → require approval → pause → block.
- Never label normal long-running work a loop solely because it contains tool calls.

Acceptance criteria:

- The website session is classified as sustained interactive work with severe context/cache inefficiency, not an autonomous loop.
- A synthetic no-human-progress tool loop is paused within its configured allowance.
- The xNaut four-feature task is attributed as 55 calls / 7.37M fresh input under one human turn.

### Phase 5 — Cancellation and control plane

Goal: operators can stop consumption immediately and applications can propagate cancellation.

- Add session, task, agent-tree, project, key, and provider-lane pause controls.
- Expose a cancellation endpoint and event stream for xNaut.
- Reject new requests carrying a cancelled task/session ID.
- Require child agents to inherit the parent's budget and cancellation state.
- Add dashboard actions: stop, pause, route local, compact, authorize once, or increase budget.

Acceptance criteria:

- A user interrupt stops all subsequent child/task calls.
- A paused identity receives a structured, non-retryable NautGate response.
- The dashboard shows exactly what was stopped and why.

### Phase 6 — Max Guard dashboard and incident reports

Goal: answer “what consumed my plan and why?” without database forensics.

- Correct project/app/session usage breakdowns.
- Show fresh-token velocity, context growth, calls per human turn, cache-hit ratio, and concurrency.
- Add a rolling five-hour Max Guard gauge based on observed consumption.
- Clearly label the gauge as an estimate unless provider quota telemetry is available.
- Surface anomalies with evidence and confidence.
- Generate downloadable HTML incident reports.
- Show attribution gaps as product errors, not as `(none)` without explanation.

Acceptance criteria:

- The August 22 incident report identifies xNaut at 68.7% and andrewolke.me at 31.3%.
- The UI explains why the small website was expensive.
- The operator can drill from account → project → native session → human turn → tool calls.

## Executable delivery sequence

### Milestone A — Contain and prove

1. Temporarily configure the affected subscription lane to warn/pause on large cache-free Opus requests.
2. Build the local upstream boundary-capture harness.
3. Add the failing cache round-trip regression tests.
4. Implement Anthropic-native forwarding for Anthropic destinations.
5. Verify marker preservation, usage parsing, streaming, tools, thinking blocks, and OAuth credential substitution.

Exit gate: the second stable-prefix fixture request is observable as cache-eligible and no marker is lost inside NautGate.

### Milestone B — Make attribution trustworthy

1. Add schema fields and trusted attribution headers.
2. Replace the first-message session heuristic with native/scoped identity.
3. Update xNaut to send app, project, session, task, human-turn, agent-tree, and process identifiers.
4. Add collision detection and attribution-confidence UI.

Exit gate: replayed concurrent xNaut and website fixtures never merge.

### Milestone C — Observe Max capacity

1. Implement atomic token reservations and rolling counters.
2. Add observe-only policies for fresh input, cache failure, calls per human turn, concurrency, and velocity.
3. Replay the August 22 dataset and tune thresholds.
4. Observe at least seven representative days before enabling default enforcement.

Exit gate: replay warns before 1M fresh tokens and would pause before the configured 5M human-turn/task allowance without misclassifying the website session as an autonomous loop.

### Milestone D — Enforce and control

1. Add approval, route-local, pause, and block actions.
2. Add hierarchical cancellation state and structured non-retryable responses.
3. Integrate xNaut process-group cancellation and acknowledgement.
4. Test concurrent reservation races and cancellation propagation.

Exit gate: a paused task cannot send another upstream request, while unrelated sessions continue.

### Milestone E — Explain

1. Ship account-to-tool-call drill-down.
2. Add cache lifecycle and rolling-window visualizations.
3. Generate deterministic incident narratives with confidence labels.
4. Validate the UI against the August 22 forensic report.

Exit gate: an operator can answer what consumed the plan, why it was fresh, which harness invocation caused it, and which control stopped it without querying PostgreSQL.

## Release gates

- No session collisions in concurrency tests.
- Cache-hit regression suite passes for Anthropic tool loops.
- Preflight budget reservation is concurrency-safe.
- Cancellation prevents subsequent upstream requests.
- Max traffic cannot bypass token budgets because monetary cost is zero.
- Existing API, local-model, ChatGPT subscription, and privacy-routing behavior remains covered.
- Observe-only replay of August 22 would have warned before 1M fresh tokens and paused before the configured 5M task allowance.

## Non-goals

- Claiming exact Anthropic remaining-plan percentages without provider-supported telemetry.
- Treating every long session or frequent tool use as malicious or broken.
- Replacing application-level agent lifecycle controls in xNaut.
- Depending on prompt caching as the only protection against runaway consumption.
