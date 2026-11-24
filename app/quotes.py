"""
v52: repair quote approval by text.

Staff send the customer a private link; the customer opens it on their phone and taps Approve or Decline — no
sign-in, no phone digits. The link is a long random token (so it can't be guessed), opening it never changes
anything (a text-message scanner or link preview can't "approve" a quote), only a deliberate tap on the page does.

Rules, kept here so the screens, the text and the tests all agree:

  * one live (pending) quote per ticket: sending a new one retires the old link ("replaced");
  * a quote is valid for a number of days, then it is "expired" and the link says so;
  * an answer is final: the first Approve / Decline wins, the link can't be used to change it, and two taps at the
    same instant can't both win (the database update only succeeds while the quote is still pending);
  * approving sets the ticket's estimate to the approved price and writes a line in the ticket's history — it does
    NOT move the ticket to another status, staff do that;
  * the customer only ever sees: shop name, ticket number, device, what was quoted, the price before tax and the
    shop's note. Never the customer's name, other tickets, costs or margin.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import QuoteApproval, Repair

VALID_DAY_CHOICES = (3, 7, 14, 30)
DEFAULT_VALID_DAYS = 7
MAX_PRICE = 100_000.00
MAX_SUMMARY, MAX_NOTE, MAX_COMMENT = 120, 300, 300
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,64}$")

LIVE = "pending"
ANSWERED = ("approved", "declined")
STATUS_LABELS = {
    "pending": "Waiting for the customer", "approved": "Approved", "declined": "Declined",
    "expired": "Expired — no answer", "replaced": "Replaced by a newer quote", "cancelled": "Cancelled by the shop",
}
STATUS_BADGES = {"pending": "badge-amber", "approved": "badge-green", "declined": "badge-red",
                 "expired": "badge-gray", "replaced": "badge-gray", "cancelled": "badge-gray"}


class QuoteError(Exception):
    """Something the person can be told as is."""


def _clean(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_price(value) -> float:
    raw = str(value if value is not None else "").replace("$", "").replace(",", "").strip()
    if not raw:
        raise QuoteError("Enter the price you are quoting.")
    try:
        x = float(raw)
    except ValueError:
        raise QuoteError("The price must be a number, like 129.99.")
    if x != x or x in (float("inf"), float("-inf")):
        raise QuoteError("The price must be a number, like 129.99.")
    if x <= 0:
        raise QuoteError("The price must be more than zero.")
    if x > MAX_PRICE:
        raise QuoteError(f"The price is too large (the limit is ${MAX_PRICE:,.0f}).")
    return round(x + 1e-9, 2)


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"•••• {digits[-4:]}" if len(digits) >= 4 else ""


def new_token() -> str:
    return secrets.token_urlsafe(18)          # 24 URL-safe characters, 144 bits


# ── creating, retiring, answering ─────────────────────────────────────

def expire_overdue(db: Session, repair_id: str | None = None) -> int:
    """Marks pending quotes past their date as expired. Cheap enough to call whenever one is looked at."""
    q = db.query(QuoteApproval).filter(QuoteApproval.status == LIVE, QuoteApproval.expires_at.isnot(None),
                                       QuoteApproval.expires_at < datetime.utcnow())
    if repair_id:
        q = q.filter(QuoteApproval.repair_id == repair_id)
    n = 0
    for row in q.all():
        row.status = "expired"
        n += 1
    if n:
        db.flush()
    return n


def create(db: Session, staff, repair: Repair, amount, summary, note="", days: int = DEFAULT_VALID_DAYS) -> QuoteApproval:
    """Saves a new quote and retires any other live one on the ticket (does not commit)."""
    price = parse_price(amount)
    what = _clean(summary, MAX_SUMMARY)
    if not what:
        raise QuoteError("Say what the quote covers (for example “Screen replacement”).")
    days = days if days in VALID_DAY_CHOICES else DEFAULT_VALID_DAYS
    for old in db.query(QuoteApproval).filter(QuoteApproval.repair_id == repair.id, QuoteApproval.status == LIVE).all():
        old.status = "replaced"
    now = datetime.utcnow()
    q = QuoteApproval(repair_id=repair.id, token=new_token(), amount=price, summary=what, note=_clean(note, MAX_NOTE),
                      status=LIVE, created_at=now, created_by=getattr(staff, "name", "") or "", expires_at=now + timedelta(days=days))
    db.add(q)
    db.flush()
    return q


def cancel(db: Session, quote: QuoteApproval) -> None:
    if quote.status != LIVE:
        raise QuoteError("Only a quote that is still waiting for an answer can be cancelled.")
    quote.status = "cancelled"


def _history_line(db: Session, repair: Repair, note: str) -> None:
    try:
        hist = json.loads(repair.status_history or "[]")
    except (ValueError, TypeError):
        hist = []
    hist.append({"status": repair.status, "note": note, "date": datetime.utcnow().isoformat()})
    repair.status_history = json.dumps(hist)


def answer(db: Session, quote: QuoteApproval, decision: str, via: str, comment: str = "", staff=None,
           ip: str = "", device: str = "") -> QuoteApproval:
    """Records the first answer to a quote. Raises QuoteError if it can no longer be answered. Does not commit.

    The state change is one conditional UPDATE (…WHERE status = 'pending'), so two answers arriving together can't
    both be accepted — whichever the database sees first wins and the other is told it was already answered."""
    if decision not in ("approve", "decline"):
        raise QuoteError("Choose Approve or Decline.")
    if via not in ("link", "staff"):
        raise QuoteError("Unknown way of answering.")
    now = datetime.utcnow()
    if quote.status == LIVE and quote.expires_at is not None and quote.expires_at < now:
        quote.status = "expired"
        db.flush()
    if quote.status != LIVE:
        raise QuoteError({"approved": "This quote was already approved.", "declined": "This quote was already declined.",
                          "expired": "This quote has expired.", "replaced": "The shop has sent a newer quote.",
                          "cancelled": "The shop cancelled this quote."}.get(quote.status, "This quote can't be answered any more."))
    new_status = "approved" if decision == "approve" else "declined"
    changed = (db.query(QuoteApproval).filter(QuoteApproval.id == quote.id, QuoteApproval.status == LIVE)
               .update({"status": new_status, "responded_at": now, "responded_via": via,
                        "responded_by": getattr(staff, "name", "") or "" if via == "staff" else "",
                        "response_note": _clean(comment, MAX_COMMENT), "responder_ip": (ip or "")[:45],
                        "responder_device": (device or "")[:80]}, synchronize_session=False))
    if changed != 1:
        db.expire(quote)
        raise QuoteError("This quote was just answered by someone else.")
    db.expire(quote)
    repair = db.get(Repair, quote.repair_id)
    where = "by text link" if via == "link" else f"recorded by {getattr(staff, 'name', 'staff')}"
    if repair is not None:
        if new_status == "approved":
            repair.estimated_cost = quote.amount
            _history_line(db, repair, f"Customer approved the quote — ${quote.amount:.2f} + tax ({quote.summary}) {where}")
        else:
            _history_line(db, repair, f"Customer declined the quote — ${quote.amount:.2f} ({quote.summary}) {where}")
        repair.updated_at = now
    db.flush()
    return quote


# ── looking things up ─────────────────────────────────────────────────

def by_token(db: Session, token: str) -> QuoteApproval | None:
    if not TOKEN_RE.match(token or ""):
        return None
    q = db.query(QuoteApproval).filter(QuoteApproval.token == token).first()
    if q is not None:
        expire_overdue(db, q.repair_id)
    return q


def latest(db: Session, repair_id: str) -> QuoteApproval | None:
    expire_overdue(db, repair_id)
    return (db.query(QuoteApproval).filter(QuoteApproval.repair_id == repair_id)
            .order_by(QuoteApproval.created_at.desc(), QuoteApproval.id.desc()).first())


def history(db: Session, repair_id: str, limit: int = 10) -> list:
    return (db.query(QuoteApproval).filter(QuoteApproval.repair_id == repair_id)
            .order_by(QuoteApproval.created_at.desc(), QuoteApproval.id.desc()).limit(limit).all())


def badge_map(db: Session) -> dict:
    """{repair_id: 'pending' | 'approved' | 'declined'} for each ticket's newest quote, for the repairs list."""
    expire_overdue(db)
    out: dict = {}
    for q in (db.query(QuoteApproval).filter(QuoteApproval.status.in_((LIVE,) + ANSWERED))
              .order_by(QuoteApproval.created_at.desc(), QuoteApproval.id.desc()).all()):
        out.setdefault(q.repair_id, q.status)   # newest first, so the first one seen wins
    return out


