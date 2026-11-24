"""v52 — the Full Backup: returns and expenses come back after a restore, and a backup made by v51 (which has none of the
v52 tables) still restores."""
import pytest

from app import backup as B
from app.models import Expense, Invoice, Product, SaleReturn, SaleReturnLine, Staff
from tests.test_v52_returns import _close_sessions, _db, do_return, lines_of, mk_invoice  # noqa: F401  (the autouse fixture closes sessions)

V52_TABLES = ("sale_returns", "sale_return_lines", "expenses", "quote_approvals")


def _doc(db_session):
    db_session.expire_all()
    return B.read_backup(B.make_backup(db_session, None)[0])


def test_returns_and_expenses_come_back_after_a_restore(owner_client, db_session):
    iid = mk_invoice(0, [("Restore Thing", 20.0, 2, 5, True)])
    rid = do_return(owner_client, iid, {lines_of(iid)["Restore Thing"]: 1}, "Cash")
    assert rid
    owner_client.post("/expenses", data={"amount": "9.99", "category": "Supplies", "description": "restore cycle", "paid_from": "drawer"})
    doc = _doc(db_session)
    before = next(r for r in doc["tables"]["sale_returns"] if r["id"] == rid)

    # lose them, as if the data had been wiped
    db_session.rollback()
    db_session.query(SaleReturnLine).filter(SaleReturnLine.return_id == rid).delete(synchronize_session=False)
    db_session.query(SaleReturn).filter(SaleReturn.id == rid).delete(synchronize_session=False)
    db_session.query(Expense).filter(Expense.description == "restore cycle").delete(synchronize_session=False)
    db_session.commit()
    assert db_session.get(SaleReturn, rid) is None

    B.restore_full(db_session, doc, db_session.get(Staff, "s1"))
    db_session.expire_all()
    back = db_session.get(SaleReturn, rid)
    assert back is not None and back.invoice_id == iid and round(back.total, 2) == round(before["total"], 2)
    assert back.number == before["number"]
    assert db_session.query(SaleReturnLine).filter(SaleReturnLine.return_id == rid).count() == 1
    assert db_session.query(Expense).filter(Expense.description == "restore cycle").count() == 1
    # and the invoice still knows about its return, so a second full refund stays blocked
    assert len(db_session.get(Invoice, iid).returns) == 1
    page = owner_client.get(f"/invoices/{iid}").text
    assert "Return items" in page


def test_a_v51_backup_without_the_new_tables_restores(owner_client, db_session):
    """A file made before v52 has none of the v52 tables. It must restore without an error and give back the core data."""
    iid = mk_invoice(0, [("Old File Thing", 15.0, 1, 4, True)])
    doc = _doc(db_session)
    for name in V52_TABLES:
        doc["tables"].pop(name, None)
    doc.pop("app", None)
    assert not any(t in doc["tables"] for t in V52_TABLES)
    result = B.restore_full(db_session, doc, db_session.get(Staff, "s1"))
    db_session.expire_all()
    assert result.get("invoices", 0) >= 1
    assert db_session.get(Invoice, iid) is not None
    assert db_session.query(SaleReturn).count() == 0 and db_session.query(Expense).count() == 0   # an old file has no returns/expenses
    assert db_session.get(Staff, "s1").role == "owner"
    # the app still works on the restored data
    for url in ("/expenses", "/cashup", "/repairs", f"/invoices/{iid}"):
        assert owner_client.get(url, follow_redirects=False).status_code == 200, url


def test_restore_summary_names_the_new_tables():
    for name in V52_TABLES:
        assert name in B.TABLE_LABELS, name
    assert "Returned items" in B.TABLE_LABELS["sale_returns"] and "Expenses" in B.TABLE_LABELS["expenses"]
