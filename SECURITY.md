# Security Policy

## Reporting a vulnerability

**Please don't open a public issue.** Email <hello@48nauts.com> with:

- what the issue is and how to trigger it
- the impact you think it has
- anything you've already tried

You'll get an acknowledgement within a few days. NautGate is maintained by a
small team, so please allow reasonable time for a fix before disclosing
publicly.

## Scope

NautGate is designed to run on **localhost or a private network** (Tailscale,
VPN, LAN). It is not hardened for direct exposure to the public internet — the
dashboard and admin endpoints assume a trusted network. Reports that depend on
having deliberately published `:8090` to the internet are out of scope.

In scope and genuinely useful:

- Authentication bypass — reaching a route without a valid `ng_` key
- One agent reading another agent's decisions, keys or captured bodies
- Provider credentials leaking into logs, responses, audit rows or the dashboard
- Captured prompt/response bodies escaping the sensitivity policy — in
  particular, anything classified `secret` being persisted
- SQL injection or template injection anywhere in the pipeline

## Handling secrets

NautGate holds provider API keys and captures prompt and response bodies. If you
are filing a bug with logs attached, redact:

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`
- any `ng_…` gateway token
- captured bodies, which may contain real prompts

Provider keys live only in `deploy/.env`, which is gitignored. If you ever find
a live key committed anywhere in this repo, treat it as a vulnerability and
report it by email.