def price_drift(quote: QuoteApproval, repair: Repair) -> bool:
    """True when the ticket's price no longer matches an APPROVED quote (someone edited it after the customer said yes).
    A quote still waiting for an answer is never flagged: the ticket takes the quote's price only when it is approved, so
    a different (or empty) estimate on a ticket with a quote out is the normal state, not a problem."""
    if quote is None or quote.status != "approved":
        return False
    return repair.estimated_cost is None or abs(float(repair.estimated_cost) - float(quote.amount or 0)) > 0.004


def unseen_answers(db: Session) -> list:
    """Answered quotes nobody has looked at yet (feeds the bell)."""
    return (db.query(QuoteApproval).filter(QuoteApproval.status.in_(ANSWERED), QuoteApproval.responded_via == "link",
                                           QuoteApproval.seen_at.is_(None)).order_by(QuoteApproval.responded_at.desc()).limit(20).all())


def mark_seen(db: Session, repair_id: str) -> None:
    now = datetime.utcnow()
    for q in db.query(QuoteApproval).filter(QuoteApproval.repair_id == repair_id, QuoteApproval.status.in_(ANSWERED),
                                            QuoteApproval.seen_at.is_(None)).all():
        q.seen_at = now


# ── the text message ──────────────────────────────────────────────────

def link_for(origin: str, quote: QuoteApproval) -> str:
    return f"{origin.rstrip('/')}/q/{quote.token}"


def sms_text(shop_name: str, repair: Repair, quote: QuoteApproval, link: str) -> str:
    until = quote.expires_at.strftime("%b %d").replace(" 0", " ") if quote.expires_at else ""
    first = (repair.customer.name or "").split(" ")[0] if repair.customer and repair.customer.name else ""
    hello = f"Hi {first}, " if first else ""
    return (f"{hello}{shop_name}: quote for your {repair.device or 'device'} (ticket #{repair.ticket_no}) — {quote.summary}, "
            f"${quote.amount:.2f} + tax. Approve or decline here: {link}"
            + (f" (valid until {until})" if until else ""))
