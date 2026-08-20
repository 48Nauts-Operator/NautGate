# Offline evidence verification

An Evidence Bundle v1 proves one narrow claim: the disclosed decision receipt is
included in a checkpoint signed for NautGate's audit-checkpoint purpose, and none
of those disclosed bytes have changed. It does not prove that an omitted event
occurred, that captured content was truthful, or that a private key was never
misused.

Export a verified bundle from
`GET /v1/audit/receipts/{receipt_id}/bundle`, using the same bearer key that owns
the decision. The endpoint deliberately returns 404 for pending, failed, unknown,
and other-agent receipts.

Copy the JSON bundle, the applicable public key from the independently published
key history, and the `nautgate` executable onto an offline machine. Then run:

```console
nautgate receipt verify evidence.json \
  --public-key audit-public.pem \
  --key-id nautgate-attestation-v1 \
  --fingerprint <trusted-sha256-fingerprint>
```

Add `--json` for automation. Exit status `0` means verified, `2` means evidence
verification failed, and argparse retains its standard status `2` for invalid
command-line use. Verification fails closed for unknown schemas, algorithms,
keys, malformed hashes, invalid inclusion proofs, or invalid signatures.

Key history follows `signing-key-history.schema.json`. Pin its fingerprint from
an independent trusted channel; a key shipped only inside the evidence bundle
would let the bundle vouch for itself and provides no useful trust anchor.
