"""v50: stock-count mode — the rules, without any web code.

A count is a list of "I counted N of this product" lines. Nothing in `products` changes until the count is applied, and
applying keeps every sale or delivery that happened while the shelf was being counted:

    new stock = (stock right now) + (counted - stock when that line was counted)

Example: the system said 5, you counted 3 (so 2 are missing). A customer then buys 1 (system now 4). Applying gives
4 + (3 - 5) = 2: the 2 missing units are removed and the sale is not undone.

* Adding to a line (+1 per scan, the + / - buttons) keeps the line's original "stock when counted": what was counted
  first is still what the later scans are added to.
* Typing an exact total replaces the count and starts from the stock as it is at that moment.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Product, StockCount, StockCountLine

MAX_QTY = 100000          # a typo like 5000000 is refused, not applied
NAME_MAX = 80

# v51: why a count came out different from the system. key -> (what the screen says, which differences it fits)
#      "loss" = fewer than expected, "gain" = more than expected, "both" = either way.
REASONS = {
    "damaged": ("Damaged or defective", "loss"),
    "stolen": ("Lost or stolen", "loss"),
    "unrung": ("Sold or used but not rung in", "loss"),
    "supplier_short": ("Supplier delivered less than invoiced", "loss"),
    "delivery": ("Delivery not entered", "gain"),
    "return": ("Customer return not entered", "gain"),
    "miscount": ("Counting or entry mistake", "both"),
    "other": ("Other", "both"),
}
NO_REASON = "No reason given"


def reason_label(key: str) -> str:
    return REASONS[key][0] if key in REASONS else NO_REASON


def reason_fits(key: str, change: int) -> bool:
    """Can this reason be given for a difference of `change` units (negative = fewer than the system said)?"""
    if key not in REASONS:
        return False
    kind = REASONS[key][1]
    return kind == "both" or (kind == "loss" and change < 0) or (kind == "gain" and change > 0)


def reasons_for(change: int) -> list[tuple[str, str]]:
    """The (key, label) pairs worth offering for a difference of `change` units."""
    return [(k, v[0]) for k, v in REASONS.items() if reason_fits(k, change)]


class CountError(Exception):
    """Something the person at the counter can be told as-is."""


def clean_name(text: str) -> str:
    return " ".join(str(text or "").split())[:NAME_MAX]


def start(db: Session, staff_name: str, name: str = "", scope_category: str = "", now: Optional[datetime] = None) -> StockCount:
    now = now or datetime.utcnow()
    name = clean_name(name) or f"Count {now.strftime('%b %d')}"
    count = StockCount(name=name, scope_category=(scope_category or "").strip(), status="open", created_by=staff_name or "", created_at=now)
    db.add(count)
    db.flush()
    return count


def _require_open(count: Optional[StockCount]) -> StockCount:
    if not count:
        raise CountError("That count no longer exists.")
    if count.status != "open":
        raise CountError(f"That count is already {count.status}, so it can't be changed.")
    return count


def _line(db: Session, count_id: str, product_id: str) -> Optional[StockCountLine]:
    return db.query(StockCountLine).filter(StockCountLine.count_id == count_id, StockCountLine.product_id == product_id).first()


def add_to_line(db: Session, count: StockCount, product: Product, qty: int, staff_name: str, now: Optional[datetime] = None) -> StockCountLine:
    """Count `qty` more (or fewer, if negative) of a product. The first time a product is counted, the system's
    current stock is remembered as the line's baseline."""
    count = _require_open(count)
    now = now or datetime.utcnow()
    qty = int(qty)
    if abs(qty) > MAX_QTY:
        raise CountError("That quantity is too big to be right.")
    line = _line(db, count.id, product.id)
    if line is None:
        if qty < 0:
            raise CountError("Nothing has been counted for this product yet, so there is nothing to take away.")
        line = StockCountLine(count_id=count.id, product_id=product.id, sku=product.sku or "", name=product.name or "",
                              counted=0, stock_at_count=int(product.stock or 0), counted_by=staff_name or "",
                              first_at=now, updated_at=now)
        db.add(line)
        try:
            db.flush()
        except IntegrityError:               # two phones scanned the same product for the first time at once
            db.rollback()
            line = _line(db, count.id, product.id)
            if line is None:
                raise
    # one atomic UPDATE so two phones counting the same shelf never overwrite each other
    bumped = StockCountLine.counted + qty
    db.query(StockCountLine).filter(StockCountLine.id == line.id).update(
        {StockCountLine.counted: case((bumped < 0, 0), else_=bumped),
         StockCountLine.updated_at: now, StockCountLine.counted_by: staff_name or line.counted_by},
        synchronize_session=False)
    db.flush()
    db.refresh(line)
    return line


