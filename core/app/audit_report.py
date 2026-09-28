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
body{{background:#0A0D12;color:#E6EBF2;font:13px/1.55 ui-monospace,"SF Mono",Menlo,monospace;margin:0;padding:32px}}
.wrap{{max-width:760px;margin:0 auto}}
h1{{font-size:16px;color:#C3CE1F;margin:0 0 2px}}
.sub{{color:#8893A4;font-size:11px;margin-bottom:20px}}
h2{{font-size:12px;color:#8893A4;text-transform:uppercase;letter-spacing:.08em;margin:22px 0 8px;border-bottom:1px solid #232B36;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%}}
th{{text-align:left;width:15rem;color:#5C6675;font-weight:400;padding:3px 0;vertical-align:top;border-bottom:1px dotted #232B36}}
td{{font-size:12px;word-break:break-all;padding:3px 0;border-bottom:1px dotted #232B36}}
.ok{{color:#3FB950;border:1px solid #3FB950;border-radius:10px;padding:2px 8px;font-size:11px}}
.pending{{color:#D6A100;border:1px solid #D6A100;border-radius:10px;padding:2px 8px;font-size:11px}}
.verify{{background:#12161F;border:1px solid #232B36;border-radius:6px;padding:10px 14px;font-size:12px}}
footer{{margin-top:28px;color:#5C6675;font-size:10px;border-top:1px solid #232B36;padding-top:8px}}
@media print{{body{{background:#fff;color:#16202a}}h1{{color:#5f6b00}}}}
</style></head><body>
<div class="wrap">
<h1>NautGate · Decision receipt</h1>
<p class="sub">{badge} · schema {_e(receipt.get("schema"))} · sequence {_e(receipt.get("sequence"))}</p>

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
</div></body></html>"""


_VERDICT_STYLE = """<style>
body{background:#0A0D12;color:#E6EBF2;font:13px/1.55 ui-monospace,"SF Mono",Menlo,monospace;margin:0;padding:32px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:16px;color:#C3CE1F;margin:0 0 2px}
.sub{color:#8893A4;font-size:11px;margin-bottom:20px}
h2{font-size:12px;color:#8893A4;text-transform:uppercase;letter-spacing:.08em;margin:22px 0 8px;border-bottom:1px solid #232B36;padding-bottom:4px}
table{border-collapse:collapse;width:100%}
th{text-align:left;width:15rem;color:#5C6675;font-weight:400;padding:3px 0;vertical-align:top;border-bottom:1px dotted #232B36}
td{font-size:12px;word-break:break-all;padding:3px 0;border-bottom:1px dotted #232B36}
.ok{color:#3FB950;border:1px solid #3FB950;border-radius:10px;padding:2px 8px;font-size:11px}
.pending{color:#D6A100;border:1px solid #D6A100;border-radius:10px;padding:2px 8px;font-size:11px}
.verify{background:#12161F;border:1px solid #232B36;border-radius:6px;padding:10px 14px;font-size:12px}
footer{margin-top:28px;color:#5C6675;font-size:10px;border-top:1px solid #232B36;padding-top:8px}
@media print{body{background:#fff;color:#16202a}h1{color:#5f6b00}}
</style>"""


def render_verify_verdict(*, ok: bool, receipt_id: str, detail: dict) -> str:
    state = '<span class="ok">VERIFIED</span>' if ok else '<span class="pending">NOT VERIFIED</span>'
    rows = "".join(_row(k, v) for k, v in detail.items())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>NautGate Receipt Verification</title>
{_VERDICT_STYLE}</head><body><div class="wrap">
<h1>NautGate · Receipt verification</h1>
<p class="sub">{state} · receipt {_e(receipt_id)}</p>
<table>{rows}</table>
<footer>Server-side check by this NautGate instance. For trust-grade verification run
nautgate receipt verify against the evidence bundle with a public key obtained out of band.</footer>
</div></body></html>"""
