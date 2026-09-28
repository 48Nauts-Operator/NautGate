"""Human-readable rendering of decision receipts (NAUTGATE-66).

Uses the baseline NautGate report template (the drift-investigator design:
#0a0a0a ground, #c2410c brand accent, brand header, hero, findings tables,
branded footer). The canonical JSON bundle stays the proof; everything here
is a view of it. Client-controlled strings are escaped, never trusted.
"""

from __future__ import annotations

import html as _html
from datetime import UTC, datetime

# Baseline NG report tokens, lifted from the drift-investigator template.
_STYLE = """<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 0; background: #0a0a0a; color: #e8e8e8;
    font-family: -apple-system, "SF Pro Display", "Helvetica Neue", Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased; }
  .page { max-width: 880px; margin: 0 auto; padding: 48px 56px 36px; }
  header.brand { display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 1px solid #222; padding-bottom: 16px; margin-bottom: 32px; }
  .brand-title { font-size: 13px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #c2410c; }
  .brand-meta { color: #666; font-size: 13px; }
  .hero-eyebrow { font-size: 11px; letter-spacing: 0.15em; text-transform: uppercase; color: #777; }
  .hero-title { font-size: 34px; margin: 6px 0 4px; font-weight: 700; }
  .hero-title.ok { color: #4caf50; } .hero-title.pending { color: #f0b132; }
  .hero-title.bad { color: #ff5c5c; }
  .hero-sub { color: #999; font-size: 15px; margin: 0 0 8px; }
  h2 { font-size: 11px; font-weight: 500; letter-spacing: 0.15em; text-transform: uppercase;
    color: #777; margin: 34px 0 6px; }
  table.findings { border-collapse: collapse; width: 100%; }
  table.findings td { padding: 10px 12px 10px 0; border-bottom: 1px solid #1a1a1a;
    vertical-align: middle; }
  table.findings tr:last-child td { border-bottom: none; }
  td.k { color: #666; width: 16rem; font-size: 14px; }
  td.v { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 13px;
    word-break: break-all; }
  .muted { color: #666; }
  .verifybox { background: #111; border: 1px solid #222; border-left: 3px solid #c2410c;
    padding: 14px 18px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 13px; }
  footer { display: flex; justify-content: space-between; margin-top: 44px; color: #666;
    font-size: 12px; border-top: 1px solid #222; padding-top: 14px; }
  .nautgate-mark strong { color: #e8e8e8; }
  @media print { body { background: #fff; color: #111; } }
</style>"""


def _e(value) -> str:
    return _html.escape(str(value)) if value is not None else "—"


def _row(label: str, value) -> str:
    return f'<tr><td class="k">{_e(label)}</td><td class="v">{_e(value)}</td></tr>'


def _shell(*, report_name: str, meta_line: str, body: str, module: str) -> str:
    now = datetime.now(UTC).strftime("%b %d, %Y %H:%M UTC")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>NautGate {_e(report_name)}</title>
{_STYLE}</head><body>
<div class="page">
  <header class="brand">
    <div class="brand-title">NautGate · {_e(report_name)}</div>
    <div class="brand-meta">{_e(meta_line)}</div>
  </header>
{body}
  <footer>
    <div class="nautgate-mark"><strong>NautGate</strong> · memory-aware LLM gateway · {_e(module)}</div>
    <div>Generated {now}</div>
  </footer>
</div>
</body></html>"""


def render_receipt_report(receipt: dict, meta: dict) -> str:
    req = receipt.get("request") or {}
    sampling = req.get("sampling") or {}
    routing = receipt.get("routing") or {}
    result = receipt.get("result") or {}
    env = receipt.get("environment") or {}
    integrity = receipt.get("model_integrity") or {}
    runtime = receipt.get("runtime") or {}
    attested = bool(meta.get("attested"))
    hero_class = "ok" if attested else "pending"
    hero = "ATTESTED" if attested else "NOT YET ATTESTED"
    body = f"""
  <section class="hero">
    <div class="hero-eyebrow">Verified Audit Trail · Decision Receipt</div>
    <h1 class="hero-title {hero_class}">{hero}</h1>
    <p class="hero-sub">receipt {_e(receipt.get("receipt_id"))} · sequence {_e(receipt.get("sequence"))} · {_e(receipt.get("schema"))}</p>
  </section>

  <h2>Transaction</h2>
  <table class="findings">
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
  <table class="findings">
{_row("Requested model", req.get("requested_model"))}
{_row("Selected model", routing.get("selected_model"))}
{_row("Observed model", routing.get("observed_model"))}
{_row("Provider", routing.get("selected_provider"))}
{_row("Substituted", routing.get("substituted"))}
{_row("Weights digest", integrity.get("weights_digest"))}
{_row("Digest source", integrity.get("digest_source"))}
  </table>

  <h2>Sampling · determinism evidence</h2>
  <table class="findings">
{_row("temperature", sampling.get("temperature"))}
{_row("top_p", sampling.get("top_p"))}
{_row("seed", sampling.get("seed"))}
{_row("Provider fingerprint", result.get("provider_fingerprint"))}
  </table>

  <h2>Result</h2>
  <table class="findings">
{_row("Status", f"{result.get('status')} (upstream {result.get('upstream_status')})")}
{_row("Finish reason", result.get("finish_reason"))}
{_row("Tokens in / out", f"{result.get('input_tokens')} / {result.get('output_tokens')}")}
{_row("Request body sha256", req.get("body_sha256"))}
{_row("Response sha256", result.get("response_sha256"))}
  </table>

  <h2>Attestation</h2>
  <table class="findings">
{_row("Checkpoint", meta.get("checkpoint_id"))}
{_row("Signing key", meta.get("key_id"))}
{_row("Key fingerprint", meta.get("key_fingerprint"))}
{_row("NautGate version", runtime.get("nautgate_version"))}
{_row("Instance", runtime.get("instance_id"))}
  </table>

  <h2>Independent verification</h2>
  <p class="verifybox">nautgate receipt verify evidence-{_e(receipt.get("receipt_id"))}.json --public-key &lt;trusted-public-key.pem&gt;</p>
  <p class="muted" style="font-size:12px">This report is a rendering of the canonical receipt. The receipt's canonical JSON,
  not this page, is the signed evidence; verify it with the command above against a public key obtained out of band.</p>
"""
    return _shell(
        report_name="Decision Receipt",
        meta_line=str(receipt.get("completed_at") or ""),
        body=body,
        module="verified audit trail",
    )


def render_verify_verdict(*, ok: bool, receipt_id: str, detail: dict) -> str:
    hero_class = "ok" if ok else "bad"
    hero = "VERIFIED" if ok else "NOT VERIFIED"
    rows = "".join(_row(k, v) for k, v in detail.items())
    body = f"""
  <section class="hero">
    <div class="hero-eyebrow">Verified Audit Trail · Receipt Verification</div>
    <h1 class="hero-title {hero_class}">{hero}</h1>
    <p class="hero-sub">receipt {_e(receipt_id)}</p>
  </section>
  <table class="findings">{rows}</table>
  <p class="muted" style="font-size:12px;margin-top:20px">Server-side check by this NautGate instance.
  For trust-grade verification run nautgate receipt verify against the evidence bundle with a
  public key obtained out of band.</p>
"""
    return _shell(
        report_name="Receipt Verification",
        meta_line=datetime.now(UTC).strftime("%b %d, %Y"),
        body=body,
        module="verified audit trail",
    )
