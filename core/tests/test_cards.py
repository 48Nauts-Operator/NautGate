"""Card validation — Luhn + IIN/BIN prefix library."""

from app.cards import card_network, is_valid_card_number
from app.classify import classify, scan_for_findings


# --- valid test numbers per network (public Luhn-valid test PANs) -----------
def test_known_test_cards_validate():
    cases = {
        "4111111111111111": "Visa",
        "4242424242424242": "Visa",
        "4012888888881881": "Visa",
        "5555555555554444": "Mastercard",
        "5105105105105100": "Mastercard",
        "2223003122003222": "Mastercard",  # 2-series
        "378282246310005": "Amex",
        "371449635398431": "Amex",
        "6011111111111117": "Discover",
        "3566002020360505": "JCB",
        "30569309025904": "Diners Club",
    }
    for pan, net in cases.items():
        assert card_network(pan) == net, f"{pan} should be {net}"
        assert is_valid_card_number(pan)


def test_cards_with_separators():
    assert is_valid_card_number("4111 1111 1111 1111")
    assert is_valid_card_number("4111-1111-1111-1111")


# --- the false positives this whole change exists to kill -------------------
def test_packed_timestamps_are_not_cards():
    for ts in ("20240531005850", "20240531041527", "20230629103718",
               "20230326225437", "20230705003749"):
        assert card_network(ts) is None
        assert not is_valid_card_number(ts)


def test_luhn_fail_rejected():
    # 4111...1112 breaks the checksum.
    assert not is_valid_card_number("4111111111111112")


def test_wrong_length_or_prefix_rejected():
    assert not is_valid_card_number("1234567890123")     # no network prefix
    assert not is_valid_card_number("9999999999999999")  # 9 prefix unused
    assert not is_valid_card_number("12345678901")       # too short


# --- end-to-end through the classifier --------------------------------------
def test_classify_ignores_timestamp_flags_real_card():
    ts_text = "log entry 20240531005850 ref 20230705003749 done"
    assert classify(ts_text).sensitivity == "none"
    real = "customer paid with 4111 1111 1111 1111 today"
    c = classify(real)
    assert c.sensitivity == "pii"
    assert any(s["rule_id"] == "credit_card_like" for s in c.signals)


def test_ls_listing_columns_not_glued_into_card():
    # The real-world FP: an `ls -l` row whose size/date/time columns the old
    # regex welded into a Luhn-valid 16-digit "Visa". Must now be clean.
    ls = "-rw-r--r--@ 1 cand0rian staff 3388 May 27 08:47 2026 4744 engram.py"
    assert classify(ls).sensitivity == "none"
    assert not [f for f in scan_for_findings(ls) if f["rule_id"] == "credit_card_like"]


def test_grouped_card_still_detected():
    # 4-4-4-4 spacing is a real card shape and must still be caught.
    c = classify("pay 4111 1111 1111 1111 now")
    assert any(s["rule_id"] == "credit_card_like" for s in c.signals)


def test_scan_for_findings_skips_timestamps():
    findings = scan_for_findings("ids 20240531005850 and 20230629103718")
    assert not [f for f in findings if f["rule_id"] == "credit_card_like"]
    findings = scan_for_findings("card 4242424242424242")
    assert [f for f in findings if f["rule_id"] == "credit_card_like"]
