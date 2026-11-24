"""
Money helpers shared by the POS, the reports and the reconciliation screen.

Kept free of FastAPI/SQLAlchemy-session state so they can be unit tested on
their own. Everything works in integer cents internally — floats are only
used at the edges — so a rounding rule can never be off by a binary-float
hair.
"""
from __future__ import annotations

import re

from .models import Setting


def to_cents(x) -> int:
    """Float dollars -> integer cents (half-up on the absolute value)."""
    x = float(x or 0)
    sign = -1 if x < 0 else 1
    return sign * int(abs(x) * 100 + 0.5 + 1e-9)


def from_cents(c: int) -> float:
    return round(c / 100.0, 2)


def round_cash(amount) -> float:
    """Canadian cash rounding: the penny is gone, so a cash payment is
    rounded to the nearest 5 cents — 1-2 cents round down, 3-4 round up,
    6-7 down, 8-9 up. Card, e-transfer and other electronic payments are
    NOT rounded; only the cash that physically changes hands is.

        10.01 -> 10.00   10.03 -> 10.05   10.06 -> 10.05   10.08 -> 10.10
    """
    c = to_cents(amount)
    sign = -1 if c < 0 else 1
    c = abs(c)
    r = c % 5
    if r in (1, 2):
        c -= r
    elif r in (3, 4):
        c += 5 - r
    return from_cents(sign * c)


def _setting_bool(db, key: str, default: bool) -> bool:
    row = db.get(Setting, key)
    if row is None or row.value is None or str(row.value).strip() == "":
        return default
    return str(row.value).strip().lower() in ("1", "true", "on", "yes")


def cash_rounding_on(db) -> bool:
    """Default ON: Canadian shops round cash to 5 cents. One toggle in
    Settings turns it off for a shop that doesn't."""
    return _setting_bool(db, "cash_rounding", True)


def require_card_ref_on(db) -> bool:
    """Default ON: a card sale needs the Moneris reference so it can be
    matched against the terminal batch later."""
    return _setting_bool(db, "require_card_ref", True)


def cash_refund_amount(db, total) -> float:
    """What physically leaves the drawer when an invoice is refunded in
    cash — the total, rounded the same way the sale was."""
    return round_cash(total) if cash_rounding_on(db) else round(float(total or 0), 2)


# ── never keep a full card number ─────────────────────────────────────

_DIGIT_RUN = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _pan_shape_ok(d: str) -> bool:
    """True when the digit run has the length AND leading digits of a real
    card network (Visa, Mastercard, Amex, Discover, JCB, Diners, UnionPay).

    Luhn alone is not enough: a long numeric terminal reference passes Luhn
    one time in ten, and Moneris references are long numbers. Requiring a
    real network prefix and length keeps those references intact."""
    n = len(d)
    if d[0] == "4":                                            # Visa
        return n in (13, 16, 19)
    if n == 16 and (51 <= int(d[:2]) <= 55 or 2221 <= int(d[:4]) <= 2720):   # Mastercard
        return True
    if n == 15 and d[:2] in ("34", "37"):                      # Amex
        return True
    if 16 <= n <= 19:
        p2, p3, p4 = d[:2], int(d[:3]), int(d[:4])
        if d[:4] == "6011" or p2 == "65" or 644 <= p3 <= 649:  # Discover
            return True
        if 3528 <= p4 <= 3589:                                 # JCB
            return True
        if p2 == "62":                                         # UnionPay
            return True
    if 14 <= n <= 19 and (d[:2] in ("36", "38", "39") or 300 <= int(d[:3]) <= 305):   # Diners
        return True
    return False


def scrub_card_numbers(text: str) -> tuple[str, bool]:
    """Masks anything that looks like a payment card number (13-19 digits,
    a real card network's leading digits and length, passing the Luhn check,
    with or without spaces/dashes) down to its last four digits. Returns
    (clean_text, changed).

    This keeps the CRM out of card-data territory: a staff member pasting
    the card number into the reference box by mistake never gets it stored.
    It masks rather than rejects, so a sale is never blocked by a false
    positive."""
    changed = False

    def repl(m):
        nonlocal changed
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _pan_shape_ok(digits) and luhn_ok(digits):
            changed = True
            return "*" * (len(digits) - 4) + digits[-4:]
        return m.group(0)

    return _DIGIT_RUN.sub(repl, text or ""), changed


