# NautGate Dashboard — v2 Redesign Handover

**Purpose:** everything a fresh chat needs to implement the approved v2 visual redesign of
the NautGate dashboard, end to end, without re-deriving the design. The design phase is
**done and signed off**; this is the build.

**Status:** design locked (8 Paper mockups + chart study). Implementation: **not started.**
**Scope:** frontend only — `core/app/static/{index.html, app.js, style.css}` + vendored uPlot
+ the Help module. **Do NOT touch** the Python gateway/routing/data logic. All data endpoints
already exist and stay the same.

---

## 0. Read first / gotchas

- **Branch:** `dashboard-auto-discovery`. It has **large uncommitted backend work** (cache
  accounting, credit-card FP fix, LLM-Probing, provider-status + 529-retry — see `git status`).
  The redesign is additive frontend. **Run `git status` and agree with Andre** whether to commit
  the backend features first or branch. Do **not** `git add -A` (it would sweep `.pi/` and
  `gitvm-extension/` — untracked tooling that must NOT be committed).
- **Open the dashboard on `localhost:8090/dashboard`, never `127.0.0.1`** — hard rule (per-origin
  localStorage holds the session tokens). `scripts/nautgate.sh` already opens localhost.
- **No browser harness in this CLI.** You verify by: `node --check app.js`, curl the dashboard
  HTML for structure, bounce uvicorn (`scripts/nautgate.sh restart`), then **hand visual
  verification to Andre** at localhost:8090/dashboard. The Paper mockups are the visual target.
- Commands: `just lint` (ruff), `just test`, `scripts/nautgate.sh restart|status|logs`.
- Andre is terse; English in code, German only for end-user UI copy (dashboard is operator-facing
  → English is fine). Bold reserved for active nav + one hero number per card.

---

## 1. The mockups (visual source of truth)

Paper file **"NautGate"**: https://app.paper.design/file/01KVA76D7XFQFKKYFWGVPF4WY1/1-0

Use the Paper MCP (`get_screenshot`, `get_jsx`, `get_computed_styles`) on these artboards to pull
exact spacing/values. Artboard names:

- `NautGate — Redesign (Cost)` — the system exemplar
- `NautGate — Redesign (Overview)`
- `NautGate — Redesign (Audit Log)`
- `NautGate — Redesign (LLM Probing)`
- `NautGate — Redesign (Drift)`
- `NautGate — Redesign (Scorecard)`
- `NautGate — Redesign (Cache)`
- `NautGate — Redesign (Quality)`
- `Chart styles — inline SVG vs tiny library`

---

## 2. Locked design decisions

**Mood:** control-room / instrument panel (dark slate, one amber "live" accent).

**Palette** — implement by changing the `:root` token VALUES in `style.css` (every page
references `var(--…)`, so this reskins the whole app instantly), then add the new tokens:

| token | current | NEW | role |
|---|---|---|---|
| `--bg` | #0b0e14 | **#0A0D12** | app ground |
| `--bg-card` | #11151c | **#12161F** | sidebar / cards |
| `--bg-raised` | (new) | **#1A2029** | hover / active row / chart track |
| `--border` | #1f2630 | **#232B36** | hairlines |
| `--text` | #d8e0ec | **#E6EBF2** | primary text |
| `--text-dim` | #7a8595 | **#8893A4** | muted text |
| `--label` | (new) | **#5C6675** | uppercase group labels (most muted) |
| `--accent` | #c2410c | **#E8833A** | THE amber accent (active nav, live, key numbers) |
| `--accent-bright` | (new) | **#F0A86A** | active-nav text / hero numbers |
| `--good` | #4caf50 | **#3FB950** | status up / healthy |
| `--warn` | #f59e0b | **#D6A100** | status degraded / watch |
| `--bad` | #ef4444 | **#E5484D** | status down / error / demoted |
| `--info` | (new) | **#4C8DFF** | info + chart series 2 |
| chart series | — | amber `#E8833A`, blue `#4C8DFF`, green `#3FB950`, violet `#9A6CE0` | multi-series |

**Type:** Inter (loaded) + mono (`--mono`, SF Mono / Roboto Mono) for IDs/hashes only.
Scale: 32 hero metric · 22 page title · 15 card value · 14 body · 12 uppercase-tracked label
(letter-spacing .05em) · 11 mono. Metrics use `font-variant-numeric: tabular-nums`, light/medium
weight next to small muted labels. **Kill gratuitous bold.**

**Shell:** fixed **left sidebar 236px** + main column (header + scrolling content). Replaces the
cramped top nav (14 tabs).

