import io
import json

from pypdf import PdfReader

from app.audit_report_pdf import build_receipt_pdf
from tests.test_audit_report import META, RECEIPT

BUNDLE = json.dumps({"receipt": RECEIPT, "proof": ["ab" * 32], "checkpoint": {}}).encode()


def test_pdf_renders_and_embeds_the_evidence_bundle():
    pdf = build_receipt_pdf(RECEIPT, META, bundle_json=BUNDLE)
    assert pdf.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(pdf))
    name = f"evidence-{RECEIPT['receipt_id']}.json"
    assert name in reader.attachments
    assert reader.attachments[name][0] == BUNDLE  # byte-identical, still verifiable


def test_qr_is_a_verify_link_when_a_public_base_url_is_configured():
    import hashlib

    from app.audit_report_pdf import qr_payload

    url = qr_payload(RECEIPT, META, BUNDLE, public_base_url="https://ng.example.ch")
    assert url.startswith(
        "https://ng.example.ch/v1/audit/receipts/88328fd3-dd5f-4e39-ad9d-c337ee421e29/verify?h="
    )
    assert url.endswith(hashlib.sha256(BUNDLE).hexdigest()[:16])


def test_qr_falls_back_to_integrity_anchors_without_a_base_url():
    import json as _json

    from app.audit_report_pdf import qr_payload

    payload = _json.loads(qr_payload(RECEIPT, META, BUNDLE, public_base_url=None))
    assert payload["receipt_id"] == RECEIPT["receipt_id"]
    assert payload["key_fingerprint"] == META["key_fingerprint"]


def test_pdf_page_carries_the_material_facts():
    pdf = build_receipt_pdf(RECEIPT, META, bundle_json=BUNDLE)
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    for fact in (
        RECEIPT["receipt_id"],
        "openrouter/deepseek/deepseek-v4-flash",
        "seed",
        "ATTESTED",
        META["key_fingerprint"][:12],
        "nautgate receipt verify",
    ):
        assert fact in text, fact


def test_evidence_package_zip_contains_pdf_bundle_and_instructions():
    import io as _io
    import zipfile

    from app.audit_report_pdf import build_evidence_package

    blob = build_evidence_package(RECEIPT, META, bundle_json=BUNDLE)
    zf = zipfile.ZipFile(_io.BytesIO(blob))
    names = set(zf.namelist())
    rid = RECEIPT["receipt_id"]
    assert names == {
        f"nautgate-receipt-{rid}.pdf",
        f"evidence-{rid}.json",
        "VERIFY.txt",
    }
    assert zf.read(f"evidence-{rid}.json") == BUNDLE  # byte-identical, verifiable
    assert zf.read(f"nautgate-receipt-{rid}.pdf").startswith(b"%PDF-")
    assert b"nautgate receipt verify" in zf.read("VERIFY.txt")
