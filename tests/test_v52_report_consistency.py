"""v52.1 — the older Reports / End-of-day screens agree with the Report Center when a sale had a discount
(found by the whole-build end-to-end run: Gross Profit showed $866.94 on one screen and $845.94 on the other, and the
End-of-day row showed subtotal + tax that did not add up to its total)."""
import re

from app.models import Invoice, Product

TAG = "V52RC"


def _num(html, label):
    m = re.search(label + r'</div>\s*<div class="stat-value[^"]*">\$(-?[\d,]+\.\d\d)', html)
    assert m, label
    return float(m.group(1).replace(",", ""))


def test_discounted_sale_profit_matches_between_screens(owner_client, db_session):
    p = Product(sku=f"{TAG}-1", name=f"{TAG} item", category="ACCESSORY", price=100.0, cost=40.0, stock=20)
    db_session.add(p)
    db_session.commit()
    before = _num(owner_client.get("/reports").text, "Gross Profit \\(All Time\\)")
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    owner_client.post("/pos/discount", data={"mode": "$", "value": "10"})
    owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "500"}, follow_redirects=False)
    after = _num(owner_client.get("/reports").text, "Gross Profit \\(All Time\\)")
    assert round(after - before, 2) == 50.00          # $100 - $10 discount - $40 cost, not $60


def test_end_of_day_subtotal_plus_tax_equals_total(owner_client, db_session):
    p = Product(sku=f"{TAG}-2", name=f"{TAG} item2", category="ACCESSORY", price=100.0, cost=40.0, stock=20)
    db_session.add(p)
    db_session.commit()
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    owner_client.post("/pos/discount", data={"mode": "$", "value": "10"})
    owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "500"}, follow_redirects=False)
    from app.main import _build_eod_data, _local_month_str
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        days, *_ = _build_eod_data(db, _local_month_str(db))
        assert days
        for d in days:
            assert abs(round(d["subtotal"] + d["tax"], 2) - round(d["total"], 2)) < 0.005
    finally:
        db.close()
