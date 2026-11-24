"""v52.2 — a customer (and a repair ticket) needs a real name; the refusal is shown on the page.

Found by the whole-build end-to-end run: the Edit form (or a hand-made request) could save a customer with a blank name, and a
repair ticket could be created for a blank name or phone. The Customers and Repairs pages had nowhere to show a refusal message.
"""
import pytest

from app.models import Customer, Repair


@pytest.fixture
def cust(db_session):
    c = Customer(name="Edit Edna", phone="555-0401", email="")
    db_session.add(c)
    db_session.commit()
    yield c
    db_session.rollback()
    db_session.delete(db_session.get(Customer, c.id))
    db_session.commit()


def test_blank_customer_name_is_refused_on_add(owner_client, db_session):
    before = db_session.query(Customer).count()
    for bad in ("", "   ", "\t"):
        r = owner_client.post("/customers/add", data={"name": bad, "phone": "555-0402"}, follow_redirects=False)
        assert r.status_code in (303, 422)
    r = owner_client.post("/customers/add", data={"name": "x" * 500, "phone": "555-0402"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.query(Customer).count() == before
    page = owner_client.get("/customers")
    assert "Customer not added" in page.text           # the refusal is shown, once
    assert "Customer not added" not in owner_client.get("/customers").text


def test_customer_name_is_trimmed_on_add_and_edit(owner_client, db_session, cust):
    owner_client.post("/customers/add", data={"name": "  Trim Tina  ", "phone": " 555-0403 ", "email": " t@example.com "})
    db_session.expire_all()
    t = db_session.query(Customer).filter_by(name="Trim Tina").one()
    assert (t.phone, t.email) == ("555-0403", "t@example.com")
    db_session.delete(t)
    db_session.commit()
    owner_client.post(f"/customers/{cust.id}/edit", data={"name": "  Edna Two ", "phone": "555-0401", "email": ""})
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).name == "Edna Two"


def test_blank_name_is_refused_on_edit_and_the_old_one_stays(owner_client, db_session, cust):
    for bad in ("", "   "):
        r = owner_client.post(f"/customers/{cust.id}/edit", data={"name": bad, "phone": "555-0401", "email": "z@example.com"}, follow_redirects=False)
        assert r.status_code in (303, 422)
    db_session.expire_all()
    c = db_session.get(Customer, cust.id)
    assert c.name == "Edit Edna" and (c.email or "") == ""
    assert "Not saved: Enter the customer" in owner_client.get(f"/customers/{cust.id}").text


def test_repair_ticket_needs_a_name_a_phone_and_a_device(owner_client, db_session):
    before = db_session.query(Repair).count()
    base = {"phone": "555-0404", "name": "Ticket Tim", "device": "Pixel 8", "issue": "Screen Replacement", "warranty_days": "90"}
    for field in ("name", "phone", "device"):
        owner_client.post("/repairs/add", data={**base, field: "   "}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.query(Repair).count() == before
    assert "Ticket not created" in owner_client.get("/repairs").text
