"""Credit-card number validation — issuer (IIN/BIN) prefix library + Luhn.

The cheap `credit_card_like` regex (any 13–19 digit run) false-positives on
anything numeric: packed timestamps (``20240531005850``), order IDs, hashes,
phone runs. This module turns that candidate match into a *verified* card by
requiring all three of:

  1. Luhn checksum passes,
  2. length is valid for a real network, AND
  3. the leading digits (IIN/BIN) fall in a real issuer range.

A 14-digit ``YYYYMMDD…`` timestamp fails on (2)+(3): no network issues 14-digit
cards starting with ``19``/``20``. That's how date-timestamps stop tripping the
detector — not a special case, just a consequence of validating the prefix.

Prefix ranges below are compared on the leading N digits, where N is the width
of the range bound (e.g. ("2221","2720") checks the first 4 digits in
[2221, 2720]). Sources: ISO/IEC 7812 issuer ranges + public network BIN docs.
"""

from __future__ import annotations

import re

# (network, [(lo, hi), ...] inclusive prefix ranges, {valid lengths})
# Maestro is deliberately omitted: its 12-digit / very broad prefix space
# re-introduces exactly the false positives we're removing.
CARD_NETWORKS: list[tuple[str, list[tuple[str, str]], set[int]]] = [
    ("Visa", [("4", "4")], {13, 16, 19}),
    ("Mastercard", [("51", "55"), ("2221", "2720")], {16}),
    ("Amex", [("34", "34"), ("37", "37")], {15}),
    (
        "Discover",
        [("6011", "6011"), ("644", "649"), ("65", "65"), ("622126", "622925")],
        {16, 17, 18, 19},
    ),
    ("Diners Club", [("300", "305"), ("3095", "3095"), ("36", "36"), ("38", "39")], {14, 16, 19}),
    ("JCB", [("3528", "3589")], {16, 17, 18, 19}),
    ("UnionPay", [("62", "62"), ("81", "81")], {16, 17, 18, 19}),
]

_SEP = re.compile(r"[ \-]")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _prefix_in_range(digits: str, lo: str, hi: str) -> bool:
    w = len(lo)
    if len(digits) < w:
        return False
    return int(lo) <= int(digits[:w]) <= int(hi)


def card_network(raw: str) -> str | None:
    """Return the issuer network name if ``raw`` is a valid card number, else None.

    Strips spaces/dashes. Requires Luhn + a network whose length set and prefix
    range both match. Returns the first matching network (ranges don't overlap
    in practice for these networks).
    """
    digits = _SEP.sub("", raw)
    if not digits.isdigit() or not (12 <= len(digits) <= 19):
        return None
    if not _luhn_ok(digits):
        return None
    for name, ranges, lengths in CARD_NETWORKS:
        if len(digits) in lengths and any(_prefix_in_range(digits, lo, hi) for lo, hi in ranges):
            return name
    return None


def is_valid_card_number(raw: str) -> bool:
    """True only for a Luhn-valid number with a real issuer prefix + length."""
    return card_network(raw) is not None
