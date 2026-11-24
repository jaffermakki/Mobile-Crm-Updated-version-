"""v51: tidy and sanity-check an IMEI / serial number.

An IMEI is 15 digits, the last one a check digit (Luhn). Phone boxes print it as a barcode and the phone shows it when
you dial *#06#. Staff type it, or (v51) scan it. Serial numbers (letters and digits) are accepted exactly as given: only
a value that is clearly a number with spaces, dashes or dots in it is tidied, and only a 15-digit number is judged.
Nothing here ever refuses a save: a wrong check digit is a warning to look again, not an error.
"""
from __future__ import annotations

import re

_SEPARATED_NUMBER = re.compile(r"[\d][\d\s\-./]*")
MAX_LEN = 40


def clean(raw: str) -> str:
    """'35 123456 789012 3' -> '351234567890123'.  Serial numbers and anything with letters are only trimmed."""
    text = " ".join(str(raw or "").split())[:MAX_LEN]
    if text and _SEPARATED_NUMBER.fullmatch(text):
        return re.sub(r"\D", "", text)
    return text


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def warning(imei: str) -> str:
    """'' when the value looks fine (or is not an IMEI at all); otherwise one short sentence to show the person."""
    imei = (imei or "").strip()
    if not imei.isdigit():
        return ""                       # a serial number, or empty: nothing to judge
    if len(imei) == 15:
        return "" if luhn_ok(imei) else "This does not look like a valid IMEI (its check digit is wrong). Dial *#06# on the phone and compare."
    if len(imei) in (14, 16):
        return "An IMEI is normally 15 digits. Dial *#06# on the phone and compare."
    if len(imei) >= 8:
        return "That is all digits but not 15 long. If it is an IMEI, dial *#06# on the phone and compare."
    return ""
