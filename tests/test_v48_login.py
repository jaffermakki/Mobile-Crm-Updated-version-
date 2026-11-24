"""
v48 — sign-in hardening: per-device wrong-PIN pausing, 4-8 digit PINs, idle / absolute / sign-out-everywhere
sessions, PIN changes ending old sessions, the public status page limits and private links, security headers,
and the card-number guard.
"""
import json
from datetime import datetime, timedelta

import pytest

from app import security as S
from app.auth import hash_pin
from app.database import engine
from app.models import AuditLog, Customer, LoginAttempt, Repair, Setting, Staff
from v48_helpers import (ago, audit_actions, backdate_attempts, fail_logins, new_client, read_session,
                         reset_security_state, signed_in, write_session)


@pytest.fixture(autouse=True)
def _clean(db_session, monkeypatch):
    # tests pretend to come from different addresses by sending X-Real-IP
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-real-ip")
    reset_security_state(db_session)
    yield
    reset_security_state(db_session)
    for s in db_session.query(Staff).filter(Staff.name.like("V48 %")).all():
        db_session.delete(s)
    # put the shared accounts' PINs back, whatever a test did to them
    for sid, pin in (("s1", "1234"), ("test-manager", "2222"), ("test-cashier", "3333"), ("test-technician", "4444")):
        row = db_session.get(Staff, sid)
        if row:
            row.pin_hash, row.pin_len, row.active = hash_pin(pin), 4, True
    db_session.commit()


# ═══ which address is "this device" ═══════════════════════════════════

