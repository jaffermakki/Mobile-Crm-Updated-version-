"""Shared helpers for the v48 security / backup tests."""
import base64
import json
import os
import time
from datetime import datetime, timedelta

from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

from app.main import app
from app.models import LoginAttempt, Setting, Staff, AuditLog
from app.encryption import decrypt_value
from app import security as S

SECRET = os.environ["SESSION_SECRET"]


def new_client(pin=None, ip=None):
    """A fresh browser ("device"). ip = the address the server should believe it comes from."""
    c = TestClient(app)
    if ip:
        c.headers.update({"X-Real-IP": ip})
    if pin:
        r = c.post("/login", data={"pin": pin}, follow_redirects=False)
        assert r.status_code == 303, r.status_code
    return c


def _session_cookie(client):
    values = [c.value for c in client.cookies.jar if c.name == "session"]
    return values[-1] if values else None


def read_session(client) -> dict:
    raw = _session_cookie(client)
    if not raw:
        return {}
    data = TimestampSigner(SECRET).unsign(raw.encode(), max_age=3600)
    return json.loads(base64.b64decode(data))


def write_session(client, **changes):
    """Edit the signed session cookie, to simulate time passing."""
    data = read_session(client)
    data.update(changes)
    signed = TimestampSigner(SECRET).sign(base64.b64encode(json.dumps(data).encode()))
    for c in list(client.cookies.jar):
        if c.name == "session":
            client.cookies.jar.clear(c.domain, c.path, c.name)
    client.cookies.set("session", signed.decode(), domain="testserver.local", path="/")   # same place the server's own cookie lives


def ago(**kw):
    return (datetime.utcnow() - timedelta(**kw)).isoformat()


def signed_in(client) -> bool:
    r = client.get("/", follow_redirects=False)
    return r.status_code == 200


def reset_security_state(db):
    """Put every v48 knob back to its default, so tests never leak into each other."""
    db.rollback()
    db.query(LoginAttempt).delete()
    for key in ("idle_timeout_minutes", "min_pin_length", "backup_password", "session_epoch",
                "security_question", "security_answer_hash"):
        row = db.get(Setting, key)
        if row:
            db.delete(row)
    for staff in db.query(Staff).all():
        staff.totp_enabled, staff.totp_secret, staff.totp_last_step, staff.recovery_codes = False, "", 0, ""
    db.commit()
    db.expire_all()


def current_code(db, staff_id, offset=0):
    """The code the authenticator app would show right now for this staff member."""
    db.expire_all()
    staff = db.get(Staff, staff_id)
    return S.totp_code(decrypt_value(staff.totp_secret), int(time.time() // 30) + offset)


def fail_logins(client, n, pin="9999"):
    for _ in range(n):
        client.post("/login", data={"pin": pin}, follow_redirects=False)


def backdate_attempts(db, **kw):
    """Make every recorded attempt look older, as if time had passed."""
    db.expire_all()
    for a in db.query(LoginAttempt).all():
        a.ts = a.ts - timedelta(**kw)
    db.commit()


def audit_actions(db):
    db.expire_all()
    return [a.action for a in db.query(AuditLog).order_by(AuditLog.ts).all()]
