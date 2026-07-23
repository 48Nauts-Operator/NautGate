/**
 * NautRouter — Smart LLM Routing Proxy for 21nauts fleet
 *
 * Forked scoring engine from ClawRouter (14-dimension weighted classifier).
 * Stripped all x402/USDC/wallet/BlockRun code. Routes to our own providers:
 *   - Anthropic (Opus 4, Sonnet 4, Haiku 4.5) via API key
 *   - LM Studio local (free inference)
 *   - Google Gemini via API key
 *
 * OpenAI-compatible: accepts POST /v1/chat/completions
 * Cost logging: POSTs to Memory API on Stargate
 *
 * Profiles: eco (local first), auto (balanced), premium (Opus)
 */

import express from "express";
import cors from "cors";
import { createServer } from "node:http";
import { WebSocketServer, WebSocket } from "ws";

// ── Config from env ──
const PORT = parseInt(process.env.NAUT_PORT ?? "8402", 10);
const WS_PORT = parseInt(process.env.NAUT_WS_PORT ?? "8403", 10);
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY ?? "";
const GEMINI_API_KEY = process.env.GEMINI_API_KEY ?? "";
const OPENAI_API_KEY = process.env.OPENAI_API_KEY ?? "";
const OPENROUTER_API_KEY = process.env.OPENROUTER_API_KEY ?? "";
const LMSTUDIO_URL = process.env.LMSTUDIO_URL ?? "http://localhost:1238";
const MEMORY_API = process.env.MEMORY_API ?? "http://100.71.163.122:8085/memories";
const DEFAULT_PROFILE = (process.env.NAUT_PROFILE ?? "auto") as Profile;

// ── Types ──
type Tier = "SIMPLE" | "MEDIUM" | "COMPLEX" | "REASONING";
type Profile = "eco" | "auto" | "premium";

type ScoringResult = {
  score: number;
  tier: Tier | null;
  confidence: number;
  signals: string[];
};

type RoutingDecision = {
  model: string;
  provider: "anthropic" | "lmstudio" | "gemini";
  tier: Tier;
  confidence: number;
  reasoning: string;
  costPer1MInput: number;
  costPer1MOutput: number;
};

// ── Provider model definitions ──
type ModelDef = {
  id: string; // what we send to the provider
  provider: "anthropic" | "lmstudio" | "gemini";
  inputPrice: number; // per 1M tokens
  outputPrice: number;
  contextWindow: number;
};

const MODELS: Record<string, ModelDef> = {
  // Anthropic
  "claude-opus-4": {
    id: "claude-opus-4-7",
    provider: "anthropic",
    inputPrice: 15,
    outputPrice: 75,
    contextWindow: 200_000,
  },
  "claude-sonnet-4": {
    id: "claude-sonnet-4-6",
    provider: "anthropic",
    inputPrice: 3,
    outputPrice: 15,
    contextWindow: 200_000,
  },
  "claude-haiku-4.5": {
    id: "claude-haiku-4-5",
    provider: "anthropic",
    inputPrice: 1,
    outputPrice: 5,
    contextWindow: 200_000,
  },
  // Google Gemini
  "gemini-2.5-flash": {
    id: "gemini-2.5-flash",
    provider: "gemini",
    inputPrice: 0.15,
    outputPrice: 0.6,
    contextWindow: 1_000_000,
  },
  "gemini-2.5-pro": {
    id: "gemini-2.5-pro",
    provider: "gemini",
    inputPrice: 1.25,
    outputPrice: 10,
    contextWindow: 1_000_000,
  },
  // OpenRouter — meta-provider with hundreds of models. `openrouter/auto`
  // is OpenRouter's own auto-selector; it picks based on prompt + price.
  "openrouter/auto": {
    id: "openrouter/auto",
    provider: "openrouter",
    inputPrice: 0,   // varies — actual model recorded in response.model
    outputPrice: 0,
    contextWindow: 200_000,
  },
  "openrouter/anthropic/claude-haiku": {
    id: "anthropic/claude-haiku-4.5",
    provider: "openrouter",
    inputPrice: 1,
    outputPrice: 5,
    contextWindow: 200_000,
  },
  "openrouter/google/gemini-flash": {
    id: "google/gemini-2.5-flash",
    provider: "openrouter",
    inputPrice: 0.15,
    outputPrice: 0.60,
    contextWindow: 1_000_000,
  },
  "openrouter/google/gemini-pro": {
    id: "google/gemini-2.5-pro",
    provider: "openrouter",
    inputPrice: 1.25,
    outputPrice: 5.00,
    contextWindow: 1_000_000,
  },
  "openrouter/anthropic/claude-sonnet": {
    id: "anthropic/claude-sonnet-4",
    provider: "openrouter",
    inputPrice: 3,
    outputPrice: 15,
    contextWindow: 200_000,
  },
  "openrouter/deepseek/deepseek-chat": {
    id: "deepseek/deepseek-chat",
    provider: "openrouter",
    inputPrice: 0.32,
    outputPrice: 0.89,
    contextWindow: 163_840,
  },
  "openrouter/deepseek/deepseek-v4-flash": {
    id: "deepseek/deepseek-v4-flash",
    provider: "openrouter",
    inputPrice: 0.14,
    outputPrice: 0.28,
    contextWindow: 1_048_576,
  },
  "openrouter/deepseek/deepseek-v4-pro": {
    id: "deepseek/deepseek-v4-pro",
    provider: "openrouter",
    inputPrice: 0.43,
    outputPrice: 0.87,
    contextWindow: 1_048_576,
  },
  "openrouter/moonshotai/kimi-k2-thinking": {
    id: "moonshotai/kimi-k2-thinking",
    provider: "openrouter",
    inputPrice: 0.60,
    outputPrice: 2.50,
    contextWindow: 262_144,
  },
  "openrouter/moonshotai/kimi-k2.6": {
    id: "moonshotai/kimi-k2.6",
    provider: "openrouter",
    inputPrice: 0.75,
    outputPrice: 3.50,
    contextWindow: 262_144,
  },
  "openrouter/openai/gpt-4o-mini": {
    id: "openai/gpt-4o-mini",
    provider: "openrouter",
    inputPrice: 0.15,
    outputPrice: 0.60,
    contextWindow: 128_000,
  },
  "openrouter/meta-llama/llama-3.3-70b": {
    id: "meta-llama/llama-3.3-70b-instruct",
    provider: "openrouter",
    inputPrice: 0.13,
    outputPrice: 0.40,
    contextWindow: 128_000,
  },
  "openrouter/qwen/qwen-2.5-72b": {
    id: "qwen/qwen-2.5-72b-instruct",
    provider: "openrouter",
    inputPrice: 0.13,
    outputPrice: 0.40,
    contextWindow: 32_000,
  },
  // LM Studio local (free)
  "local": {
    id: "alibaba-nlp.tongyi-deepresearch-30b-a3b",
    provider: "lmstudio",
    inputPrice: 0,
    outputPrice: 0,
    contextWindow: 32_000,
  },
};

