"""v52.1 — two active people can't share a sign-in PIN.

The sign-in screen asks for a PIN only, so a shared PIN signs in whoever comes first (sales and the audit trail land
on the wrong name; a cashier could land in a manager's account). Found by the whole-build end-to-end run.
The refusal also tells the asker "somebody uses that PIN", so refusals are counted and, past a few an hour, the
answer stops depending on the PIN.
"""
import pytest

from app import security as S
from app.auth import hash_pin
from app.models import LoginAttempt, Staff
from v48_helpers import new_client, signed_in


@pytest.fixture(autouse=True)
def _clean(db_session, monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-real-ip")

    def wipe():
        db_session.rollback()
        db_session.query(LoginAttempt).filter(LoginAttempt.kind == "pin_taken").delete()
        for s in db_session.query(Staff).filter(Staff.name.like("V52P %")).all():
            db_session.delete(s)
        for sid, pin in (("s1", "1234"), ("test-manager", "2222"), ("test-cashier", "3333"), ("test-technician", "4444")):
            row = db_session.get(Staff, sid)
            if row:
                row.pin_hash, row.pin_len, row.active = hash_pin(pin), 4, True
        db_session.commit()
    wipe()
    yield
    wipe()


def _add(c, name, pin, role="cashier"):
    return c.post("/staff/add", data={"name": name, "pin": pin, "role": role}, follow_redirects=False)


def _count(db, name):
    db.expire_all()
    return db.query(Staff).filter(Staff.name == name).count()


def test_adding_someone_with_a_pin_already_in_use_is_refused(owner_client, db_session):
    r = _add(owner_client, "V52P Twin", "3333")                      # the cashier's PIN
    assert r.status_code == 400
    assert S.PIN_TAKEN_MESSAGE in r.text
    assert _count(db_session, "V52P Twin") == 0


def test_a_free_pin_still_works_and_the_new_person_signs_in_as_themselves(owner_client, db_session):
    r = _add(owner_client, "V52P Solo", "482915")
    assert r.status_code == 303 and _count(db_session, "V52P Solo") == 1
    c = new_client("482915", ip="8.8.1.1")
    assert signed_in(c)
    me = db_session.query(Staff).filter(Staff.name == "V52P Solo").one()
    assert me.pin_len == 6
    assert "V52P Solo" in c.get("/security").text                      # their own sign-in is on their own history


def test_editing_a_person_to_someone_elses_pin_is_refused_but_their_own_pin_is_fine(owner_client, db_session):
    owner_client.post("/staff/add", data={"name": "V52P Edit", "pin": "573920", "role": "cashier"})
    s = db_session.query(Staff).filter(Staff.name == "V52P Edit").one()
    before = s.pin_hash
    r = owner_client.post(f"/staff/{s.id}/edit", data={"name": "V52P Edit", "role": "cashier", "new_pin": "4444"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Staff, s.id).pin_hash == before             # nothing changed
    assert "pick a different one" in owner_client.get("/staff").text
    # re-saving a person's own PIN is not "taken by someone else"
    r = owner_client.post(f"/staff/{s.id}/edit", data={"name": "V52P Edit", "role": "cashier", "new_pin": "573920"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(Staff, s.id).pin_hash != before            # re-saved (new salt), so accepted
    assert signed_in(new_client("573920", ip="8.8.1.2"))


def test_changing_your_own_pin_to_a_pin_in_use_is_refused(cashier_client, db_session):
    r = cashier_client.post("/security/pin", data={"current_pin": "3333", "new_pin": "2222", "confirm_pin": "2222"}, follow_redirects=True)
    assert "pick a different one" in r.text
    assert signed_in(new_client("3333", ip="8.8.1.3"))                # still the old PIN
    assert signed_in(new_client("2222", ip="8.8.1.4"))                # and the manager is still the manager
    assert new_client(ip="8.8.1.5").get("/", follow_redirects=False).status_code != 200


def test_a_pin_belonging_to_a_deactivated_person_is_free(owner_client, db_session):
    owner_client.post("/staff/add", data={"name": "V52P Gone", "pin": "615284", "role": "cashier"})
    gone = db_session.query(Staff).filter(Staff.name == "V52P Gone").one()
    gone.active = False
    db_session.commit()
    r = _add(owner_client, "V52P Heir", "615284")
    assert r.status_code == 303 and _count(db_session, "V52P Heir") == 1


def test_forgot_pin_recovery_cannot_pick_someone_elses_pin(db_session):
    from app.main import set_setting
    set_setting(db_session, "security_answer_hash", hash_pin("blue"))
    db_session.commit()
    c = new_client(ip="8.8.2.1")
    owner = db_session.get(Staff, "s1")
    r = c.post("/forgot-pin", data={"staff_id": owner.id, "security_answer": "blue", "new_pin": "3333", "confirm_pin": "3333"}, follow_redirects=True)
    assert "pick a different one" in r.text
    assert signed_in(new_client("1234", ip="8.8.2.2"))                # owner's PIN unchanged
    set_setting(db_session, "security_answer_hash", "")
    db_session.commit()


def test_refusals_are_counted_and_the_answer_stops_depending_on_the_pin(owner_client, db_session):
    for i in range(S.PIN_TAKEN_LIMIT):
        assert S.PIN_TAKEN_MESSAGE in _add(owner_client, f"V52P Probe{i}", "4444").text
    r = _add(owner_client, "V52P Probe-free", "951846")               # a perfectly free PIN, but the limit is reached
    assert r.status_code == 400 and S.PIN_TAKEN_THROTTLED in r.text
    assert _count(db_session, "V52P Probe-free") == 0
    r = _add(owner_client, "V52P Probe-taken", "4444")                # and a taken one gets the same answer
    assert S.PIN_TAKEN_THROTTLED in r.text and S.PIN_TAKEN_MESSAGE not in r.text


def test_the_refusals_do_not_count_toward_the_wrong_pin_pause(owner_client, db_session):
    for i in range(S.PIN_TAKEN_LIMIT):
        _add(owner_client, f"V52P Q{i}", "4444")
    assert S.login_block_seconds(db_session, type("R", (), {"headers": {"x-real-ip": "9.9.9.9"}, "client": type("C", (), {"host": "testclient"})()})()) == 0


def test_the_refusals_show_in_the_sign_in_history(owner_client, db_session):
    _add(owner_client, "V52P Hist", "2222")
    page = owner_client.get("/security?all=1").text
    assert "PIN already used by someone else" in page
