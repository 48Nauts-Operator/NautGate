import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "safeguard_backfill.py"
_SPEC = importlib.util.spec_from_file_location("safeguard_backfill", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_decodes_json_response():
    payloads = _MODULE.response_payloads('{"model":"fable-5-1","stop_reason":"refusal"}')
    assert payloads[0]["stop_reason"] == "refusal"


def test_decodes_sse_and_ignores_done():
    body = 'event: message\ndata: {"type":"message_delta","delta":{"stop_reason":"refusal"}}\n\ndata: [DONE]\n'
    payloads = _MODULE.response_payloads(body)
    assert len(payloads) == 1
    assert payloads[0]["delta"]["stop_reason"] == "refusal"


def test_does_not_treat_arbitrary_text_as_payload():
    assert _MODULE.response_payloads("The model refused this prompt") == []
