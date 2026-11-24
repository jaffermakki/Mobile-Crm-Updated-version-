"""v54: the repair price menu.

The owner keeps a list of "this phone + this repair = this price, using this part". On a new ticket the staff member picks the
phone and taps the repair, and the issue and the estimate fill in. On the ticket page the matching part is offered with a one
tap Add. Nothing here changes how a ticket, a part or stock behaves: the menu only FILLS IN boxes the staff member can still
change, and the part is added by the same route as before.

Matching a phone on a ticket to a menu line is deliberately strict: a price for "iPhone 15" is never offered for an
"iPhone 15 Pro", and "Galaxy S24" never matches "Galaxy S24+" (see repair_parts for the shared word rules). A line with no phone
("any phone") is the fallback for repairs that cost the same everywhere.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from sqlalchemy import func

from . import inputs as I
from .models import Product, RepairPrice
from .repair_parts import _MODEL_SUFFIX, _drop_brand, _toks
from .repairs_const import ISSUE_TYPES

MAX_MODEL = 80
MAX_REPAIR = 60
MAX_MINUTES = 1440          # a day: anything longer is a typing slip
MAX_LINES = 5000
MAX_IMPORT_BYTES = 400_000
CSV_HEADERS = ["Phone model", "Repair", "Price (before tax)", "Part SKU", "Minutes"]
ANY_PHONE_LABEL = "Any phone"


# ── words ────────────────────────────────────────────────────────────────────

def _words(text) -> list:
    toks = _toks(text)
    dropped = _drop_brand(toks, [])
    return dropped or toks


def model_key(model: str) -> str:
    return " ".join(_words(model))


def repair_key(repair: str) -> str:
    return " ".join(_toks(repair))


def clean_text(text, limit: int) -> str:
    """One line, no control characters, single spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))).strip()[:limit]


def model_matches_device(model: str, device: str) -> bool:
    """True when the menu line's phone model is the phone written on the ticket. Storage or colour after the model is fine
    ("iPhone 15, 128GB, Blue"); another model word is not ("iPhone 15 Pro")."""
    m, d = _words(model), _words(device)
    if not m or len(d) < len(m) or d[:len(m)] != m:
        return False
    nxt = d[len(m)] if len(d) > len(m) else ""
    return nxt not in _MODEL_SUFFIX


def _order(name: str) -> tuple:
    try:
        return (0, ISSUE_TYPES.index(name))
    except ValueError:
        return (1, name.lower())


# ── lookups ──────────────────────────────────────────────────────────────────

def _best(rows, device: str):
    """From the lines for ONE repair: the line for the longest phone name that matches, else the any-phone line."""
    best, best_len, generic = None, 0, None
    for r in rows:
        if not r.model_key:
            generic = r
        elif model_matches_device(r.model, device):
            n = len(_words(r.model))
            if n > best_len:
                best, best_len = r, n
    return best or generic


def menu_for_device(db, device: str) -> list:
    """Every menu line that applies to `device`, one per repair, in the usual repair order."""
    by_repair = {}
    for r in db.query(RepairPrice).all():
        by_repair.setdefault(r.repair_key, []).append(r)
    out = []
    for rows in by_repair.values():
        chosen = _best(rows, device)
        if chosen:
            out.append(chosen)
    out.sort(key=lambda r: _order(r.repair))
    return out


def lookup(db, device: str, repair_name: str):
    """The menu line for this phone and this repair, or None."""
    rk = repair_key(repair_name)
    if not rk or not (device or "").strip():
        return None
    return _best(db.query(RepairPrice).filter(RepairPrice.repair_key == rk).all(), device)


def part_info(db, product_id: str):
    p = db.get(Product, product_id) if product_id else None
    if not p:
        return None
    return {"id": p.id, "sku": p.sku, "name": p.name, "stock": p.stock or 0}