// ── WebSocket event types ──
type WebSocketEventType = "request_received" | "scoring_complete" | "model_selected" | "response_complete" | "error";

type WebSocketEvent = {
  type: WebSocketEventType;
  request_id: string;
  timestamp: string;
  data: Record<string, unknown>;
};

// ── Request history for stats ──
type RequestRecord = {
  id: string;
  timestamp: Date;
  agent_id: string;
  profile: Profile;
  tier: Tier;
  provider: string;
  model: string;
  latency_ms: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  success: boolean;
};

const requestHistory: RequestRecord[] = [];
const MAX_HISTORY = 10_000;

function addRequestRecord(record: RequestRecord) {
  requestHistory.push(record);
  if (requestHistory.length > MAX_HISTORY) {
    requestHistory.splice(0, requestHistory.length - MAX_HISTORY);
  }
}

// ── Provider health tracking ──
type ProviderHealth = {
  status: "online" | "offline" | "degraded";
  lastCheck: Date;
  totalRequests: number;
  totalCost: number;
  totalLatency: number;
  successCount: number;
  failCount: number;
};

const providerHealth: Record<string, ProviderHealth> = {
  anthropic: { status: "online", lastCheck: new Date(), totalRequests: 0, totalCost: 0, totalLatency: 0, successCount: 0, failCount: 0 },
  lmstudio: { status: "online", lastCheck: new Date(), totalRequests: 0, totalCost: 0, totalLatency: 0, successCount: 0, failCount: 0 },
  gemini: { status: "online", lastCheck: new Date(), totalRequests: 0, totalCost: 0, totalLatency: 0, successCount: 0, failCount: 0 },
};

function updateProviderHealth(provider: string, success: boolean, latency: number, cost: number) {
  const h = providerHealth[provider];
  if (!h) return;
  h.totalRequests++;
  h.totalLatency += latency;
  h.totalCost += cost;
  h.lastCheck = new Date();
  if (success) {
    h.successCount++;
    h.status = "online";
  } else {
    h.failCount++;
    if (h.failCount > 3 && h.failCount > h.successCount * 0.3) h.status = "degraded";
    if (h.failCount > 10 && h.successCount === 0) h.status = "offline";
  }
}

// ── Profile state (mutable at runtime) ──
let currentProfile: Profile = DEFAULT_PROFILE;

// ── Request ID generator ──
let requestCounter = 0;
function generateRequestId(): string {
  return `req_${Date.now().toString(36)}_${(++requestCounter).toString(36)}`;
}

// ── Tier → Model mappings per profile ──
type TierConfig = { primary: string; fallback: string[] };

const TIER_CONFIGS: Record<Profile, Record<Tier, TierConfig>> = {
  eco: {
    SIMPLE:    { primary: "local", fallback: ["gemini-2.5-flash", "claude-haiku-4.5"] },
    MEDIUM:    { primary: "local", fallback: ["gemini-2.5-flash", "claude-haiku-4.5"] },
    COMPLEX:   { primary: "gemini-2.5-flash", fallback: ["local", "claude-sonnet-4"] },
    REASONING: { primary: "gemini-2.5-flash", fallback: ["claude-sonnet-4"] },
  },
  auto: {
    SIMPLE:    { primary: "local", fallback: ["gemini-2.5-flash", "claude-haiku-4.5"] },
    MEDIUM:    { primary: "gemini-2.5-flash", fallback: ["local", "claude-haiku-4.5"] },
    COMPLEX:   { primary: "claude-sonnet-4", fallback: ["gemini-2.5-pro", "gemini-2.5-flash"] },
    REASONING: { primary: "claude-sonnet-4", fallback: ["claude-opus-4", "gemini-2.5-pro"] },
  },
  premium: {
    SIMPLE:    { primary: "claude-haiku-4.5", fallback: ["gemini-2.5-flash", "local"] },
    MEDIUM:    { primary: "claude-sonnet-4", fallback: ["gemini-2.5-flash"] },
    COMPLEX:   { primary: "claude-opus-4", fallback: ["claude-sonnet-4", "gemini-2.5-pro"] },
    REASONING: { primary: "claude-opus-4", fallback: ["claude-sonnet-4"] },
  },
};

// ══════════════════════════════════════════════════════════════
// 14-DIMENSION SCORING ENGINE (extracted from ClawRouter v2)
// ══════════════════════════════════════════════════════════════

const SCORING_CONFIG = {
  tokenCountThresholds: { simple: 50, complex: 500 },

  codeKeywords: ["function", "class", "import", "def", "SELECT", "async", "await", "const", "let", "var", "return", "```"],
  reasoningKeywords: ["prove", "theorem", "derive", "step by step", "chain of thought", "formally", "mathematical", "proof", "logically"],
  simpleKeywords: ["what is", "define", "translate", "hello", "yes or no", "capital of", "how old", "who is", "when was"],
  technicalKeywords: ["algorithm", "optimize", "architecture", "distributed", "kubernetes", "microservice", "database", "infrastructure"],
  creativeKeywords: ["story", "poem", "compose", "brainstorm", "creative", "imagine", "write a"],
  imperativeVerbs: ["build", "create", "implement", "design", "develop", "construct", "generate", "deploy", "configure", "set up"],
  constraintIndicators: ["under", "at most", "at least", "within", "no more than", "o(", "maximum", "minimum", "limit", "budget"],
  outputFormatKeywords: ["json", "yaml", "xml", "table", "csv", "markdown", "schema", "format as", "structured"],
  referenceKeywords: ["above", "below", "previous", "following", "the docs", "the api", "the code", "earlier", "attached"],
  negationKeywords: ["don't", "do not", "avoid", "never", "without", "except", "exclude", "no longer"],
  domainSpecificKeywords: ["quantum", "fpga", "vlsi", "risc-v", "asic", "photonics", "genomics", "proteomics", "topological", "homomorphic", "zero-knowledge"],

  dimensionWeights: {
    tokenCount: 0.08,
    codePresence: 0.15,
    reasoningMarkers: 0.18,
    technicalTerms: 0.10,
    creativeMarkers: 0.05,
    simpleIndicators: 0.06,
    multiStepPatterns: 0.12,
    questionComplexity: 0.05,
    imperativeVerbs: 0.03,
    constraintCount: 0.04,
    outputFormat: 0.03,
    referenceComplexity: 0.02,
    negationComplexity: 0.01,
    domainSpecificity: 0.02,
  } as Record<string, number>,

  tierBoundaries: { simpleMedium: 0.0, mediumComplex: 0.3, complexReasoning: 0.5 },
  confidenceSteepness: 12,
  confidenceThreshold: 0.7,
};

type DimScore = { name: string; score: number; signal: string | null };

