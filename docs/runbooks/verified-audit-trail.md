# Verified Audit Trail operator runbook

## Proof boundary

NautGate hashes each completed decision receipt, builds an ordered Merkle tree,
and asks the Securosys TSB sidecar to sign only the typed checkpoint root. A
verified bundle proves that the disclosed receipt existed in that checkpoint
and has not changed. It does not prove completeness before the gap checks ran,
the truth of captured inputs, operator identity, legal compliance, or that a
key was never misused. Use “verified” or “included in a hardware-signed
checkpoint”; do not call a pending receipt attested, compliant, or immutable.

## Modes

The v1 runtime is **availability mode**: model traffic continues while TSB is
unavailable. Receipts remain durably `pending`/`batched`, response headers say
`X-NautGate-Evidence-Status: pending`, and the signer catches up in sequence
after recovery. This preserves service availability without overstating proof.

**Strict mode** means withholding a response until its receipt is durably
checkpointed and verified. That changes streaming semantics and can turn a TSB
outage into an LLM outage. It is deliberately not enabled in v1; setting
`NAUTGATE_AUDIT_MODE=strict` is not a compliance shortcut. A future strict-mode
release must implement bounded synchronous sealing, explicit timeout behavior,
and client opt-in before operators may describe it as strict.

## Health and alerts

Use the Audit Log dashboard or `GET /v1/audit/status`. Investigate immediately
when `health` is `critical`, `open_gaps` or `checkpoint_failures` is non-zero,
or an alert reports `signing_lag`. Defaults are warning after 120 seconds and
critical after 600 seconds; tune with `NAUTGATE_AUDIT_LAG_WARNING_S` and
`NAUTGATE_AUDIT_LAG_CRITICAL_S`.

The lifecycle is `pending → batched → verified`. `failed` means signing exhausted
its retry budget. `gap` means the next committed sequence did not match the
expected sequence; never delete or renumber rows to hide a gap.

## Recover from TSB downtime

1. Leave NautGate and PostgreSQL running. Do not delete the outbox, checkpoints,
   receipts, or audit state, and do not restage already-created checkpoints.
2. Confirm the sidecar is reachable and its health response identifies the
   expected key. Check its TSB connectivity and internal-token configuration.
3. Compare the returned public-key fingerprint with the independently recorded
   fingerprint. A mismatch is a security incident, not a retry condition.
4. Restore the sidecar. The signer retries the oldest staged checkpoint first;
   the checkpoint identity and canonical bytes remain unchanged.
5. Watch pending count and signing lag fall to zero. Export and verify one old
   and one new bundle offline before clearing the incident.

If a checkpoint reached terminal `failed`, inspect `last_error`, fix the cause,
and explicitly return that checkpoint to `signing` while retaining its ID,
canonical bytes, hash, key ID, and attempt history in incident records. Never
rebuild the tree from a different receipt range.

## Resolve an evidence gap

1. Stop the checkpoint worker, but do not stop receipt capture.
2. Compare `audit_state.next_sequence`, `audit_receipts`, and `audit_outbox` in
   one read-only transaction. Look for an uncommitted/spooled outcome before
   assuming deletion or corruption.
3. Restore the missing committed receipt from the durable outcome spool or a
   verified backup using its original receipt ID and sequence.
4. Record the cause and resolution in `audit_gaps`; set `resolved_at` only after
   the sequence is contiguous and independently checked.
5. Restart the worker. Never renumber later receipts or sign across an open gap.

## Rotate a signing key

1. Create the new RSA signing key inside the TSB with checkpoint-only policy.
2. Export only its public key and record the SHA-256 SPKI fingerprint through an
   independent channel.
3. Give it a new immutable key ID. Reusing a key ID with different material is
   rejected by NautGate.
4. Set `NAUTGATE_AUDIT_SIGNING_KEY_ID`, `NAUTGATE_AUDIT_PUBLIC_KEY_PEM`, and
   `NAUTGATE_AUDIT_PUBLIC_KEY_FINGERPRINT`, then restart NautGate and the sidecar.
5. Mark the prior key `retired` with `valid_until`; never remove its public key,
   because old bundles depend on that trust anchor.
6. Verify the first new-key bundle and a historical old-key bundle offline.
   Use `revoked` only with an incident record explaining the trust impact.

## Evidence export

Receipt and checkpoint metadata are tenant-scoped. The bundle endpoint returns
404 for pending, failed, unknown, and other-agent receipts to prevent enumeration.
Follow `docs/specs/audit-v1/OFFLINE-VERIFICATION.md` for independent verification.

## Set it up on a compose stack

Proven on stargate on 2026-09-16 against the Securosys CloudsHSM sandbox. The
whole path, from an empty HSM partition to an offline-verified bundle, takes
about fifteen minutes; most of that is the signer catching up on old receipts.

### 1. Mint the signing key inside the HSM

Use the xNAUT `securosys-attest` MCP (tool `create_key`) or its CLI. It creates
an RSA-4096 key that can sign and nothing else, not extractable, not modifiable,
no approval policy, and writes the public half next to the plugin's data:

