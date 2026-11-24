"""
Card processing fees and the Moneris monthly statement (v47).

The profit reports used to stop at gross profit. Card fees are a real cost
of every card sale, so the owner can type each month's fees from the Moneris
statement (CardStatement) and the reports subtract them. For months without
a statement an optional estimated rate (% of card sales) fills the gap — and
the report says which figure it used, so an estimate is never passed off as
an actual.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import func

from .localtime import day_bounds_utc
from .models import CardStatement, Invoice, LayawayPayment, Setting
from . import returns as _returns


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def parse_month_key(s: str) -> date | None:
    try:
        y, m = (s or "").strip()[:7].split("-")
        y, m = int(y), int(m)
        if 1 <= m <= 12 and 2000 <= y <= 2100:
            return date(y, m, 1)
    except (ValueError, AttributeError):
        pass
    return None


def month_range(first: date) -> tuple[date, date]:
    return first, date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])


def fee_rate(db) -> float | None:
    """Estimated card fee as a percentage of card sales (e.g. 2.65), or None."""
    row = db.get(Setting, "card_fee_rate")
    try:
        v = float(row.value) if row and str(row.value).strip() != "" else None
    except ValueError:
        return None
    return v if v is not None and 0 < v < 20 else None


def crm_card_month(db, tz, first: date) -> dict:
    """What the CRM says went through the card terminal in a calendar month:
    card money taken (invoices + layaway card instalments), card refunds paid
    back to cards, and the net — the figure to compare with the statement."""
    d1, d2 = month_range(first)
    s, e = day_bounds_utc(d1, d2, tz)
    taken = float(db.query(func.coalesce(func.sum(Invoice.card_amount), 0.0)).filter(Invoice.date >= s, Invoice.date < e).scalar() or 0)
    lay = float(db.query(func.coalesce(func.sum(LayawayPayment.amount), 0.0))
                .filter(LayawayPayment.created_at >= s, LayawayPayment.created_at < e,
                        LayawayPayment.method.in_(("Debit", "Credit"))).scalar() or 0)
    refunds = 0.0
    for inv in db.query(Invoice).filter(Invoice.refunded.is_(True), Invoice.refund_method == "Card",
                                        Invoice.refunded_at >= s, Invoice.refunded_at < e):
        refunds += inv.total or 0
    refunds += _returns.sum_total(db, s, e, "Card")      # v52: part of a sale returned to the card
    return {"taken": round(taken + lay, 2), "refunds": round(refunds, 2), "net": round(taken + lay - refunds, 2)}


def card_fees_for_period(db, tz, d1: date, d2: date) -> dict:
    """Card fees attributable to the local-date range d1..d2.

    Each calendar month the range touches contributes either
      * the statement's fees, pro-rated by days when the range covers only
        part of the month (exact for a whole month), or
      * an estimate: the configured rate x card money taken in those days,
    and months with neither contribute nothing. Returns
    {amount, basis: 'actual'|'estimated'|'mixed'|'none', note}."""
    rate = fee_rate(db)
    amount, kinds, prorated = 0.0, set(), False
    cur = date(d1.year, d1.month, 1)
    while cur <= d2:
        m1, m2 = month_range(cur)
        o1, o2 = max(d1, m1), min(d2, m2)
        days_in_month = (m2 - m1).days + 1
        overlap = (o2 - o1).days + 1
        st = db.query(CardStatement).filter(CardStatement.month == month_key(cur)).first()
        if st is not None and st.fees is not None:
            frac = overlap / days_in_month
            amount += (st.fees or 0) * frac
            kinds.add("actual")
            prorated = prorated or overlap != days_in_month
        elif rate is not None:
            s, e = day_bounds_utc(o1, o2, tz)
            base = float(db.query(func.coalesce(func.sum(Invoice.card_amount), 0.0))
                         .filter(Invoice.date >= s, Invoice.date < e, Invoice.refunded.isnot(True)).scalar() or 0)
            base = max(0.0, base - _returns.sum_total(db, s, e, "Card"))   # v52: card money handed back on partial returns
            amount += base * rate / 100.0
            kinds.add("estimated")
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    basis = "none" if not kinds else ("mixed" if len(kinds) > 1 else next(iter(kinds)))
    if basis == "actual":
        note = "from your Moneris statement" + (" (pro-rated for part of a month)" if prorated else "")
    elif basis == "estimated":
        note = f"estimated at {rate:g}% of card sales — enter your statement for the exact figure"
    elif basis == "mixed":
        note = "statement figures where entered, an estimate for other months"
    else:
        note = ""
    return {"amount": round(amount, 2), "basis": basis, "note": note}
