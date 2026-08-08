# Changelog

All notable changes to NautGate are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**How entries are written here.** NautGate's product claim is *evidence* — an
attested record of what every call did and cost. A changelog that says "fixed
pricing" while the numbers on your dashboard silently move is corrosive to
exactly that claim. So an entry states the **symptom** in user-visible terms,
the **mechanism** that caused it, **who it affected**, and — when a figure the
product reports changes — **what it read before and after**. If a fix was a
regression we introduced, the entry says so.

## [Unreleased]

### Added

- **Model catalogue — 442 selectable models, up from ~8.** The Settings → Keys
  picker only ever listed the models named in `config/routing.yaml`'s tiers,
  while OpenRouter, Anthropic and OpenAI between them serve hundreds. A
  hardcoded list would need hand-editing every time a provider ships a model, so
  NautGate now fetches each provider's *own* catalogue on a 24-hour timer.
  OpenRouter's catalogue is public, so it populates even on a fresh install with
  no keys configured. Measured on first run: openrouter 354, openai 75,
  anthropic 10, lmstudio 3.

  The scheduler owns fetching and `/v1/models` does **no** network I/O, so the
  endpoint stays fast (~70 ms) and deterministic; it falls back to the local LM
  Studio probe until the first refresh lands, so a just-booted gateway is never
  empty. Non-chat models are filtered out — an embedding or image model in a
  chat picker is a broken choice. Two deliberate carve-outs: `vision` models are
  **kept** (they do answer chat completions), and the `-instruct` exclusion
  applies to OpenAI only, because on OpenRouter "instruct" denotes an ordinary
  chat model — 22 llama/mistral entries are correctly retained ([#38]).

- **Compliance audit layer** — a per-call compliance trace: what activity a call
  performed, which provider and jurisdiction actually served it, and which rules
  it touched. Records labels and flags; it never gates a call. Declaring a
  jurisdiction scope changes which flags *raise*, never what gets *recorded*
  (NAUTGATE-25, [#36]).

- **nautproxy — universal capture** for clients that ignore `OPENAI_BASE_URL`
  (today: Codex in ChatGPT-OAuth mode, which pins traffic to chatgpt.com but
  honours `HTTPS_PROXY` plus a trusted CA). Ships as an opt-in compose profile
  and tees observed turns into `/v1/ingest`, the same path inline traffic takes
  (NAUTGATE-22, [#33]).

- **Local-model tool-call normalization** for the Anthropic Messages bridge —
  opt-in and **off by default**, for driving Claude Code against a local model
  (NAUTGATE-24, [#34]).

### Fixed

- **Long generations died at exactly 120 s with `502 upstream_failed`.** The
  upstream read timeout was hard-coded to `120.0`; requests failed at 120071 ms
  and 120066 ms — our own deadline, not a provider outage. What made it hard to
  see: the log line read `error: ""`, because `str(httpx.ReadTimeout())` is the
  empty string, so the record showed a failure with no reason and pointed
  suspicion upstream. The timeout is now configurable with a single source of
  truth in settings, and the log records `error_type` so an exception with an
  empty message is still identifiable ([#37]).

- **Every CSS rule after line 2440 was silently dead.** An unclosed
  `@media (max-width: 640px) {` at `core/app/static/style.css:2437` swallowed
  the remainder of the stylesheet, so a run of later rules — `.app-version`
  among them — never applied. CSS fails silently: no console error, the
  stylesheet still returns 200, and the page renders *almost* right. It
  presented as a stale-cache problem and was misdiagnosed as one before the real
  cause was found. Two related gaps were closed at the same time: `index.html`
  keyed its asset `?v=` cache-buster on its own mtime, so editing only the CSS
  served a stale query string, and `/static` now sends `Cache-Control: no-cache`
  (`7f7eb5a`).

- **Captured turns only appeared after a session ended.** The nautproxy addon
  hooked mitmproxy's `websocket_end`, but Codex holds a single long-lived
  WebSocket for an entire session — so nothing surfaced in the dashboard until
  the user quit, at which point a whole session arrived at once and looked like
  the gateway was lagging. Frames are now folded in `websocket_message` and a
  turn is emitted the moment its `response.completed` arrives (`11c5e95`).

- **nautproxy could not deliver a single captured turn**, for three independent
  reasons: the default ingest URL was the Docker service name, which a proxy
  running on the host can never resolve; `urllib` inherited the `HTTP(S)_PROXY`
  the client had set, so the POST to NautGate was routed back through the very
  proxy that produced it; and delivery ran via `asyncio.to_thread`, which at
  shutdown hit mitmproxy's already-torn-down executor. Delivery now bypasses any
  inherited proxy explicitly and runs on a daemon thread (`dfd56a2`).

- **Compliance traces recorded the routing lane instead of the provider that
  actually served the call** (`passthrough` rather than, say, `anthropic`).
  Because provider identity is what jurisdiction rules key on, the
  third-country-transfer flag silently never fired — the trace looked complete
  and was wrong. Found only by running real traffic through it (`ba4ecb6`).

- Sidebar collapse moved to a top chevron, the duplicated left-hand Help entry
  removed now that help lives in the right pane, and Settings grouped under a
  new Admin heading (NAUTGATE-21, [#35]).

## [0.1.0] — 2026-07-23

First public release. NautGate sits between your coding agents and every model
provider: one OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible
(`/v1/messages`) endpoint that routes each call, records what happened, and
reports what it cost.

### Added

- **Two credential lanes** — OAuth subscription traffic passes through untouched
  and unmetered; metered keys get routed. Per-key model override pins any model
  to a key, so Claude Code can be pointed at a different model without touching
  the client.
- **Attested audit log** — the served model is read from the provider's own
  response, never echoed from the request, so silent substitution is detectable
  rather than assumed. Policy-gated prompt/response capture, token counts,
  timings and cost per call.
- **Sensitivity classification** (PII/secrets) gating body capture.
- **Self-contained Docker image** and a published stack on GHCR, with a
  one-command install (NAUTGATE-5, [#2]).
- **In-app provider key management**, encrypted at rest (NAUTGATE-8, [#4]).
- **First-run onboarding** — mint your first API key from the dashboard
  (NAUTGATE-6, [#3]); the master key is generated on first boot so provider keys
  need no manual configuration.
- **Go-live smoke test** — fresh install plus all-functions flow (NAUTGATE-9,
  [#5]).
- Local models via LM Studio, addressable as `lmstudio/<id>`.

[#2]: https://github.com/48Nauts-Operator/NautGate/pull/2
[#3]: https://github.com/48Nauts-Operator/NautGate/pull/3
[#4]: https://github.com/48Nauts-Operator/NautGate/pull/4
[#5]: https://github.com/48Nauts-Operator/NautGate/pull/5
[#33]: https://github.com/48Nauts-Operator/NautGate/pull/33
[#34]: https://github.com/48Nauts-Operator/NautGate/pull/34
[#35]: https://github.com/48Nauts-Operator/NautGate/pull/35
[#36]: https://github.com/48Nauts-Operator/NautGate/pull/36
[#37]: https://github.com/48Nauts-Operator/NautGate/pull/37
[#38]: https://github.com/48Nauts-Operator/NautGate/pull/38
