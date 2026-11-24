"""v50: finding a product from a scanned or typed code.

A product can be reached by its SKU (our own shelf labels print the SKU as a CODE128 barcode) or by the
manufacturer's UPC/EAN printed on the package (`Product.barcode`). The two ways a shop floor trips over the second one:

* **Same number, different length.** A scanner may read a UPC-A as 12 digits on one phone and as the same number with a
  leading 0 (EAN-13) on another. They are the same product, so `variants()` lists every spelling of one number.
* **Two products with one barcode** (the same case sold in several colours under one package code). Linking refuses a
  barcode that is already on another product, so a scan never has to guess.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Product

MAX_LEN = 64
_JUNK = re.compile(r"[\x00-\x1f\x7f\s]+")


def clean(code) -> str:
    """What a scanner or a keyboard gave us, minus spaces, line breaks and control characters."""
    return _JUNK.sub("", str(code or "")).strip()[:MAX_LEN]


def variants(code: str) -> list[str]:
    """Every way the same number can be written: UPC-A (12 digits), EAN-13 (13) and GTIN-14 (14) differ only by
    leading zeros. Anything else (a short SKU, a Code 128 string) is matched exactly."""
    c = clean(code)
    if not c:
        return []
    out = {c}
    if c.isdigit() and len(c) in (12, 13, 14):
        core = c.lstrip("0")
        if core and len(core) <= 12:
            for width in (12, 13, 14):
                out.add(core.zfill(width))
        elif core and len(core) <= 13:
            for width in (13, 14):
                out.add(core.zfill(width))
        elif core:
            out.add(core)
    return sorted(out)


def check_digit_ok(code: str) -> Optional[bool]:
    """True/False for an 8, 12, 13 or 14 digit product number (the last digit is a check digit); None for anything
    else. Used only to warn about a probable typing mistake, never to refuse a code."""
    c = clean(code)
    if not c.isdigit() or len(c) not in (8, 12, 13, 14):
        return None
    digits = [int(d) for d in c]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def find_products(db: Session, code: str) -> tuple[list[Product], str]:
    """(products, how): the SKU match when there is one, otherwise whatever carries that barcode.
    `how` is "sku", "barcode" or "" (nothing found)."""
    c = clean(code)
    if not c:
        return [], ""
    by_sku = db.query(Product).filter(func.lower(Product.sku) == c.lower()).order_by(Product.name).all()
    if by_sku:
        return by_sku, "sku"
    by_code = db.query(Product).filter(Product.barcode.in_(variants(c))).order_by(Product.name).all()
    return by_code, ("barcode" if by_code else "")


def find_product(db: Session, code: str) -> Optional[Product]:
    products, _ = find_products(db, code)
    return products[0] if products else None


def conflict(db: Session, code: str, product_id: str = "") -> Optional[Product]:
    """Another product that already answers to this code (as its barcode or its SKU), or None."""
    c = clean(code)
    if not c:
        return None
    q = db.query(Product).filter(func.lower(Product.sku) == c.lower())
    hit = q.first()
    if hit and hit.id != product_id:
        return hit
    hits = db.query(Product).filter(Product.barcode.in_(variants(c))).all()
    for h in hits:
        if h.id != product_id:
            return h
    return None