def set_line(db: Session, count: StockCount, product: Product, qty: int, staff_name: str, now: Optional[datetime] = None) -> StockCountLine:
    """Replace the count with an exact total, measured against the stock as it is now."""
    count = _require_open(count)
    now = now or datetime.utcnow()
    qty = int(qty)
    if qty < 0 or qty > MAX_QTY:
        raise CountError("Enter a quantity from 0 up.")
    line = _line(db, count.id, product.id)
    if line is None:
        line = add_to_line(db, count, product, 0, staff_name, now)
    line.counted = qty
    line.stock_at_count = int(product.stock or 0)
    line.sku, line.name = product.sku or line.sku, product.name or line.name
    line.updated_at = now
    line.counted_by = staff_name or line.counted_by
    db.flush()
    return line


def remove_line(db: Session, count: StockCount, product_id: str) -> bool:
    count = _require_open(count)
    line = _line(db, count.id, product_id)
    if not line:
        return False
    db.delete(line)
    db.flush()
    return True


# ═══ reviewing ═══════════════════════════════════════════════════════

def review_rows(db: Session, count: StockCount) -> list[dict]:
    """One row per counted product with the numbers a manager decides on, biggest differences first."""
    lines = db.query(StockCountLine).filter(StockCountLine.count_id == count.id).all()
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_([l.product_id for l in lines] or [""])).all()}
    rows = []
    for line in lines:
        product = products.get(line.product_id)
        now_stock = int(product.stock or 0) if product else None
        change = int(line.counted) - int(line.stock_at_count)
        if count.status == "applied":
            result = None if line.applied_delta is None else line.applied_delta
        else:
            result = None if product is None else max(0, now_stock + change) - now_stock
        if count.status == "applied" and line.unit_cost is not None:
            cost = float(line.unit_cost)                 # frozen when the count was applied (v51)
        else:
            cost = float(product.cost or 0) if product else 0.0
        reason = line.reason if reason_fits(line.reason or "", change) else ""
        rows.append({
            "product_id": line.product_id, "sku": product.sku if product else line.sku, "name": product.name if product else line.name,
            "counted": int(line.counted), "at_count": int(line.stock_at_count), "now": now_stock,
            "change": change, "will_change": result, "deleted": product is None, "cost": cost,
            "value": round((result or 0) * cost, 2), "updated_at": line.updated_at, "counted_by": line.counted_by,
            "reason": reason, "reason_label": reason_label(reason) if reason else "",
        })
    rows.sort(key=lambda r: (-abs(r["change"]), r["name"].lower()))
    return rows


def uncounted(db: Session, count: StockCount, limit: int = 300) -> tuple[list[Product], int]:
    """Products in the count's scope that have no line, with the total number (the list is cut at `limit`)."""
    counted_ids = db.query(StockCountLine.product_id).filter(StockCountLine.count_id == count.id)
    q = db.query(Product).filter(~Product.id.in_(counted_ids))
    if count.scope_category:
        q = q.filter(Product.category == count.scope_category)
    total = q.count()
    return q.order_by(Product.name).limit(limit).all(), total