# ── a checkout's payment, worked out in one place ─────────────────────

def plan_payment(payment_method: str, total: float, tendered: float, cash_part: float,
                 card_part: float, rounding_on: bool) -> dict:
    """Works out how a checkout's payment splits into drawer cash, terminal
    card, the rounding adjustment and the change to hand back. Returns a
    dict with `error` (str or None) plus payment_method, tendered, cash_amount,
    card_amount, cash_rounding, change_given.

    Rules:
      * Cash: the amount due is the total rounded to 5c (if rounding is on).
        cash_amount is what lands in the drawer; cash_rounding is the
        difference from the true total (+ means the customer paid slightly
        more). Tax and revenue always stay on the true total.
      * Split: the card side is exact; the cash side covers what is left
        after the card, rounded to 5c. Any overpayment comes back as change
        out of the cash side.
      * Card / E-Transfer / UPI / Store Credit: never rounded.
    """
    total = round(float(total or 0), 2)
    tendered = float(tendered or 0)
    cash_part = float(cash_part or 0)
    card_part = float(card_part or 0)
    out = {"error": None, "payment_method": payment_method, "tendered": tendered,
           "cash_amount": 0.0, "card_amount": 0.0, "cash_rounding": 0.0, "change_given": 0.0}

    # A "Split" with only one side filled in is really a Cash or a Card sale; with nothing (or a minus) entered it is not a
    # payment at all. Before v52.2 these were saved as "Split" with no cash and no card, so the money counted in neither
    # column of Cash Up.
    if payment_method == "Split" and not (cash_part > 0 and card_part > 0):
        if cash_part < 0 or card_part < 0 or (cash_part <= 0 and card_part <= 0):
            out["error"] = "Enter the cash and the card amounts for a split payment (together they must cover the total)."
            return out
        if card_part > 0:
            if round(card_part, 2) < round(total, 2):
                out["error"] = f"Split payment total (${card_part:.2f}) is less than the invoice total (${total:.2f})."
                return out
            if round(card_part, 2) > round(total, 2):
                out["error"] = f"The card amount (${card_part:.2f}) is more than the total (${total:.2f}) and there is no cash to give change from."
                return out
            payment_method, tendered = "Card", 0.0
        else:
            payment_method, tendered = "Cash", cash_part
        out["payment_method"], out["tendered"] = payment_method, tendered

    if cash_part > 0 and card_part > 0:
        remaining = round(total - card_part, 2)
        if remaining > 0:
            due = round_cash(remaining) if rounding_on else remaining
            if round(cash_part, 2) < due:
                short = round(due - cash_part, 2)
                if rounding_on and due != remaining:
                    out["error"] = (f"Cash is short — ${due:.2f} is due in cash (${remaining:.2f} rounded to the "
                                    f"nearest 5¢), ${short:.2f} short.")
                else:
                    out["error"] = f"Split payment total (${cash_part + card_part:.2f}) is less than the invoice total (${total:.2f})."
                return out
            out["cash_rounding"] = round(due - remaining, 2)
            out["cash_amount"] = round(due, 2)
            out["change_given"] = round(cash_part - due, 2)
        else:
            # The card alone covers the sale (a card overpayment): keep the
            # long-standing behaviour — any excess comes back out of cash.
            change = max(0.0, round(cash_part + card_part - total, 2))
            out["cash_amount"] = round(cash_part - change, 2)
            out["change_given"] = change
        out["card_amount"] = round(card_part, 2)
        out["payment_method"] = f"Split (Cash ${cash_part:.2f} + Card ${card_part:.2f})"
        out["tendered"] = cash_part
        return out

    if payment_method == "Cash":
        due = round_cash(total) if rounding_on else total
        if tendered > 0 and round(tendered, 2) < due:
            out["error"] = (f"Tendered amount (${tendered:.2f}) is less than the cash due (${due:.2f})."
                            if due != total else f"Tendered amount (${tendered:.2f}) is less than the total (${total:.2f}).")
            return out
        out["cash_amount"] = due
        out["cash_rounding"] = round(due - total, 2)
        out["change_given"] = round(tendered - due, 2) if tendered > 0 else 0.0
        return out

    if payment_method == "Card":
        out["card_amount"] = total
    # UPI / E-Transfer / Store Credit settle outside the drawer and terminal
    out["change_given"] = max(0.0, round(tendered - total, 2)) if tendered > 0 else 0.0
    return out
