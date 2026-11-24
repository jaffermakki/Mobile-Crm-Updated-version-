"""v52.1 — Cash Up refuses nonsense numbers instead of saving them (found by the whole-build end-to-end run).
A saved "nan" opening float would have been carried into every later day's expected-cash figure."""
import pytest

from app.models import CashSession, Setting


@pytest.fixture(autouse=True)
def _restore(db_session):
    before = db_session.query(Setting).filter(Setting.key == "cash_float").first()
    keep = before.value if before else None
    n = db_session.query(CashSession).count()
    yield
    db_session.rollback()
    row = db_session.query(Setting).filter(Setting.key == "cash_float").first()
    if keep is None and row is not None:
        db_session.delete(row)
    elif row is not None:
        row.value = keep
    for s in db_session.query(CashSession).order_by(CashSession.closed_at.desc()).limit(max(0, db_session.query(CashSession).count() - n)).all():
        db_session.delete(s)
    db_session.commit()


@pytest.mark.parametrize("float_,actual,batch", [("nan", "100", ""), ("200", "inf", ""), ("-5", "100", ""), ("200", "-1", ""),
                                                 ("200", "100", "abc"), ("200", "100", "nan"), ("1e12", "100", "")])
def test_nonsense_numbers_are_not_saved(owner_client, db_session, float_, actual, batch):
    n = db_session.query(CashSession).count()
    r = owner_client.post("/cashup/close", data={"open_float": float_, "actual": actual, "card_batch": batch, "notes": ""}, follow_redirects=True)
    assert r.status_code == 200 and "was not saved" in r.text
    db_session.expire_all()
    assert db_session.query(CashSession).count() == n
    row = db_session.query(Setting).filter(Setting.key == "cash_float").first()
    assert row is None or row.value not in ("nan", "-5", "1e12")


def test_a_normal_close_still_works(owner_client, db_session):
    r = owner_client.post("/cashup/close", data={"open_float": "150.00", "actual": "150.00", "card_batch": "", "notes": "ok"}, follow_redirects=True)
    assert r.status_code == 200 and "was not saved" not in r.text
