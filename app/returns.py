"""
v52: partial returns — take back SOME of an invoice (one phone case out of three items, 1 of 2 chargers) and refund just
that, instead of refunding the whole sale.

How the money is worked out (everything in whole cents, so a rounding rule can never leave a stray penny):

  * The customer gets back what they actually paid for the returned items: the items' share of the invoice's NET
    (after any discount) plus the same share of EACH tax line (GST, PST, HST ...).
  * "Share" is cumulative. If an invoice is returned in three pieces, the pieces add up to EXACTLY the invoice total —
    the last piece takes whatever rounding is left over — so a sale can never be refunded for more than was paid.
  * Cash refunds are rounded to the nearest 5 cents the same way a cash sale is (when that setting is on).

The return is stored as its own record (SaleReturn + SaleReturnLine). The invoice stays a normal paid sale; reports,
Cash Up, the card check and the dashboard count each return as a refund on the day it happened.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Customer, Invoice, InvoiceLine, Product, SaleReturn, SaleReturnLine, Setting
from .money import cash_refund_amount, from_cents, to_cents

REFUND_METHODS = ("Cash", "Card", "Store Credit", "E-Transfer")
MAX_REASON = 200


def points_for(db: Session, amount: float) -> int:
    """Loyalty points a sale of `amount` earns: the same whole-points rule the checkout uses (int(total x points per dollar))."""
    row = db.query(Setting).filter(Setting.key == "points_per_dollar").first()
    try:
        per_dollar = float(row.value) if row is not None else 1.0
    except (TypeError, ValueError):
        per_dollar = 1.0
    return int(max(0.0, float(amount or 0)) * per_dollar)


class ReturnError(Exception):
    """A problem that can be shown to staff as it is."""


# ── what is left to return ────────────────────────────────────────────

def returned_by_line(invoice: Invoice) -> dict:
    """{invoice_line_id: units already returned}."""
    out: dict = {}
    for r in invoice.returns:
        for l in r.lines:
            if l.invoice_line_id:
                out[l.invoice_line_id] = out.get(l.invoice_line_id, 0) + int(l.qty or 0)
    return out


def line_status(invoice: Invoice) -> list:
    """One row per invoice line: how many were sold, how many already came back, how many could still be returned."""
    done = returned_by_line(invoice)
    rows = []
    for l in invoice.lines:
        sold = int(l.qty or 0)
        back = min(sold, done.get(l.id, 0))
        rows.append({"line": l, "sold": sold, "returned": back, "left": max(0, sold - back)})
    return rows


def state(invoice: Invoice) -> str:
    """'none' | 'partial' | 'all' — how much of the invoice has come back through partial returns."""
    rows = line_status(invoice)
    if not invoice.returns:
        return "none"
    return "all" if rows and all(r["left"] == 0 for r in rows) else "partial"


def returned_total(invoice: Invoice) -> float:
    return round(sum(float(r.total or 0) for r in invoice.returns), 2)


def can_start(invoice: Invoice) -> str:
    """'' when items can be returned, otherwise the reason they cannot."""
    if invoice.refunded:
        return "This invoice was already refunded in full."
    if not invoice.lines:
        return "This invoice has no items to return."
    if all(r["left"] == 0 for r in line_status(invoice)):
        return "Every item on this invoice has already been returned."
    return ""


# ── the arithmetic ────────────────────────────────────────────────────

def _tax_lines(invoice: Invoice) -> list:
    """[(label, cents)] — the invoice's tax by label. An invoice with a tax total but no usable breakdown gets one line."""
    try:
        raw = json.loads(invoice.tax_breakdown or "[]")
    except (ValueError, TypeError):
        raw = []
    out = []
    for t in raw if isinstance(raw, list) else []:
        try:
            out.append((str(t.get("label") or "Tax"), to_cents(t.get("amount") or 0)))
        except (AttributeError, TypeError, ValueError):
            continue
    if not out and to_cents(invoice.tax_total or 0):
        out = [("Tax", to_cents(invoice.tax_total))]
    return out


def _gross_cents(price, qty) -> int:
    return to_cents(price or 0) * int(qty or 0)


