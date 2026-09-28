"""Auditor-facing PDF of a decision receipt (NAUTGATE-66).

One document, two audiences: the page carries the transaction facts for a
human; the canonical evidence bundle is embedded as an uncompressed file
attachment for `nautgate receipt verify`; a QR carries receipt id, canonical
hash and signing-key fingerprint for a phone-side integrity check. The PDF is
a rendering; the embedded JSON stays the proof.
"""

from __future__ import annotations

import hashlib
import io
import json

import segno
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def qr_payload(
    receipt: dict, meta: dict, bundle_json: bytes, *, public_base_url: str | None
) -> str:
    """Verify link when the instance has a reachable base URL; anchors otherwise.

    The hash prefix in the link is proof-of-possession: scanning grants the
    verdict page, never receipt content.
    """
    digest = hashlib.sha256(bundle_json).hexdigest()
    if public_base_url:
        base = public_base_url.rstrip("/")
        return f"{base}/v1/audit/receipts/{receipt.get('receipt_id')}/verify?h={digest[:16]}"
    return json.dumps(
        {
            "receipt_id": receipt.get("receipt_id"),
            "bundle_sha256": digest,
            "key_fingerprint": meta.get("key_fingerprint"),
        },
        separators=(",", ":"),
    )


def build_receipt_pdf(
    receipt: dict, meta: dict, *, bundle_json: bytes, public_base_url: str | None = None
) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=0)
    c.setTitle(f"NautGate Decision Receipt {receipt.get('receipt_id')}")
    c.setSubject(f"key_fingerprint={meta.get('key_fingerprint')}")
    width, height = A4
    x, y = 20 * mm, height - 25 * mm

    def line(label: str, value, dy: float = 6.2) -> None:
        nonlocal y
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.33, 0.38, 0.43)
        c.drawString(x, y, label)
        c.setFont("Courier", 8.5)
        c.setFillColorRGB(0.09, 0.13, 0.16)
        c.drawString(x + 48 * mm, y, "—" if value is None else str(value))
        y -= dy * mm / 2.5

    def section(title: str) -> None:
        nonlocal y
        y -= 3 * mm
        c.setFont("Helvetica-Bold", 10.5)
        c.setFillColorRGB(0.09, 0.13, 0.16)
        c.drawString(x, y, title)
        y -= 5.5 * mm / 1.2

    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.76, 0.25, 0.05)
    c.drawString(x, y + 5 * mm, "NAUTGATE · DECISION RECEIPT")
    c.setFillColorRGB(0.09, 0.13, 0.16)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(x, y, "Decision Receipt")
    y -= 6 * mm
    c.setFont("Helvetica", 9)
    status = "ATTESTED" if meta.get("attested") else "NOT YET ATTESTED"
    c.drawString(x, y, f"{status} · {receipt.get('schema')} · sequence {receipt.get('sequence')}")
    y -= 4 * mm

    # QR: integrity anchors for a phone-side check against the printed page.
    qr = segno.make(qr_payload(receipt, meta, bundle_json, public_base_url=public_base_url), error="m")
    qr_buf = io.BytesIO()
    qr.save(qr_buf, kind="png", scale=3, border=1)
    qr_buf.seek(0)
    from reportlab.lib.utils import ImageReader

    c.drawImage(
        ImageReader(qr_buf), width - 52 * mm, height - 58 * mm, 32 * mm, 32 * mm, mask="auto"
    )

    req = receipt.get("request") or {}
    sampling = req.get("sampling") or {}
    routing = receipt.get("routing") or {}
    result = receipt.get("result") or {}
    env = receipt.get("environment") or {}
    integrity = receipt.get("model_integrity") or {}

    section("Transaction")
    line("Receipt", receipt.get("receipt_id"))
    line("Decision", receipt.get("decision_id"))
    line("Started", receipt.get("started_at"))
    line("Completed", receipt.get("completed_at"))
    line("Agent", (receipt.get("client") or {}).get("agent_id"))
    line("Capture path", env.get("capture_path"))
    line("Harness", env.get("harness"))

    section("Model and routing")
    line("Requested model", req.get("requested_model"))
    line("Selected model", routing.get("selected_model"))
    line("Observed model", routing.get("observed_model"))
    line("Substituted", routing.get("substituted"))
    line("Weights digest", integrity.get("weights_digest"))
    line("Digest source", integrity.get("digest_source"))

    section("Sampling (determinism evidence)")
    line("temperature", sampling.get("temperature"))
    line("top_p", sampling.get("top_p"))
    line("seed", sampling.get("seed"))
    line("Provider fingerprint", result.get("provider_fingerprint"))

    section("Result")
    line("Status", f"{result.get('status')} (upstream {result.get('upstream_status')})")
    line("Tokens in / out", f"{result.get('input_tokens')} / {result.get('output_tokens')}")
    line("Response sha256", result.get("response_sha256"))

    section("Attestation")
    line("Checkpoint", meta.get("checkpoint_id"))
    line("Signing key", meta.get("key_id"))
    line("Key fingerprint", meta.get("key_fingerprint"))

    section("Independent verification")
    c.setFont("Courier", 8)
    c.drawString(x, y, "Extract the attached evidence bundle, then:")
    y -= 4 * mm
    c.drawString(x, y, "  nautgate receipt verify <bundle.json> --public-key <trusted.pem>")
    y -= 6 * mm
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(x, y, "This page is a rendering. The embedded canonical JSON is the signed evidence.")

    c.showPage()
    c.save()

    # reportlab 5 has no attachment support; pypdf adds the bundle as an
    # embedded file so any standard PDF tool can extract it byte-identical.
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(buf.getvalue())))
    writer.add_attachment(f"evidence-{receipt.get('receipt_id')}.json", bundle_json)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_evidence_package(
    receipt: dict, meta: dict, *, bundle_json: bytes, public_base_url: str | None = None
) -> bytes:
    """One auditor-ready zip: branded PDF, canonical bundle, verify instructions."""
    import zipfile

    rid = receipt.get("receipt_id")
    pdf = build_receipt_pdf(receipt, meta, bundle_json=bundle_json, public_base_url=public_base_url)
    instructions = (
        "NautGate evidence package\n"
        f"Receipt: {rid}\n\n"
        f"The signed evidence is evidence-{rid}.json (canonical, byte-exact).\n"
        "Verify it independently, without trusting NautGate:\n\n"
        f"  nautgate receipt verify evidence-{rid}.json --public-key <trusted-public-key.pem>\n\n"
        "Obtain the public key out of band (not from this package).\n"
        "The PDF is a human-readable rendering and also embeds the same JSON as an attachment.\n"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"nautgate-receipt-{rid}.pdf", pdf)
        # Stored uncompressed: the bundle must stay byte-identical and obvious.
        zf.writestr(zipfile.ZipInfo(f"evidence-{rid}.json"), bundle_json)
        zf.writestr("VERIFY.txt", instructions)
    return out.getvalue()
