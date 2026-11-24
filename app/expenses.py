"""
v52: expenses and till paid-outs.

Cash taken out of the drawer (supplies, a lunch run, a supplier paid in cash, a bank deposit) used to leave no
trace, so Cash Up came up short with no explanation. An expense recorded here with `paid_from = "drawer"` lowers
the cash Cash Up expects; one paid another way (business card, bank, the owner's own pocket) is only a record for
the books and never touches the drawer.

Rules kept in one place so the Cash Up page, the Daily report and the expenses report can't disagree:

  * amount is what was paid, tax included; tax_paid is the GST/HST/PST inside it (kept so the accountant can
    claim input tax credits) and can never exceed the amount;
  * a voided entry stays on file (who, when, why) but counts for nothing;
  * "Bank deposit" and "Owner draw" take cash out of the drawer but are NOT business expenses, so they are
    left out of expense totals and tax-paid totals;
  * everything is dated by the shop's local day, never UTC.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Expense
from .money import from_cents, to_cents

CATEGORIES = (
    "Supplies", "Parts & inventory", "Food & drink", "Fuel & delivery", "Rent & utilities",
    "Repairs & maintenance", "Advertising", "Wages", "Bank & card fees", "Other",
    "Bank deposit", "Owner draw",
)
# cash that leaves the till but is not a cost of running the shop
NOT_AN_EXPENSE = ("Bank deposit", "Owner draw")
PAID_FROM = {"drawer": "Cash from the till", "other": "Paid another way (card, bank, own pocket)"}

MAX_AMOUNT = 100_000.00
MAX_TEXT = {"description": 200, "payee": 80, "receipt_ref": 60, "void_reason": 200}


class ExpenseError(Exception):
    """Something the person typed can't be saved; the message is shown to them as is."""


def _clean(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_money(value, label: str, allow_zero: bool = False) -> float:
    raw = str(value if value is not None else "").replace("$", "").replace(",", "").strip()
    if raw == "":
        if allow_zero:
            return 0.0
        raise ExpenseError(f"Enter the {label}.")
    try:
        x = float(raw)
    except ValueError:
        raise ExpenseError(f"The {label} must be a number, like 12.50.")
    if x != x or x in (float("inf"), float("-inf")):
        raise ExpenseError(f"The {label} must be a number, like 12.50.")
    if x < 0 or (x == 0 and not allow_zero):
        raise ExpenseError(f"The {label} must be more than zero.")
    if x > MAX_AMOUNT:
        raise ExpenseError(f"The {label} is too large (the limit is ${MAX_AMOUNT:,.0f}).")
    return from_cents(to_cents(x))


def validate(amount, tax_paid, category, description, payee, receipt_ref, paid_from) -> dict:
    """Turns raw form text into clean values, or raises ExpenseError saying what to fix."""
    amt = parse_money(amount, "amount")
    tax = parse_money(tax_paid, "tax amount", allow_zero=True)
    if category not in CATEGORIES:
        raise ExpenseError("Pick a category from the list.")
    if paid_from not in PAID_FROM:
        raise ExpenseError("Say whether this came out of the till or was paid another way.")
    desc = _clean(description, MAX_TEXT["description"])
    if not desc:
        raise ExpenseError("Say what it was for (a few words is fine).")
    if category in NOT_AN_EXPENSE:
        tax = 0.0
    if tax > amt:
        raise ExpenseError("The tax can't be more than the total amount paid.")
    if category in NOT_AN_EXPENSE and paid_from != "drawer":
        raise ExpenseError(f"“{category}” is cash leaving the till, so it has to come from the till.")
    return {"amount": amt, "tax_paid": tax, "category": category, "description": desc,
            "payee": _clean(payee, MAX_TEXT["payee"]), "receipt_ref": _clean(receipt_ref, MAX_TEXT["receipt_ref"]),
            "paid_from": paid_from}


def record(db: Session, staff, values: dict, spent_at: datetime | None = None) -> Expense:
    """Saves one entry (does not commit). `values` must come from validate()."""
    e = Expense(spent_at=spent_at or datetime.utcnow(), staff_id=getattr(staff, "id", None),
                staff_name=getattr(staff, "name", "") or "", **values)
    db.add(e)
    db.flush()
    return e


def void(db: Session, staff, expense: Expense, reason: str) -> None:
    if expense.voided:
        raise ExpenseError("That entry was already voided.")
    why = _clean(reason, MAX_TEXT["void_reason"])
    if not why:
        raise ExpenseError("Say why you're voiding it.")
    expense.voided = True
    expense.voided_at = datetime.utcnow()
    expense.voided_by = getattr(staff, "name", "") or ""
    expense.void_reason = why


def describe(e: Expense) -> str:
    who = f" to {e.payee}" if e.payee else ""
    return f"${e.amount:.2f} {e.category}{who}: {e.description} ({'till' if e.paid_from == 'drawer' else 'other'})"


# ── queries ───────────────────────────────────────────────────────────

def _live(q):
    return q.filter(Expense.voided.isnot(True))


def in_window(db: Session, start_utc, end_utc, include_voided: bool = False) -> list:
    q = db.query(Expense).filter(Expense.spent_at >= start_utc, Expense.spent_at < end_utc)
    if not include_voided:
        q = _live(q)
    return q.order_by(Expense.spent_at, Expense.created_at).all()


def drawer_out(db: Session, start_utc, end_utc) -> float:
    """Cash that left the till in the window — what Cash Up subtracts from the expected drawer."""
    total = (_live(db.query(func.coalesce(func.sum(Expense.amount), 0.0)))
             .filter(Expense.spent_at >= start_utc, Expense.spent_at < end_utc, Expense.paid_from == "drawer")
             .scalar())
    return round(float(total or 0), 2)


def business_total(db: Session, start_utc, end_utc) -> dict:
    """Totals of real business costs (not deposits / owner draws) in the window, any way they were paid."""
    q = (_live(db.query(func.coalesce(func.sum(Expense.amount), 0.0), func.coalesce(func.sum(Expense.tax_paid), 0.0),
                        func.count(Expense.id)))
         .filter(Expense.spent_at >= start_utc, Expense.spent_at < end_utc, Expense.category.notin_(NOT_AN_EXPENSE)))
    amount, tax, n = q.one()
    return {"amount": round(float(amount or 0), 2), "tax_paid": round(float(tax or 0), 2), "count": int(n or 0)}


def by_category(rows) -> list:
    """[(category, count, amount, tax)] largest first, for the expenses report."""
    out: dict = {}
    for e in rows:
        c = out.setdefault(e.category or "Other", [0, 0, 0])      # count, cents, tax cents
        c[0] += 1
        c[1] += to_cents(e.amount)
        c[2] += to_cents(e.tax_paid)
    return sorted(((k, v[0], from_cents(v[1]), from_cents(v[2])) for k, v in out.items()), key=lambda r: -r[2])
