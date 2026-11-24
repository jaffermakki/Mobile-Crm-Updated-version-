"""
v52: repair quote approval — the web side (the rules are in quotes.py).

Public (no sign-in, reached by the private link in the text message):
  GET  /q/{token}     show the quote (never changes anything)
  POST /q/{token}     the customer taps Approve or Decline

Staff (any signed-in staff, from the repair screen):
  POST /repairs/{id}/quote                  write a quote and send it (text, or just make the link)
  POST /repairs/{id}/quote/{qid}/resend     send the same link again
  POST /repairs/{id}/quote/{qid}/record     "they told me yes / no on the phone"
  POST /repairs/{id}/quote/{qid}/cancel     withdraw a quote that hasn't been answered
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import brand as _brand
from . import notifications as _notify
from . import quotes as Q
from . import security as _sec
from .auth import add_audit
from .csrf import csrf_protect
from .models import QuoteApproval, Repair

_NO_STORE = {"Cache-Control": "no-store, max-age=0", "X-Robots-Tag": "noindex, nofollow, noarchive",
             "Referrer-Policy": "no-referrer"}
_TOO_MANY = "Too many tries from this device. Please wait a few minutes and try again."


def detail_context(db: Session, repair: Repair, origin: str) -> dict:
    """What the repair screen needs to draw the quote card."""
    latest = Q.latest(db, repair.id)
    return {
        "quote": latest, "quote_history": Q.history(db, repair.id, 8)[1:] if latest else [],
        "quote_link": Q.link_for(origin, latest) if latest is not None and latest.status == Q.LIVE else "",
        "quote_drift": Q.price_drift(latest, repair), "quote_days": Q.VALID_DAY_CHOICES,
        "quote_labels": Q.STATUS_LABELS, "quote_badges": Q.STATUS_BADGES,
        "quote_default_amount": (repair.estimated_cost if repair.estimated_cost is not None else ""),
        "quote_default_summary": (repair.issue or "")[:Q.MAX_SUMMARY],
        "quote_can_text": bool(repair.customer and repair.customer.phone and not repair.customer.sms_opt_out),
        "quote_blocked_text": ("This customer asked not to be texted (STOP)." if repair.customer and repair.customer.sms_opt_out
                               else ("No phone number on file for this customer." if not (repair.customer and repair.customer.phone) else "")),
    }


def build_router(templates, require_login, get_db, get_setting, log_outgoing_sms, public_origin) -> APIRouter:
    router = APIRouter()

    def _page(request, db, quote, error="", code=200, notfound=False):
        shop = get_setting(db, "shop_name", "TechPro+")
        repair = db.get(Repair, quote.repair_id) if quote is not None else None
        brand = _brand.get_brand(db)
        return templates.TemplateResponse(request, "quote_public.html", {
            "shop_name": brand["shop_name"] or shop, "q": quote, "repair": repair, "error": error, "notfound": notfound,
            "labels": Q.STATUS_LABELS, "shop_phone": get_setting(db, "shop_phone", ""), "brand": brand,
        }, status_code=code, headers=_NO_STORE)

    # ── the customer's side ───────────────────────────────────────────
    @router.get("/q/{token}", response_class=HTMLResponse)
    def quote_view(request: Request, token: str, db: Session = Depends(get_db)):
        if _sec.status_lookup_blocked(db, request):
            return _page(request, db, None, _TOO_MANY, 429, notfound=True)
        quote = Q.by_token(db, token)
        if quote is None:
            _sec.log_attempt(db, request, "status", False, note="quote link: unknown code")
            return _page(request, db, None, "", 404, notfound=True)
        db.commit()                                   # by_token may have just marked it expired
        return _page(request, db, quote)

    @router.post("/q/{token}", response_class=HTMLResponse)
    def quote_answer(request: Request, token: str, decision: str = Form(""), comment: str = Form(""),
                     db: Session = Depends(get_db)):
        # No CSRF token on purpose: the long random link IS the secret (nobody else can build this URL), and a
        # customer whose phone drops cookies between the text and the tap must still be able to answer.
        if _sec.status_lookup_blocked(db, request):
            return _page(request, db, None, _TOO_MANY, 429, notfound=True)
        quote = Q.by_token(db, token)
        if quote is None:
            _sec.log_attempt(db, request, "status", False, note="quote link: unknown code")
            return _page(request, db, None, "", 404, notfound=True)
        try:
            Q.answer(db, quote, decision, "link", comment, None, _sec.client_ip(request),
                     _sec.device_label(request.headers.get("user-agent", "")))
        except Q.QuoteError as e:
            db.commit()
            return _page(request, db, quote, str(e), 200)
        repair = db.get(Repair, quote.repair_id)
        verb = "approved" if quote.status == "approved" else "declined"
        add_audit(db, None, "QUOTE_" + verb.upper(),
                  f"Customer {verb} the ${quote.amount:.2f} quote for ticket #{repair.ticket_no if repair else '?'} by text link")
        db.commit()
        return RedirectResponse(f"/q/{token}", status_code=303, headers=_NO_STORE)

    # ── staff ─────────────────────────────────────────────────────────
    def _guard(request, db, repair_id):
        staff = require_login(request, db)
        if not staff:
            return None, None, RedirectResponse("/login", status_code=303)
        repair = db.get(Repair, repair_id)
        if not repair:
            return staff, None, RedirectResponse("/repairs", status_code=303)
        return staff, repair, None

    def _back(request, repair_id, colour, message):
        request.session["flash"] = (colour, message)
        return RedirectResponse(f"/repairs/{repair_id}#quote", status_code=303)

    def _text_it(db, staff, repair, quote, origin) -> tuple[bool, str]:
        cust = repair.customer
        if not cust or not cust.phone:
            return False, "No phone number on file for this customer."
        if cust.sms_opt_out:
            return False, f"{cust.name} asked not to be texted (STOP)."
        body = Q.sms_text(get_setting(db, "shop_name", "the shop"), repair, quote, Q.link_for(origin, quote))
        res = _notify.send_sms_detailed(db, cust.phone, body, get_setting)
        quote.sms_note = (res.get("message") or "")[:200]
        if res.get("ok"):
            quote.sent_via, quote.sent_to = "sms", Q.mask_phone(cust.phone)
            log_outgoing_sms(db, cust.phone, body, staff.name, repair.customer_id)
        return bool(res.get("ok")), res.get("message") or ""

    @router.post("/repairs/{repair_id}/quote")
    def quote_send(request: Request, repair_id: str, amount: str = Form(""), summary: str = Form(""), note: str = Form(""),
                   days: str = Form("7"), send: str = Form("sms"), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, repair, resp = _guard(request, db, repair_id)
        if resp:
            return resp
        try:
            quote = Q.create(db, staff, repair, amount, summary, note, int(days) if str(days).isdigit() else Q.DEFAULT_VALID_DAYS)
        except Q.QuoteError as e:
            db.rollback()
            return _back(request, repair_id, "red", str(e))
        origin = public_origin(request)
        add_audit(db, staff, "QUOTE_SENT", f"Quote ${quote.amount:.2f} ({quote.summary}) for ticket #{repair.ticket_no}")
        if send == "sms":
            ok, msg = _text_it(db, staff, repair, quote, origin)
            db.commit()
            if ok:
                return _back(request, repair_id, "green", f"Quote texted to {quote.sent_to}. You'll get a bell notification when they answer.")
            return _back(request, repair_id, "amber",
                         f"Quote saved, but the text wasn't sent: {msg} Copy the link below and send it yourself.")
        db.commit()
        return _back(request, repair_id, "green", "Quote saved. Copy the link below and send it to the customer.")

    def _quote_of(db, repair, quote_id):
        q = db.get(QuoteApproval, quote_id)
        return q if q is not None and q.repair_id == repair.id else None

    @router.post("/repairs/{repair_id}/quote/{quote_id}/resend")
    def quote_resend(request: Request, repair_id: str, quote_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, repair, resp = _guard(request, db, repair_id)
        if resp:
            return resp
        q = _quote_of(db, repair, quote_id)
        Q.expire_overdue(db, repair.id)
        if q is None or q.status != Q.LIVE:
            db.commit()
            return _back(request, repair_id, "red", "That quote can't be sent again — it was answered, replaced or has expired.")
        ok, msg = _text_it(db, staff, repair, q, public_origin(request))
        add_audit(db, staff, "QUOTE_RESENT", f"Quote link re-sent for ticket #{repair.ticket_no} ({'sent' if ok else 'not sent'})")
        db.commit()
        return _back(request, repair_id, "green" if ok else "amber", "Quote texted again." if ok else f"The text wasn't sent: {msg}")

    @router.post("/repairs/{repair_id}/quote/{quote_id}/record")
    def quote_record(request: Request, repair_id: str, quote_id: str, decision: str = Form(""), note: str = Form(""),
                     db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, repair, resp = _guard(request, db, repair_id)
        if resp:
            return resp
        q = _quote_of(db, repair, quote_id)
        if q is None:
            return _back(request, repair_id, "red", "That quote wasn't found.")
        try:
            Q.answer(db, q, decision, "staff", note, staff)
        except Q.QuoteError as e:
            db.commit()
            return _back(request, repair_id, "red", str(e))
        Q.mark_seen(db, repair.id)
        verb = "approved" if q.status == "approved" else "declined"
        add_audit(db, staff, "QUOTE_" + verb.upper(), f"Recorded: customer {verb} the ${q.amount:.2f} quote for ticket #{repair.ticket_no} "
                                                   f"(told {staff.name} in person/by phone)" + (f" — {q.response_note}" if q.response_note else ""))
        db.commit()
        return _back(request, repair_id, "green", f"Recorded: the customer {verb} the quote."
                                                  + (" The ticket's price is now the approved amount." if verb == "approved" else ""))

    @router.post("/repairs/{repair_id}/quote/{quote_id}/cancel")
    def quote_cancel(request: Request, repair_id: str, quote_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, repair, resp = _guard(request, db, repair_id)
        if resp:
            return resp
        q = _quote_of(db, repair, quote_id)
        if q is None:
            return _back(request, repair_id, "red", "That quote wasn't found.")
        try:
            Q.cancel(db, q)
        except Q.QuoteError as e:
            return _back(request, repair_id, "red", str(e))
        add_audit(db, staff, "QUOTE_CANCELLED", f"Quote ${q.amount:.2f} cancelled for ticket #{repair.ticket_no}")
        db.commit()
        return _back(request, repair_id, "green", "Quote cancelled. The old link no longer works.")

    return router