def plan(invoice: Invoice, picks: dict) -> dict:
    """Works out a return without saving anything.

    `picks` is {invoice_line_id: units to return}. Returns {"lines", "subtotal", "discount", "net", "tax_lines",
    "tax_total", "total"} with money in dollars, or raises ReturnError."""
    status = {r["line"].id: r for r in line_status(invoice)}
    chosen = []
    for lid, qty in (picks or {}).items():
        if lid not in status:
            raise ReturnError("One of the items does not belong to this invoice.")
        try:
            n = int(qty)
        except (TypeError, ValueError):
            raise ReturnError("Quantities must be whole numbers.")
        if n < 0:
            raise ReturnError("A quantity can't be negative.")
        if n == 0:
            continue
        if n > status[lid]["left"]:
            nm = status[lid]["line"].name
            raise ReturnError(f"Only {status[lid]['left']} of {nm} can still be returned (you asked for {n}).")
        chosen.append((status[lid]["line"], n))
    if not chosen:
        raise ReturnError("Choose at least one item to return.")

    already = returned_by_line(invoice)
    all_gross = sum(_gross_cents(l.price, l.qty) for l in invoice.lines)           # list price x qty of everything sold
    sel_gross = sum(_gross_cents(l.price, n) for l, n in chosen)                   # ...of what comes back now
    cum_gross = sum(_gross_cents(l.price, already.get(l.id, 0)) for l in invoice.lines) + sel_gross   # ...back so far, incl. now
    picked = {l.id: n for l, n in chosen}
    everything = all(s["left"] - picked.get(lid, 0) <= 0 for lid, s in status.items())

    net_total = to_cents(invoice.total or 0) - to_cents(invoice.tax_total or 0)
    prev_net = sum(to_cents(r.total or 0) - to_cents(r.tax_total or 0) for r in invoice.returns)

    def share(whole_c: int, already_c: int) -> int:
        """This return's slice of `whole_c`, given `already_c` was refunded before. Cumulative and half-up, so the
        pieces of one invoice add up to exactly the invoice (the last piece absorbs the rounding)."""
        if everything:
            target = whole_c
        elif all_gross <= 0:
            target = 0
        else:
            target = (2 * whole_c * cum_gross + all_gross) // (2 * all_gross)
        return target - already_c

    net_c = share(net_total, prev_net)
    prev_tax = {}
    for r in invoice.returns:
        try:
            for t in json.loads(r.tax_breakdown or "[]"):
                prev_tax[str(t.get("label") or "Tax")] = prev_tax.get(str(t.get("label") or "Tax"), 0) + to_cents(t.get("amount") or 0)
        except (ValueError, TypeError, AttributeError):
            continue
    tax_rows, tax_c = [], 0
    for label, whole in _tax_lines(invoice):
        part = share(whole, prev_tax.get(label, 0))
        part = max(0, part)
        tax_rows.append({"label": label, "amount": from_cents(part)})
        tax_c += part
    net_c = max(0, net_c)
    total_c = net_c + tax_c
    discount_c = max(0, sel_gross - net_c)
    return {
        "lines": [{"line": l, "qty": n, "gross": from_cents(_gross_cents(l.price, n))} for l, n in chosen],
        "subtotal": from_cents(sel_gross), "discount": from_cents(discount_c), "net": from_cents(net_c),
        "tax_lines": tax_rows, "tax_total": from_cents(tax_c), "total": from_cents(total_c),
    }


# ── saving it ─────────────────────────────────────────────────────────

def _category_for(db: Session, line: InvoiceLine, invoice: Invoice, products: dict) -> str:
    """The category to remember for a returned line (the same fallbacks the reports use for a sold line)."""
    if line.category:
        return line.category
    p = products.get(line.product_id) if line.product_id else None
    if p is not None and p.category:
        return p.category
    if (line.sku or "").startswith("RPR-") or invoice.repair_id:
        return "REPAIR"
    return "UNCATEGORIZED" if line.product_id else "OTHER"