class _Req:
    def __init__(self, headers=None, host="10.0.0.9"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


def test_client_ip_uses_the_trusted_header_only_when_configured(monkeypatch):
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    assert S.client_ip(_Req({"x-real-ip": "1.2.3.4"})) == "10.0.0.9"          # forgeable header ignored by default
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-real-ip")
    assert S.client_ip(_Req({"x-real-ip": "1.2.3.4"})) == "1.2.3.4"
    assert S.client_ip(_Req({"x-real-ip": "not-an-ip"})) == "10.0.0.9"        # junk falls back to the socket
    assert S.client_ip(_Req({})) == "10.0.0.9"


def test_on_railway_the_real_ip_header_is_trusted_by_default(monkeypatch):
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert S.client_ip(_Req({"x-real-ip": "5.6.7.8"})) == "5.6.7.8"


def test_device_label_is_readable():
    ua = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
    assert S.device_label(ua) == "Chrome on Android"
    assert S.device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit Version/17 Mobile Safari/604") == "Safari on iPhone"
    assert S.device_label("") == "Browser on unknown device"


# ═══ wrong-PIN pausing ════════════════════════════════════════════════

def test_five_wrong_pins_pause_that_device_even_for_the_right_pin(db_session):
    c = new_client(ip="7.7.7.1")
    fail_logins(c, 5)
    r = c.post("/login", data={"pin": "1234"}, follow_redirects=False)         # the CORRECT pin, but the device is paused
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert not signed_in(c)
    page = c.get("/login").text
    assert "Too many wrong tries" in page
    assert "SECURITY_BLOCK" in audit_actions(db_session)


def test_the_pause_is_per_device_a_stranger_cannot_lock_the_owner_out(db_session):
    stranger = new_client(ip="7.7.7.2")
    fail_logins(stranger, 12)
    owner = new_client(ip="7.7.7.3")
    r = owner.post("/login", data={"pin": "1234"}, follow_redirects=False)
    assert r.headers["location"] == "/" and signed_in(owner)


def test_attempts_made_while_paused_are_not_counted(db_session):
    c = new_client(ip="7.7.7.4")
    fail_logins(c, 5)
    fail_logins(c, 6)                                    # all refused before being counted
    db_session.expire_all()
    assert db_session.query(LoginAttempt).filter(LoginAttempt.ip == "7.7.7.4").count() == 5


def test_the_pause_ends_and_a_correct_pin_resets_the_count(db_session):
    c = new_client(ip="7.7.7.5")
    fail_logins(c, 5)
    backdate_attempts(db_session, minutes=5)
    assert S.login_block_seconds(db_session, _Req({"x-real-ip": "7.7.7.5"})) == 0
    assert c.post("/login", data={"pin": "1234"}, follow_redirects=False).headers["location"] == "/"
    # a fresh run of mistakes starts from zero again
    fail_logins(c, 4)
    assert S.login_block_seconds(db_session, _Req({"x-real-ip": "7.7.7.5"})) == 0


def test_the_pause_gets_longer_each_round(db_session):
    req = _Req({"x-real-ip": "7.7.7.6"})
    c = new_client(ip="7.7.7.6")
    fail_logins(c, 5)
    assert 50 <= S.login_block_seconds(db_session, req) <= 60
    backdate_attempts(db_session, minutes=2)
    assert S.login_block_seconds(db_session, req) == 0
    fail_logins(c, 5)                                    # round two: 10 wrong in total
    assert 110 <= S.login_block_seconds(db_session, req) <= 120
    assert S._lock_seconds_for(40) == 30 * 60            # and it never exceeds 30 minutes
    assert S._lock_seconds_for(200) == 30 * 60


def test_a_flood_from_many_addresses_pauses_strangers_but_not_known_devices(db_session):
    known = new_client(ip="7.7.8.1")
    assert known.post("/login", data={"pin": "1234"}, follow_redirects=False).headers["location"] == "/"
    for i in range(45):                                  # a distributed guessing run, one try per address
        c = new_client(ip=f"8.8.{i}.1")
        c.post("/login", data={"pin": "9999"}, follow_redirects=False)
    stranger = new_client(ip="8.8.200.1")
    r = stranger.post("/login", data={"pin": "1234"}, follow_redirects=False)
    assert not signed_in(stranger), "an unknown device must wait while the shop is under attack"
    assert known.post("/login", data={"pin": "1234"}, follow_redirects=False).headers["location"] == "/"


def test_attempts_are_recorded_with_device_and_address(db_session):
    c = new_client(ip="7.7.9.1")
    c.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile Safari/537.36"})
    c.post("/login", data={"pin": "9999"}, follow_redirects=False)
    c.post("/login", data={"pin": "1234"}, follow_redirects=False)
    db_session.expire_all()
    rows = db_session.query(LoginAttempt).filter(LoginAttempt.ip == "7.7.9.1").order_by(LoginAttempt.ts).all()
    assert [(r.kind, r.success) for r in rows] == [("pin", False), ("pin", True)]
    assert rows[0].device == "Chrome on Android" and rows[1].staff_name == "Owner"


@pytest.mark.parametrize("junk", ["", "abcd", "12", "123456789", "12 34 56 78 9", "1234; DROP TABLE staff", "１２３４", "x" * 500])
def test_nonsense_pins_are_just_wrong_not_errors(junk):
    c = new_client(ip="7.7.9.2")
    r = c.post("/login", data={"pin": junk}, follow_redirects=False)
    assert r.status_code in (303, 422)
    assert not signed_in(c)


# ═══ PIN length ═══════════════════════════════════════════════════════

def _add_staff(owner_client, name, pin, role="cashier"):
    return owner_client.post("/staff/add", data={"name": name, "pin": pin, "role": role}, follow_redirects=False)


@pytest.mark.parametrize("pin,ok", [("1357", True), ("24680", True), ("135792", True), ("86420975", True),
                                    ("135", False), ("123456789", False), ("12a45", False), ("12 4", False), ("", False)])
def test_staff_pins_may_be_4_to_8_digits(owner_client, db_session, pin, ok):
    before = db_session.query(Staff).count()
    r = _add_staff(owner_client, "V48 Pin Test", pin)
    db_session.expire_all()
    assert (r.status_code == 303) is ok, r.text[:100]
    assert db_session.query(Staff).count() == before + (1 if ok else 0)
    if ok:
        added = db_session.query(Staff).filter(Staff.name == "V48 Pin Test").one()
        assert added.pin_len == len(pin)
        assert new_client(pin, ip="7.7.10.1") and True
        db_session.delete(added)
        db_session.commit()


def test_a_six_digit_pin_signs_in(owner_client, db_session):
    _add_staff(owner_client, "V48 Six", "135792")
    c = new_client(ip="7.7.10.2")
    assert c.post("/login", data={"pin": "135792"}, follow_redirects=False).headers["location"] == "/"
    assert signed_in(c)
    db_session.query(Staff).filter(Staff.name == "V48 Six").delete()
    db_session.commit()


def test_the_shortest_allowed_pin_is_a_shop_rule(owner_client, db_session):
    assert owner_client.post("/security/settings", data={"idle_minutes": "60", "min_pin": "6"}, follow_redirects=False).status_code == 303
    assert _add_staff(owner_client, "V48 Short", "1357").status_code == 400
    assert _add_staff(owner_client, "V48 Long", "135792").status_code == 303
    db_session.query(Staff).filter(Staff.name == "V48 Long").delete()
    db_session.commit()


def test_the_keypad_only_autosubmits_when_everyone_uses_the_same_length(owner_client, db_session):
    c = new_client(ip="7.7.10.3")
    assert "const PIN_LEN = 4;" in c.get("/login").text
    _add_staff(owner_client, "V48 Mixed", "135792")
    assert "const PIN_LEN = 0;" in c.get("/login").text
    db_session.query(Staff).filter(Staff.name == "V48 Mixed").delete()
    db_session.commit()
    assert "const PIN_LEN = 4;" in c.get("/login").text


def test_editing_a_pin_validates_and_records_its_length(owner_client, db_session):
    r = owner_client.post("/staff/test-cashier/edit", data={"name": "Test Cashier", "role": "cashier", "new_pin": "12"}, follow_redirects=True)
    assert "digits" in r.text
    db_session.expire_all()
    assert db_session.get(Staff, "test-cashier").pin_len == 4
    owner_client.post("/staff/test-cashier/edit", data={"name": "Test Cashier", "role": "cashier", "new_pin": "802467"})
    db_session.expire_all()
    assert db_session.get(Staff, "test-cashier").pin_len == 6
    assert new_client("802467", ip="7.7.10.4")


def test_forgot_pin_accepts_a_longer_pin_and_resets_the_count(owner_client, anon_client, db_session):
    owner_client.post("/settings", data={"shop_name": "T", "province": "ON", "invoice_prefix": "INV", "points_per_dollar": "1",
                                          "points_redeem_rate": "100", "digest_hour": "21", "security_question": "Street?",
                                          "security_answer": "Maple"})
    r = anon_client.post("/forgot-pin", data={"staff_id": "s1", "security_answer": "maple", "new_pin": "55667788", "confirm_pin": "55667788"},
                         follow_redirects=False)
    assert r.headers["location"] == "/login"
    db_session.expire_all()
    assert db_session.get(Staff, "s1").pin_len == 8
    assert new_client("55667788", ip="7.7.10.5")


def test_forgot_pin_wrong_answers_are_paused_like_the_pin_pad(owner_client, anon_client, db_session):
    owner_client.post("/settings", data={"shop_name": "T", "province": "ON", "invoice_prefix": "INV", "points_per_dollar": "1",
                                          "points_redeem_rate": "100", "digest_hour": "21", "security_question": "Street?",
                                          "security_answer": "Maple"})
    c = new_client(ip="7.7.10.6")
    for _ in range(5):
        c.post("/forgot-pin", data={"staff_id": "s1", "security_answer": "nope", "new_pin": "135792", "confirm_pin": "135792"})
    r = c.post("/forgot-pin", data={"staff_id": "s1", "security_answer": "maple", "new_pin": "135792", "confirm_pin": "135792"},
               follow_redirects=False)
    assert r.headers["location"] == "/forgot-pin"
    db_session.expire_all()
    assert db_session.get(Staff, "s1").pin_len == 4, "the right answer must not work while paused"


# ═══ sessions ═════════════════════════════════════════════════════════

def test_a_signed_in_session_remembers_when_it_began(owner_client):
    s = read_session(owner_client)
    assert {"staff_id", "last_activity", "login_at", "epoch", "pk"} <= set(s)


def test_idle_sign_out_after_the_shop_limit(owner_client, db_session):
    owner_client.post("/security/settings", data={"idle_minutes": "15", "min_pin": "4"})
    assert signed_in(owner_client)
    write_session(owner_client, last_activity=ago(minutes=20))
    r = owner_client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "without activity" in owner_client.get("/login").text


def test_within_the_idle_limit_the_session_continues_and_is_refreshed(owner_client):
    owner_client.post("/security/settings", data={"idle_minutes": "15", "min_pin": "4"})
    write_session(owner_client, last_activity=ago(minutes=10))
    assert signed_in(owner_client)
    assert datetime.fromisoformat(read_session(owner_client)["last_activity"]) > datetime.utcnow() - timedelta(minutes=1)


def test_idle_limit_of_never_means_only_the_cookie_and_24h_limits_apply(owner_client):
    owner_client.post("/security/settings", data={"idle_minutes": "0", "min_pin": "4"})
    write_session(owner_client, last_activity=ago(hours=9), login_at=ago(hours=9))
    assert signed_in(owner_client)


def test_background_polling_does_not_keep_an_idle_session_alive(owner_client):
    owner_client.post("/security/settings", data={"idle_minutes": "60", "min_pin": "4"})
    old = ago(minutes=30)
    write_session(owner_client, last_activity=old)
    assert owner_client.get("/api/notifications").status_code == 200
    assert read_session(owner_client)["last_activity"] == old


def test_a_signin_never_lasts_longer_than_24_hours(owner_client):
    owner_client.post("/security/settings", data={"idle_minutes": "0", "min_pin": "4"})
    write_session(owner_client, login_at=ago(hours=25))
    assert owner_client.get("/", follow_redirects=False).status_code == 303


def test_a_disabled_account_is_signed_out_immediately(owner_client, db_session):
    c = new_client("3333", ip="7.7.11.1")
    assert signed_in(c)
    owner_client.post("/staff/test-cashier/toggle")
    assert c.get("/", follow_redirects=False).status_code == 303
    owner_client.post("/staff/test-cashier/toggle")          # enabled again for the other tests


def test_sign_out_everywhere_keeps_only_this_device(owner_client, db_session):
    other = new_client("1234", ip="7.7.11.2")
    third = new_client("2222", ip="7.7.11.3")
    assert signed_in(other) and signed_in(third)
    r = owner_client.post("/security/sign-out-all", follow_redirects=False)
    assert r.status_code == 303
    assert signed_in(owner_client), "the device that asked stays signed in"
    assert not signed_in(other) and not signed_in(third)
    assert new_client("1234", ip="7.7.11.4") and signed_in(new_client("1234", ip="7.7.11.5"))   # signing in again works


def test_only_the_owner_can_sign_everyone_out(manager_client, cashier_client):
    assert manager_client.post("/security/sign-out-all", follow_redirects=False).status_code == 403
    assert cashier_client.post("/security/sign-out-all", follow_redirects=False).status_code == 403
    assert manager_client.post("/security/settings", data={"idle_minutes": "15", "min_pin": "4"}).status_code == 403


def test_security_settings_reject_values_outside_the_choices(owner_client, db_session):
    for bad in ({"idle_minutes": "7", "min_pin": "4"}, {"idle_minutes": "60", "min_pin": "3"}, {"idle_minutes": "60", "min_pin": "9"}):
        owner_client.post("/security/settings", data=bad)
    db_session.expire_all()
    assert S.idle_timeout_minutes(db_session) == 60 and S.min_pin_length(db_session) == 4


def test_changing_someones_pin_ends_only_their_sessions(manager_client, cashier_client, db_session):
    tech = new_client("4444", ip="7.7.11.6")
    assert signed_in(cashier_client) and signed_in(tech)
    manager_client.post("/staff/test-cashier/edit", data={"name": "Test Cashier", "role": "cashier", "new_pin": "8080"})
    assert not signed_in(cashier_client), "the cashier's old sign-in must stop working"
    assert signed_in(tech) and signed_in(manager_client)
    assert "PIN was changed" in cashier_client.get("/login").text


def test_changing_your_own_pin_keeps_this_device_but_ends_the_others(owner_client, db_session):
    other = new_client("1234", ip="7.7.11.7")
    r = owner_client.post("/security/pin", data={"current_pin": "1234", "new_pin": "718293", "confirm_pin": "718293"}, follow_redirects=False)
    assert r.status_code == 303
    assert signed_in(owner_client) and not signed_in(other)
    assert new_client("718293", ip="7.7.11.8")
    assert "PIN_CHANGE" in audit_actions(db_session)


def test_changing_your_own_pin_via_the_staff_page_does_not_sign_you_out(owner_client):
    owner_client.post("/staff/s1/edit", data={"name": "Owner", "role": "owner", "new_pin": "718293"})
    assert signed_in(owner_client)


def test_own_pin_change_checks_the_current_pin_and_counts_wrong_ones(owner_client, db_session):
    r = owner_client.post("/security/pin", data={"current_pin": "0000", "new_pin": "718293", "confirm_pin": "718293"}, follow_redirects=True)
    assert "Your PIN was not changed" in r.text
    db_session.expire_all()
    assert db_session.query(LoginAttempt).filter(LoginAttempt.success == False, LoginAttempt.note.like("%Security page%")).count() == 1  # noqa: E712
    for bad in ({"current_pin": "1234", "new_pin": "718293", "confirm_pin": "000111"},     # doesn't match
                {"current_pin": "1234", "new_pin": "12", "confirm_pin": "12"}):             # too short
        r = owner_client.post("/security/pin", data=bad, follow_redirects=True)
        assert "was not changed" in r.text
    assert new_client("1234", ip="7.7.11.9")


def test_the_pin_check_on_security_pages_cannot_be_used_to_guess(owner_client, db_session):
    for _ in range(5):
        owner_client.post("/security/pin", data={"current_pin": "0000", "new_pin": "718293", "confirm_pin": "718293"})
    r = owner_client.post("/security/pin", data={"current_pin": "1234", "new_pin": "718293", "confirm_pin": "718293"}, follow_redirects=True)
    assert "Too many wrong tries" in r.text
    db_session.expire_all()
    assert new_client("1234", ip="7.7.11.10") and True      # a different device still gets in; the PIN was not changed


def test_an_easy_pin_is_flagged_not_forbidden(owner_client):
    r = owner_client.post("/security/pin", data={"current_pin": "1234", "new_pin": "111111", "confirm_pin": "111111"}, follow_redirects=True)
    assert "easy to guess" in r.text
    assert "easy to guess" in owner_client.get("/").text            # and the banner stays until it is changed
    owner_client.post("/security/pin", data={"current_pin": "111111", "new_pin": "718293", "confirm_pin": "718293"})
    assert "easy to guess" not in owner_client.get("/").text


@pytest.mark.parametrize("pin,weak", [("1234", True), ("0000", True), ("111111", True), ("123456", True), ("654321", True),
                                      ("2580", True), ("718293", False), ("5039", False), ("40281957", False), ("", False)])
def test_weak_pin_detection(pin, weak):
    assert S.is_weak_pin(pin) is weak


def test_logging_in_with_a_weak_pin_shows_a_nudge_a_strong_one_does_not(owner_client, db_session):
    assert "easy to guess" in owner_client.get("/").text          # the shared test owner uses 1234
    s = db_session.get(Staff, "test-manager")
    s.pin_hash, s.pin_len = hash_pin("718293"), 6
    db_session.commit()
    c = new_client("718293", ip="7.7.12.1")
    assert "easy to guess" not in c.get("/").text


def test_the_owner_is_reminded_to_turn_on_two_step(owner_client, manager_client):
    notes = owner_client.get("/api/notifications").json()["notifications"]
    assert any("2-step" in n["message"] for n in notes)
    notes = manager_client.get("/api/notifications").json()["notifications"]
    assert not any("2-step" in n["message"] for n in notes)


# ═══ the history page ═════════════════════════════════════════════════

def test_history_page_shows_failures_to_the_owner_and_only_own_rows_to_a_cashier(owner_client, cashier_client, db_session):
    new_client(ip="7.7.13.1").post("/login", data={"pin": "9999"}, follow_redirects=False)
    page = owner_client.get("/security").text
    assert "7.7.13.1" in page and "Failed" in page
    own = cashier_client.get("/security").text
    assert "7.7.13.1" not in own, "a cashier must not see other people's sign-in attempts"
    assert "Test Cashier" in own


def test_security_page_needs_a_signin(anon_client):
    assert anon_client.get("/security", follow_redirects=False).headers["location"] == "/login"
    assert anon_client.post("/security/pin", data={}, follow_redirects=False).headers["location"] == "/login"


# ═══ the public status page ═══════════════════════════════════════════

def _make_repair(db, ticket, phone="(250) 555-4321", status="IN_PROGRESS"):
    cust = Customer(name="V48 Status Customer", phone=phone)
    db.add(cust)
    db.flush()
    rep = Repair(ticket_no=ticket, customer_id=cust.id, device="Pixel 8", issue="Screen", status=status, estimated_cost=123.45)
    db.add(rep)
    db.commit()
    return rep


def _cleanup_repair(db, rep):
    cid = rep.customer_id
    db.rollback()
    db.delete(db.get(Repair, rep.id))
    db.commit()                                   # the repair must be gone before its customer (PostgreSQL enforces this)
    db.query(Customer).filter(Customer.id == cid).delete()
    db.commit()


def test_status_lookup_still_works_with_ticket_and_phone_digits(anon_client, db_session):
    rep = _make_repair(db_session, 480001)
    r = anon_client.post("/status", data={"ticket_no": "480001", "phone_last4": "4321"})
    assert "Pixel 8" in r.text and "#480001" in r.text
    assert "V48 Status Customer" not in r.text and "123.45" not in r.text
    _cleanup_repair(db_session, rep)


def test_status_lookup_is_limited_after_repeated_misses(anon_client, db_session):
    rep = _make_repair(db_session, 480002)
    c = new_client(ip="7.7.14.1")
    for _ in range(10):
        c.post("/status", data={"ticket_no": "480002", "phone_last4": "0000"})
    r = c.post("/status", data={"ticket_no": "480002", "phone_last4": "4321"})          # even the right answer waits
    assert r.status_code == 429 and "Too many tries" in r.text and "Pixel 8" not in r.text
    elsewhere = new_client(ip="7.7.14.2").post("/status", data={"ticket_no": "480002", "phone_last4": "4321"})
    assert "Pixel 8" in elsewhere.text
    backdate_attempts(db_session, minutes=20)
    assert "Pixel 8" in c.post("/status", data={"ticket_no": "480002", "phone_last4": "4321"}).text
    _cleanup_repair(db_session, rep)


def test_status_misses_do_not_pause_staff_sign_in(db_session):
    c = new_client(ip="7.7.14.3")
    for _ in range(12):
        c.post("/status", data={"ticket_no": "1", "phone_last4": "0000"})
    assert c.post("/login", data={"pin": "1234"}, follow_redirects=False).headers["location"] == "/"


def test_wrong_ticket_and_wrong_digits_give_the_same_answer(anon_client, db_session):
    rep = _make_repair(db_session, 480003)
    a = anon_client.post("/status", data={"ticket_no": "480003", "phone_last4": "0000"}).text
    b = anon_client.post("/status", data={"ticket_no": "999999", "phone_last4": "4321"}).text
    assert "find a matching ticket" in a and "find a matching ticket" in b
    _cleanup_repair(db_session, rep)


def test_a_huge_ticket_number_is_just_not_found(anon_client):
    r = anon_client.post("/status", data={"ticket_no": "9" * 400, "phone_last4": "4321"})
    assert r.status_code == 200 and "find a matching ticket" in r.text


def test_staff_get_a_private_link_and_it_shows_only_status(owner_client, anon_client, db_session):
    rep = _make_repair(db_session, 480004, status="READY")
    page = owner_client.get(f"/repairs/{rep.id}").text
    link = page.split('id="status-link" value="')[1].split('"')[0]
    assert "/s/" in link
    token = link.rsplit("/s/", 1)[1]
    assert len(token) >= 16
    again = owner_client.get(f"/repairs/{rep.id}").text
    assert token in again, "the same link every time"
    r = anon_client.get(f"/s/{token}")
    assert r.status_code == 200 and "Pixel 8" in r.text and "Ready for pickup" in r.text
    assert "V48 Status Customer" not in r.text and "123.45" not in r.text and "555-4321" not in r.text
    _cleanup_repair(db_session, rep)


def test_unknown_or_malformed_links_give_a_generic_answer(anon_client):
    for token in ("a" * 16, "short", "x" * 200, "<script>alert(1)</script>"):
        r = anon_client.get(f"/s/{token}")
        assert r.status_code in (404, 422) or "isn't valid" in r.text
        assert "Pixel" not in r.text


def test_link_guessing_is_limited_too(db_session):
    c = new_client(ip="7.7.14.4")
    for i in range(10):
        c.get(f"/s/guess-number-{i:04d}-xxxx")
    assert c.get("/s/guess-number-9999-xxxx").status_code == 429


def test_the_status_pages_are_not_cached_by_browsers(anon_client):
    assert anon_client.get("/status").headers["cache-control"] == "no-store"


# ═══ security headers ═════════════════════════════════════════════════

def test_every_page_carries_the_security_headers(owner_client, anon_client):
    for r in (anon_client.get("/login"), owner_client.get("/"), anon_client.get("/does-not-exist"), anon_client.get("/static/manifest.json")):
        h = r.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "object-src 'none'" in h["content-security-policy"]
        assert h["referrer-policy"] == "same-origin"
        assert "camera=(self)" in h["permissions-policy"]


def test_html_is_never_cached_but_static_files_are_left_alone(owner_client, anon_client):
    assert owner_client.get("/").headers["cache-control"] == "no-store"
    assert "no-store" not in anon_client.get("/static/manifest.json").headers.get("cache-control", "")
    assert "no-store" not in owner_client.get("/api/notifications").headers.get("cache-control", "")


def test_hsts_only_when_hosted():
    assert not any(k == b"strict-transport-security" for k, _ in S.security_headers(False))
    assert any(k == b"strict-transport-security" for k, _ in S.security_headers(True))


def test_the_policy_still_allows_what_the_pages_really_use(anon_client):
    csp = anon_client.get("/login").headers["content-security-policy"]
    assert "'unsafe-inline'" in csp and "https://fonts.googleapis.com" in csp and "https://fonts.gstatic.com" in csp
    assert "data:" in csp and "blob:" in csp          # barcodes / camera previews (v50) need these
    assert "connect-src 'self'" in csp


# ═══ card numbers never stay in notes ═════════════════════════════════

def test_a_card_number_typed_into_a_note_is_masked_before_it_is_saved(db_session):
    c = Customer(name="V48 PAN", notes="paid with 4111 1111 1111 1111 last time")
    db_session.add(c)
    db_session.commit()
    db_session.expire_all()
    saved = db_session.get(Customer, c.id).notes
    assert saved == "paid with ************1111 last time"
    db_session.delete(db_session.get(Customer, c.id))
    db_session.commit()


def test_imeis_skus_and_other_long_numbers_are_left_alone(db_session):
    cust = Customer(name="V48 NoPAN", notes="ticket 480001, SKU 123456789012, order 20261122")
    db_session.add(cust)
    db_session.flush()
    rep = Repair(ticket_no=480099, customer_id=cust.id, device="Galaxy", imei="356938035643809",
                 issue="screen", description="IMEI 356938035643809 serial 000123456789")
    db_session.add(rep)
    db_session.commit()
    db_session.expire_all()
    r = db_session.get(Repair, rep.id)
    assert r.imei == "356938035643809" and "356938035643809" in r.description and "000123456789" in r.description
    assert db_session.get(Customer, cust.id).notes == "ticket 480001, SKU 123456789012, order 20261122"
    db_session.delete(r)
    db_session.delete(db_session.get(Customer, cust.id))
    db_session.commit()


def test_a_card_number_in_a_repair_description_is_masked_on_update_too(db_session):
    cust = Customer(name="V48 PAN2")
    db_session.add(cust)
    db_session.flush()
    rep = Repair(ticket_no=480098, customer_id=cust.id, device="X", description="ok")
    db_session.add(rep)
    db_session.commit()
    rep.description = "customer read out 5500 0000 0000 0004 over the phone"
    db_session.commit()
    db_session.expire_all()
    assert "5500" not in db_session.get(Repair, rep.id).description
    db_session.delete(db_session.get(Repair, rep.id))
    db_session.delete(db_session.get(Customer, cust.id))
    db_session.commit()


# ═══ upgrade safety ═══════════════════════════════════════════════════

def test_the_new_columns_and_table_exist():
    from sqlalchemy import inspect
    insp = inspect(engine)
    assert {"pin_len", "totp_secret", "totp_enabled", "totp_last_step", "recovery_codes"} <= {c["name"] for c in insp.get_columns("staff")}
    assert "status_token" in {c["name"] for c in insp.get_columns("repairs")}
    assert {"ip", "kind", "success", "staff_id", "device", "note"} <= {c["name"] for c in insp.get_columns("login_attempts")}


def test_history_is_trimmed_to_six_months_now_and_then(db_session, monkeypatch):
    old = LoginAttempt(ts=datetime.utcnow() - timedelta(days=400), ip="1.1.1.1", kind="pin", success=False)
    db_session.add(old)
    db_session.commit()
    old_id = old.id
    monkeypatch.setattr(S.secrets, "randbelow", lambda n: 0)
    S.log_attempt(db_session, _Req({}), "pin", False)
    db_session.expire_all()
    assert db_session.get(LoginAttempt, old_id) is None
