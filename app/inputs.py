"""Sanity limits for typed amounts and quantities (v52.2).

The forms already stop most nonsense in the browser (number boxes, min/max). These helpers do the same on the server, because a
request does not have to come from the form: "nan", "inf", a minus sign or a huge number saved as a price breaks every page that
lists or totals products ("Out of range float values are not JSON compliant"), and a number past the database's integer limit
turns into a server error on PostgreSQL. Found by the whole-build end-to-end run."""
import math

MONEY_MAX = 1_000_000.0       # one price, cost, deposit or payment typed into a form
QTY_MAX = 100_000             # one stock level, reorder level or cart quantity
POINTS_MAX = 10_000_000


def money(value, lo: float = 0.0, hi: float = MONEY_MAX) -> bool:
    """True when `value` is a real number from lo to hi (not nan, not infinite)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(x) and lo <= x <= hi


def whole(value, lo: int = 0, hi: int = QTY_MAX) -> bool:
    """True when `value` is a whole number from lo to hi."""
    try:
        x = int(value)
    except (TypeError, ValueError):
        return False
    return lo <= x <= hi


def money_text(hi: float = MONEY_MAX) -> str:
    return f"$0 to ${hi:,.0f}"


def parse_money(text, hi: float = MONEY_MAX):
    """For a typed amount that may be left blank. Returns (value, error): (None, None) for blank, (number, None) when fine,
    (None, "must be ...") when it is not a real number in range."""
    raw = (text or "").replace("$", "").replace(",", "").strip() if isinstance(text, str) else text
    if raw in ("", None):
        return None, None
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None, f"must be a number from {money_text(hi)}"
    if not money(x, 0.0, hi):
        return None, f"must be a number from {money_text(hi)}"
    return round(x, 2), None
