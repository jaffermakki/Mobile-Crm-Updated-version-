"""
Pages for the v48 security pack:

  /security                      your sign-in: change your PIN, 2-step sign-in, recovery codes, sign-in history
  /security/pin                  change your own PIN (needs the current PIN)
  /security/2fa/setup|enable     set up the authenticator app
  /security/2fa/disable          turn it off again (needs PIN + a current code)
  /security/2fa/recovery         make a fresh set of one-time recovery codes
  /security/sign-out-all         sign every device out (owner)
  /security/settings             idle sign-out time and shortest allowed PIN (owner)
  /staff/{id}/reset-2fa          turn off someone else's authenticator step, e.g. a lost phone (owner)

Built with a factory so it can borrow main.py's template engine and login check.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import security as S
from .auth import add_audit, hash_pin, pin_fingerprint, verify_pin
from .csrf import csrf_protect
from .encryption import decrypt_value, encrypt_value
from .models import LoginAttempt, Staff


def build_router(templates, get_setting, set_setting, require_login, get_db) -> APIRouter:
    router = APIRouter()

    def _gate(request: Request, db: Session, *roles):
        staff = require_login(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        if roles and staff.role not in roles and staff.role != "owner":
            return None, HTMLResponse("Forbidden — this needs the owner role.", status_code=403)
        return staff, None

    def _flash(request: Request, colour: str, message: str):
        request.session["flash"] = (colour, message)

    def _reauth(request: Request, db: Session, staff, pin: str) -> str | None:
        """Re-ask for the PIN before something sensitive. Wrong PINs count towards the same per-device pause as
        the login pad, so these pages cannot be used to guess a PIN."""
        wait = S.login_block_seconds(db, request)
        if wait:
            return f"Too many wrong tries. Try again in {S.format_wait(wait)}."
        if verify_pin(re.sub(r"\s+", "", pin or ""), staff.pin_hash):
            return None
        S.log_attempt(db, request, "pin", False, staff=staff, note="wrong PIN on a Security page")
        return "That PIN isn't right."

    # ── overview ─────────────────────────────────────────────────────
    @router.get("/security", response_class=HTMLResponse)
    def security_home(request: Request, all: str = "", db: Session = Depends(get_db)):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        can_see_all = staff.role in ("owner", "manager")
        q = db.query(LoginAttempt)
        if not can_see_all:                      # a cashier only sees their own sign-ins
            q = q.filter(LoginAttempt.staff_id == staff.id)
        elif all != "1":                         # the default view hides the noisy "PIN ok, waiting for code" rows
            q = q.filter(LoginAttempt.kind != "pin_ok")
        rows = q.order_by(LoginAttempt.ts.desc()).limit(60).all()
        week = datetime.utcnow() - timedelta(days=7)
        wrong_week = (db.query(func.count(LoginAttempt.id))
                      .filter(LoginAttempt.success == False, LoginAttempt.ts > week,  # noqa: E712
                              LoginAttempt.kind.in_(("pin", "2fa", "recovery"))).scalar() or 0) if can_see_all else None
        return templates.TemplateResponse(request, "security.html", {
            "staff": staff, "rows": rows, "can_see_all": can_see_all, "wrong_week": wrong_week,
            "flash": request.session.pop("flash", None), "weak_pin": bool(request.session.get("weak_pin")),
            "codes_left": S.recovery_codes_left(staff), "min_pin": S.min_pin_length(db),
            "idle_minutes": S.idle_timeout_minutes(db), "idle_choices": S.IDLE_CHOICES, "pin_choices": S.MIN_PIN_CHOICES,
            "this_ip": S.client_ip(request),
        })

    # ── own PIN ──────────────────────────────────────────────────────
    @router.post("/security/pin")
    def change_own_pin(request: Request, current_pin: str = Form(""), new_pin: str = Form(""), confirm_pin: str = Form(""),
                       db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        err = _reauth(request, db, staff, current_pin)
        new_pin = re.sub(r"\s+", "", new_pin or "")
        err = err or S.validate_new_pin(db, new_pin, staff.id, request=request, actor=staff)
        if not err and new_pin != re.sub(r"\s+", "", confirm_pin or ""):
            err = "The two new PINs don't match."
        if err:
            _flash(request, "red", err + " Your PIN was not changed.")
            return RedirectResponse("/security", status_code=303)
        staff.pin_hash = hash_pin(new_pin)
        staff.pin_len = len(new_pin)
        add_audit(db, staff, "PIN_CHANGE", f"{staff.name} changed their own PIN")
        db.commit()
        request.session["pk"] = pin_fingerprint(staff.pin_hash)       # stay signed in on this device
        if S.is_weak_pin(new_pin):
            request.session["weak_pin"] = True
            _flash(request, "amber", "PIN changed — but it is easy to guess (a repeat or a run like 1234). Pick something random.")
        else:
            request.session.pop("weak_pin", None)
            _flash(request, "green", "PIN changed. Any other device you were signed in on has been signed out.")
        return RedirectResponse("/security", status_code=303)

    # ── authenticator app ────────────────────────────────────────────
    @router.get("/security/2fa/setup", response_class=HTMLResponse)
    def twofa_setup(request: Request, db: Session = Depends(get_db)):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        if staff.totp_enabled:
            return RedirectResponse("/security", status_code=303)
        secret = decrypt_value(staff.totp_secret) if staff.totp_secret else ""
        if not secret:
            secret = S.new_totp_secret()
            staff.totp_secret = encrypt_value(secret)
            db.commit()
        issuer = get_setting(db, "shop_name", "TechPro+") or "TechPro+"
        uri = S.otpauth_uri(secret, staff.name, issuer)
        return templates.TemplateResponse(request, "security_2fa_setup.html", {
            "staff": staff, "secret": S.group_secret(secret), "qr": S.qr_svg(uri), "uri": uri,
            "flash": request.session.pop("flash", None),
        })

    @router.post("/security/2fa/enable", response_class=HTMLResponse)
    def twofa_enable(request: Request, code: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        if staff.totp_enabled or not staff.totp_secret:
            return RedirectResponse("/security", status_code=303)
        wait = S.login_block_seconds(db, request)
        if wait:
            _flash(request, "red", f"Too many wrong tries. Try again in {S.format_wait(wait)}.")
            return RedirectResponse("/security/2fa/setup", status_code=303)
        step = S.verify_totp(decrypt_value(staff.totp_secret), code, 0)
        if step is None:
            S.log_attempt(db, request, "2fa", False, staff=staff, note="wrong code while setting up 2-step")
            _flash(request, "red", "That code didn't match. Check the app (the code changes every 30 seconds) and try again.")
            return RedirectResponse("/security/2fa/setup", status_code=303)
        codes = S.new_recovery_codes()
        S.store_recovery_codes(staff, codes)
        staff.totp_enabled = True
        staff.totp_last_step = step
        add_audit(db, staff, "SECURITY", f"{staff.name} turned on 2-step sign-in")
        db.commit()
        return templates.TemplateResponse(request, "security_codes.html", {
            "staff": staff, "codes": codes, "first_time": True,
        })

    @router.post("/security/2fa/recovery", response_class=HTMLResponse)
    def twofa_new_recovery(request: Request, pin: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        if not staff.totp_enabled:
            return RedirectResponse("/security", status_code=303)
        err = _reauth(request, db, staff, pin)
        if err:
            _flash(request, "red", err)
            return RedirectResponse("/security", status_code=303)
        codes = S.new_recovery_codes()
        S.store_recovery_codes(staff, codes)
        add_audit(db, staff, "SECURITY", f"{staff.name} made a new set of recovery codes")
        db.commit()
        return templates.TemplateResponse(request, "security_codes.html", {"staff": staff, "codes": codes, "first_time": False})

    @router.post("/security/2fa/disable")
    def twofa_disable(request: Request, pin: str = Form(""), code: str = Form(""), db: Session = Depends(get_db),
                      _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        if not staff.totp_enabled:
            return RedirectResponse("/security", status_code=303)
        err = _reauth(request, db, staff, pin)
        if not err:
            step = S.verify_totp(decrypt_value(staff.totp_secret), code, staff.totp_last_step or 0)
            if step is None:
                S.log_attempt(db, request, "2fa", False, staff=staff, note="wrong code while turning 2-step off")
                err = "That authenticator code isn't right."
        if err:
            _flash(request, "red", err + " 2-step sign-in is still on.")
            return RedirectResponse("/security", status_code=303)
        staff.totp_enabled, staff.totp_secret, staff.totp_last_step, staff.recovery_codes = False, "", 0, ""
        add_audit(db, staff, "SECURITY", f"{staff.name} turned off 2-step sign-in")
        db.commit()
        _flash(request, "amber", "2-step sign-in is off. Your PIN alone signs you in now.")
        return RedirectResponse("/security", status_code=303)

    @router.post("/staff/{staff_id}/reset-2fa")
    def reset_other_2fa(request: Request, staff_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        target = db.get(Staff, staff_id)
        if target and target.id != staff.id:
            target.totp_enabled, target.totp_secret, target.totp_last_step, target.recovery_codes = False, "", 0, ""
            add_audit(db, staff, "SECURITY", f"{staff.name} reset 2-step sign-in for {target.name}")
            db.commit()
            _flash(request, "green", f"2-step sign-in is off for {target.name}. They can turn it on again from Security.")
        return RedirectResponse("/staff", status_code=303)

    # ── owner controls ───────────────────────────────────────────────
    @router.post("/security/sign-out-all")
    def sign_out_everywhere(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        S.bump_session_epoch(db, request)
        add_audit(db, staff, "SECURITY", f"{staff.name} signed every device out")
        _flash(request, "green", "Every other device is signed out. This one stays signed in.")
        return RedirectResponse("/security", status_code=303)

    @router.post("/security/settings")
    def save_security_settings(request: Request, idle_minutes: int = Form(60), min_pin: int = Form(4),
                               db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, deny = _gate(request, db, "owner")
        if deny:
            return deny
        if idle_minutes not in S.IDLE_CHOICES or min_pin not in S.MIN_PIN_CHOICES:
            _flash(request, "red", "Pick one of the listed choices. Nothing was changed.")
            return RedirectResponse("/security", status_code=303)
        set_setting(db, "idle_timeout_minutes", str(idle_minutes))
        set_setting(db, "min_pin_length", str(min_pin))
        add_audit(db, staff, "SECURITY", f"Sign-out after idle set to {idle_minutes or 'never'}; shortest PIN set to {min_pin} digits")
        _flash(request, "green", "Saved. The shortest-PIN rule applies to PINs set from now on; existing PINs keep working.")
        return RedirectResponse("/security", status_code=303)

    return router
