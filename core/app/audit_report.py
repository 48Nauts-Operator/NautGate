"""Human-readable rendering of decision receipts (NAUTGATE-66).

Uses the baseline NautGate public-report template (the one published under
nautgate.dev assets/reports): olive brand bar, wordmark header with logo,
hero + badges + metrics, sectioned body, print stylesheet that flips to a
light edition. The canonical JSON bundle stays the proof; everything here is
a view of it. Client-controlled strings are escaped, never trusted.
"""

from __future__ import annotations

import html as _html
from datetime import UTC, datetime

_LOGO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="30" height="30" '
    'role="img" aria-label="NautGate"><rect width="32" height="32" rx="7" fill="#808000"/>'
    '<rect x="4.5" y="8.5" width="23" height="6.6" rx="1" fill="#0A0B00"/>'
    '<rect x="7.5" y="19.8" width="5" height="5" rx="0.9" fill="#0A0B00"/>'
    '<rect x="13.5" y="19.8" width="5" height="5" rx="0.9" fill="#0A0B00"/>'
    '<rect x="19.5" y="19.8" width="5" height="5" rx="0.9" fill="#0A0B00"/></svg>'
)

# Tokens and structure lifted from assets/reports (public-report baseline).
_STYLE = """<style>
:root{color-scheme:dark;--ink:#E6EBF2;--muted:#8893A4;--line:#232B36;--accent:#C3CE1F;--paper:#12161F;--soft:#1A2029;--bg:#0A0D12;--brand:#808000;--good:#3FB950;--warn:#D6A100;--bad:#E5484D}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:980px;margin:32px auto;background:var(--paper);box-shadow:0 8px 40px #20334012}
header{padding:26px 48px;background:var(--bg);border-top:4px solid var(--brand);display:flex;justify-content:space-between;align-items:center}
.brandwrap{display:flex;align-items:center;gap:14px}
.brand{font-size:24px;font-weight:750;letter-spacing:-.6px}
.eyebrow{font-size:11px;letter-spacing:1.8px;text-transform:uppercase;font-weight:650}
header .eyebrow{color:var(--accent)}
.stamp{font-size:12px;color:var(--muted);text-align:right}
.hero,section{padding:34px 48px}.hero{padding-bottom:26px}
h1{font-size:36px;line-height:1.14;letter-spacing:-1.2px;margin:10px 0 12px}
h1.ok{color:var(--good)}h1.pending{color:var(--warn)}h1.bad{color:var(--bad)}
.lede{font-size:16px;color:var(--muted);max-width:760px}
.badges{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}
.badge{font-size:12px;font-weight:650;border:1px solid var(--line);border-radius:4px;padding:5px 10px;background:var(--soft)}
.metrics{display:grid;grid-template-columns:1fr 1fr 1fr;border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-top:26px}
.metric{padding:16px 12px 16px 0}.metric b{display:block;font-size:16px;margin-top:5px;overflow-wrap:anywhere}
.metric span{color:var(--muted);font-size:12px}
section{border-top:1px solid var(--line)}
h2{font-size:21px;line-height:1.3;margin:0 0 16px;letter-spacing:-.4px}
dl{display:grid;grid-template-columns:200px 1fr;gap:10px;font-size:13px}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--bg);border-left:3px solid var(--brand);padding:14px;margin:14px 0 0}
.note{font-size:13px;color:var(--muted)}
footer{padding:22px 48px;background:var(--bg);font-size:12px;color:var(--muted)}
.print-tip{float:right}
@media (max-width:700px){main{margin:0}header,.hero,section,footer{padding:24px}h1{font-size:28px}.metrics{grid-template-columns:1fr}.metric{border-bottom:1px solid var(--line)}.stamp,.print-tip{display:none}dl{grid-template-columns:1fr;gap:4px}dd{margin-bottom:12px}}
@media print{:root{color-scheme:light;--ink:#172331;--muted:#576674;--line:#dce3e7;--accent:#626800;--paper:#fff;--soft:#f3f4f0;--bg:#fff;--good:#186b3c;--warn:#8a5d00;--bad:#a3403c}
body{background:white;font-size:11px}main{max-width:none;margin:0;box-shadow:none}
header{background:white;color:var(--ink);border-bottom:2px solid var(--ink)}
.hero,section{padding:22px 28px}h1{font-size:26px}h2{break-after:avoid}.print-tip{display:none}
pre{font-size:10px}footer{padding:14px 28px}}
</style>"""


def _e(value) -> str:
    return _html.escape(str(value)) if value is not None else "—"


def _dl(pairs: list[tuple[str, object]]) -> str:
    return "<dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in pairs) + "</dl>"


