"""Auditor-facing PDF of a decision receipt (NAUTGATE-66).

The PDF is rendered from the same branded HTML template as the report page
(weasyprint applies the template's print stylesheet, so the PDF is the light
edition of exactly one design). The canonical evidence bundle rides inside as
an uncompressed pypdf attachment; a QR carries the verify link (or integrity
anchors when no public base URL is configured). The PDF is a rendering; the
embedded JSON stays the proof.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json

import segno

from app.audit_report import render_receipt_report


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


def _qr_data_uri(payload: str) -> str:
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="png", scale=4, border=1)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_receipt_pdf(
    receipt: dict, meta: dict, *, bundle_json: bytes, public_base_url: str | None = None
) -> bytes:
    from weasyprint import HTML  # lazy: needs system pango, loaded only when rendering

    html = render_receipt_report(
        receipt,
        meta,
        qr_data_uri=_qr_data_uri(
            qr_payload(receipt, meta, bundle_json, public_base_url=public_base_url)
        ),
    )
    pdf = HTML(string=html).write_pdf()

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.add_attachment(f"evidence-{receipt.get('receipt_id')}.json", bundle_json)
    writer.add_metadata({"/Subject": f"key_fingerprint={meta.get('key_fingerprint')}"})
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