**Sidebar IA** (grouped, with the existing `data-tab` values):
- **Overview**
- **OBSERVE:** Audit Log (`audit`), Decisions (`decisions`), Provider Health (`health`)
- **QUALITY:** Scorecard (`scorecard`), Behavior (`behavior`), Drift (`drift`), Quality (`quality`), LLM Probing (`probe`)
- **SPEND:** Cost (`cost`), Cache (`cache`)
- **GOVERN:** Privacy (`privacy`)
- **footer:** Help & Ask (new, blue `--info` accent, "AI" chip), Settings (`settings`)

Active item = amber-tinted bg `rgba(232,131,58,0.13)` + `box-shadow: inset 2px 0 0 #E8833A`,
text `--accent-bright`, icon stroke `--accent`. Provider Health shows a live status dot; Drift
shows a red open-alert count badge.

**Header:** dynamic page title + one-line subtitle · **⌘K global search** · live provider-status
pill (from `/v1/health/providers`) · Help button · avatar. Some pages add a primary action
button (e.g. LLM Probing "Run probe now", Drift "Generate report").

**Component kit (build once, reuse everywhere):**
- **Card** — `bg --bg-card`, `1px solid --border`, `border-radius 12px`, `padding 18px 20px`.
- **Stat card** — uppercase `--text-dim` label → big tabular number → one delta line
  (`--good`/`--bad` ▲▼). The ONE highlighted card per page uses warm tint
  (`bg #17120C`, `border #3A2A18`, number `--accent-bright`).
- **DataTable** — toolbar (title + count + **filter-rows search** + **Columns** menu +
  **Table/Chart** segmented toggle); header row on `#0E121A` with uppercase `--label`,
  **sortable** (amber ▲ on active sort col); rows with consistent **fixed-width lanes**
  (`flex-shrink:0`, right-aligned tabular numbers), hairline `#1A2029` separators, flagged row =
  tinted bg + `inset 2px 0 0` accent rail. Optional inline **sparkline** column.
- **Badges/chips** — status badge (dot + label, up/degraded/down/no-data), verdict chips
  (Match/Divergent, Healthy/Watch/Demoted, Reused/Leaky, Open/Resolved), tier pills.

**Charts: uPlot** (~40KB, vendor it — no build step). Wrap in ONE helper
`chart(el, {type, series, data, opts})` so pages never touch uPlot directly. Chart types proven in
the mocks: area/line (multi-series), donut, horizontal bars, **ranked bars + threshold line**,
sparkline, stacked "anatomy" bar, **anomaly-band** (μ ±3σ shaded + observed + anomaly dots),
**heatmap** (model × bucket, green→red), comparison cells, score bars. Donut/heatmap/anatomy can
stay hand-rolled SVG/flex (cheap, static); uPlot is for the time-series/live ones (Cost spend,
Overview requests, Drift baseline, LLM-Probing fingerprint). Tooltip + crosshair come free.

