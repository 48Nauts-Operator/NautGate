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

from bowden_pii.validators import credit_card_network


def card_network(raw: str) -> str | None:
    """Return the issuer network name if ``raw`` is a valid card number, else None.

    Strips spaces/dashes. Requires Luhn + a network whose length set and prefix
    range both match. Returns the first matching network (ranges don't overlap
    in practice for these networks).
    """
    return credit_card_network(raw)


def is_valid_card_number(raw: str) -> bool:
    """True only for a Luhn-valid number with a real issuer prefix + length."""
    return card_network(raw) is not None