def as_json(db, rows) -> list:
    out = []
    cache = {}
    for r in rows:
        if r.product_id not in cache:
            cache[r.product_id] = part_info(db, r.product_id)
        out.append({"id": r.id, "repair": r.repair, "price": round(float(r.price or 0), 2), "minutes": r.minutes,
                    "any_phone": not r.model_key, "part": cache[r.product_id]})
    return out


def repair_names(db) -> list:
    """The repair names on the menu that are not already one of the standard issue types (they join the Issue list)."""
    std = {repair_key(i) for i in ISSUE_TYPES}
    seen, out = set(), []
    for (name, key) in db.query(RepairPrice.repair, RepairPrice.repair_key).order_by(RepairPrice.repair).all():
        if key not in std and key not in seen:
            seen.add(key)
            out.append(name)
    return out


# ── saving ───────────────────────────────────────────────────────────────────

class PriceError(ValueError):
    pass


def find_product(db, sku: str):
    sku = clean_text(sku, 60)
    if not sku:
        return None
    p = db.query(Product).filter(Product.sku == sku).first()
    if p:
        return p
    return db.query(Product).filter(func.lower(Product.sku) == sku.lower()).first()


def clean_line(db, model, repair, price_text, sku="", minutes_text=""):
    """Check one menu line typed by the owner. Returns a dict ready to save, or raises PriceError with a plain message."""
    model = clean_text(model, MAX_MODEL + 1)
    if model.lower() in ("any", "any phone", "all", "all phones", "*"):
        model = ""
    if len(model) > MAX_MODEL:
        raise PriceError(f"The phone name is too long (most {MAX_MODEL} letters).")
    repair = clean_text(repair, MAX_REPAIR + 1)
    if not repair:
        raise PriceError("Choose or type the repair (for example Screen Replacement).")
    if len(repair) > MAX_REPAIR:
        raise PriceError(f"The repair name is too long (most {MAX_REPAIR} letters).")
    if not repair_key(repair):
        raise PriceError("The repair name needs some letters or numbers.")
    if model and not model_key(model):
        raise PriceError("The phone name needs some letters or numbers.")
    price, err = I.parse_money(price_text)
    if err or price is None:
        raise PriceError("The price " + (err or f"must be a number from {I.money_text()}") + ".")
    product_id = ""
    sku = clean_text(sku, 60)
    if sku:
        p = find_product(db, sku)
        if not p:
            raise PriceError(f"No product has the SKU “{sku}”. Check the spelling, or leave the part blank.")
        product_id = p.id
    minutes = None
    mt = str(minutes_text or "").strip()
    if mt:
        if not re.fullmatch(r"\d{1,5}", mt) or not I.whole(int(mt), 1, MAX_MINUTES):
            raise PriceError(f"Minutes must be a whole number from 1 to {MAX_MINUTES}, or left blank.")
        minutes = int(mt)
    return {"model": model, "model_key": model_key(model), "repair": repair, "repair_key": repair_key(repair),
            "price": price, "product_id": product_id, "minutes": minutes}


def count_lines(db) -> int:
    return db.query(RepairPrice).count()


def save_new(db, line: dict, who: str):
    """Add a line. Raises PriceError if that phone + repair is already on the menu."""
    if db.query(RepairPrice).filter(RepairPrice.model_key == line["model_key"], RepairPrice.repair_key == line["repair_key"]).first():
        raise PriceError("That phone and repair are already on the list. Edit the existing line instead.")
    if count_lines(db) >= MAX_LINES:
        raise PriceError(f"The price list is full ({MAX_LINES} lines).")
    row = RepairPrice(**line, updated_by=who, updated_at=datetime.utcnow())
    db.add(row)
    db.flush()
    return row


