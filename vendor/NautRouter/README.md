# NautRouter

**Smart LLM routing proxy for the 21nauts agent fleet.**

NautRouter sits between your agents and LLM providers. It reads each incoming prompt, scores its complexity across 14 dimensions, and routes it to the cheapest model capable of handling it — while giving you a real-time dashboard of every decision, latency, and dollar spent.

Forked scoring engine from ClawRouter v2 (14-dimension weighted classifier). Stripped of all x402/USDC/wallet/BlockRun payment code.

```
  Agent Request ──► NautRouter ──► Scoring Engine (14-dim)
                        │
                        ├─ SIMPLE ───► LM Studio (free, local)
                        ├─ MEDIUM ───► Gemini Flash ($0.15/M)
                        ├─ COMPLEX ──► Sonnet 4 ($3/M)
                        └─ REASONING ► Opus 4 ($15/M)
                        │
                        └──► Dashboard (real-time WebSocket)
```

---

## Features

- **14-dimension scoring engine** — classifies prompt complexity in <1ms, zero cost
- **3 routing profiles**: `eco` (local-first), `auto` (balanced), `premium` (quality-first)
- **3 providers**: Anthropic (Opus 4 / Sonnet 4 / Haiku 4.5), Google Gemini (2.5 Pro / Flash), LM Studio (local, free)
- **OpenAI-compatible** — drop-in replacement at `POST /v1/chat/completions`
- **Streaming support** — full SSE translation between Anthropic and OpenAI formats
- **Automatic fallback** — if primary model returns 5xx, tries the fallback chain
- **Real-time dashboard** — React + Vite frontend with live WebSocket feed
- **Cost tracking** — per-request cost calculation with savings vs. Opus baseline
- **Provider health** — tracks success rates, latency, and auto-degrades unhealthy providers

## Quick Start

```bash
# Clone
git clone git@github.com:48Nauts-Operator/NautRouter.git
cd NautRouter

# Configure
cp .env.example .env
# Add your API keys to .env

# Install & run backend
npm install
npm run dev          # → http://localhost:8402

# Install & run dashboard (separate terminal)
cd dashboard
npm install
npm run dev          # → http://localhost:5174
```

## Architecture

```
naut-router/
├── src/
│   └── index.ts              # Express server, scoring engine, provider forwarding
├── dashboard/
│   └── src/
│       ├── App.tsx            # Main layout
│       ├── components/
│       │   ├── Layout/        # Header, Sidebar, Shell
│       │   ├── NodeGraph/     # Visual routing pipeline (React Flow)
│       │   ├── RequestFeed/   # Live request stream
│       │   ├── ScoreBreakdown/# 14-dimension radar/bars
│       │   ├── StatsDashboard/# Cost, latency, savings charts
│       │   └── ProfileSelector/# eco/auto/premium toggle
│       ├── hooks/
│       │   ├── useWebSocket.ts   # Real-time event stream
│       │   ├── useNautRouter.ts  # API data fetching
│       │   └── useMockData.ts    # Demo mode (?mock)
│       ├── stores/            # Zustand state management
│       └── utils/
│           └── mockData.ts    # Synthetic data for demo mode
├── package.json
└── tsconfig.json
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/chat/completions` | Main routing endpoint (OpenAI-compatible) |
| `GET` | `/v1/models` | List available models (real + virtual) |
| `GET` | `/v1/profile` | Get current routing profile |
| `PUT` | `/v1/profile` | Change routing profile |
| `GET` | `/v1/providers` | Provider status, models, health |
| `GET` | `/v1/stats?range=24h` | Cost, latency, savings statistics |
| `GET` | `/health` | Health check |
| `WS` | `ws://localhost:8403` | Real-time event stream |

## Usage

```bash
# Auto-routed (NautRouter picks the best model)
curl http://localhost:8402/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: my-agent" \
  -d '{"model":"naut/auto","messages":[{"role":"user","content":"hello"}]}'

# Force a specific profile
curl http://localhost:8402/v1/chat/completions \
  -d '{"model":"naut/premium","messages":[{"role":"user","content":"prove P=NP"}]}'

# Direct model bypass (skip scoring)
curl http://localhost:8402/v1/chat/completions \
  -d '{"model":"claude-sonnet-4","messages":[{"role":"user","content":"explain kubernetes"}]}'
```

## Routing Profiles

Each profile maps complexity tiers to models differently:

| Tier | `eco` | `auto` | `premium` |
|------|-------|--------|-----------|
| SIMPLE | LM Studio | LM Studio | Haiku 4.5 |
| MEDIUM | LM Studio | Gemini Flash | Sonnet 4 |
| COMPLEX | Gemini Flash | Sonnet 4 | Opus 4 |
| REASONING | Gemini Flash | Sonnet 4 | Opus 4 |

## Scoring Dimensions

The classifier evaluates prompts across 14 weighted dimensions:

| # | Dimension | Weight | Signals |
|---|-----------|--------|---------|
| 1 | Reasoning markers | 18% | "prove", "theorem", "step by step" |
| 2 | Code presence | 15% | `function`, `class`, `import`, code fences |
| 3 | Multi-step patterns | 12% | "first...then", "step 1", numbered lists |
| 4 | Technical terms | 10% | "kubernetes", "algorithm", "distributed" |
| 5 | Token count | 8% | Short (<50) vs long (>500) prompts |
| 6 | Simple indicators | 6% | "what is", "hello", "define" (pushes score down) |
| 7 | Creative markers | 5% | "story", "poem", "brainstorm" |
| 8 | Question complexity | 5% | Number of `?` marks in prompt |
| 9 | Constraint indicators | 4% | "at most", "O(n)", "budget", "limit" |
| 10 | Imperative verbs | 3% | "build", "create", "implement", "deploy" |
| 11 | Output format | 3% | "json", "yaml", "csv", "markdown" |
| 12 | Reference complexity | 2% | "above", "the docs", "previous", "attached" |
| 13 | Domain specificity | 2% | "quantum", "fpga", "genomics", "zero-knowledge" |
| 14 | Negation complexity | 1% | "don't", "avoid", "never", "except" |

Weighted score maps to tiers: `<0.0` SIMPLE, `0.0–0.3` MEDIUM, `0.3–0.5` COMPLEX, `>0.5` REASONING.

## Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `ANTHROPIC_API_KEY` | — | Yes | Anthropic API key |
| `GEMINI_API_KEY` | — | Yes | Google Gemini API key |
| `LMSTUDIO_URL` | `http://localhost:1238` | No | LM Studio endpoint |
| `MEMORY_API` | `http://100.71.163.122:8085/memories` | No | Cost logging endpoint (Stargate) |
| `NAUT_PORT` | `8402` | No | HTTP server port |
| `NAUT_WS_PORT` | `8403` | No | WebSocket server port |
| `NAUT_PROFILE` | `auto` | No | Default routing profile |

## Dashboard

The real-time dashboard connects via WebSocket and shows:

- **Node Graph** — visual routing pipeline from request → scoring → provider → response
- **Request Feed** — live stream of incoming requests with tier, model, cost
- **Score Breakdown** — 14-dimension scores for the selected request
- **Stats Dashboard** — cost trends, latency, savings vs. Opus baseline
- **Profile Selector** — switch routing profiles on the fly

Visit `http://localhost:5174/?mock` for a demo with synthetic data.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Express 5, TypeScript, tsx |
| WebSocket | ws |
| Frontend | React 19, Vite 7, Tailwind CSS 4 |
| State | Zustand |
| Visualization | Recharts, React Flow |
| Build | tsup (backend), Vite (frontend) |

## License

Private — 48Nauts / 21nauts internal use.
