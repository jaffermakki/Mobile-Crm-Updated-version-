"""v52.1 — trade-ins: the server checks the amount, and a new customer typed with an existing number is that customer
(found by the whole-build end-to-end run)."""
import pytest

from app.models import Customer, TradeIn

TAG = "V52TI"


@pytest.fixture(autouse=True)
def _clean(db_session):
    def wipe():
        db_session.rollback()
        db_session.query(TradeIn).filter(TradeIn.device.like(f"{TAG}%")).delete(synchronize_session=False)
        db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).delete(synchronize_session=False)
        db_session.commit()
    wipe()
    yield
    wipe()


def _post(c, **kw):
    data = {"device": f"{TAG} phone", "offered_amount": "50.00", "payout_method": "cash"}
    data.update(kw)
    return c.post("/trade-ins/add", data=data, follow_redirects=True)


def _count(db):
    db.expire_all()
    return db.query(TradeIn).filter(TradeIn.device.like(f"{TAG}%")).count()


@pytest.mark.parametrize("amount", ["-50", "-0.01", "nan", "inf", "-inf", "10000.01", "99999999"])
def test_bad_amounts_are_refused_and_nothing_is_recorded(owner_client, db_session, amount):
    r = _post(owner_client, offered_amount=amount)
    assert r.status_code == 200
    assert "not recorded" in r.text.lower()
    assert _count(db_session) == 0


def test_a_negative_store_credit_trade_in_cannot_reduce_a_balance(owner_client, db_session):
    c = Customer(name=f"{TAG} Holder", phone="555-0911", store_credit=40.0)
    db_session.add(c)
    db_session.commit()
    _post(owner_client, customer_id=c.id, offered_amount="-25", payout_method="store_credit")
    db_session.expire_all()
    assert db_session.get(Customer, c.id).store_credit == 40.0 and _count(db_session) == 0


def test_a_blank_device_is_refused(owner_client, db_session):
    r = _post(owner_client, device="   ")
    assert "enter the device" in r.text.lower()


def test_zero_and_the_limit_are_allowed(owner_client, db_session):
    _post(owner_client, offered_amount="0", device=f"{TAG} recycle")
    _post(owner_client, offered_amount="10000", device=f"{TAG} flagship")
    assert _count(db_session) == 2


def test_new_customer_with_an_existing_number_reuses_that_customer(owner_client, db_session):
    c = Customer(name=f"{TAG} Priya", phone="(250) 555-0142")
    db_session.add(c)
    db_session.commit()
    _post(owner_client, new_customer_name=f"{TAG} Priya S", new_customer_phone="250-555-0142", offered_amount="80", payout_method="store_credit")
    db_session.expire_all()
    assert db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).count() == 1
    assert db_session.get(Customer, c.id).store_credit == 80.0
