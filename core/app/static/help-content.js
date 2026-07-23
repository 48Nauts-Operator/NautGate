/* NautGate help content, keyed by dashboard tab (the pane is passed "/<tab>").
   Consumed by the vendored @48nauts/help-module vanilla pane. `hints` are fed
   to the assistant's system prompt; keep them factual. */
window.NAUTGATE_HELP = {
  "/overview": {
    title: "Overview",
    summary: "The state of everything at a glance — what ran, what it cost, which sessions are live, and whether a provider is struggling.",
    sections: [
      { heading: "What the tiles mean", bullets: [
        "**Subscription saved** — the metered list price of traffic your Max/Plus plans covered. Notional, not billed.",
        "**Data shipped** — how many calls carried PII/secrets; bodies are policy-gated before storage.",
      ] },
    ],
    suggestedQuestions: ["What is \"subscription saved\"?", "Why is a provider showing no recent calls?"],
    hints: ["User is on the Overview. The headline figures are efficiency index, savings identified, experiments, data shipped, requests, empty rate, latency."],
  },
  "/audit": {
    title: "Audit Log",
    summary: "Every call in and out, with the model that was **asked for** and the model that **actually answered**. Click a row for the full record.",
    sections: [
      { heading: "The flow view", bullets: [
        "Open a call, then the flow view draws client → lane → decision → upstream → served model.",
        "A silent substitution is highlighted at the hop where the name changed.",
      ] },
    ],
    suggestedQuestions: ["How do you know which model actually answered?", "What is a decision id?"],
    hints: ["Audit Log. The served/actual model is parsed from the provider's response, never echoed from the request. Attestation is strongest on OpenRouter/OpenAI; Anthropic currently echoes."],
  },
  "/cost": {
    title: "Cost",
    summary: "Spend broken down by project, agent, model and provider — plus the notional value your subscriptions covered.",
    sections: [
      { heading: "Reading it", bullets: [
        "Switch to 30 days to see **subscription saved** accumulate.",
        "An unpriced model shows a blank cost, not zero — a missing `pricing.yaml` entry.",
      ] },
    ],
    suggestedQuestions: ["Why is a cost showing as blank?", "How is subscription saved calculated?"],
    hints: ["Cost tab. cost_usd is NULL (blank), not 0, when a model has no pricing entry. Subscription saved = SUM(notional_cost_usd)."],
  },
  "/cache": {
    title: "Cache",
    summary: "Prefix reuse and what it's worth: hit rate, write-to-read ratio, and dollars saved against the uncached price.",
    sections: [
      { heading: "Leaky prefixes", bullets: [
        "A **leaky** prefix has reuse ratio below 1 — something early in the prompt changes every turn (a timestamp, an id) and busts the cache.",
        "Local models report no cache tokens, so use the latency lens for them.",
      ] },
    ],
    suggestedQuestions: ["What is a leaky prefix?", "Why do local models show no cache savings?"],
    hints: ["Cache tab. hit_rate = cache_read / (cache_read + cache_write + prompt_tokens)."],
  },
  "/tooling": {
    title: "Tooling",
    summary: "What your MCP servers cost simply by existing — every tool schema travels in **every** request, called or not.",
    sections: [
      { heading: "Carrying cost", bullets: [
        "Measured from the latest captured tool manifest per agent.",
        "The discovery mix (filesystem reads vs `mcp__*` calls) shows whether a server earns its place.",
      ] },
    ],
    suggestedQuestions: ["What is carrying cost?", "Is there a savings number here?"],
    hints: ["Tooling tab. Carrying cost = schema tokens carried per request. There is deliberately NO single savings dollar figure — only the discovery mix."],
  },
  "/quality": {
    title: "Quality",
    summary: "A judge model scores answers after the fact. Every anomaly is judged; everything else is sampled.",
    sections: [
      { heading: "What's judged", bullets: [
        "Empty responses, errors, disconnects, truncation and high bloat are **always** judged.",
        "Secret-classified calls are never sent to the judge.",
      ] },
    ],
    suggestedQuestions: ["What gets evaluated?", "Does the judge see my secrets?"],
    hints: ["Quality tab. Post-hoc LLM-as-judge; nothing is ever blocked. Secret-classified calls are skipped."],
  },
  "/insights": {
    title: "Insights",
    summary: "Where context is wasted, which prompts drift, and how much of a request is overhead rather than your question.",
    suggestedQuestions: ["What is context ratio?", "How is waste detected?"],
    hints: ["Insights tab. Panels: simulator, substitution, control chart, efficiency, context-waste detectors, overthinking, dataflow."],
  },
  "/bench": {
    title: "Bench",
    summary: "Send one task to several models at once. **Head-to-head** pairs real calls that answered the same task, so you compare like for like.",
    suggestedQuestions: ["Why is head-to-head better than averaging?", "What does in_per_out mean?"],
    hints: ["Bench tab. Head-to-head correlates real calls by md5(prompt_excerpt). in_per_out = input tokens per output token."],
  },
  "/modelhealth": {
    title: "Model Health",
    summary: "Watches for a provider quietly degrading — slower, emptier, or serving something other than what you asked for.",
    suggestedQuestions: ["How is drift detected?", "What is a model_mismatch alert?"],
    hints: ["Model Health tab. EWMA baselines per (provider, model, metric); |z|>3 is an anomaly, two in a row raises a clustered alert."],
  },
  "/privacy": {
    title: "Privacy",
    summary: "What actually leaves your machine. Every prompt is classified for PII and secrets before anything is stored.",
    suggestedQuestions: ["What happens to a secret-classified call?", "Where do the samples come from?"],
    hints: ["Privacy tab. secret → body never written; pii → body stored with matches redacted. Samples are re-scanned from stored bodies."],
  },
  "/experiments": {
    title: "Experiments",
    summary: "Champion–challenger tests in the background: a cheaper model answers the same prompt, and a blind judge scores whether anyone would notice.",
    suggestedQuestions: ["How is the winner decided?", "Is the projected saving real?"],
    hints: ["Experiments tab. Blind pairwise judging; one-sided binomial non-inferiority test at p0=0.90. Projected saving is a linear extrapolation."],
  },
  "/settings": {
    title: "Settings",
    summary: "Keys, providers, offline mode, quality/shadow config, and backups.",
    sections: [
      { heading: "Providers", bullets: [
        "**Providers** stores your model-provider keys encrypted at rest; needs `NAUTGATE_MASTER_KEY` set.",
        "**Offline mode** stands down every timer-driven outbound call.",
      ] },
    ],
    suggestedQuestions: ["How do I add a provider key?", "What does offline mode do?"],
    hints: ["Settings tab. Providers pane stores keys AES-256-GCM encrypted. Offline mode gates timer-driven callers only."],
  },
};
