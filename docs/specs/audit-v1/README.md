# NautGate Verified Audit Trail v1

This directory is the normative byte-level contract for issue #68. Production
receipt capture, Merkle workers, TSB signing, exports, and independent verifiers
must conform to it.

## Canonical profile

Artifacts use RFC 8785 JSON Canonicalization Scheme with a deliberately strict
profile: JSON strings, booleans, null, arrays, objects, and integers in
`[-9007199254740991, 9007199254740991]`. Floats are forbidden. Monetary values
use integer micro-USD. Object names sort by UTF-16 code units. Strings are not
Unicode-normalized; producers must preserve their scalar values exactly.

All artifacts are UTF-8. Unknown schema versions fail closed.

## Domain-separated hashes

```text
receipt_hash = SHA256("NAUTGATE-DECISION-RECEIPT-V1\0" || JCS(receipt))
leaf         = SHA256("NAUTGATE-MERKLE-LEAF-V1\0" || receipt_hash)
parent       = SHA256("NAUTGATE-MERKLE-NODE-V1\0" || left || right)
TSB payload  = "NAUTGATE-AUDIT-CHECKPOINT-V1\0" || JCS(checkpoint)
```

At each Merkle level, an unpaired final node is promoted unchanged. A tree may
not be empty. Receipt order is the durable ascending `sequence` order.

The HSM signs the full domain-separated checkpoint payload, not a naked Merkle
root. This binds the range, count, instance, prior checkpoint, schema, and key
identity to the signature.

## Privacy boundary

Receipts contain hashes and operational metadata, not prompt or response text.
Authorization headers, API keys, OAuth tokens, cookies, and hashes derived from
credentials are forbidden. Hashing is not anonymization; receipt access and
retention still require privacy controls.

## Proof boundary

A valid bundle proves that the disclosed receipt was included in the signed
checkpoint and has not changed. It does not independently prove that a provider
physically executed particular model weights, that an unobserved tool ran, that
the host was uncompromised, or that the operator meets a legal framework.

## Files

- `decision-receipt.schema.json` — recorded routing claim
- `audit-checkpoint.schema.json` — Merkle range submitted to TSB
- `evidence-bundle.schema.json` — portable selective-disclosure proof
- `../../fixtures/audit-v1/vectors.json` — public golden test vectors

The executable reference implementation is `core/app/audit_evidence.py`.
