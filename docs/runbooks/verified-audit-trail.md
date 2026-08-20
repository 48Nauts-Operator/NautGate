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
