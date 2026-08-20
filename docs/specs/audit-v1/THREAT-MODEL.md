# Verified Audit Trail v1 threat model

## Protected claims

- A disclosed canonical receipt has not changed since checkpoint signing.
- Its receipt hash was included at a specific position in a signed Merkle root.
- Checkpoint ranges cannot be reordered or replaced without breaking their chain.
- Signing key and schema changes are explicit and verifier-visible.

## In scope

- Database-row mutation after receipt creation
- Receipt insertion, deletion, reordering, or substitution
- Proof or bundle tampering
- Wrong signing key or algorithm
- TSB response corruption
- Signing outage and silent backlog
- Cross-protocol signature confusion
- Credential leakage into exported evidence

## Out of scope for v1

- A compromised NautGate process lying before it constructs a receipt
- A provider lying about its observed model
- Tool execution outside an authenticated NautGate/xNaut evidence path
- Trusted legal time
- Availability after every copy of a bundle/checkpoint is deleted
- Legal or regulatory compliance as a whole

## Required mitigations

- Domain separation, canonical schemas, safe integers, and public vectors
- Transactional receipt/outbox persistence and monotonic sequences
- Contiguous-range enforcement and explicit gap declarations
- Dedicated TSB key with pinned public-key fingerprint
- Local verification of every returned signature
- Offline verifier that fails closed
- Credential-exclusion regression tests
- Metrics and alerts for lag, gaps, key mismatch, and verification failure

## Approved claim

> NautGate provides hardware-signed, tamper-evident routing receipts that can be
> independently verified without trusting the live NautGate database.
