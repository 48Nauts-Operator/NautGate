"""Rule-based PII detectors."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

from bowden_pii.types import Detection, DetectionConflict
from bowden_pii.validators import (
    IBAN_LENGTHS,
    compact_alnum,
    compact_digits,
    format_ahv,
    format_iban,
    format_swiss_vat,
    format_uid,
    is_qr_iban,
    is_valid_ahv,
    is_valid_credit_card,
    is_valid_iban,
    is_valid_swiss_phone,
    is_valid_swiss_vat,
    is_valid_uid,
    normalize_swiss_phone,
    uid_check_digit,
)

AHV_RE = re.compile(r"(?<!\d)756[ .-]?\d{4}[ .-]?\d{4}[ .-]?\d{2}(?!\d)")

IBAN_START_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{2})", re.IGNORECASE)

SWISS_VAT_RE = re.compile(
    r"(?<![A-Z0-9])CHE[- ]?\d{3}[ .]?\d{3}[ .]?\d{3}\s*(?:MWST|TVA|IVA|TPV)"
    r"(?![A-Z0-9])",
    re.IGNORECASE,
)

UID_RE = re.compile(
    r"(?<![A-Z0-9])CHE[- ]?\d{3}[ .]?\d{3}[ .]?\d{3}(?![A-Z0-9])",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(
    r"(?<![A-Z0-9._%+-])"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
    r"(?![A-Z0-9_%+-])",
    re.IGNORECASE,
)

URL_RE = re.compile(r"(?<![A-Z0-9])(?:https?://|www\.)[^\s<>\"]+", re.IGNORECASE)

IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w])")
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-F]{0,4}:){2,7}[0-9A-F]{0,4}(?![\w:])", re.IGNORECASE)

CREDIT_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

PHONE_RE = re.compile(
    r"(?<!\d)(?:\+41|0041|0)(?:[ \-/().]?\d){9}(?!\d)",
    re.IGNORECASE,
)

MAC_RE = re.compile(
    r"(?<![A-F0-9])(?:[A-F0-9]{2}[:-]){5}[A-F0-9]{2}(?![A-F0-9])",
    re.IGNORECASE,
)


def detect_ahv(text: str) -> Iterable[Detection]:
    for match in AHV_RE.finditer(text):
        value = match.group(0)
        if is_valid_ahv(value):
            yield Detection(
                start=match.start(),
                end=match.end(),
                label="AHV",
                value=value,
                normalized=format_ahv(value),
                source="rule",
                rule_id="ch_ahv_ean13_v1",
            )


def detect_iban(text: str) -> Iterable[Detection]:
    for match in IBAN_START_RE.finditer(text):
        country = match.group(1)[:2].upper()
        expected_length = IBAN_LENGTHS.get(country)
        if expected_length is None:
            continue

        alnum_count = 0
        end = match.start()
        for idx in range(match.start(), len(text)):
            char = text[idx]
            if char.isalnum():
                alnum_count += 1
                end = idx + 1
            elif char in {" ", "\t", "\n", "\r"}:
                if alnum_count == 0:
                    break
                end = idx + 1
            else:
                break

            if alnum_count == expected_length:
                break

        if alnum_count != expected_length:
            continue

        value = text[match.start() : end].rstrip()
        if is_valid_iban(value):
            label = "QR_IBAN" if is_qr_iban(value) else "IBAN"
            rule_id = "ch_qr_iban_iid_v1" if label == "QR_IBAN" else "iban_iso13616_mod97_v1"
            yield Detection(
                start=match.start(),
                end=match.start() + len(value),
                label=label,
                value=value,
                normalized=format_iban(value),
                source="rule",
                rule_id=rule_id,
            )


def detect_swiss_vat(text: str) -> Iterable[Detection]:
    for match in SWISS_VAT_RE.finditer(text):
        value = match.group(0)
        if is_valid_swiss_vat(value):
            yield Detection(
                start=match.start(),
                end=match.end(),
                label="VAT_ID",
                value=value,
                normalized=format_swiss_vat(value),
                source="rule",
                rule_id="ch_vat_uid_suffix_v1",
            )


def detect_uid(text: str) -> Iterable[Detection]:
    for match in UID_RE.finditer(text):
        value = match.group(0)
        if is_valid_uid(value):
            yield Detection(
                start=match.start(),
                end=match.end(),
                label="UID",
                value=value,
                normalized=format_uid(value),
                source="rule",
                rule_id="ch_uid_mod11_v1",
            )


def detect_email(text: str) -> Iterable[Detection]:
    for match in EMAIL_RE.finditer(text):
        value = match.group(0)
        yield Detection(
            start=match.start(),
            end=match.end(),
            label="EMAIL",
            value=value,
            normalized=value.lower(),
            source="rule",
            rule_id="email_basic_v1",
        )


def detect_url(text: str) -> Iterable[Detection]:
    for match in URL_RE.finditer(text):
        value = match.group(0).rstrip(".,;:!?)")
        if "." not in value:
            continue
        yield Detection(
            start=match.start(),
            end=match.start() + len(value),
            label="URL",
            value=value,
            normalized=value,
            source="rule",
            rule_id="url_basic_v1",
        )


def detect_ip_address(text: str) -> Iterable[Detection]:
    seen: set[tuple[int, int]] = set()
    for regex in (IPV4_RE, IPV6_RE):
        for match in regex.finditer(text):
            value = match.group(0)
            if (
                match.end() < len(text) - 1
                and text[match.end()] == "."
                and text[match.end() + 1].isdigit()
            ):
                continue
            try:
                normalized = str(ipaddress.ip_address(value))
            except ValueError:
                continue
            key = (match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            yield Detection(
                start=match.start(),
                end=match.end(),
                label="IP_ADDRESS",
                value=value,
                normalized=normalized,
                source="rule",
                rule_id="ip_address_stdlib_v1",
            )


def detect_credit_card(text: str) -> Iterable[Detection]:
    for match in CREDIT_CARD_RE.finditer(text):
        value = match.group(0).strip()
        if is_valid_credit_card(value):
            yield Detection(
                start=match.start(),
                end=match.start() + len(value),
                label="CREDIT_CARD",
                value=value,
                normalized=compact_digits(value),
                source="rule",
                rule_id="credit_card_luhn_v1",
            )


def detect_phone(text: str) -> Iterable[Detection]:
    for match in PHONE_RE.finditer(text):
        value = match.group(0).strip()
        if is_valid_swiss_phone(value):
            yield Detection(
                start=match.start(),
                end=match.start() + len(value),
                label="PHONE",
                value=value,
                normalized=normalize_swiss_phone(value),
                source="rule",
                rule_id="ch_phone_basic_v1",
            )


def detect_mac_address(text: str) -> Iterable[Detection]:
    for match in MAC_RE.finditer(text):
        value = match.group(0)
        yield Detection(
            start=match.start(),
            end=match.end(),
            label="MAC_ADDRESS",
            value=value,
            normalized=value.upper().replace("-", ":"),
            source="rule",
            rule_id="mac_address_basic_v1",
        )


def rule_detections(text: str) -> list[Detection]:
    detections = [
        *detect_ahv(text),
        *detect_iban(text),
        *detect_swiss_vat(text),
        *detect_uid(text),
        *detect_email(text),
        *detect_url(text),
        *detect_ip_address(text),
        *detect_credit_card(text),
        *detect_phone(text),
        *detect_mac_address(text),
    ]
    return merge_rule_detections(detections)


def rule_detection_report(text: str) -> tuple[list[Detection], list[DetectionConflict]]:
    detections = [
        *detect_ahv(text),
        *detect_iban(text),
        *detect_swiss_vat(text),
        *detect_uid(text),
        *detect_email(text),
        *detect_url(text),
        *detect_ip_address(text),
        *detect_credit_card(text),
        *detect_phone(text),
        *detect_mac_address(text),
    ]
    return merge_rule_detection_report(detections)


def merge_rule_detections(detections: list[Detection]) -> list[Detection]:
    """Resolve overlapping deterministic detections.

    Longer structured spans win. This avoids shorter accidental matches inside a
    larger validated identifier.
    """

    accepted, _conflicts = merge_rule_detection_report(detections)
    return accepted


def merge_rule_detection_report(
    detections: list[Detection],
) -> tuple[list[Detection], list[DetectionConflict]]:
    """Resolve overlaps and report dropped deterministic conflicts."""

    priority = {
        "VAT_ID": 0,
        "UID": 1,
        "AHV": 2,
        "IBAN": 3,
        "QR_IBAN": 3,
        "CREDIT_CARD": 4,
        "PHONE": 5,
        "EMAIL": 6,
        "URL": 7,
        "IP_ADDRESS": 8,
        "MAC_ADDRESS": 9,
    }
    ordered = sorted(
        detections,
        key=lambda item: (item.start, -(item.end - item.start), priority.get(item.label, 99)),
    )
    accepted: list[Detection] = []
    conflicts: list[DetectionConflict] = []
    for candidate in ordered:
        conflict = next((existing for existing in accepted if candidate.overlaps(existing)), None)
        if conflict is not None:
            conflicts.append(
                DetectionConflict(
                    kept=conflict,
                    dropped=candidate,
                    reason="overlap_precedence",
                )
            )
            continue
        accepted.append(candidate)
    return sorted(accepted, key=lambda item: item.start), conflicts


def make_valid_ahv(first_12_digits: str) -> str:
    from bowden_pii.validators import ean13_check_digit

    digits = compact_digits(first_12_digits)
    if len(digits) != 12 or not digits.startswith("756"):
        raise ValueError("AHV seed must contain 12 digits starting with 756")
    return format_ahv(digits + ean13_check_digit(digits))


def make_valid_uid(first_8_digits: str) -> str:
    digits = compact_digits(first_8_digits)
    if len(digits) != 8:
        raise ValueError("UID seed must contain 8 digits")
    return format_uid("CHE" + digits + uid_check_digit(digits))


def make_iban(country_code: str, bban: str) -> str:
    country_code = country_code.upper()
    bban = compact_alnum(bban)
    check_input = bban + country_code + "00"
    check = 98 - (int(_iban_numeric(check_input)) % 97)
    return format_iban(country_code + f"{check:02d}" + bban)


def _iban_numeric(value: str) -> str:
    parts: list[str] = []
    for char in value:
        if char.isdigit():
            parts.append(char)
        else:
            parts.append(str(ord(char.upper()) - 55))
    return "".join(parts)
