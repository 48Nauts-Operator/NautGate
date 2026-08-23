/* NautGate help content, keyed by dashboard tab (the pane is passed "/<tab>").
   Consumed by the vendored @48nauts/help-module vanilla pane. `hints` are fed
   to the assistant's system prompt; keep them factual. */
window.NAUTGATE_HELP = {
  "/observatory": {
    title: "Observatory",
    summary: "A compact operational record of what ran, where capacity went, and which signals deserve attention.",
    sections: [{ heading: "How to read it", bullets: [
      "**Observed work** joins calls, fresh input, output and metered cost for the last 24 hours.",
      "**Live risk** surfaces the tracked Max-plan session with the most fresh input; open Max Guard for controls.",
      "**Drift** is provider/model behavior moving outside NautGate's learned baseline, not prompt-topic drift.",
    ] }],
    suggestedQuestions: ["Which project used the most capacity?", "Why is this session the live risk?", "Open Drift"],
    hints: ["Observatory is the operational home. Treat provider quota as private: Max Guard figures are NautGate estimates from observed fresh input."],
  },
  "/team": {
    title: "Observatory · Team",
    summary: "Activity, routing cost, Max-plan use and compliance indicators grouped by ng_key identity.",
    sections: [{ heading: "Identity and compliance", bullets: [
      "A team member is an **ng_key identity**, not a guessed human identity.",
      "Click the compliance card to see the evidence NautGate can attribute to that identity.",
    ] }],
    suggestedQuestions: ["Who generated the most traffic?", "Explain this compliance indicator"],
    hints: ["Team joins API keys, per-agent cost streams, quality anti-patterns and Max Guard sessions."],
  },
  "/accounts": {
    title: "Observatory · Accounts",
    summary: "Provider availability, plan type, metered API spend, subscription value and observed capacity in one account inspector.",
    sections: [{ heading: "Important limits", bullets: [
      "NautGate can measure traffic and estimate subscription value, but it cannot read Anthropic's private Max quota balance.",
      "API balance is shown only when a provider exposes it and NautGate has access.",
    ] }],
    suggestedQuestions: ["Which account is active?", "Compare API spend with Max-plan value"],
    hints: ["Accounts uses logos/labels and provider evidence. Never present Max Guard estimates as an official vendor balance."],
  },
  "/teamcompliance": {
    title: "Team · Compliance explanation",
    summary: "The evidence chain behind a team compliance indicator: source identity, observed behavior, impact and attribution limits.",
    suggestedQuestions: ["Who did what?", "What evidence is missing?"],
    hints: ["Explain only retained evidence; do not reconstruct prompts or claim source-app attribution when metadata was absent."],
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
  "/maxguard": {
    title: "Max Guard",
    summary: "Protects Claude Max capacity from runaway or cache-broken sessions while keeping each xNaut project and Claude session separately attributable.",
    sections: [
      { heading: "What the gauges mean", bullets: [
        "**Rolling five hours** and **rolling seven days** compare recorded fresh input with your configured protection thresholds; they do not claim to know Anthropic's private quota formula.",
        "Fresh input, cache reads and cache writes are separate because repeated fresh context consumes capacity very differently from a cache hit.",
      ] },
      { heading: "Controls", bullets: [
        "**Pause** blocks the selected identity with a non-retryable response; integrated xNaut runs stop their exact zellij session.",
        "**Resume** removes the pause. **Authorize** grants a small, expiring allowance for one or more requests.",
        "Every control action receives a signed audit receipt. In **observe** mode NautGate reports risk but does not automatically pause traffic.",
      ] },
    ],
    suggestedQuestions: ["Why is a session using fresh tokens?", "What does observe mode do?", "How does Authorize work?"],
    hints: ["User is on Max Guard. Never imply NautGate knows Anthropic's exact Max quota. Explain native-session scope, cache accounting, observe/warn/pause modes, and non-retryable cooperative xNaut stopping."],
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
  "/drift": {
    title: "Drift",
    summary: "Detailed provider/model behavior changes against NautGate's learned baselines, with anomalies, investigations and reports.",
    suggestedQuestions: ["Why did this drift alert fire?", "Show the underlying request evidence"],
    hints: ["Drift uses EWMA baselines per provider, model and metric. It is behavior drift, not a claim about the user's intent."],
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