function scoreKeywords(text: string, keywords: string[], name: string, label: string,
  thresholds: { low: number; high: number }, scores: { none: number; low: number; high: number }): DimScore {
  const matches = keywords.filter(kw => text.includes(kw.toLowerCase()));
  if (matches.length >= thresholds.high) return { name, score: scores.high, signal: `${label} (${matches.slice(0, 3).join(", ")})` };
  if (matches.length >= thresholds.low) return { name, score: scores.low, signal: `${label} (${matches.slice(0, 3).join(", ")})` };
  return { name, score: scores.none, signal: null };
}

function classifyByRules(prompt: string, systemPrompt: string | undefined, estimatedTokens: number): ScoringResult {
  const text = `${systemPrompt ?? ""} ${prompt}`.toLowerCase();
  const userText = prompt.toLowerCase();
  const cfg = SCORING_CONFIG;

  const dimensions: DimScore[] = [
    // 1. Token count
    estimatedTokens < cfg.tokenCountThresholds.simple
      ? { name: "tokenCount", score: -1.0, signal: `short (${estimatedTokens} tokens)` }
      : estimatedTokens > cfg.tokenCountThresholds.complex
        ? { name: "tokenCount", score: 1.0, signal: `long (${estimatedTokens} tokens)` }
        : { name: "tokenCount", score: 0, signal: null },
    // 2-6. Keyword dimensions
    scoreKeywords(text, cfg.codeKeywords, "codePresence", "code", { low: 1, high: 2 }, { none: 0, low: 0.5, high: 1.0 }),
    scoreKeywords(userText, cfg.reasoningKeywords, "reasoningMarkers", "reasoning", { low: 1, high: 2 }, { none: 0, low: 0.7, high: 1.0 }),
    scoreKeywords(text, cfg.technicalKeywords, "technicalTerms", "technical", { low: 2, high: 4 }, { none: 0, low: 0.5, high: 1.0 }),
    scoreKeywords(text, cfg.creativeKeywords, "creativeMarkers", "creative", { low: 1, high: 2 }, { none: 0, low: 0.5, high: 0.7 }),
    scoreKeywords(text, cfg.simpleKeywords, "simpleIndicators", "simple", { low: 1, high: 2 }, { none: 0, low: -1.0, high: -1.0 }),
    // 7. Multi-step patterns
    (() => {
      const patterns = [/first.*then/i, /step \d/i, /\d\.\s/];
      return patterns.some(p => p.test(text))
        ? { name: "multiStepPatterns", score: 0.5, signal: "multi-step" } as DimScore
        : { name: "multiStepPatterns", score: 0, signal: null } as DimScore;
    })(),
    // 8. Question complexity
    (() => {
      const count = (prompt.match(/\?/g) || []).length;
      return count > 3
        ? { name: "questionComplexity", score: 0.5, signal: `${count} questions` } as DimScore
        : { name: "questionComplexity", score: 0, signal: null } as DimScore;
    })(),
    // 9-14. New dimensions
    scoreKeywords(text, cfg.imperativeVerbs, "imperativeVerbs", "imperative", { low: 1, high: 2 }, { none: 0, low: 0.3, high: 0.5 }),
    scoreKeywords(text, cfg.constraintIndicators, "constraintCount", "constraints", { low: 1, high: 3 }, { none: 0, low: 0.3, high: 0.7 }),
    scoreKeywords(text, cfg.outputFormatKeywords, "outputFormat", "format", { low: 1, high: 2 }, { none: 0, low: 0.4, high: 0.7 }),
    scoreKeywords(text, cfg.referenceKeywords, "referenceComplexity", "references", { low: 1, high: 2 }, { none: 0, low: 0.3, high: 0.5 }),
    scoreKeywords(text, cfg.negationKeywords, "negationComplexity", "negation", { low: 2, high: 3 }, { none: 0, low: 0.3, high: 0.5 }),
    scoreKeywords(text, cfg.domainSpecificKeywords, "domainSpecificity", "domain-specific", { low: 1, high: 2 }, { none: 0, low: 0.5, high: 0.8 }),
  ];

  const signals = dimensions.filter(d => d.signal).map(d => d.signal!);

  // Weighted score
  let weightedScore = 0;
  for (const d of dimensions) {
    weightedScore += d.score * (cfg.dimensionWeights[d.name] ?? 0);
  }

  // Direct reasoning override
  const reasoningMatches = cfg.reasoningKeywords.filter(kw => userText.includes(kw.toLowerCase()));
  if (reasoningMatches.length >= 2) {
    const confidence = 1 / (1 + Math.exp(-cfg.confidenceSteepness * Math.max(weightedScore, 0.3)));
    return { score: weightedScore, tier: "REASONING", confidence: Math.max(confidence, 0.85), signals };
  }

  // Map to tier
  const { simpleMedium, mediumComplex, complexReasoning } = cfg.tierBoundaries;
  let tier: Tier;
  let dist: number;

  if (weightedScore < simpleMedium) { tier = "SIMPLE"; dist = simpleMedium - weightedScore; }
  else if (weightedScore < mediumComplex) { tier = "MEDIUM"; dist = Math.min(weightedScore - simpleMedium, mediumComplex - weightedScore); }
  else if (weightedScore < complexReasoning) { tier = "COMPLEX"; dist = Math.min(weightedScore - mediumComplex, complexReasoning - weightedScore); }
  else { tier = "REASONING"; dist = weightedScore - complexReasoning; }

  const confidence = 1 / (1 + Math.exp(-cfg.confidenceSteepness * dist));
  if (confidence < cfg.confidenceThreshold) {
    return { score: weightedScore, tier: null, confidence, signals };
  }
  return { score: weightedScore, tier, confidence, signals };
}

// ══════════════════════════════════════════════════════════════
// ROUTING
// ══════════════════════════════════════════════════════════════

function routeRequest(prompt: string, systemPrompt: string | undefined, profile: Profile): RoutingDecision {
  const fullText = `${systemPrompt ?? ""} ${prompt}`;
  const estimatedTokens = Math.ceil(fullText.length / 4);

  // Override: very large context → COMPLEX
  if (estimatedTokens > 100_000) {
    const tc = TIER_CONFIGS[profile]["COMPLEX"];
    const m = MODELS[tc.primary]!;
    return { model: tc.primary, provider: m.provider, tier: "COMPLEX", confidence: 0.95, reasoning: "large context override", costPer1MInput: m.inputPrice, costPer1MOutput: m.outputPrice };
  }

  const result = classifyByRules(prompt, systemPrompt, estimatedTokens);
  const tier: Tier = result.tier ?? "MEDIUM";
  const tc = TIER_CONFIGS[profile][tier];
  const modelKey = tc.primary;
  const m = MODELS[modelKey]!;
  const reasoning = `score=${result.score.toFixed(2)} | ${result.signals.join(", ")} | profile=${profile}`;

  return { model: modelKey, provider: m.provider, tier, confidence: result.confidence, reasoning, costPer1MInput: m.inputPrice, costPer1MOutput: m.outputPrice };
}