def update(db, row, line: dict, who: str):
    """Change a line. Raises PriceError if the new phone + repair already belongs to a different line."""
    clash = db.query(RepairPrice).filter(RepairPrice.model_key == line["model_key"], RepairPrice.repair_key == line["repair_key"],
                                         RepairPrice.id != row.id).first()
    if clash:
        raise PriceError("Another line already has that phone and repair.")
    for k, v in line.items():
        setattr(row, k, v)
    row.updated_by, row.updated_at = who, datetime.utcnow()
    return row


# ── CSV ──────────────────────────────────────────────────────────────────────

def to_csv_rows(db) -> list:
    rows = []
    for r in db.query(RepairPrice).order_by(RepairPrice.model_key, RepairPrice.repair_key).all():
        p = part_info(db, r.product_id)
        rows.append((r.model or ANY_PHONE_LABEL, r.repair, f"{float(r.price or 0):.2f}", p["sku"] if p else "", r.minutes or ""))
    return rows


_HEADER_ALIASES = {
    "model": "model", "phone": "model", "phonemodel": "model", "device": "model",
    "repair": "repair", "service": "repair", "issue": "repair",
    "price": "price", "pricebeforetax": "price", "amount": "price",
    "sku": "sku", "partsku": "sku", "part": "sku",
    "minutes": "minutes", "time": "minutes", "mins": "minutes",
}


def _header_map(first_row) -> dict | None:
    m = {}
    for i, cell in enumerate(first_row):
        k = _HEADER_ALIASES.get(re.sub(r"[^a-z]", "", (cell or "").lower()))
        if k and k not in m:
            m[k] = i
    return m if {"repair", "price"} <= set(m) else None


def parse_csv(raw: bytes):
    """-> (rows, problem). Each row is a dict of the five fields as text. Understands the file this page exports, and any
    sheet whose first row names Repair and Price columns (Phone model, Part SKU and Minutes are optional)."""
    if len(raw) > MAX_IMPORT_BYTES:
        return [], f"That file is too big (over {MAX_IMPORT_BYTES // 1000} KB). Split it into smaller files."
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            return [], "That file isn't plain text. Save it as CSV (Comma delimited) and try again."
    sample = text[:4096]
    delim = "\t" if sample.count("\t") > sample.count(",") else (";" if sample.count(";") > sample.count(",") else ",")
    table = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any((c or "").strip() for c in r)]
    if not table:
        return [], "The file is empty."
    hm = _header_map(table[0])
    if not hm:
        return [], ("The first row must name the columns. It needs at least “Repair” and “Price”; "
                    "“Phone model”, “Part SKU” and “Minutes” are optional.")
    out = []
    for r in table[1:]:
        def cell(name):
            i = hm.get(name)
            return r[i].strip() if i is not None and i < len(r) else ""
        out.append({"model": cell("model"), "repair": cell("repair"), "price": cell("price"), "sku": cell("sku"), "minutes": cell("minutes")})
    return out, ""


def import_rows(db, rows: list, who: str) -> dict:
    """Add new lines and update existing ones (same phone + repair). A bad row is skipped and listed; the rest still go in.
    Returns {"added", "updated", "skipped": [(row number, why)]}."""
    added = updated = 0
    skipped = []
    seen = set()
    for n, r in enumerate(rows, start=2):                 # row 1 is the header
        try:
            line = clean_line(db, r["model"], r["repair"], r["price"], r["sku"], r["minutes"])
        except PriceError as e:
            skipped.append((n, str(e)))
            continue
        k = (line["model_key"], line["repair_key"])
        if k in seen:
            skipped.append((n, "This phone and repair appear twice in the file; the first one was used."))
            continue
        seen.add(k)
        existing = db.query(RepairPrice).filter(RepairPrice.model_key == k[0], RepairPrice.repair_key == k[1]).first()
        try:
            if existing:
                update(db, existing, line, who)
                updated += 1
            else:
                save_new(db, line, who)
                added += 1
        except PriceError as e:
            skipped.append((n, str(e)))
    return {"added": added, "updated": updated, "skipped": skipped}
