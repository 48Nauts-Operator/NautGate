# Flow Proxy → NautGate cutover runbook

**Audience:** the operator (Andre) flipping the MacBook hooks from
`flow-proxy` (port 4002) to NautGate (port 8090).

**Reversibility:** every step is reversible by re-running the previous step.
Flow Proxy stays running for 48 h after the flip; rollback = re-point the env vars.

---

## Pre-flight

1. **Confirm services are healthy on stargate**:
   ```bash
   ssh stargate "docker compose -f /path/to/NautGate/deploy/docker-compose.yml ps"
   # nautgate-db (healthy), nautrouter (healthy)
   ```
2. **Confirm `agents_memory` Postgres is reachable from stargate**:
   ```bash
   ssh stargate "psql postgres://agents:agents_secure_2026@100.71.163.122:5433/agents_memory -c 'SELECT count(*) FROM memories'"
   ```
3. **Confirm sb-capture has provider keys** (or accepts that until you drop
   keys in `.env`, `model:auto` will 502 for new traffic):
   ```bash
   grep -E 'ANTHROPIC|OPENAI|GEMINI' /path/to/NautGate/deploy/.env || echo "no keys yet"
   ```

---

## Step 1 — Bring up sb-capture pointing at agents_memory

On stargate:

```bash
cd /path/to/NautGate
cat >> deploy/.env <<EOF
SB_CAPTURE_SINK=both
SB_CAPTURE_DB_URL=postgres://agents:agents_secure_2026@100.71.163.122:5433/agents_memory
EOF

docker compose \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.with-extensions.yml \
  up -d sb-capture nautgate-core

# Verify sb-capture sees both sinks:
curl -fsS http://localhost:8001/health
# {"status":"ok","sinks":["NDJSONSink","PostgresSink"]}
```

---

## Step 2 — Issue an API key for the MacBook agent

```bash
ssh stargate "cd /path/to/NautGate && just issue-key claude-code"
# Save the printed token. It's shown once.
```

Repeat for `codex` if you also flip the Codex CLI.

---

## Step 3 — Smoke test from your laptop

Replace `<TOKEN>` with the issued key:

```bash
curl -sS -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"model":"claude-haiku-4-5","max_tokens":50,
          "messages":[{"role":"user","content":"smoke test"}]}' \
     https://stargate.tail/v1/messages | head -c 500
```

Expected: an Anthropic-shaped response (or 502 if no provider keys yet —
either is fine; the audit log still gets a row).

Verify the row landed:

```bash
ssh stargate "psql .../agents_memory -c \"SELECT category, content FROM memories WHERE metadata->>'source'='nautgate-sb-capture' ORDER BY created_at DESC LIMIT 5\""
```

---

## Step 4 — Flip ONE hook, leave Flow Proxy running

The MacBook hooks live in `~/.claude/...` or wherever you wired them. The
flip is just env-var changes; the hooks themselves don't change.

**For Claude Code:**
```bash
# old
export ANTHROPIC_BASE_URL=http://localhost:4002

# new
export ANTHROPIC_BASE_URL=https://stargate.tail:8090
export ANTHROPIC_API_KEY="ng_<your-claude-code-token>"
```

**For Codex CLI:**
```bash
# old
export OPENAI_BASE_URL=http://localhost:4002/v1

# new
export OPENAI_BASE_URL=https://stargate.tail:8090/v1
export OPENAI_API_KEY="ng_<your-codex-token>"
```

Run a real session, watch:

```bash
# stargate
docker compose -f deploy/docker-compose.yml logs -f nautgate-core
ssh stargate "psql .../agents_memory -c \"SELECT count(*) FROM memories WHERE metadata->>'source'='nautgate-sb-capture' AND created_at > NOW() - interval '5 min'\""
```

---

## Step 5 — Watch for 24 h

Check daily:

```sql
-- NautGate's own audit log
SELECT count(*), date_trunc('hour', ts) AS h
  FROM nautgate.route_decisions
 WHERE ts > NOW() - INTERVAL '24 hours'
 GROUP BY h ORDER BY h DESC;

-- sb-capture mirroring into agents_memory
SELECT count(*), date_trunc('hour', created_at) AS h
  FROM memories
 WHERE metadata->>'source' = 'nautgate-sb-capture'
   AND created_at > NOW() - INTERVAL '24 hours'
 GROUP BY h ORDER BY h DESC;
```

Counts should match within ±1 (some requests fail before sb-capture fires).

---

## Step 6 — Retire Flow Proxy

After 24 h of clean traffic:

```bash
# MacBook
launchctl unload ~/Library/LaunchAgents/com.flow-proxy.plist 2>/dev/null || true
pkill -f flow-proxy/proxy.js || true
# Optional: remove the symlink / service file entirely
```

Flow Proxy can stay installed (zero ongoing cost) or be uninstalled per
Build Plan §"Week 4 — decommission".

---

## Rollback

At any point in steps 4-5, the rollback is one line per machine:

```bash
unset ANTHROPIC_API_KEY
export ANTHROPIC_BASE_URL=http://localhost:4002
```

Same for `OPENAI_BASE_URL`. Flow Proxy is still running on `:4002`, ready to
take traffic.

---

## Open items / things I cannot do for you

- **Provider API keys**: drop them in `deploy/.env` (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, etc.) before you flip — otherwise every call 502s on
  upstream. The audit/capture pipeline still records, but agents on the other
  end won't see real responses.
- **TLS**: this runbook assumes `stargate.tail` is your Tailscale hostname.
  If you want HTTPS on `:8090`, terminate at a Caddy/Nginx in front; not in
  scope for v1.
- **Multiple machines**: each machine that runs Claude Code / Codex needs
  its own issued key. `just issue-key <agent>` is idempotent for the agent
  but creates a fresh secret per call.
