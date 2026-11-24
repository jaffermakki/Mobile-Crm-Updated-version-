"""Matching customers by phone number, whatever punctuation it was typed with.

Phones are stored exactly as typed ("(250) 555-0142"), so comparing the raw text
treats "250-555-0142" and "+1 250 555 0142" as three different people. Everything
that looks a customer up by phone goes through here instead, so the same person
is never split in two (their points, store credit, layaways and repair history
would otherwise end up on separate records).
"""
from __future__ import annotations

import re

from sqlalchemy import func

from .models import Customer

_NON_DIGIT = re.compile(r"\D")
_PHONE_LIKE = re.compile(r"^[\d\s\-\(\)\+\.]+$")


def phone_digits(raw) -> str:
    return _NON_DIGIT.sub("", str(raw or ""))


def phone_key(raw) -> str:
    """What identifies a number: its last 10 digits ("+1 (250) 555-0142" -> "2505550142").
    Anything shorter than 7 digits (a blank, "N/A", an extension) has no key and is only
    ever matched by exact text."""
    d = phone_digits(raw)
    if len(d) < 7:
        return ""
    return d[-10:]


def find_customer_by_phone(db, raw):
    """The existing customer for this phone number, or None."""
    text = (raw or "").strip()
    if not text:
        return None
    exact = db.query(Customer).filter(Customer.phone == text).first()
    if exact is not None:
        return exact
    key = phone_key(text)
    if not key:
        return None
    # cheap pre-filter on the last 4 digits would miss "01 42" style spacing, and a shop's
    # customer list is small, so compare the digits of every stored number.
    for cid, stored in db.query(Customer.id, Customer.phone).filter(Customer.phone.isnot(None), Customer.phone != ""):
        if phone_key(stored) == key:
            return db.get(Customer, cid)
    return None


def _digits_sql(col):
    expr = col
    for ch in (" ", "-", "(", ")", "+", "."):
        expr = func.replace(expr, ch, "")
    return expr


def phone_search_clause(q: str):
    """An extra SQL condition for a search box: when the search text looks like a phone number
    (digits, spaces, dashes, brackets) also match stored numbers by their digits, so "2505550142"
    finds "(250) 555-0142". Returns None when the text doesn't look like a phone number."""
    q = (q or "").strip()
    if not q or not _PHONE_LIKE.match(q):
        return None
    d = phone_digits(q)
    if len(d) < 3:
        return None
    return _digits_sql(Customer.phone).like(f"%{d}%")