function extractDimensionScores(prompt: string, systemPrompt: string | undefined, estimatedTokens: number): Record<string, number> {
  const text = `${systemPrompt ?? ""} ${prompt}`.toLowerCase();
  const userText = prompt.toLowerCase();
  const cfg = SCORING_CONFIG;

  const countMatches = (keywords: string[], source: string) => keywords.filter(kw => source.includes(kw.toLowerCase())).length;

  const normalize = (count: number, low: number, high: number) => {
    if (count >= high) return 1.0;
    if (count >= low) return 0.5;
    return 0;
  };

  return {
    code_keywords: normalize(countMatches(cfg.codeKeywords, text), 1, 2),
    reasoning_markers: normalize(countMatches(cfg.reasoningKeywords, userText), 1, 2),
    creative_markers: normalize(countMatches(cfg.creativeKeywords, text), 1, 2),
    analysis_markers: normalize(countMatches(cfg.technicalKeywords, text), 2, 4),
    math_markers: normalize(countMatches(cfg.constraintIndicators, text), 1, 3),
    length_score: estimatedTokens > cfg.tokenCountThresholds.complex ? 1.0 : estimatedTokens > cfg.tokenCountThresholds.simple ? 0.5 : 0,
    question_complexity: Math.min(1, (prompt.match(/\?/g) || []).length / 5),
    context_depth: normalize(countMatches(cfg.referenceKeywords, text), 1, 2),
    instruction_complexity: normalize(countMatches(cfg.imperativeVerbs, text), 1, 2),
    multi_step: [/first.*then/i, /step \d/i, /\d\.\s/].some(p => p.test(text)) ? 0.7 : 0,
    domain_specificity: normalize(countMatches(cfg.domainSpecificKeywords, text), 1, 2),
    ambiguity: normalize(countMatches(cfg.negationKeywords, text), 2, 3),
    formatting_complexity: normalize(countMatches(cfg.outputFormatKeywords, text), 1, 2),
    overall_confidence: 0,
  };
}

// ══════════════════════════════════════════════════════════════
// PROVIDER FORWARDING
// ══════════════════════════════════════════════════════════════

// Resolve a model key to a ModelDef. Known keys come from the MODELS table;
// any unknown `openrouter/<vendor>/<slug>` is passed straight through to
// OpenRouter (it has hundreds of models — we don't mirror the catalogue).
// Prices unknown for passthrough models → 0; actual cost comes from the
// upstream usage in the response. ponytail: passthrough only for openrouter/*,
// add other providers' passthrough when a caller actually needs one.
function resolveModel(modelKey: string): ModelDef | undefined {
  const known = MODELS[modelKey];
  if (known) return known;
  if (modelKey.startsWith("openrouter/")) {
    return {
      id: modelKey.slice("openrouter/".length),
      provider: "openrouter" as ModelDef["provider"],
      inputPrice: 0,
      outputPrice: 0,
      contextWindow: 0,
    };
  }
  // LM Studio passthrough: "lmstudio/<id>" forwards <id> verbatim to the local
  // LM Studio server. Without this, only the single hardcoded "local" model was
  // reachable, so you could not pick which local model to run. The id keeps its
  // own namespace ("lmstudio/qwen/qwen3.6-35b-a3b" -> "qwen/qwen3.6-35b-a3b").
  // Context window 0 = unknown; local models vary and NautGate does not price them.
  if (modelKey.startsWith("lmstudio/")) {
    return {
      id: modelKey.slice("lmstudio/".length),
      provider: "lmstudio" as ModelDef["provider"],
      inputPrice: 0,
      outputPrice: 0,
      contextWindow: 0,
    };
  }
  // Anthropic passthrough: clients send dashed snapshot ids (claude-opus-4-8,
  // claude-haiku-4-5-20251001, claude-fable-5) that the curated MODELS map
  // doesn't list. Silently routing those to the DEFAULT provider served
  // "Claude" requests with Gemini — forward unknown claude-* ids verbatim
  // to Anthropic instead. Pricing 0 = unknown; NautGate prices by its own table.
  if (modelKey.startsWith("claude-") || modelKey.startsWith("anthropic/claude-")) {
    return {
      id: modelKey.replace(/^anthropic\//, ""),
      provider: "anthropic" as ModelDef["provider"],
      inputPrice: 0,
      outputPrice: 0,
      contextWindow: 200_000,
    };
  }
  // OpenAI passthrough — same fix as claude-* above. Unknown OpenAI-family ids
  // (gpt-5.6-sol, o3-*, chatgpt-*) were falling into auto-routing and getting
  // silently served by Gemini. Forward them verbatim to OpenAI, which
  // NautRouter already has a direct forwarder + key for (forwardOpenAI).
  if (/^(gpt-|o1-|o3-|o4-|chatgpt-)/.test(modelKey) || modelKey.startsWith("openai/")) {
    return {
      id: modelKey.replace(/^openai\//, ""),
      provider: "openai" as ModelDef["provider"],
      inputPrice: 0,
      outputPrice: 0,
      contextWindow: 128_000,
    };
  }
  return undefined;
}

// Per-request provider keys sent by core (NAUTGATE-8): decrypted db-stored keys
// that override this process's env for that provider. Plaintext over loopback,
// used transiently, never stored here.
type ProviderKeys = Record<string, string>;

async function forwardToProvider(
  modelKey: string,
  body: any,
  stream: boolean,
  overrides: ProviderKeys = {},
): Promise<Response> {
  const modelDef = resolveModel(modelKey);
  if (!modelDef) throw new Error(`Unknown model: ${modelKey}`);

  if (modelDef.provider === "anthropic") {
    return forwardAnthropic(modelDef, body, stream, overrides);
  } else if (modelDef.provider === "gemini") {
    return forwardGemini(modelDef, body, stream, overrides);
  } else if (modelDef.provider === "openrouter") {
    return forwardOpenRouter(modelDef, body, stream, overrides);
  } else if (modelDef.provider === "openai") {
    return forwardOpenAI(modelDef, body, stream, overrides);
  } else {
    return forwardLMStudio(modelDef, body, stream);
  }
}

async function forwardOpenRouter(modelDef: ModelDef, body: any, stream: boolean, overrides: ProviderKeys = {}): Promise<Response> {
  // OpenRouter speaks OpenAI Chat Completions natively. Pass through with key.
  return fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${overrides.openrouter || OPENROUTER_API_KEY}`,
      "HTTP-Referer": "https://github.com/48Nauts-Operator/NautGate",
      "X-Title": "NautGate",
    },
    body: JSON.stringify({ ...body, model: modelDef.id, stream }),
  });
}

async function forwardOpenAI(modelDef: ModelDef, body: any, stream: boolean, overrides: ProviderKeys = {}): Promise<Response> {
  const out: any = { ...body, model: modelDef.id, stream };
  // The gpt-5 / o-series reasoning models reject `max_tokens` and require
  // `max_completion_tokens`; translate so callers (e.g. Fusion) don't 400.
  if (/^(gpt-5|o1|o3|o4)/.test(modelDef.id) && out.max_tokens != null) {
    out.max_completion_tokens = out.max_tokens;
    delete out.max_tokens;
  }
  return fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${overrides.openai || OPENAI_API_KEY}`,
    },
    body: JSON.stringify(out),
  });
}

