# sb-privacy

NautGate Week 4 extension. When the gateway classifies a prompt as `pii` or
`secret`, sb-privacy:

1. Returns routing hints biasing toward the privacy-safe allowlist
   (`allowlist.yaml`).
2. Appends a hash-chained row to `nautgate.privacy_log` so every sensitive
   prompt has a tamper-evident audit trail (Weaver-shaped, per Tech Paper §13).

## Configuration

```
SB_PRIVACY_DB_URL          postgres://nautgate:nautgate@nautgate-db:5432/nautgate
SB_PRIVACY_ALLOWLIST_PATH  /etc/sb-privacy/allowlist.yaml   (defaults to bundled allowlist.yaml)
SB_PRIVACY_LOG_LEVEL       INFO
```

## Wire into NautGate

`nautgate.yaml`:

```yaml
extensions:
  sb-privacy:
    base_url: http://sb-privacy:8003
    hooks: [before_route, on_request]
    timeout_ms_before_route: 50
    timeout_ms: 200
```

## Allowlist format

`allowlist.yaml`:

```yaml
providers:
  - lmstudio
  - anthropic-no-train

models:
  - lmstudio/qwen3-30b
  - claude-haiku-4-5
```

`promoted_models` is set from `models`. The allowlist is YAML for ops-easy editing.

## Hash chain

Each `privacy_log` row has:

- `prev_hash` — `this_hash` of the previous row (`0…0` for genesis)
- `payload_hash` — `sha256(prompt_excerpt)`
- `this_hash` — `sha256(prev_hash | payload_hash | ts | decision_id | agent_id | sensitivity)`

Walk the rows in `id` order and compare each computed hash to the stored one.
Any tamper anywhere in the chain breaks every subsequent row. See `chain.py`'s
`verify_chain()` for the validator.

## Why "privacy-safe" not "anonymize+forward"

For v1 we route around the problem. The Privacy-Swarm anonymizer integration
(redact PII inline, forward redacted prompt to any provider) lands in v2 once
that pipeline's contract is stable. sb-privacy v1 is the audit + routing
gate; that's enough to make sensitive traffic visible and policy-compliant.
