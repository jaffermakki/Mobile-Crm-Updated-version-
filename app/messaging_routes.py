"""
Pages for the automatic-text add-on (v46):

  /messaging                      queue + history (owner, manager)
  /messaging/{id}/cancel|retry    fix one message (owner, manager)
  /messaging/run-now              process the queue now (owner)
  /messaging/catchup              remind customers whose phones are already waiting (owner)
  /messaging/settings (+ /test)   switches, wording, limits (owner)
  /customers/{id}/sms-optout      "do not text" toggle on a customer (owner, manager)

Built with a factory so it can borrow main.py's template engine and login check.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from . import messaging as M
from .auth import add_audit, role_allowed
from .csrf import csrf_protect
from .models import Customer, OutboundMessage

KIND_LABEL = {"ready": "Ready for pickup", "reminder": "Pickup reminder", "review": "Review request", "winback": "Win-back"}
TABS = {
    "upcoming": ("Upcoming", ("pending", "sending")),
    "history": ("Sent", ("sent",)),
    "problems": ("Failed / skipped", ("failed", "skipped", "cancelled")),
}


def build_router(templates, get_setting, require_login, get_db) -> APIRouter:
    router = APIRouter()
    templates.env.globals["KIND_LABEL"] = KIND_LABEL

    def _gate(request: Request, db: Session, *roles):
        staff = require_login(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        if not role_allowed(staff, *roles):
            who = "owner" if roles == ("owner",) else "manager or owner"
            return None, HTMLResponse(f"Forbidden — this needs the {who} role.", status_code=403)
        return staff, None

    # ── queue / history ──────────────────────────────────────────────
    @router.get("/messaging", response_class=HTMLResponse)
    def messaging_home(request: Request, tab: str = "upcoming", kind: str = "", db: Session = Depends(get_db)):
        staff, deny = _gate(request, db, "owner", "manager")
        if deny:
            return deny
        if tab not in TABS:
            tab = "upcoming"
        q = db.query(OutboundMessage).options(joinedload(OutboundMessage.customer), joinedload(OutboundMessage.repair))
        q = q.filter(OutboundMessage.status.in_(TABS[tab][1]))
        if kind in M.KINDS:
            q = q.filter(OutboundMessage.kind == kind)
        if tab == "upcoming":
            q = q.order_by(OutboundMessage.due_at)
        elif tab == "history":
            q = q.order_by(OutboundMessage.sent_at.desc())
        else:
            q = q.order_by(OutboundMessage.created_at.desc())
        counts = {t: db.query(OutboundMessage.id).filter(OutboundMessage.status.in_(v[1])).count() for t, v in TABS.items()}
        return templates.TemplateResponse(request, "messaging.html", {
            "staff": staff, "tab": tab, "kind": kind, "tabs": TABS, "counts": counts, "rows": q.limit(150).all(),
            "s": M.summary(db), "flash": request.session.pop("flash", None),
        })

    @router.post("/messaging/{mid}/cancel")
    def cancel_message(request: Request, mid: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner", "manager")
        if deny:
            return deny
        row = db.get(OutboundMessage, mid)
        if row and row.status == "pending":
            row.status, row.skip_reason = "cancelled", f"Cancelled by {staff.name}"
            db.commit()
            add_audit(db, staff, "SMS_CANCEL", f"Cancelled queued {row.kind} text")
            request.session["flash"] = ("green", "Message cancelled — it will not be sent.")
        else:
            request.session["flash"] = ("red", "That message is no longer waiting to be sent.")
        return RedirectResponse("/messaging", status_code=303)

    @router.post("/messaging/{mid}/retry")
    def retry_message(request: Request, mid: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner", "manager")
        if deny:
            return deny
        row = db.get(OutboundMessage, mid)
        if row and row.status == "failed" and row.source != "manual":
            row.status, row.attempts, row.due_at = "pending", 0, M.utcnow()
            row.error_code = row.last_error = ""
            db.commit()
            add_audit(db, staff, "SMS_RETRY", f"Re-queued failed {row.kind} text")
            request.session["flash"] = ("green", "Queued again — it goes out on the next run if it is still wanted.")
        else:
            request.session["flash"] = ("red", "Only failed automatic messages can be retried.")
        return RedirectResponse("/messaging?tab=problems", status_code=303)

    @router.post("/messaging/run-now")
    def run_now(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        st = M.process_due(db)
        if st["aborted"]:
            msg = ("red", f"Stopped: {st['aborted']}")
        elif st["deferred"]:
            msg = ("red", f"Nothing sent: {st['deferred']}.")
        else:
            msg = ("green", f"Done — {st['sent']} sent, {st['skipped']} skipped, {st['failed']} failed, {st['retry']} to retry.")
        request.session["flash"] = msg
        return RedirectResponse("/messaging", status_code=303)

    @router.post("/messaging/catchup")
    def catchup(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        n = M.enqueue_catchup(db)
        add_audit(db, staff, "SMS_CATCHUP", f"Queued {n} reminder(s) for devices already waiting")
        request.session["flash"] = ("green", f"Queued {n} reminder(s). They go out during sending hours, within the daily limit.")
        return RedirectResponse("/messaging", status_code=303)

    # ── settings ─────────────────────────────────────────────────────
    def _settings_ctx(request, staff, db, form=None, errors=None, test_result=None):
        cfg = M.load_config(db)
        raw = {k: M.get_setting(db, k, v) for k, v in M.DEFAULTS.items()}
        for k in M.KINDS:
            raw[f"msg_tpl_{k}"] = cfg.templates[k]
        if form:                                  # show what they typed, not what is saved
            raw.update({k: v for k, v in form.items() if isinstance(v, str)})
            for k in ("msg_enabled", "msg_ready_enabled", "msg_remind_enabled", "msg_review_enabled", "msg_winback_enabled"):
                raw[k] = "true" if form.get(k) in ("on", "true", "1") else "false"
        previews = {}
        for k in M.KINDS:
            t = raw.get(f"msg_tpl_{k}", cfg.templates[k])
            previews[k] = {**M.preview(cfg, k, t), "problems": M.template_problems(t)}
        return {"staff": staff, "v": raw, "previews": previews, "errors": errors or [], "s": M.summary(db),
                "placeholders": M.PLACEHOLDERS, "flash": request.session.pop("flash", None), "test_result": test_result}

    @router.get("/messaging/settings", response_class=HTMLResponse)
    def settings_page(request: Request, db: Session = Depends(get_db)):
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        return templates.TemplateResponse(request, "messaging_settings.html", _settings_ctx(request, staff, db))

    @router.post("/messaging/settings", response_class=HTMLResponse)
    async def settings_save(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        was_on = M.get_setting(db, "msg_enabled", "false") == "true"
        errors = M.save_settings(db, form)
        if errors:
            return templates.TemplateResponse(request, "messaging_settings.html", _settings_ctx(request, staff, db, form=form, errors=errors),
                                              status_code=400)
        now_on = M.get_setting(db, "msg_enabled", "false") == "true"
        add_audit(db, staff, "SETTINGS", "Messaging settings saved" + (" — automatic texts switched ON" if now_on and not was_on else
                                                                      " — automatic texts switched OFF" if was_on and not now_on else ""))
        request.session["flash"] = ("green", "Messaging settings saved.")
        return RedirectResponse("/messaging/settings", status_code=303)

    @router.post("/messaging/settings/test", response_class=HTMLResponse)
    def settings_test(request: Request, kind: str = Form(""), to_phone: str = Form(""), db: Session = Depends(get_db),
                      _csrf: None = Depends(csrf_protect)):
        """Sends the saved wording for one message type, with sample values, to a phone the owner types."""
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        from .notifications import send_sms_detailed
        if kind not in M.KINDS:
            request.session["flash"] = ("red", "Unknown message type.")
            return RedirectResponse("/messaging/settings", status_code=303)
        cfg = M.load_config(db)
        body = "[TEST] " + M.preview(cfg, kind)["body"]
        r = send_sms_detailed(db, to_phone, body, get_setting)
        add_audit(db, staff, "SMS_TEST", f"Test {kind} text to {r.get('to') or to_phone}: {'sent' if r['ok'] else 'failed'}")
        request.session["flash"] = ("green" if r["ok"] else "red", ("Test text sent." if r["ok"] else r["message"]))
        return RedirectResponse("/messaging/settings", status_code=303)

    # ── per-customer do-not-text ─────────────────────────────────────
    @router.post("/customers/{customer_id}/sms-optout")
    def customer_optout(request: Request, customer_id: str, opt_out: str = Form("1"), db: Session = Depends(get_db),
                        _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner", "manager")
        if deny:
            return deny
        c = db.get(Customer, customer_id)
        if not c:
            return RedirectResponse("/customers", status_code=303)
        M.set_opt_out(db, c, opt_out == "1", staff)
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)

    return router