**Help & Ask module:** `ssh://git@cosmos.tail138398.ts.net:2222/48Nauts/help-module.git`. Clone,
inspect its integration contract, wire it as a slide-in chat panel opened from the sidebar
"Help & Ask" entry + header help button. (Confirm the module's embed API with Andre.)

---

## 3. Per-page content (what each page holds — mockups have the layout)

- **Overview:** provider-status strip (Anthropic/OpenRouter/Codex badges) → 24h KPI cards
  (requests, empty rate, p50, p95) → requests-over-time area chart + by-tier horizontal bars →
  **paginated** sessions table (10 default, prev/next + page-size). Data: `/v1/stats`,
  `/v1/health/providers`, localStorage sessions.
- **Cost:** 4 stat cards (metered spend, **subscription saved** [highlighted], requests, $/call) →
  spend-over-time area (uPlot, 3 series) + cost-by-provider donut → "By model" DataTable
  (sortable, sparkline col). Data: `/v1/cost/summary`, `/v1/cost/timeseries`.
- **Cache:** KPI cards (hit rate, **$ saved** [highlighted], write:read, leaky count) →
  **prefix-reuse table** with leak detector (leaky row flagged red) + All/Leaky toggle.
  Optional: TTFT-by-prefix warmth + cache-off/on bars. Data: `/v1/cache/summary`, `/v1/cache/prefixes`.
- **Audit Log:** split view — live feed DataTable (time/agent/model/tier/tokens/lat/status, 529 &
  empty flagged, "Live" pill) **+ detail drawer** (token-anatomy stacked bar, TTFT/total/cache,
  full prompt/response, tool sequence, signals, quality/coach, 👎). Same data depth as today, just
  reorganized. Data: `/v1/decisions/recent`, `/v1/decisions/{id}`.
- **Scorecard:** KPI cards → **ranked score bar chart with the 0.30 demotion threshold line** →
  scores DataTable (inline score bars, tier pills, Healthy/Watch/Demoted). Data: `/v1/scorecard`.
- **Drift:** open-alert callout cards → **anomaly-band chart** (μ ±3σ + observed + red anomaly
  dots) → all-alerts DataTable (Open/Resolved). Data: `/v1/drift/*`.
- **LLM Probing:** slim config bar (enable/interval/targets/Run now) → KPI strip → **differential
  line chart** (subscription vs metered fingerprint, divergence flag) → **cross-path comparison
  table** (sub-vs-metered cells + Match/Divergent). Data: `/v1/probe/summary|history|config|run`.
- **Quality:** **failure heatmap** (model × complexity bucket 0–10, green→red) → failure-modes
  DataTable (over-thinking/off-task/looped/hallucination/partial). Data: `/v1/quality/*`.
- **Behavior** (already clean — light touch only), **Provider Health**, **Privacy**, **Decisions**,
  **Settings:** recombine the same kit; no new design needed.

---

## 4. Implementation plan (phased)

**Phase 1 — tokens + shell (do first, low risk).**
1. `style.css`: update `:root` token values per the table; add new tokens. Whole app reskins.
2. `index.html`: wrap body in `.app-shell` (flex row) = `<aside class="sidebar">` (grouped nav,
   brand top, Help/Settings footer) + `<div class="appmain">` (`<header class="appheader">` +
   `<div class="appcontent">` holding ALL existing `<section id="tab-X">` blocks **unchanged**).
   Remove the old top `<nav>`. New CSS for shell/sidebar/header/badges.
3. `app.js`: `activateTab()` keeps the `data-tab` mechanism + `refreshActive()`/load fns intact;
   add: drive sidebar active state, set the header page title/subtitle per tab (a small map),
   keep the existing per-tab `setInterval` polling. Wire ⌘K (focus search) + Help open stub.
   *Every page must still work after Phase 1 — only the chrome changed.*
4. Verify: `node --check app.js`, curl HTML shows all sections, bounce, Andre eyeballs.

**Phase 2 — component kit.** Extract `Card`, `DataTable` (search/sort/columns/view-toggle/lanes/
sparkline), badge/chip helpers, and the `chart()` uPlot wrapper (vendor uPlot js+css into
`static/`, reference in index.html). Build as small vanilla helpers consistent with the existing
`app.js` style (no framework, no bundler).

**Phase 3 — migrate pages onto the kit**, 2–3 at a time. Order: **LLM Probing, Cache** (clunkiest)
→ **Overview, Cost, Scorecard, Drift, Quality** (chart-hungry) → the rest. Each page's existing
`load*()` already fetches the data; reshape its render to emit `Card`/`DataTable`/`chart()`.

**Phase 4 — Help & Ask module** integration.

Verify after each phase; hand visual check to Andre.

---

## 5. Current frontend facts (so you don't rediscover)

- Vanilla, no framework/bundler. `index.html` = top `<nav>` (14 `<a data-tab>`) + `<main>` of
  `<section id="tab-X" class="tab">`. `app.js` ≈ 2.7k lines: `activateTab(name)` toggles `.active`,
  `refreshActive()` dispatches to `load<Tab>()`; `REFRESH_MS=5000` polling on audit/decisions
  (and a 60s `loadProviderStatus` on overview, just added). `style.css` `:root` holds the tokens.
- Existing tab `data-tab` values: `overview, audit, scorecard, drift, probe, behavior, cost,
  cache, quality, privacy, decisions, health, models, settings`.
- All redesigned-page data endpoints already exist (cost/cache/probe/drift/quality/health/
  decisions/stats/scorecard). The redesign does not add backend.
- Existing helpers in app.js to reuse: `api(path)`, `apiPut`, `usd()`, `pct()`, `ms()`, `esc()`,
  `fmtNum()`, `fmtAgo()`, session/token helpers, the cache `loadCache`/`renderProviderStatus`,
  the sessions pagination already added on Overview.

---

## 6. Definition of done

Every page renders under the new shell with the instrument-amber palette, one card style, one
searchable/sortable DataTable, uPlot charts where the mock shows time-series, the Help & Ask panel
opens, no console errors, `just lint` clean, and Andre signs off visually at
`localhost:8090/dashboard`. Don't claim visual success you can't see — bounce and hand off the
eyeball to Andre.