def record(db: Session, staff, invoice: Invoice, picks: dict, restock: dict, method: str, reason: str) -> SaleReturn:
    """Saves a return after re-checking everything (the form could be stale or tampered with). Does not commit."""
    problem = can_start(invoice)
    if problem:
        raise ReturnError(problem)
    method = method if method in REFUND_METHODS else ""
    if not method:
        raise ReturnError("Choose how the money goes back to the customer.")
    if method == "Store Credit" and not invoice.customer_id:
        raise ReturnError("Attach a customer to this invoice before refunding to store credit — nothing was returned.")
    p = plan(invoice, picks)

    seq = (db.query(func.max(SaleReturn.seq)).filter(SaleReturn.invoice_id == invoice.id).scalar() or 0) + 1
    now = datetime.utcnow()
    cash_paid = cash_refund_amount(db, p["total"]) if method == "Cash" else 0.0
    ret = SaleReturn(
        invoice_id=invoice.id, seq=seq, number=f"{invoice.number}-R{seq}", customer_id=invoice.customer_id,
        date=invoice.date, refunded_at=now, refunded_by=staff.name, refund_method=method,
        refund_reason=(reason or "").strip()[:MAX_REASON], subtotal=p["subtotal"], discount=p["discount"],
        tax_breakdown=json.dumps(p["tax_lines"]), tax_total=p["tax_total"], total=p["total"], cash_paid=cash_paid,
    )
    db.add(ret)
    products = {pr.id: pr for pr in db.query(Product).filter(Product.id.in_([x["line"].product_id for x in p["lines"] if x["line"].product_id] or [""])).all()}
    for item in p["lines"]:
        l, n = item["line"], item["qty"]
        shelf = bool(restock.get(l.id, True)) and bool(l.product_id) and l.product_id in products
        if shelf:
            products[l.product_id].stock = (products[l.product_id].stock or 0) + n
        ret.lines.append(SaleReturnLine(
            invoice_line_id=l.id, product_id=l.product_id, name=l.name, sku=l.sku or "", imei=l.imei or "", qty=n,
            price=l.price or 0, unit_cost=l.unit_cost, category=_category_for(db, l, invoice, products), restocked=shelf))
    if invoice.customer_id:
        customer = db.get(Customer, invoice.customer_id)
        if customer:
            customer.spent = round((customer.spent or 0) - p["total"], 2)
            # points earned on the money that went back are taken back too
            customer.points = max(0, (customer.points or 0) - points_for(db, p["total"]))
            if method == "Store Credit":
                customer.store_credit = round((customer.store_credit or 0) + p["total"], 2)
    db.flush()
    return ret


def describe(ret: SaleReturn) -> str:
    items = ", ".join(f"{l.qty}× {l.name}" for l in ret.lines)
    return f"{ret.number}: {items} — ${ret.total:.2f} via {ret.refund_method or 'unspecified'}"


# ── for the reports ───────────────────────────────────────────────────

def in_window(db: Session, start_utc, end_utc) -> list:
    """Every partial return that happened between two UTC moments, with its lines and customer loaded."""
    from sqlalchemy.orm import selectinload
    return (db.query(SaleReturn).options(selectinload(SaleReturn.lines), selectinload(SaleReturn.customer))
            .filter(SaleReturn.refunded_at >= start_utc, SaleReturn.refunded_at < end_utc)
            .order_by(SaleReturn.refunded_at).all())


def sum_total(db: Session, start_utc, end_utc, method: str | None = None) -> float:
    q = db.query(func.coalesce(func.sum(SaleReturn.total), 0.0)).filter(SaleReturn.refunded_at >= start_utc, SaleReturn.refunded_at < end_utc)
    if method:
        q = q.filter(SaleReturn.refund_method == method)
    return float(q.scalar() or 0)


def cash_out(db: Session, start_utc, end_utc) -> float:
    """Cash that physically left the drawer for returns in the window (what Cash Up subtracts)."""
    return float(db.query(func.coalesce(func.sum(SaleReturn.cash_paid), 0.0))
                 .filter(SaleReturn.refunded_at >= start_utc, SaleReturn.refunded_at < end_utc,
                         SaleReturn.refund_method == "Cash").scalar() or 0)