def _shell(*, eyebrow: str, stamp: str, body: str, module: str) -> str:
    now = datetime.now(UTC).strftime("%d %B %Y %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NautGate · {_e(eyebrow)}</title>
{_STYLE}</head><body><main>
<header><div class="brandwrap">{_LOGO}<div><div class="brand">NautGate</div>
<div class="eyebrow">{_e(eyebrow)}</div></div></div>
<div class="stamp">{stamp}</div></header>
{body}
<footer>NautGate · memory-aware LLM gateway · {_e(module)} · generated {now}
<span class="print-tip">Browser Print → Save as PDF</span></footer>
</main></body></html>"""


def render_receipt_report(receipt: dict, meta: dict, *, qr_data_uri: str | None = None) -> str:
    req = receipt.get("request") or {}
    sampling = req.get("sampling") or {}
    routing = receipt.get("routing") or {}
    result = receipt.get("result") or {}
    env = receipt.get("environment") or {}
    integrity = receipt.get("model_integrity") or {}
    runtime = receipt.get("runtime") or {}
    attested = bool(meta.get("attested"))
    h1_class = "ok" if attested else "pending"
    headline = "Attested decision receipt" if attested else "Decision receipt · NOT YET ATTESTED"
    ok = result.get("status") == "success"
    badges = "".join(
        f'<span class="badge">{_e(b)}</span>'
        for b in (
            "ATTESTED" if attested else "PENDING",
            f"capture: {env.get('capture_path')}",
            f"protocol: {(receipt.get('client') or {}).get('protocol')}",
            "success" if ok else f"error {result.get('upstream_status')}",
        )
    )
    body = f"""
<div class="hero">
  {f'<img src="{qr_data_uri}" alt="verification QR" style="float:right;width:112px;height:112px;image-rendering:pixelated;border:6px solid #fff;border-radius:4px">' if qr_data_uri else ""}
  <div class="eyebrow">Verified Audit Trail</div>
  <h1 class="{h1_class}">{_e(headline)}</h1>
  <p class="lede">Cryptographic record of one model decision: what was asked, which model
  answered under which settings, in which environment, signed into the hardware-backed
  audit chain.</p>
  <div class="badges">{badges}</div>
  <div class="metrics">
    <div class="metric"><span>Model observed</span><b>{_e(routing.get("observed_model") or routing.get("selected_model"))}</b></div>
    <div class="metric"><span>Evidence sequence</span><b>{_e(receipt.get("sequence"))}</b></div>
    <div class="metric"><span>Completed</span><b>{_e(receipt.get("completed_at"))}</b></div>
  </div>
</div>

<section><h2>Transaction</h2>{_dl([
    ("Receipt", receipt.get("receipt_id")),
    ("Decision", receipt.get("decision_id")),
    ("Started", receipt.get("started_at")),
    ("Completed", receipt.get("completed_at")),
    ("Agent", (receipt.get("client") or {}).get("agent_id")),
    ("Harness", env.get("harness")),
    ("Sandbox", env.get("sandbox_id")),
    ("Capture path", env.get("capture_path")),
])}</section>

<section><h2>Model and routing</h2>{_dl([
    ("Requested model", req.get("requested_model")),
    ("Selected model", routing.get("selected_model")),
    ("Observed model", routing.get("observed_model")),
    ("Provider", routing.get("selected_provider")),
    ("Substituted", routing.get("substituted")),
    ("Weights digest", integrity.get("weights_digest")),
    ("Digest source", integrity.get("digest_source")),
])}</section>

<section><h2>Sampling · determinism evidence</h2>{_dl([
    ("temperature", sampling.get("temperature")),
    ("top_p", sampling.get("top_p")),
    ("seed", sampling.get("seed")),
    ("Provider fingerprint", result.get("provider_fingerprint")),
])}</section>

<section><h2>Result</h2>{_dl([
    ("Status", f"{result.get('status')} (upstream {result.get('upstream_status')})"),
    ("Finish reason", result.get("finish_reason")),
    ("Tokens in / out", f"{result.get('input_tokens')} / {result.get('output_tokens')}"),
    ("Request body sha256", req.get("body_sha256")),
    ("Response sha256", result.get("response_sha256")),
])}</section>

<section><h2>Attestation</h2>{_dl([
    ("Checkpoint", meta.get("checkpoint_id")),
    ("Signing key", meta.get("key_id")),
    ("Key fingerprint", meta.get("key_fingerprint")),
    ("NautGate version", runtime.get("nautgate_version")),
    ("Instance", runtime.get("instance_id")),
])}
<h2 style="margin-top:26px">Independent verification</h2>
<pre>nautgate receipt verify evidence-{_e(receipt.get("receipt_id"))}.json --public-key &lt;trusted-public-key.pem&gt;</pre>
<p class="note">This report is a rendering of the canonical receipt. The receipt's canonical
JSON, not this page, is the signed evidence; verify it with the command above against a
public key obtained out of band.</p></section>
"""
    return _shell(
        eyebrow="Verified Audit Trail · Decision receipt",
        stamp=f"{_e(receipt.get('completed_at'))}<br>Signed evidence edition",
        body=body,
        module="verified audit trail",
    )


def render_verify_verdict(*, ok: bool, receipt_id: str, detail: dict) -> str:
    h1_class = "ok" if ok else "bad"
    headline = "VERIFIED" if ok else "NOT VERIFIED"
    body = f"""
<div class="hero">
  <div class="eyebrow">Verified Audit Trail · Receipt verification</div>
  <h1 class="{h1_class}">{headline}</h1>
  <p class="lede">Signature check of receipt {_e(receipt_id)} against this instance's
  registered hardware-backed signing key.</p>
</div>
<section>{_dl(list(detail.items()))}
<p class="note" style="margin-top:20px">Server-side check by the issuing NautGate instance.
For trust-grade verification run <span style="font-family:ui-monospace,monospace">nautgate
receipt verify</span> against the evidence bundle with a public key obtained out of band.</p>
</section>
"""
    return _shell(
        eyebrow="Verified Audit Trail · Receipt verification",
        stamp=datetime.now(UTC).strftime("%d %B %Y"),
        body=body,
        module="verified audit trail",
    )
