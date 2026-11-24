"""
v48 — authenticator-app (TOTP) second step and one-time recovery codes.
"""
import time
from datetime import datetime, timedelta

import pytest

from app import security as S
from app.auth import hash_pin
from app.encryption import decrypt_value
from app.models import AuditLog, LoginAttempt, Staff
from app.seed import _emergency_reset_2fa
from v48_helpers import (audit_actions, current_code, fail_logins, new_client, read_session, reset_security_state,
                         signed_in, write_session, ago)


@pytest.fixture(autouse=True)
def _clean(db_session, monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-real-ip")
    reset_security_state(db_session)
    yield
    reset_security_state(db_session)
    for sid, pin in (("s1", "1234"), ("test-manager", "2222"), ("test-cashier", "3333"), ("test-technician", "4444")):
        row = db_session.get(Staff, sid)
        row.pin_hash, row.pin_len, row.active = hash_pin(pin), 4, True
    db_session.commit()


# ═══ the arithmetic (RFC 6238 test vectors) ═══════════════════════════

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # base32 of the RFC's ASCII key 12345678901234567890


@pytest.mark.parametrize("unix_time,expected", [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                                                (1234567890, "005924"), (2000000000, "279037")])
def test_codes_match_the_rfc_6238_vectors(unix_time, expected):
    assert S.totp_code(RFC_SECRET, unix_time // 30) == expected


def test_a_code_is_accepted_one_step_either_side_but_not_two():
    now = 1_700_000_000.0
    step = int(now // 30)
    assert S.verify_totp(RFC_SECRET, S.totp_code(RFC_SECRET, step), now=now) == step
    assert S.verify_totp(RFC_SECRET, S.totp_code(RFC_SECRET, step - 1), now=now) == step - 1
    assert S.verify_totp(RFC_SECRET, S.totp_code(RFC_SECRET, step + 1), now=now) == step + 1
    assert S.verify_totp(RFC_SECRET, S.totp_code(RFC_SECRET, step - 2), now=now) is None
    assert S.verify_totp(RFC_SECRET, S.totp_code(RFC_SECRET, step + 2), now=now) is None


def test_a_code_cannot_be_used_twice():
    now = 1_700_000_000.0
    step = int(now // 30)
    code = S.totp_code(RFC_SECRET, step)
    assert S.verify_totp(RFC_SECRET, code, last_step=step - 1, now=now) == step
    assert S.verify_totp(RFC_SECRET, code, last_step=step, now=now) is None
    assert S.verify_totp(RFC_SECRET, S.totp_code(RFC_SECRET, step - 1), last_step=step, now=now) is None   # no going backwards either


@pytest.mark.parametrize("junk", ["", "12345", "1234567", "abcdef", "12 34 5", None, "٣٣٣٣٣٣"])
def test_malformed_codes_are_rejected(junk):
    assert S.verify_totp(RFC_SECRET, junk) is None


def test_a_damaged_secret_fails_closed_instead_of_crashing():
    assert S.verify_totp("!!!not base32!!!", "123456") is None
    assert S.verify_totp("", "123456") is None


def test_spaces_in_a_typed_code_are_fine():
    now = 1_700_000_000.0
    code = S.totp_code(RFC_SECRET, int(now // 30))
    assert S.verify_totp(RFC_SECRET, f"{code[:3]} {code[3:]}", now=now) is not None


def test_new_secrets_are_random_and_valid_base32():
    a, b = S.new_totp_secret(), S.new_totp_secret()
    assert a != b and len(a) == 32
    S.totp_code(a, 1)


def test_the_otpauth_uri_is_what_authenticator_apps_expect():
    uri = S.otpauth_uri("ABCDEFGH", "Test Owner", "Tech Pro+")
    assert uri.startswith("otpauth://totp/Tech%20Pro%2B:Test%20Owner?secret=ABCDEFGH")
    assert "issuer=Tech%20Pro%2B" in uri and "digits=6" in uri and "period=30" in uri


def test_the_qr_code_is_an_inline_svg():
    assert S.qr_svg("otpauth://totp/x?secret=ABC").startswith("<svg")


# ═══ recovery codes ═══════════════════════════════════════════════════

def test_recovery_codes_look_right_and_are_unique():
    codes = S.new_recovery_codes()
    assert len(codes) == 8 and len(set(codes)) == 8
    for c in codes:
        assert len(c) == 9 and c[4] == "-"
        assert not set(c.replace("-", "")) & set("0o1liO")


def test_recovery_codes_are_stored_hashed_and_each_works_once():
    staff = Staff(name="x", pin_hash="h")
    codes = S.new_recovery_codes()
    S.store_recovery_codes(staff, codes)
    for c in codes:
        assert c not in staff.recovery_codes and c.replace("-", "") not in staff.recovery_codes
    assert S.recovery_codes_left(staff) == 8
    assert S.use_recovery_code(staff, codes[0].upper())          # case and the dash don't matter
    assert not S.use_recovery_code(staff, codes[0])
    assert S.use_recovery_code(staff, codes[1].replace("-", " "))
    assert S.recovery_codes_left(staff) == 6
    assert not S.use_recovery_code(staff, "zzzz-zzzz")
    assert not S.use_recovery_code(staff, "short")
    assert not S.use_recovery_code(Staff(name="y", pin_hash="h", recovery_codes="{broken"), codes[2])


# ═══ setting it up ════════════════════════════════════════════════════

def _enable(client, db, staff_id="s1"):
    """Run the real set-up screens; returns the recovery codes shown."""
    page = client.get("/security/2fa/setup")
    assert page.status_code == 200
    r = client.post("/security/2fa/enable", data={"code": current_code(db, staff_id)})
    assert r.status_code == 200 and "recovery codes" in r.text.lower(), r.text[:300]
    codes = [line.split(">")[1].split("<")[0] for line in r.text.splitlines() if line.strip().startswith("<div>") and "-" in line]
    assert len(codes) == 8, codes
    return codes


def test_setup_shows_the_key_and_a_qr_and_stores_the_secret_encrypted(owner_client, db_session):
    page = owner_client.get("/security/2fa/setup")
    assert "<svg" in page.text
    db_session.expire_all()
    stored = db_session.get(Staff, "s1").totp_secret
    assert stored.startswith("enc:"), "secrets are encrypted at rest"
    secret = decrypt_value(stored)
    assert S.group_secret(secret) in page.text
    assert not db_session.get(Staff, "s1").totp_enabled, "not on until a code is proved"
    again = owner_client.get("/security/2fa/setup")
    assert S.group_secret(secret) in again.text, "reloading the page keeps the same key"


def test_a_wrong_code_does_not_turn_it_on(owner_client, db_session):
    owner_client.get("/security/2fa/setup")
    r = owner_client.post("/security/2fa/enable", data={"code": "000000"}, follow_redirects=True)
    assert "didn" in r.text and "match" in r.text
    db_session.expire_all()
    assert not db_session.get(Staff, "s1").totp_enabled


def test_the_right_code_turns_it_on_and_shows_recovery_codes_once(owner_client, db_session):
    codes = _enable(owner_client, db_session)
    db_session.expire_all()
    s = db_session.get(Staff, "s1")
    assert s.totp_enabled and s.totp_last_step > 0 and S.recovery_codes_left(s) == 8
    assert "SECURITY" in audit_actions(db_session)
    assert not any(c in owner_client.get("/security").text for c in codes), "codes are never shown again"
    assert "On" in owner_client.get("/security").text


def test_the_setup_page_is_only_for_signed_in_people_without_it_already_on(owner_client, anon_client, db_session):
    assert anon_client.get("/security/2fa/setup", follow_redirects=False).headers["location"] == "/login"
    _enable(owner_client, db_session)
    assert owner_client.get("/security/2fa/setup", follow_redirects=False).headers["location"] == "/security"


def test_wrong_setup_codes_count_towards_the_device_pause(owner_client, db_session):
    owner_client.get("/security/2fa/setup")
    for _ in range(5):
        owner_client.post("/security/2fa/enable", data={"code": "000000"})
    r = owner_client.post("/security/2fa/enable", data={"code": current_code(db_session, "s1")}, follow_redirects=True)
    assert "Too many wrong tries" in r.text
    db_session.expire_all()
    assert not db_session.get(Staff, "s1").totp_enabled


# ═══ signing in with it ═══════════════════════════════════════════════

def _with_2fa(owner_client, db):
    return _enable(owner_client, db)


def test_with_it_on_the_pin_alone_is_no_longer_enough(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.1.1")
    r = c.post("/login", data={"pin": "1234"}, follow_redirects=False)
    assert r.headers["location"] == "/login/2fa"
    assert not signed_in(c)
    assert c.get("/repairs", follow_redirects=False).status_code == 303
    assert "Owner" in c.get("/login/2fa").text


def test_the_right_code_completes_the_signin(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.1.2")
    c.post("/login", data={"pin": "1234"})
    db_session.expire_all()
    last = db_session.get(Staff, "s1").totp_last_step
    # the setup used the current step, so the next code must come from a later one
    code = S.totp_code(decrypt_value(db_session.get(Staff, "s1").totp_secret), int(time.time() // 30) + 1)
    r = c.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/" and signed_in(c)
    db_session.expire_all()
    assert db_session.get(Staff, "s1").totp_last_step > last
    assert "pending_2fa" not in read_session(c)


def test_a_used_code_cannot_sign_anyone_in_again(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    secret = decrypt_value(db_session.get(Staff, "s1").totp_secret)
    code = S.totp_code(secret, int(time.time() // 30) + 1)
    a = new_client(ip="9.1.1.3")
    a.post("/login", data={"pin": "1234"})
    assert a.post("/login/2fa", data={"code": code}, follow_redirects=False).headers["location"] == "/"
    b = new_client(ip="9.1.1.4")
    b.post("/login", data={"pin": "1234"})
    r = b.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/login/2fa" and not signed_in(b)


def test_a_wrong_code_is_refused_and_recorded(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.1.5")
    c.post("/login", data={"pin": "1234"})
    r = c.post("/login/2fa", data={"code": "123456"}, follow_redirects=True)
    assert "isn" in r.text and "right" in r.text and not signed_in(c)
    db_session.expire_all()
    assert db_session.query(LoginAttempt).filter(LoginAttempt.kind == "2fa", LoginAttempt.success == False).count() == 1  # noqa: E712


def test_five_wrong_codes_throw_away_the_half_finished_signin(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.1.6")
    c.post("/login", data={"pin": "1234"})
    for _ in range(5):
        c.post("/login/2fa", data={"code": "123456"})
    assert "pending_2fa" not in read_session(c)
    assert c.get("/login/2fa", follow_redirects=False).headers["location"] == "/login"


def test_a_half_finished_signin_expires(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.1.7")
    c.post("/login", data={"pin": "1234"})
    write_session(c, pending_at=ago(minutes=6))
    assert c.get("/login/2fa", follow_redirects=False).headers["location"] == "/login"
    code = current_code(db_session, "s1", offset=1)
    r = c.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/login" and not signed_in(c)


def test_the_second_step_cannot_be_reached_without_the_pin(db_session):
    c = new_client(ip="9.1.1.8")
    assert c.get("/login/2fa", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/login/2fa", data={"code": "123456"}, follow_redirects=False).headers["location"] == "/login"
    assert not signed_in(c)


def test_a_forged_pending_marker_for_someone_without_2fa_is_useless(db_session):
    c = new_client(ip="9.1.1.9")
    write_session(c, pending_2fa="test-cashier", pending_at=datetime.utcnow().isoformat(), pending_fails=0)
    assert c.get("/login/2fa", follow_redirects=False).headers["location"] == "/login"
    assert not signed_in(c)


def test_a_correct_pin_does_not_reset_the_wrong_try_counter_when_2fa_is_on(owner_client, db_session):
    """Otherwise someone who knows the PIN could guess second-step codes forever."""
    _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.2.1")
    fail_logins(c, 4)
    c.post("/login", data={"pin": "1234"})                         # PIN right, code still needed
    c.post("/login/2fa", data={"code": "111111"})                  # the 5th wrong thing from this device
    r = c.post("/login", data={"pin": "1234"}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert "Too many wrong tries" in c.get("/login").text


def test_other_accounts_are_unaffected_by_the_owner_turning_it_on(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    c = new_client("3333", ip="9.1.2.2")
    assert signed_in(c)


# ═══ recovery codes at sign-in ════════════════════════════════════════

def test_a_recovery_code_signs_in_once_and_says_how_many_are_left(owner_client, db_session):
    codes = _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.3.1")
    c.post("/login", data={"pin": "1234"})
    r = c.post("/login/2fa", data={"code": codes[0]}, follow_redirects=False)
    assert r.headers["location"] == "/security"
    page = c.get("/security")
    assert "recovery code" in page.text and "7" in page.text
    assert "SECURITY" in audit_actions(db_session) or "LOGIN" in audit_actions(db_session)
    again = new_client(ip="9.1.3.2")
    again.post("/login", data={"pin": "1234"})
    r2 = again.post("/login/2fa", data={"code": codes[0]}, follow_redirects=False)
    assert r2.headers["location"] == "/login/2fa" and not signed_in(again), "a recovery code only works once"


def test_made_up_recovery_codes_fail(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    c = new_client(ip="9.1.3.3")
    c.post("/login", data={"pin": "1234"})
    for guess in ("abcd-efgh", "zzzz-zzzz", "12345", "aaaaaaaa"):
        c.post("/login/2fa", data={"code": guess})
    assert not signed_in(c)


def test_new_recovery_codes_replace_the_old_ones(owner_client, db_session):
    old = _with_2fa(owner_client, db_session)
    r = owner_client.post("/security/2fa/recovery", data={"pin": "1234"})
    assert r.status_code == 200 and "recovery codes" in r.text.lower()
    fresh = [line.split(">")[1].split("<")[0] for line in r.text.splitlines() if line.strip().startswith("<div>") and "-" in line]
    assert len(fresh) == 8 and not set(fresh) & set(old)
    c = new_client(ip="9.1.3.4")
    c.post("/login", data={"pin": "1234"})
    c.post("/login/2fa", data={"code": old[0]})
    assert not signed_in(c), "an old code must not work any more"
    c.post("/login", data={"pin": "1234"})
    assert c.post("/login/2fa", data={"code": fresh[0]}, follow_redirects=False).headers["location"] == "/security"


def test_new_recovery_codes_need_the_pin(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    r = owner_client.post("/security/2fa/recovery", data={"pin": "0000"}, follow_redirects=True)
    assert "recovery codes" not in r.text.lower() or "isn" in r.text


# ═══ turning it off ═══════════════════════════════════════════════════

def test_turning_it_off_needs_the_pin_and_a_fresh_code(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    db_session.expire_all()
    secret = decrypt_value(db_session.get(Staff, "s1").totp_secret)
    nxt = S.totp_code(secret, int(time.time() // 30) + 1)
    owner_client.post("/security/2fa/disable", data={"pin": "0000", "code": nxt})
    owner_client.post("/security/2fa/disable", data={"pin": "1234", "code": "000000"})
    db_session.expire_all()
    assert db_session.get(Staff, "s1").totp_enabled, "wrong PIN or wrong code must leave it on"
    owner_client.post("/security/2fa/disable", data={"pin": "1234", "code": nxt})
    db_session.expire_all()
    s = db_session.get(Staff, "s1")
    assert not s.totp_enabled and s.totp_secret == "" and s.recovery_codes == ""
    assert new_client("1234", ip="9.1.4.1") and signed_in(new_client("1234", ip="9.1.4.2"))


def test_the_owner_can_reset_a_lost_phone_for_someone_else_only(owner_client, manager_client, db_session):
    m = db_session.get(Staff, "test-manager")
    m.totp_enabled, m.totp_secret = True, "enc:whatever"
    db_session.commit()
    assert manager_client.post("/staff/test-manager/reset-2fa", follow_redirects=False).status_code == 403
    db_session.expire_all()
    assert db_session.get(Staff, "test-manager").totp_enabled
    assert "Reset" in owner_client.get("/staff").text
    owner_client.post("/staff/test-manager/reset-2fa")
    db_session.expire_all()
    assert not db_session.get(Staff, "test-manager").totp_enabled
    assert "reset 2-step" in " ".join(a.detail for a in db_session.query(AuditLog).all())


def test_resetting_your_own_via_the_staff_route_does_nothing(owner_client, db_session):
    _with_2fa(owner_client, db_session)
    owner_client.post("/staff/s1/reset-2fa")
    db_session.expire_all()
    assert db_session.get(Staff, "s1").totp_enabled, "your own is turned off on the Security page, with a code"


# ═══ emergencies ══════════════════════════════════════════════════════

def test_the_server_variable_switches_it_off_for_everyone(owner_client, db_session, monkeypatch):
    _with_2fa(owner_client, db_session)
    monkeypatch.setenv("RESET_2FA", "1")
    db_session.expire_all()                       # the sign-in set-up above ran in another session
    _emergency_reset_2fa(db_session)
    db_session.expire_all()
    assert not db_session.get(Staff, "s1").totp_enabled
    assert any("RESET_2FA" in a.detail for a in db_session.query(AuditLog).all())
    assert signed_in(new_client("1234", ip="9.1.5.1"))


def test_without_the_variable_nothing_is_touched(owner_client, db_session, monkeypatch):
    _with_2fa(owner_client, db_session)
    monkeypatch.delenv("RESET_2FA", raising=False)
    _emergency_reset_2fa(db_session)
    db_session.expire_all()
    assert db_session.get(Staff, "s1").totp_enabled


def test_forgot_pin_resets_the_pin_but_does_not_skip_the_second_step(owner_client, anon_client, db_session):
    _with_2fa(owner_client, db_session)
    owner_client.post("/settings", data={"shop_name": "T", "province": "ON", "invoice_prefix": "INV", "points_per_dollar": "1",
                                          "points_redeem_rate": "100", "digest_hour": "21", "security_question": "Street?",
                                          "security_answer": "Maple"})
    anon_client.post("/forgot-pin", data={"staff_id": "s1", "security_answer": "maple", "new_pin": "718293", "confirm_pin": "718293"})
    c = new_client(ip="9.1.5.2")
    r = c.post("/login", data={"pin": "718293"}, follow_redirects=False)
    assert r.headers["location"] == "/login/2fa" and not signed_in(c)


def test_a_cashier_can_turn_it_on_for_themselves(cashier_client, db_session):
    assert cashier_client.get("/security/2fa/setup").status_code == 200
    r = cashier_client.post("/security/2fa/enable", data={"code": current_code(db_session, "test-cashier")})
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(Staff, "test-cashier").totp_enabled
