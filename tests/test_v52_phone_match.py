"""v52.1 — the same phone number typed with different punctuation is the same customer (found by the whole-build end-to-end run).

A new ticket for "250-555-0142" used to create a second customer next to the stored "(250) 555-0142", splitting their
repair history, points and store credit across two records."""
import pytest

from app.models import Customer, Repair
from app.phones import find_customer_by_phone, phone_key, phone_search_clause

TAG = "V52PH"


@pytest.fixture(autouse=True)
def _clean(db_session):
    def wipe():
        db_session.rollback()
        ids = [c.id for c in db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).all()]
        if ids:
            db_session.query(Repair).filter(Repair.customer_id.in_(ids)).delete(synchronize_session=False)
            db_session.query(Customer).filter(Customer.id.in_(ids)).delete(synchronize_session=False)
        db_session.commit()
    wipe()
    yield
    wipe()


def _customer(db, name, phone):
    c = Customer(name=name, phone=phone)
    db.add(c)
    db.commit()
    return c


def _ticket(c, phone, name):
    return c.post("/repairs/add", data={"phone": phone, "name": name, "device": "iPhone 15", "issue": "Screen Replacement",
                                         "description": "x", "estimated_cost": "10"}, follow_redirects=False)


def test_phone_key_ignores_punctuation_and_country_code():
    assert phone_key("(250) 555-0142") == phone_key("250-555-0142") == phone_key("+1 250 555 0142") == phone_key("12505550142") == "2505550142"
    assert phone_key("") == "" and phone_key("N/A") == "" and phone_key("x12") == ""


@pytest.mark.parametrize("typed", ["250-555-0142", "2505550142", "+1 250 555 0142", "250.555.0142", "1 (250) 555-0142"])
def test_a_ticket_for_an_existing_number_reuses_the_customer(owner_client, db_session, typed):
    first = _customer(db_session, f"{TAG} Priya", "(250) 555-0142")
    r = _ticket(owner_client, typed, f"{TAG} Priya")
    assert r.status_code == 303
    assert db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).count() == 1
    rep = db_session.query(Repair).filter(Repair.customer_id == first.id).first()
    assert rep is not None


def test_a_different_number_is_a_different_customer(owner_client, db_session):
    _customer(db_session, f"{TAG} One", "(250) 555-0142")
    _ticket(owner_client, "250-555-0143", f"{TAG} Two")
    assert db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).count() == 2


def test_short_or_unusual_text_is_only_matched_exactly(db_session):
    odd = f"N/A {TAG}"            # not a phone number at all (unique to this test, so other tests' customers can't match first)
    c = _customer(db_session, f"{TAG} Odd", odd)
    assert find_customer_by_phone(db_session, odd).id == c.id
    assert find_customer_by_phone(db_session, "") is None
    assert find_customer_by_phone(db_session, "12345") is None


def test_customer_search_finds_a_number_typed_without_punctuation(owner_client, db_session):
    _customer(db_session, f"{TAG} Finder", "(250) 555-0142")
    r = owner_client.get("/customers?q=2505550142")
    assert f"{TAG} Finder" in r.text
    r = owner_client.get("/customers?q=250 555 0142")
    assert f"{TAG} Finder" in r.text
    r = owner_client.get("/search?q=2505550142")
    assert f"{TAG} Finder" in r.text
    r = owner_client.get("/customers?q=2505550199")
    assert f"{TAG} Finder" not in r.text


def test_phone_search_clause_only_for_phone_like_text():
    assert phone_search_clause("alice") is None
    assert phone_search_clause("12") is None
    assert phone_search_clause("") is None
    assert phone_search_clause("(250) 555") is not None
