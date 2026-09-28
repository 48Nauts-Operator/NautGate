"""Human-readable rendering of decision receipts (NAUTGATE-66).

The canonical JSON bundle stays the proof; everything here is a view of it.
An auditor reads the report; the embedded bundle feeds `nautgate receipt
verify`. Client-controlled strings are escaped, never trusted.
"""

from __future__ import annotations

import html as _html


def _e(value) -> str:
    return _html.escape(str(value)) if value is not None else "—"


def _row(label: str, value) -> str:
    return f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>"


def render_receipt_report(receipt: dict, meta: dict) -> str:
    req = receipt.get("request") or {}
    sampling = req.get("sampling") or {}
    routing = receipt.get("routing") or {}
    result = receipt.get("result") or {}
    env = receipt.get("environment") or {}
    integrity = receipt.get("model_integrity") or {}
    runtime = receipt.get("runtime") or {}
    attested = bool(meta.get("attested"))
    badge = (
        '<span class="ok">ATTESTED</span>'
        if attested
        else '<span class="pending">NOT YET ATTESTED</span>'
    )
    verify_cmd = (
        f"nautgate receipt verify evidence-{_e(receipt.get('receipt_id'))}.json "
        "--public-key &lt;trusted-public-key.pem&gt;"
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>NautGate Decision Receipt {_e(receipt.get("receipt_id"))}</title>
<style>
body{{font:14px/1.5 -apple-system,system-ui,sans-serif;color:#16202a;margin:2rem auto;max-width:52rem;padding:0 1rem}}
h1{{font-size:1.3rem;margin-bottom:.2rem}} h2{{font-size:1rem;margin:1.4rem 0 .4rem;border-bottom:1px solid #cdd5db;padding-bottom:.2rem}}
table{{border-collapse:collapse;width:100%}} th{{text-align:left;width:14rem;font-weight:500;color:#54626f;padding:.18rem 0;vertical-align:top}}
td{{font-family:ui-monospace,Menlo,monospace;font-size:.85rem;word-break:break-all;padding:.18rem 0}}
.ok{{color:#186b3c;border:1px solid #186b3c;padding:2px 8px;font-size:.8rem}}
.pending{{color:#9a6b15;border:1px solid #9a6b15;padding:2px 8px;font-size:.8rem}}
.verify{{background:#f2f5f7;border:1px solid #cdd5db;padding:.8rem 1rem;font-family:ui-monospace,Menlo,monospace;font-size:.8rem}}
footer{{margin-top:2rem;color:#54626f;font-size:.78rem}}
@media print{{body{{margin:0 auto}}}}
</style></head><body>
<h1>NautGate Decision Receipt</h1>
<p>{badge} · schema {_e(receipt.get("schema"))} · sequence {_e(receipt.get("sequence"))}</p>

<h2>Transaction</h2>
<table>
{_row("Receipt", receipt.get("receipt_id"))}
{_row("Decision", receipt.get("decision_id"))}
{_row("Started", receipt.get("started_at"))}
{_row("Completed", receipt.get("completed_at"))}
{_row("Agent", (receipt.get("client") or {}).get("agent_id"))}
{_row("Protocol", (receipt.get("client") or {}).get("protocol"))}
{_row("Capture path", env.get("capture_path"))}
{_row("Harness", env.get("harness"))}
{_row("Sandbox", env.get("sandbox_id"))}
</table>

<h2>Model and routing</h2>
<table>
{_row("Requested model", req.get("requested_model"))}
{_row("Selected model", routing.get("selected_model"))}
{_row("Observed model", routing.get("observed_model"))}
{_row("Provider", routing.get("selected_provider"))}
{_row("Substituted", routing.get("substituted"))}
{_row("Weights digest", integrity.get("weights_digest"))}
{_row("Digest source", integrity.get("digest_source"))}
</table>

<h2>Sampling (determinism evidence)</h2>
<table>
{_row("temperature", sampling.get("temperature"))}
{_row("top_p", sampling.get("top_p"))}
{_row("seed", sampling.get("seed"))}
{_row("Provider fingerprint", result.get("provider_fingerprint"))}
</table>

<h2>Result</h2>
<table>
{_row("Status", f"{result.get('status')} (upstream {result.get('upstream_status')})")}
{_row("Finish reason", result.get("finish_reason"))}
{_row("Tokens in / out", f"{result.get('input_tokens')} / {result.get('output_tokens')}")}
{_row("Request body sha256", req.get("body_sha256"))}
{_row("Response sha256", result.get("response_sha256"))}
</table>

<h2>Attestation</h2>
<table>
{_row("Checkpoint", meta.get("checkpoint_id"))}
{_row("Signing key", meta.get("key_id"))}
{_row("Key fingerprint", meta.get("key_fingerprint"))}
{_row("NautGate version", runtime.get("nautgate_version"))}
{_row("Instance", runtime.get("instance_id"))}
</table>

<h2>Independent verification</h2>
<p class="verify">{verify_cmd}</p>
<footer>This report is a rendering of the canonical receipt. The receipt's canonical JSON,
not this page, is the signed evidence; verify it with the command above against a
public key obtained out of band.</footer>
</body></html>"""