def summary(rows: list[dict]) -> dict:
    changed = [r for r in rows if r["will_change"]]
    return {
        "lines": len(rows), "units_counted": sum(r["counted"] for r in rows), "changed": len(changed),
        "units_net": sum(r["will_change"] or 0 for r in rows), "value_net": round(sum(r["value"] for r in rows), 2),
        "missing_units": -sum(r["will_change"] for r in changed if r["will_change"] < 0),
        "extra_units": sum(r["will_change"] for r in changed if r["will_change"] > 0),
    }


def set_reasons(db: Session, count: StockCount, reasons: dict) -> int:
    """Save the reasons picked on the review screen ({product_id: key}; "" clears). A reason that does not fit the
    direction of the difference, or that is not on the list, is ignored. Returns how many lines changed."""
    count = _require_open(count)
    if not reasons:
        return 0
    changed = 0
    for line in db.query(StockCountLine).filter(StockCountLine.count_id == count.id, StockCountLine.product_id.in_(list(reasons))).all():
        wanted = str(reasons.get(line.product_id) or "").strip()
        diff = int(line.counted) - int(line.stock_at_count)
        if wanted and not reason_fits(wanted, diff):
            continue
        if (line.reason or "") != wanted:
            line.reason = wanted
            changed += 1
    db.flush()
    return changed


# ═══ applying ════════════════════════════════════════════════════════

def apply_count(db: Session, count: StockCount, staff_name: str, zero_uncounted: bool = False, now: Optional[datetime] = None) -> dict:
    """Write the counted numbers into the stock, all or nothing (the caller commits). Returns the totals."""
    count = _require_open(count)
    now = now or datetime.utcnow()
    lines = db.query(StockCountLine).filter(StockCountLine.count_id == count.id).all()
    ids = [l.product_id for l in lines]
    query = db.query(Product).filter(Product.id.in_(ids or [""]))
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()          # a sale being rung up right now waits its turn
    products = {p.id: p for p in query.all()}
    items_changed, units_net, value_net = 0, 0, 0.0
    for line in lines:
        product = products.get(line.product_id)
        if product is None:
            line.applied_delta = None            # the product was deleted since: nothing to update
            continue
        before = int(product.stock or 0)
        after = max(0, before + (int(line.counted) - int(line.stock_at_count)))
        product.stock = after
        line.applied_delta = after - before
        line.unit_cost = float(product.cost or 0)
        if not reason_fits(line.reason or "", int(line.counted) - int(line.stock_at_count)):
            line.reason = ""
        if after != before:
            items_changed += 1
            units_net += after - before
            value_net += (after - before) * float(product.cost or 0)
    if zero_uncounted:
        rest, _ = uncounted(db, count, limit=10 ** 9)
        for product in rest:
            before = int(product.stock or 0)
            if before <= 0:
                continue
            db.add(StockCountLine(count_id=count.id, product_id=product.id, sku=product.sku or "", name=product.name or "",
                                  counted=0, stock_at_count=before, counted_by=staff_name or "", first_at=now, updated_at=now,
                                  applied_delta=-before, unit_cost=float(product.cost or 0)))
            product.stock = 0
            items_changed += 1
            units_net -= before
            value_net -= before * float(product.cost or 0)
    count.status = "applied"
    count.closed_by, count.closed_at = staff_name or "", now
    count.items_changed, count.units_net, count.value_net = items_changed, units_net, round(value_net, 2)
    count.zero_filled = bool(zero_uncounted)
    db.flush()
    return {"items_changed": items_changed, "units_net": units_net, "value_net": round(value_net, 2)}


def cancel_count(db: Session, count: StockCount, staff_name: str, now: Optional[datetime] = None) -> None:
    count = _require_open(count)
    count.status = "cancelled"
    count.closed_by, count.closed_at = staff_name or "", now or datetime.utcnow()
    db.flush()