async function forwardLMStudio(modelDef: ModelDef, body: any, stream: boolean): Promise<Response> {
  // LM Studio is OpenAI-compatible, just forward.
  // On a STREAM, OpenAI-compatible servers omit the usage block unless it is
  // explicitly requested — without this, local runs recorded 0 prompt/completion
  // tokens, so local-vs-cloud could not be measured at all. include_usage adds a
  // final chunk carrying usage; NautGate's SSE parser already reads it.
  const out: any = { ...body, model: modelDef.id, stream };
  if (stream) {
    out.stream_options = { ...(out.stream_options ?? {}), include_usage: true };
  }
  return fetch(`${LMSTUDIO_URL}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(out),
  });
}

async function forwardAnthropic(modelDef: ModelDef, body: any, stream: boolean, overrides: ProviderKeys = {}): Promise<Response> {
  // Convert OpenAI format → Anthropic Messages API
  const messages = body.messages ?? [];
  let system: string | undefined;
  const anthropicMessages: any[] = [];

  for (const msg of messages) {
    if (msg.role === "system") {
      system = (system ? system + "\n" : "") + (typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content));
    } else if (msg.role === "tool") {
      // OpenAI tool-result message → Anthropic user message with tool_result block.
      anthropicMessages.push({
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: msg.tool_call_id,
          content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content ?? ""),
        }],
      });
    } else if (msg.role === "assistant" && Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0) {
      // Assistant turn that called tools → Anthropic content blocks: text (if any) then tool_use[].
      const blocks: any[] = [];
      if (typeof msg.content === "string" && msg.content) {
        blocks.push({ type: "text", text: msg.content });
      } else if (Array.isArray(msg.content)) {
        for (const c of msg.content) {
          if (c && c.type === "text" && c.text) blocks.push({ type: "text", text: c.text });
        }
      }
      for (const tc of msg.tool_calls) {
        let input: any = {};
        try { input = JSON.parse(tc?.function?.arguments ?? "{}"); } catch { /* keep empty */ }
        blocks.push({ type: "tool_use", id: tc.id, name: tc.function?.name, input });
      }
      anthropicMessages.push({ role: "assistant", content: blocks });
    } else {
      anthropicMessages.push({ role: msg.role === "assistant" ? "assistant" : "user", content: msg.content });
    }
  }

  const anthropicBody: any = {
    model: modelDef.id,
    messages: anthropicMessages,
    max_tokens: body.max_tokens ?? body.max_completion_tokens ?? 4096,
  };
  if (system) anthropicBody.system = system;
  if (body.temperature != null) anthropicBody.temperature = body.temperature;
  if (stream) anthropicBody.stream = true;

  // Translate OpenAI Chat `tools` → Anthropic `tools`. Same intent, different shape:
  //   OpenAI:    {type: "function", function: {name, description, parameters}}
  //   Anthropic: {name, description, input_schema}
  if (Array.isArray(body.tools) && body.tools.length > 0) {
    anthropicBody.tools = body.tools
      .map((t: any) => {
        const fn = t?.function ?? t;
        if (!fn?.name) return null;
        return {
          name: fn.name,
          description: fn.description ?? "",
          input_schema: fn.parameters ?? fn.input_schema ?? { type: "object", properties: {} },
        };
      })
      .filter(Boolean);
  }
  if (body.tool_choice) {
    // "auto" / "any" pass through; specific tool by name → {type: "tool", name}.
    if (typeof body.tool_choice === "string") {
      anthropicBody.tool_choice = { type: body.tool_choice === "required" ? "any" : body.tool_choice };
    } else if (body.tool_choice?.function?.name) {
      anthropicBody.tool_choice = { type: "tool", name: body.tool_choice.function.name };
    }
  }

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": overrides.anthropic || ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify(anthropicBody),
  });

  if (!stream) {
    // Convert Anthropic response → OpenAI format
    const data = await resp.json() as any;
    const content = (data.content ?? [])
      .filter((b: any) => b.type === "text")
      .map((b: any) => b.text)
      .join("");
    // Surface tool_use blocks as OpenAI Chat tool_calls.
    const toolCalls = (data.content ?? [])
      .filter((b: any) => b.type === "tool_use")
      .map((b: any, i: number) => ({
        index: i,
        id: b.id,
        type: "function",
        function: { name: b.name, arguments: JSON.stringify(b.input ?? {}) },
      }));
    const finishReason =
      data.stop_reason === "tool_use"
        ? "tool_calls"
        : data.stop_reason === "end_turn"
        ? "stop"
        : data.stop_reason === "max_tokens"
        ? "length"
        : (data.stop_reason ?? "stop");
    const message: any = { role: "assistant", content };
    if (toolCalls.length > 0) message.tool_calls = toolCalls;
    const openaiResp = {
      id: data.id ?? `chatcmpl-${Date.now()}`,
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: modelDef.id,
      choices: [{ index: 0, message, finish_reason: finishReason }],
      usage: { prompt_tokens: data.usage?.input_tokens ?? 0, completion_tokens: data.usage?.output_tokens ?? 0, total_tokens: (data.usage?.input_tokens ?? 0) + (data.usage?.output_tokens ?? 0) },
    };
    return new Response(JSON.stringify(openaiResp), { status: 200, headers: { "Content-Type": "application/json" } });
  }

  // Streaming: convert Anthropic SSE → OpenAI SSE
  const reader = resp.body?.getReader();
  if (!reader) return new Response("No stream", { status: 500 });

  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  let streamInputTokens = 0;
  let streamOutputTokens = 0;
  let stopReason: string | null = null;
  // Anthropic content-block index → OpenAI Chat tool_calls index. Text blocks
  // are not in tool_calls; tool_use blocks consume sequential tool_calls indices.
  const toolCallIdxByBlock: Record<number, number> = {};
  let nextToolCallIdx = 0;
  const readable = new ReadableStream({
    async pull(controller) {
      let buffer = "";
      const emit = (chunk: any) =>
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`));
      const baseChunk = (delta: any, finish_reason: string | null = null) => ({
        id: `chatcmpl-${Date.now()}`,
        object: "chat.completion.chunk",
        created: Math.floor(Date.now() / 1000),
        model: modelDef.id,
        choices: [{ index: 0, delta, finish_reason }],
      });

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          controller.enqueue(encoder.encode("data: [DONE]\n\n"));
          controller.close();
          return;
        }
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (!payload || payload === "[DONE]") continue;
          try {
            const evt = JSON.parse(payload);
            if (evt.type === "message_start" && evt.message?.usage?.input_tokens != null) {
              streamInputTokens = evt.message.usage.input_tokens;
            } else if (evt.type === "content_block_start" && evt.content_block?.type === "tool_use") {
              // Open a new tool_call entry. Anthropic gives id + name up-front;
              // arguments arrive as input_json_delta chunks.
              const blockIdx = evt.index ?? 0;
              const tcIdx = nextToolCallIdx++;
              toolCallIdxByBlock[blockIdx] = tcIdx;
              emit(baseChunk({
                tool_calls: [{
                  index: tcIdx,
                  id: evt.content_block.id,
                  type: "function",
                  function: { name: evt.content_block.name, arguments: "" },
                }],
              }));
            } else if (evt.type === "content_block_delta" && evt.delta?.type === "text_delta" && evt.delta?.text) {
              emit(baseChunk({ content: evt.delta.text }));
            } else if (evt.type === "content_block_delta" && evt.delta?.type === "input_json_delta") {
              const blockIdx = evt.index ?? 0;
              const tcIdx = toolCallIdxByBlock[blockIdx];
              if (tcIdx != null) {
                emit(baseChunk({
                  tool_calls: [{
                    index: tcIdx,
                    function: { arguments: evt.delta.partial_json ?? "" },
                  }],
                }));
              }
            } else if (evt.type === "content_block_delta" && evt.delta?.text) {
              // Fallback for older Anthropic shape where text deltas lack a `type` field.
              emit(baseChunk({ content: evt.delta.text }));
            } else if (evt.type === "message_delta") {
              if (evt.usage?.output_tokens != null) streamOutputTokens = evt.usage.output_tokens;
              if (evt.delta?.stop_reason) stopReason = evt.delta.stop_reason;
            } else if (evt.type === "message_stop") {
              // Map Anthropic stop_reason → OpenAI finish_reason. tool_use → tool_calls.
              const finish =
                stopReason === "tool_use"
                  ? "tool_calls"
                  : stopReason === "max_tokens"
                  ? "length"
                  : "stop";
              emit({
                id: `chatcmpl-${Date.now()}`,
                object: "chat.completion.chunk",
                created: Math.floor(Date.now() / 1000),
                model: modelDef.id,
                choices: [{ index: 0, delta: {}, finish_reason: finish }],
                usage: {
                  prompt_tokens: streamInputTokens,
                  completion_tokens: streamOutputTokens,
                  total_tokens: streamInputTokens + streamOutputTokens,
                },
              });
            }
          } catch { /* skip malformed */ }
        }
      }
    },
  });

  return new Response(readable, { status: 200, headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" } });
}

async function forwardGemini(modelDef: ModelDef, body: any, stream: boolean, overrides: ProviderKeys = {}): Promise<Response> {
  // Use Gemini's OpenAI-compatible endpoint
  const url = `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`;
  return fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${overrides.gemini || GEMINI_API_KEY}`,
    },
    body: JSON.stringify({ ...body, model: modelDef.id, stream }),
  });
}

// ══════════════════════════════════════════════════════════════
// COST LOGGING → Memory API
// ══════════════════════════════════════════════════════════════

async function logCost(agentId: string, decision: RoutingDecision, inputTokens: number, outputTokens: number) {
  const inputCost = (inputTokens / 1_000_000) * decision.costPer1MInput;
  const outputCost = (outputTokens / 1_000_000) * decision.costPer1MOutput;
  const totalCost = inputCost + outputCost;

  const content = `${decision.model} | ${decision.tier} | in:${inputTokens} out:${outputTokens} | $${totalCost.toFixed(6)} | ${decision.reasoning}`;

  try {
    await fetch(MEMORY_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_id: agentId,
        category: "cost",
        content,
        importance: 0.3,
      }),
    });
  } catch (err) {
    console.error("[cost-log] Failed to post to Memory API:", err);
  }
}

// ══════════════════════════════════════════════════════════════
// EXPRESS SERVER
// ══════════════════════════════════════════════════════════════

const app = express();
app.use(cors({ origin: true }));
app.use(express.json({ limit: "10mb" }));

// ══════════════════════════════════════════════════════════════
// WEBSOCKET SERVER
// ══════════════════════════════════════════════════════════════

const wss = new WebSocketServer({ port: WS_PORT });

function broadcastEvent(event: WebSocketEvent) {
  const payload = JSON.stringify(event);
  wss.clients.forEach(client => {
    if (client.readyState === WebSocket.OPEN) {
      client.send(payload);
    }
  });
}

wss.on("connection", (ws) => {
  ws.send(JSON.stringify({ type: "connected", timestamp: new Date().toISOString(), data: { profile: currentProfile } }));
});

// Health
app.get("/health", (_req, res) => {
  res.json({ status: "ok", version: "1.0.0", profiles: ["eco", "auto", "premium"], ws_port: WS_PORT });
});

// Model list (OpenAI-compatible)
app.get("/v1/models", (_req, res) => {
  const data = Object.keys(MODELS).map(id => ({
    id,
    object: "model",
    created: 1700000000,
    owned_by: "naut-router",
  }));
  // Also add virtual routing models
  data.push(
    { id: "naut/auto", object: "model", created: 1700000000, owned_by: "naut-router" },
    { id: "naut/eco", object: "model", created: 1700000000, owned_by: "naut-router" },
    { id: "naut/premium", object: "model", created: 1700000000, owned_by: "naut-router" },
  );
  res.json({ object: "list", data });
});

// ══════════════════════════════════════════════════════════════
// DASHBOARD API ENDPOINTS
// ══════════════════════════════════════════════════════════════

app.get("/v1/profile", (_req, res) => {
  res.json({ current: currentProfile, available: ["eco", "auto", "premium"] });
});

app.put("/v1/profile", (req, res) => {
  const { profile } = req.body;
  if (["eco", "auto", "premium"].includes(profile)) {
    currentProfile = profile as Profile;
    broadcastEvent({ type: "model_selected", request_id: "profile_change", timestamp: new Date().toISOString(), data: { provider: "system", model: "profile_change", reasoning: `Profile changed to ${profile}` } });
    res.json({ success: true, profile });
  } else {
    res.status(400).json({ error: "Invalid profile. Must be: eco, auto, or premium" });
  }
});

app.get("/v1/providers", (_req, res) => {
  const providers = Object.entries(providerHealth).map(([id, health]) => {
    const models = Object.entries(MODELS)
      .filter(([, def]) => def.provider === id)
      .map(([key, def]) => ({
        id: key,
        name: key,
        cost_per_1k_tokens: def.inputPrice / 1000,
        is_local: def.provider === "lmstudio",
        status: health.status === "offline" ? "unavailable" as const : "available" as const,
      }));

    return {
      id,
      name: id === "anthropic" ? "Anthropic" : id === "lmstudio" ? "LM Studio" : "Google Gemini",
      status: health.status,
      models,
      color: id === "anthropic" ? "#4F46E5" : id === "lmstudio" ? "#10B981" : "#F59E0B",
      total_requests: health.totalRequests,
      total_cost: health.totalCost,
      avg_latency: health.totalRequests > 0 ? Math.round(health.totalLatency / health.totalRequests) : 0,
    };
  });

  res.json({ providers });
});

app.get("/v1/stats", (req, res) => {
  const range = (req.query.range as string) ?? "24h";
  const now = Date.now();
  const rangeMs: Record<string, number> = { "1h": 3_600_000, "24h": 86_400_000, "7d": 604_800_000 };
  const since = now - (rangeMs[range] ?? rangeMs["24h"]);

  const filtered = requestHistory.filter(r => r.timestamp.getTime() >= since);

  const providerStats: Record<string, { total_requests: number; total_cost: number; total_latency: number; success_count: number; cost_trend: { timestamp: Date; value: number }[]; request_trend: { timestamp: Date; value: number }[] }> = {};
  const modelStats: Record<string, { provider_id: string; request_count: number; total_cost: number; total_latency: number }> = {};
  const profileDist: Record<string, number> = { eco: 0, auto: 0, premium: 0 };
  let totalCost = 0;
  let opusBaselineCost = 0;

  for (const r of filtered) {
    totalCost += r.cost_usd;

    const opusInputCost = (r.input_tokens / 1_000_000) * 15;
    const opusOutputCost = (r.output_tokens / 1_000_000) * 75;
    opusBaselineCost += opusInputCost + opusOutputCost;

    profileDist[r.profile] = (profileDist[r.profile] ?? 0) + 1;

    if (!providerStats[r.provider]) {
      providerStats[r.provider] = { total_requests: 0, total_cost: 0, total_latency: 0, success_count: 0, cost_trend: [], request_trend: [] };
    }
    const ps = providerStats[r.provider];
    ps.total_requests++;
    ps.total_cost += r.cost_usd;
    ps.total_latency += r.latency_ms;
    if (r.success) ps.success_count++;
    ps.cost_trend.push({ timestamp: r.timestamp, value: r.cost_usd });
    ps.request_trend.push({ timestamp: r.timestamp, value: 1 });

    if (!modelStats[r.model]) {
      modelStats[r.model] = { provider_id: r.provider, request_count: 0, total_cost: 0, total_latency: 0 };
    }
    const ms = modelStats[r.model];
    ms.request_count++;
    ms.total_cost += r.cost_usd;
    ms.total_latency += r.latency_ms;
  }

  const totalRequests = filtered.length;
  const profileDistNorm: Record<string, number> = {};
  for (const [k, v] of Object.entries(profileDist)) {
    profileDistNorm[k] = totalRequests > 0 ? v / totalRequests : 0;
  }

  const providers = Object.entries(providerStats).map(([id, ps]) => ({
    provider_id: id,
    total_requests: ps.total_requests,
    total_cost: Number(ps.total_cost.toFixed(6)),
    avg_latency: ps.total_requests > 0 ? Math.round(ps.total_latency / ps.total_requests) : 0,
    success_rate: ps.total_requests > 0 ? Number((ps.success_count / ps.total_requests).toFixed(3)) : 1,
    cost_trend: ps.cost_trend.slice(-50),
    request_trend: ps.request_trend.slice(-50),
  }));

  const models = Object.entries(modelStats).map(([id, ms]) => ({
    model_id: id,
    provider_id: ms.provider_id,
    request_count: ms.request_count,
    avg_cost: ms.request_count > 0 ? Number((ms.total_cost / ms.request_count).toFixed(6)) : 0,
    avg_latency: ms.request_count > 0 ? Math.round(ms.total_latency / ms.request_count) : 0,
  }));

  const savingsUsd = opusBaselineCost - totalCost;

  res.json({
    time_range: range,
    total_requests: totalRequests,
    total_cost: Number(totalCost.toFixed(6)),
    providers,
    models,
    savings: {
      actual_cost: Number(totalCost.toFixed(6)),
      opus_baseline_cost: Number(opusBaselineCost.toFixed(6)),
      savings_usd: Number(Math.max(0, savingsUsd).toFixed(6)),
      savings_percentage: opusBaselineCost > 0 ? Number((Math.max(0, savingsUsd) / opusBaselineCost).toFixed(3)) : 0,
    },
    profile_distribution: profileDistNorm,
  });
});

// Main routing endpoint
app.post("/v1/chat/completions", async (req, res) => {
  const requestId = generateRequestId();
  const requestStart = Date.now();

  try {
    const body = req.body;
    const requestedModel: string = body.model ?? "naut/auto";
    const agentId = req.headers["x-agent-id"] as string ?? "unknown";
    // Per-request provider keys from core (NAUTGATE-8) — decrypted db-stored
    // keys that override this process's env for that provider.
    let providerKeys: ProviderKeys = {};
    const pkHeader = req.headers["x-ng-provider-keys"] as string | undefined;
    if (pkHeader) {
      try { providerKeys = JSON.parse(pkHeader); } catch { /* ignore malformed */ }
    }
    const stream = body.stream === true;

    let profile: Profile = currentProfile;
    let directModel: string | null = null;

    if (requestedModel.startsWith("naut/") || requestedModel === "auto" || requestedModel === "eco" || requestedModel === "premium") {
      const p = requestedModel.replace("naut/", "");
      if (p === "eco" || p === "auto" || p === "premium") profile = p;
    } else if (resolveModel(requestedModel)) {
      // Anything resolvable (curated map, openrouter/*, claude-* passthrough)
      // is a DIRECT request — never silently re-route an explicit model.
      directModel = requestedModel;
    } else {
      profile = currentProfile;
    }

    const messages = body.messages ?? [];
    const lastUserMsg = [...messages].reverse().find((m: any) => m.role === "user");
    const prompt = typeof lastUserMsg?.content === "string" ? lastUserMsg.content : JSON.stringify(lastUserMsg?.content ?? "");
    const systemMsg = messages.find((m: any) => m.role === "system");
    const systemPrompt = typeof systemMsg?.content === "string" ? systemMsg.content : undefined;

    broadcastEvent({
      type: "request_received",
      request_id: requestId,
      timestamp: new Date().toISOString(),
      data: { agent_id: agentId, message_preview: prompt.substring(0, 100), profile },
    });

    let decision: RoutingDecision;
    if (directModel) {
      const m = resolveModel(directModel)!;
      decision = { model: directModel, provider: m.provider, tier: "MEDIUM" as Tier, confidence: 1.0, reasoning: "direct model request", costPer1MInput: m.inputPrice, costPer1MOutput: m.outputPrice };
    } else {
      decision = routeRequest(prompt, systemPrompt, profile);
    }

    const fullText = `${systemPrompt ?? ""} ${prompt}`.toLowerCase();
    const estimatedTokens = Math.ceil(fullText.length / 4);
    const scoreDimensions = classifyByRules(prompt, systemPrompt, estimatedTokens);
    const dimensionScores = extractDimensionScores(prompt, systemPrompt, estimatedTokens);

    broadcastEvent({
      type: "scoring_complete",
      request_id: requestId,
      timestamp: new Date().toISOString(),
      data: {
        scores: dimensionScores,
        complexity_tier: decision.tier.toLowerCase(),
        raw_score: scoreDimensions.score,
        confidence: scoreDimensions.confidence,
        signals: scoreDimensions.signals,
      },
    });

    broadcastEvent({
      type: "model_selected",
      request_id: requestId,
      timestamp: new Date().toISOString(),
      data: { provider: decision.provider, model: decision.model, reasoning: decision.reasoning },
    });

    console.log(`[route] ${agentId} → ${decision.model} (${decision.tier}, ${decision.provider}) | ${decision.reasoning}`);

    const tierConfig = directModel ? null : TIER_CONFIGS[profile][decision.tier];
    const modelsToTry = directModel ? [directModel] : [tierConfig!.primary, ...tierConfig!.fallback];
    let response: Response | null = null;
    let usedModel = decision.model;

    for (const modelKey of modelsToTry) {
      try {
        response = await forwardToProvider(modelKey, body, stream, providerKeys);
        if (response.ok || response.status < 500) {
          usedModel = modelKey;
          break;
        }
        console.warn(`[fallback] ${modelKey} returned ${response.status}, trying next...`);
      } catch (err) {
        console.warn(`[fallback] ${modelKey} failed:`, err);
      }
    }

    if (!response) {
      broadcastEvent({
        type: "error",
        request_id: requestId,
        timestamp: new Date().toISOString(),
        data: { error: "All providers failed" },
      });
      res.status(502).json({ error: { message: "All providers failed", type: "server_error" } });
      return;
    }

    if (stream && response.body) {
      res.setHeader("Content-Type", "text/event-stream");
      res.setHeader("Cache-Control", "no-cache");
      res.setHeader("Connection", "keep-alive");
      res.setHeader("X-Naut-Model", usedModel);
      res.setHeader("X-Naut-Tier", decision.tier);
      res.setHeader("X-Naut-Request-Id", requestId);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let totalOutput = 0;

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          res.write(chunk);
          totalOutput += Math.ceil(chunk.length / 4);
        }
      } catch { /* client disconnect */ }
      res.end();

      const latencyMs = Date.now() - requestStart;
      const inputTokens = Math.ceil(JSON.stringify(messages).length / 4);
      const m = resolveModel(usedModel) ?? resolveModel(decision.model)!;
      const costUsd = (inputTokens / 1_000_000) * m.inputPrice + (totalOutput / 1_000_000) * m.outputPrice;

      broadcastEvent({
        type: "response_complete",
        request_id: requestId,
        timestamp: new Date().toISOString(),
        data: { latency_ms: latencyMs, cost_usd: costUsd, tokens_consumed: inputTokens + totalOutput, success: true },
      });

      updateProviderHealth(m.provider, true, latencyMs, costUsd);
      addRequestRecord({ id: requestId, timestamp: new Date(), agent_id: agentId, profile, tier: decision.tier, provider: m.provider, model: usedModel, latency_ms: latencyMs, cost_usd: costUsd, input_tokens: inputTokens, output_tokens: totalOutput, success: true });
      logCost(agentId, { ...decision, model: usedModel, costPer1MInput: m.inputPrice, costPer1MOutput: m.outputPrice }, inputTokens, totalOutput);
    } else {
      const data = await response.json() as any;
      res.setHeader("X-Naut-Model", usedModel);
      res.setHeader("X-Naut-Tier", decision.tier);
      res.setHeader("X-Naut-Request-Id", requestId);
      res.json(data);

      const latencyMs = Date.now() - requestStart;
      const inputTokens = data.usage?.prompt_tokens ?? Math.ceil(JSON.stringify(messages).length / 4);
      const outputTokens = data.usage?.completion_tokens ?? 0;
      const m = resolveModel(usedModel) ?? resolveModel(decision.model)!;
      const costUsd = (inputTokens / 1_000_000) * m.inputPrice + (outputTokens / 1_000_000) * m.outputPrice;

      broadcastEvent({
        type: "response_complete",
        request_id: requestId,
        timestamp: new Date().toISOString(),
        data: { latency_ms: latencyMs, cost_usd: costUsd, tokens_consumed: inputTokens + outputTokens, success: true },
      });

      updateProviderHealth(m.provider, true, latencyMs, costUsd);
      addRequestRecord({ id: requestId, timestamp: new Date(), agent_id: agentId, profile, tier: decision.tier, provider: m.provider, model: usedModel, latency_ms: latencyMs, cost_usd: costUsd, input_tokens: inputTokens, output_tokens: outputTokens, success: true });
      logCost(agentId, { ...decision, model: usedModel, costPer1MInput: m.inputPrice, costPer1MOutput: m.outputPrice }, inputTokens, outputTokens);
    }
  } catch (err: any) {
    console.error("[error]", err);
    broadcastEvent({
      type: "error",
      request_id: requestId,
      timestamp: new Date().toISOString(),
      data: { error: err.message },
    });
    res.status(500).json({ error: { message: err.message, type: "server_error" } });
  }
});

app.get("/stats", (_req, res) => {
  res.json({
    uptime: process.uptime(),
    models: Object.keys(MODELS),
    profiles: ["eco", "auto", "premium"],
    currentProfile: currentProfile,
    totalRequests: requestHistory.length,
    wsClients: wss.clients.size,
  });
});

const server = createServer(app);
server.listen(PORT, () => {
  console.log(`
╔══════════════════════════════════════════╗
║         NautRouter v2.0.0                ║
║   Smart LLM Routing Proxy + Dashboard    ║
║                                          ║
║   HTTP:  ${String(PORT).padEnd(31)}║
║   WS:    ${String(WS_PORT).padEnd(31)}║
║   Profile: ${currentProfile.padEnd(28)}║
║   Providers: Anthropic, Gemini, LMStudio ║
║   Scoring: 14-dimension weighted engine  ║
║                                          ║
║   POST /v1/chat/completions              ║
║   GET  /v1/profile  PUT /v1/profile      ║
║   GET  /v1/providers                     ║
║   GET  /v1/stats?range=24h               ║
║   WS   ws://localhost:${String(WS_PORT).padEnd(18)}║
╚══════════════════════════════════════════╝
  `);
});
