"""Deterministic validators and normalizers for structured PII."""

from __future__ import annotations

import string

IBAN_LENGTHS: dict[str, int] = {
    "AD": 24,
    "AT": 20,
    "BE": 16,
    "BG": 22,
    "CH": 21,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "EE": 20,
    "ES": 24,
    "FI": 18,
    "FR": 27,
    "GB": 22,
    "GI": 23,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MT": 31,
    "NL": 18,
    "NO": 15,
    "PL": 28,
    "PT": 25,
    "RO": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "VA": 22,
}


def compact_digits(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def compact_alnum(value: str) -> str:
    return "".join(char for char in value.upper() if char.isalnum())


def ean13_check_digit(first_12_digits: str) -> str:
    if len(first_12_digits) != 12 or not first_12_digits.isdigit():
        raise ValueError("EAN-13 check digit requires exactly 12 digits")
    total = 0
    for idx, digit in enumerate(first_12_digits):
        total += int(digit) * (1 if idx % 2 == 0 else 3)
    return str((10 - (total % 10)) % 10)


def is_valid_ahv(value: str) -> bool:
    """Validate a Swiss AHV/AVS/NSS number.

    AHVN13 uses the EAN-13 checksum and starts with the Swiss GS1 prefix `756`.
    """

    digits = compact_digits(value)
    if len(digits) != 13:
        return False
    if not digits.startswith("756"):
        return False
    return ean13_check_digit(digits[:12]) == digits[-1]


def format_ahv(value: str) -> str:
    digits = compact_digits(value)
    if len(digits) != 13:
        raise ValueError("AHV value must contain 13 digits")
    return f"{digits[:3]}.{digits[3:7]}.{digits[7:11]}.{digits[11:]}"


def compact_uid(value: str) -> str:
    compact = compact_alnum(value)
    if len(compact) == 9 and compact.isdigit():
        return "CHE" + compact
    return compact


def uid_check_digit(first_8_digits: str) -> str:
    if len(first_8_digits) != 8 or not first_8_digits.isdigit():
        raise ValueError("UID check digit requires exactly 8 digits")
    weights = (5, 4, 3, 2, 7, 6, 5, 4)
    total = sum(weight * int(digit) for weight, digit in zip(weights, first_8_digits, strict=True))
    check = (11 - total) % 11
    return str(check)


def is_valid_uid(value: str) -> bool:
    compact = compact_uid(value)
    if len(compact) != 12 or not compact.startswith("CHE"):
        return False
    digits = compact[3:]
    if not digits.isdigit():
        return False
    return uid_check_digit(digits[:8]) == digits[-1]


def format_uid(value: str) -> str:
    compact = compact_uid(value)
    if len(compact) != 12 or not compact.startswith("CHE"):
        raise ValueError("UID value must be CHE plus 9 digits")
    digits = compact[3:]
    return f"CHE-{digits[:3]}.{digits[3:6]}.{digits[6:]}"


VAT_SUFFIXES = {"MWST", "TVA", "IVA", "TPV"}


def compact_vat(value: str) -> str:
    compact = compact_alnum(value)
    for suffix in VAT_SUFFIXES:
        if compact.endswith(suffix):
            return compact[: -len(suffix)] + suffix
    return compact


def vat_suffix(value: str) -> str | None:
    compact = compact_vat(value)
    for suffix in VAT_SUFFIXES:
        if compact.endswith(suffix):
            return suffix
    return None


def is_valid_swiss_vat(value: str) -> bool:
    suffix = vat_suffix(value)
    if suffix is None:
        return False
    compact = compact_vat(value)
    return is_valid_uid(compact[: -len(suffix)])


def format_swiss_vat(value: str) -> str:
    suffix = vat_suffix(value)
    if suffix is None:
        raise ValueError("Swiss VAT value must include MWST, TVA, IVA, or TPV suffix")
    compact = compact_vat(value)
    return f"{format_uid(compact[: -len(suffix)])} {suffix}"


def iban_mod97(value: str) -> int:
    numeric = ""
    for char in value:
        if char.isdigit():
            numeric += char
        elif char in string.ascii_uppercase:
            numeric += str(ord(char) - 55)
        else:
            raise ValueError(f"invalid IBAN character: {char!r}")
    remainder = 0
    for char in numeric:
        remainder = (remainder * 10 + int(char)) % 97
    return remainder


def is_valid_iban(value: str) -> bool:
    compact = compact_alnum(value)
    if len(compact) < 4:
        return False
    country = compact[:2]
    expected_length = IBAN_LENGTHS.get(country)
    if expected_length is None or len(compact) != expected_length:
        return False
    if not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    return iban_mod97(compact[4:] + compact[:4]) == 1


def format_iban(value: str) -> str:
    compact = compact_alnum(value)
    return " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))


def is_qr_iban(value: str) -> bool:
    compact = compact_alnum(value)
    if not is_valid_iban(compact) or not compact.startswith("CH"):
        return False
    iid = int(compact[4:9])
    return 30000 <= iid <= 31999


def luhn_checksum(value: str) -> int:
    digits = compact_digits(value)
    total = 0
    parity = len(digits) % 2
    for idx, char in enumerate(digits):
        digit = int(char)
        if idx % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10


def is_valid_credit_card(value: str) -> bool:
    digits = compact_digits(value)
    if not 13 <= len(digits) <= 19:
        return False
    return luhn_checksum(digits) == 0


def is_valid_swiss_phone(value: str) -> bool:
    digits = compact_digits(value)
    if digits.startswith("0041"):
        national = digits[4:]
    elif digits.startswith("41"):
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        return False
    if len(national) != 9:
        return False
    return national[0] in "23456789"


def normalize_swiss_phone(value: str) -> str:
    digits = compact_digits(value)
    if digits.startswith("0041"):
        national = digits[4:]
    elif digits.startswith("41"):
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        raise ValueError("Swiss phone number must start with +41, 0041, 41, or 0")
    if len(national) != 9:
        raise ValueError("Swiss phone national significant number must contain 9 digits")
    return f"+41 {national[:2]} {national[2:5]} {national[5:7]} {national[7:]}"
