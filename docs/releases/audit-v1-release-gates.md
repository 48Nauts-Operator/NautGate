# Verified Audit Trail v1 release gates

Date: 2026-08-20

## Automated matrix

- [x] Canonicalization, receipt hash, checkpoint payload, and Merkle vectors
  match published fixtures.
- [x] The core and `sb-attest` independently produce the published checkpoint
  payload bytes.
- [x] Success, upstream failure, streaming, Anthropic OAuth, ChatGPT OAuth, and
  proxy-ingest paths allocate the client-visible receipt ID before forwarding
  and persist that same ID with the outcome.
- [x] Receipt, proof, checkpoint, signature, signing key, fingerprint, schema,
  and algorithm mutations fail closed.
- [x] TSB outage retries the identical checkpoint; recovery verifies it once;
  exhausted retries never verify receipts.
- [x] Concurrent/duplicate staging uses deterministic checkpoint identity and
  conflict-safe insertion.
- [x] Worker staging, receipt updates, proof storage, and outbox deletion share
  one transaction; crash injection cannot reach the outbox delete.
- [x] Sequence gaps stop checkpoint creation and create a visible gap record.
- [x] Key rotation changes checkpoint identity; a key ID cannot be rebound to
  different public material.
- [x] Secret prompt, tool arguments, `ng_` bearer, provider-key prefixes, and
  private key material are absent from portable evidence.
- [x] A subprocess with no service credentials or network configuration verifies
  a bundle from only its JSON file, trusted public key, and local executable.
- [x] Core lint/tests, sidecar lint/tests, Compose rendering, and dashboard
  JavaScript syntax checks pass.

## Security and product-claim review

The threat model in `docs/specs/audit-v1/THREAT-MODEL.md` was reviewed against
the implementation. Required mitigations are implemented. Out-of-scope items
remain explicitly accepted for v1 and are repeated in the UI, verifier output,
release notes, and operator runbook. No endpoint describes pending evidence as
attested. Other-agent receipt lookup and bundle export use the same 404 response.
Public verification keys are retained by immutable key ID; private key material
never belongs in NautGate.

## Go-live checklist

Repository smoke completed successfully on 2026-08-20: isolated images built,
stack health/readiness/dashboard passed, first-run authentication and rejection
paths passed, backup/restore passed, and provider credentials remained encrypted.
The production attestation Compose overlay also rendered successfully with
placeholder non-secret configuration.

- [ ] Set TSB URL, signing key ID, authentication, internal sidecar token,
  public key PEM, and independently obtained SHA-256 SPKI fingerprint.
- [ ] Run `extensions/sb-attest/scripts/live_smoke.py` with
  `SB_ATTEST_LIVE=1`; it must sign a typed checkpoint and verify locally.
- [x] Render the production Compose overlay and confirm generic signing is
  disabled (`SB_ATTEST_PRODUCTION=true`).
- [ ] Start with a one-receipt batch, observe `pending → batched → verified`,
  export its bundle, disconnect the network, and run the offline verifier with
  the independently pinned fingerprint.
- [ ] Stop TSB, create traffic, confirm the lag alert and durable backlog, then
  restore TSB and confirm catch-up without changed checkpoint IDs or ranges.
- [ ] Verify another tenant cannot read receipt metadata or export a bundle.
- [ ] Record the key fingerprint, first checkpoint ID, test bundle, CI run URL,
  and operator approval in the release record.

The live TSB gate cannot be represented by repository unit tests and must not be
checked without deployment credentials. Absence of credentials is an explicit
unexecuted external gate, never a silent pass.
