"""v52.1 — text typed by staff can't become a live spreadsheet formula in the exports (found by the whole-build end-to-end run:
the plain CSV exports and the month-end workbook let a customer named "=HYPERLINK(...)" through)."""
import csv
import io

import openpyxl
import pytest

from app.models import Customer, Expense, Invoice

EVIL = '=HYPERLINK("http://evil.example","x")'


@pytest.fixture(autouse=True)
def _evil_rows(db_session, owner_client):
    c = Customer(name=EVIL, phone="778-555-0199", email="+cmd@example.com", notes="")
    db_session.add(c)
    db_session.commit()
    owner_client.post("/expenses", data={"amount": "5", "category": "Supplies", "description": "=1+1", "payee": "@SUM(A1)", "paid_from": "other"})
    yield
    db_session.rollback()
    db_session.query(Expense).filter(Expense.description == "=1+1").delete()
    db_session.query(Customer).filter(Customer.id == c.id).delete()
    db_session.commit()


@pytest.mark.parametrize("path", ["/export/csv/customers", "/export/csv/invoices", "/export/csv/inventory"])
def test_csv_exports_have_no_formula_cells(owner_client, path):
    rows = list(csv.reader(io.StringIO(owner_client.get(path).text)))
    for row in rows[1:]:
        for cell in row:
            assert not cell.startswith(("=", "@")) and not (cell.startswith("+") and not cell[1:2].isdigit()), cell


def test_customers_csv_keeps_the_text_visible(owner_client):
    rows = list(csv.reader(io.StringIO(owner_client.get("/export/csv/customers").text)))
    assert any(r[0] == "'" + EVIL for r in rows)


def test_month_end_workbook_has_no_formula_cells(owner_client, db_session):
    from datetime import datetime
    month = datetime.utcnow().strftime("%Y-%m")
    r = owner_client.get(f"/export/xlsx/month-end?month={month}")
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert sum(1 for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.data_type == "f") == 0
    texts = [str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value is not None]
    assert any("=1+1" in t for t in texts)          # still shown, as text