```console
SECUROSYS_TSB_URL=https://sbx-rest-api.cloudshsm.com \
SECUROSYS_JWT="$(security find-generic-password -s xnaut -a plugin/securosys-attest/SECUROSYS_JWT -w)" \
python3 mcp/securosys-attest.py create-key NAUTGATE_AUDIT_KEY
```

The output names the PEM path and the SPKI SHA-256 fingerprint. Write the
fingerprint down somewhere that is not the deployment (a vault note, a ticket).
That second copy is the trust anchor every later verification is pinned to.

### 2. Put the public key next to the compose files

```console
scp NAUTGATE_AUDIT_KEY.pem host:.../deploy/nautgate-audit-public.pem
ssh host 'openssl pkey -pubin -in .../deploy/nautgate-audit-public.pem -outform DER | openssl dgst -sha256'
```

The digest must equal the fingerprint from step 1. If it does not, stop.

### 3. Add the settings to the stack's env file

The overlay reads these names. Compose 2.24 and later accept a double-quoted
multi-line value, so the PEM goes in as is:

```console
SB_ATTEST_TSB_URL=https://sbx-rest-api.cloudshsm.com
SB_ATTEST_KEY_NAME=NAUTGATE_AUDIT_KEY
SB_ATTEST_INTERNAL_TOKEN=<openssl rand -hex 32>
SB_ATTEST_PUBLIC_KEY_FINGERPRINT=<64 hex from step 1>
NAUTGATE_AUDIT_INSTANCE_ID=<host name>
SB_ATTEST_PUBLIC_KEY_PEM="-----BEGIN PUBLIC KEY-----
...
-----END PUBLIC KEY-----"
SB_ATTEST_JWT=<TSB JWT>
```

`SB_ATTEST_INTERNAL_TOKEN` is only spoken between the core and the sidecar;
generate it, never reuse another token. The sidecar's database URL is derived
from `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` like the core's, or
set `SB_ATTEST_DB_URL` explicitly. Back the env file up first; stargate keeps
`profiles/stargate.env.before-<change>` copies.

Dry run before touching anything:

```console
docker compose --env-file profiles/<host>.env \
  -f compose.production.yml -f docker-compose.with-attestation.yml config \
  | grep -c "BEGIN PUBLIC"      # expect 2: sidecar and core
```

### 4. Launch

```console
docker compose --env-file profiles/<host>.env \
  -f compose.production.yml -f docker-compose.with-attestation.yml \
  up -d --build sb-attest nautgate-core
```

The sidecar starts first, migrates its own table, and reports
`sb-attest ready: key=NAUTGATE_AUDIT_KEY tsb=...`. The core is recreated once
its health check passes, so the gateway is gone for about twenty seconds.

### 5. Watch it catch up

`GET /v1/audit/status` (admin bearer) starts `critical` with a `signing_lag`
alert when the stack already holds receipts. That is the backlog, not a fault:
the signer stages 1000 receipts per checkpoint on a five-second tick and the
sandbox signs each in one to two seconds, so 100k old receipts take about ten
minutes. Done looks like this:

```json
{"total":9,"verified":9,"pending":0,"failed":0,"signing_lag_seconds":0,
 "open_gaps":0,"checkpoint_failures":0,"health":"healthy","alerts":[]}
```

`docker logs sb-attest` shows one `POST /v1/attest/checkpoint 200` per
checkpoint; the core logs `audit_checkpoint_sign` with `status: verified`.

### 6. Prove one fresh receipt end to end

Send any request through the gateway, take the newest receipt from
`GET /v1/audit/receipts?limit=1`, and poll `GET /v1/audit/receipts/{id}` until
`evidence_status` is `verified` (under a minute; the batch max age is 60 s).
Then export and verify offline on another machine with the public key and the
fingerprint from your own record, never the one inside the bundle:

```console
curl -H "Authorization: Bearer $TOKEN" $GATEWAY/v1/audit/receipts/$ID/bundle > evidence.json
nautgate receipt verify evidence.json --public-key audit-public.pem \
  --key-id NAUTGATE_AUDIT_KEY --fingerprint <64 hex>
```

Exit 0 and the claim line mean verified. Change one character in the bundle and
run it again; exit 2 with `receipt content hash mismatch` is the proof that the
verifier is doing its job.

### 7. Where it shows in the dashboard

Audit Log page: the evidence health card mirrors `/v1/audit/status`, and the
"Evidence receipts" section lists receipts with their state. Verified rows get
a "download bundle" button, which is the same export as step 6.

### JWT renewal

A sandbox JWT expires. When it does, the sidecar keeps answering `/health` but
checkpoints move to `failed` after the retry budget and `checkpoint_failures`
rises. Replace `SB_ATTEST_JWT` in the env file and run
`docker compose ... up -d sb-attest`; the signer retries the oldest staged
checkpoint first and nothing is restaged.
