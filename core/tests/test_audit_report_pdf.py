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


def test_pdf_page_carries_the_material_facts():
    pdf = build_receipt_pdf(RECEIPT, META, bundle_json=BUNDLE)
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    for fact in (
        RECEIPT["receipt_id"],
        "openrouter/deepseek/deepseek-v4-flash",
        "seed",
        "ATTESTED",
        META["key_fingerprint"][:12],
        "nautgate receipt verify",
    ):
        assert fact in text, fact
