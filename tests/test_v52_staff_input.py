"""v52.2 — a staff member needs a real name and one of the four roles, and the last Owner can't be demoted.

Found by the whole-build end-to-end run: a hand-made request could save a blank name or a role such as "superadmin",
and the only Owner could be switched to Manager on the Edit form (deactivating the last Owner was already refused),
which would leave nobody able to open Settings, backups or the Owner-only controls.
The Products page also threw a script error for cashiers and technicians (it set up the Add-product panel they do not have).
"""
import pytest

from app.auth import hash_pin
from app.models import Staff


@pytest.fixture(autouse=True)
def _clean(db_session):
    def wipe():
        db_session.rollback()
        for s in db_session.query(Staff).filter(Staff.name.like("V52S %")).all():
            db_session.delete(s)
        owner = db_session.get(Staff, "s1")
        if owner:
            owner.role, owner.active = "owner", True
        db_session.commit()
    wipe()
    yield
    wipe()


def _add(c, name, pin, role="cashier"):
    return c.post("/staff/add", data={"name": name, "pin": pin, "role": role}, follow_redirects=False)


def _n(db, **kw):
    db.expire_all()
    return db.query(Staff).filter_by(**kw).count()


def test_blank_or_spaces_only_name_is_refused(owner_client, db_session):
    before = db_session.query(Staff).count()
    for bad in ("", "   ", "\t"):
        r = _add(owner_client, bad, "518203")
        assert r.status_code in (303, 422)          # whitespace is turned back with a message; a truly empty field is stopped by the framework
    db_session.expire_all()
    assert db_session.query(Staff).count() == before
    assert "name" in owner_client.get("/staff").text.lower()


def test_made_up_role_is_refused_on_add(owner_client, db_session):
    for bad in ("superadmin", "OWNER", "admin", "Manager "):
        _add(owner_client, "V52S Rogue", "518204", bad)
    assert _n(db_session, name="V52S Rogue") == 0


def test_the_four_real_roles_still_work_and_the_name_is_trimmed(owner_client, db_session):
    for i, role in enumerate(("cashier", "technician", "manager", "owner")):
        r = _add(owner_client, f"  V52S {role}  ", f"51821{i}", role)
        assert r.status_code == 303
        db_session.expire_all()
        row = db_session.query(Staff).filter(Staff.name == f"V52S {role}").one()
        assert row.role == role


def test_overlong_name_is_refused(owner_client, db_session):
    _add(owner_client, "V52S " + "x" * 200, "518215")
    db_session.expire_all()
    assert db_session.query(Staff).filter(Staff.name.like("V52S x%")).count() == 0


def test_edit_refuses_blank_name_and_made_up_role(owner_client, db_session):
    _add(owner_client, "V52S Edit", "518216")
    row = db_session.query(Staff).filter(Staff.name == "V52S Edit").one()
    owner_client.post(f"/staff/{row.id}/edit", data={"name": "  ", "role": "cashier"})
    owner_client.post(f"/staff/{row.id}/edit", data={"name": "V52S Edit", "role": "superadmin"})
    db_session.expire_all()
    row = db_session.get(Staff, row.id)
    assert (row.name, row.role) == ("V52S Edit", "cashier")
    owner_client.post(f"/staff/{row.id}/edit", data={"name": " V52S Edit2 ", "role": "technician"})
    db_session.expire_all()
    row = db_session.get(Staff, row.id)
    assert (row.name, row.role) == ("V52S Edit2", "technician")


def test_the_only_active_owner_cannot_be_switched_to_another_role(owner_client, db_session):
    owners = db_session.query(Staff).filter(Staff.role == "owner", Staff.active == True).count()  # noqa: E712
    assert owners == 1
    r = owner_client.post("/staff/s1/edit", data={"name": "Owner", "role": "manager"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Staff, "s1").role == "owner"
    assert "last active Owner" in owner_client.get("/staff").text


def test_with_a_second_owner_the_first_can_step_down(owner_client, db_session):
    _add(owner_client, "V52S Second", "518217", "owner")
    owner_client.post("/staff/s1/edit", data={"name": "Owner", "role": "manager"})
    db_session.expire_all()
    assert db_session.get(Staff, "s1").role == "manager"
    assert db_session.query(Staff).filter(Staff.role == "owner", Staff.active == True).count() == 1  # noqa: E712


def test_a_deactivated_owner_does_not_count_as_a_second_owner(owner_client, db_session):
    _add(owner_client, "V52S Gone", "518218", "owner")
    gone = db_session.query(Staff).filter(Staff.name == "V52S Gone").one()
    gone.active = False
    db_session.commit()
    owner_client.post("/staff/s1/edit", data={"name": "Owner", "role": "manager"})
    db_session.expire_all()
    assert db_session.get(Staff, "s1").role == "owner"


def test_an_owner_can_still_rename_themselves_and_stay_owner(owner_client, db_session):
    owner_client.post("/staff/s1/edit", data={"name": "Owner Renamed", "role": "owner"})
    db_session.expire_all()
    row = db_session.get(Staff, "s1")
    assert (row.name, row.role) == ("Owner Renamed", "owner")
    owner_client.post("/staff/s1/edit", data={"name": "Owner", "role": "owner"})


def test_products_page_script_is_safe_without_the_add_panel(cashier_client, technician_client, owner_client):
    for c in (cashier_client, technician_client):
        html = c.get("/products").text
        assert 'id="ap-category"' not in html                                    # they have no Add panel...
        assert "if (document.getElementById('ap-category')" in html              # ...so the script must not reach for it
    html = owner_client.get("/products").text
    assert 'id="ap-category"' in html and "updateBrandOptions('ap-category','ap-brand')" in html
