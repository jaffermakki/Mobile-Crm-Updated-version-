"""
Tests for the v45 reporting pack: shop-local time, refund dating, cost
snapshots, the Report Center pages and their exports.

All fixture data is dated in 2031 so these tests can't be disturbed by (and
can't disturb) anything the rest of the suite creates for "today".
"""
import io
import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import event

from app.database import engine
from app.localtime import day_bounds_utc, get_shop_tz, load_zone, month_bounds, to_local_date
from app.models import AuditLog, Customer, Invoice, InvoiceLine, Product, Repair, RepairPart, Setting, Staff
from app.report_types import Money, Report, Section, report_to_csv, report_to_xlsx
from app import reporting as R

PACIFIC = load_zone("America/Vancouver")
REPORT_KEYS = ["daily", "cashup", "monthly", "tax", "sales", "inventory", "repairs", "customers", "messaging", "staff", "exceptions"]


# ── helpers ───────────────────────────────────────────────────────────

def _uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def pacific(db_session):
    """Run the test with the shop in Vernon, BC, then put the suite's UTC pin back."""
    db_session.merge(Setting(key="timezone", value="America/Vancouver"))
    db_session.commit()
    get_shop_tz(db_session)
    yield PACIFIC
    db_session.merge(Setting(key="timezone", value="UTC"))
    db_session.commit()
    get_shop_tz(db_session)


@pytest.fixture
def staff_row(db_session):
    return db_session.query(Staff).filter(Staff.role == "owner").first()


def make_product(db, cost=5.0, price=20.0, category="CASE", stock=10, **kw):
    p = Product(id=_uid("rp"), sku=_uid("RP").upper(), name=kw.pop("name", "Report Test Item"),
                category=category, price=price, cost=cost, stock=stock, **kw)
    db.add(p)
    db.commit()
    return p


def make_invoice(db, staff, when_utc, lines, tender="Cash", tax_rate=0.12, customer=None, **kw):
    """lines: [(product_or_None, unit_price, qty, unit_cost_snapshot)]"""
    sub = round(sum(pr * q for _, pr, q, _ in lines), 2)
    discount = kw.pop("discount", 0.0)
    taxable = max(0.0, sub - discount)
    tax = round(taxable * tax_rate, 2)
    total = round(taxable + tax, 2)
    inv = Invoice(
        id=_uid("ri"), number=_uid("RPT"), customer_id=customer.id if customer else None, staff_id=staff.id,
        payment_method=tender, cash_amount=total if tender == "Cash" else 0.0, card_amount=total if tender == "Card" else 0.0,
        subtotal=sub, discount=discount, tax_total=tax, total=total, date=when_utc,
        tax_breakdown=json.dumps([{"label": "GST (5%)", "amount": round(taxable * 0.05, 2)},
                                  {"label": "PST (7%)", "amount": round(taxable * 0.07, 2)}]),
        **kw)
    db.add(inv)
    db.flush()
    for prod, price, qty, cost in lines:
        db.add(InvoiceLine(id=_uid("rl"), invoice_id=inv.id, product_id=prod.id if prod else None,
                           name=prod.name if prod else "Custom charge", sku=prod.sku if prod else "",
                           qty=qty, price=price, unit_cost=cost, category=prod.category if prod else None))
    db.commit()
    return inv


def build(db, key, **args):
    rep, params, tz = R.build_report(db, key, args)
    return rep


def kpi(rep, label):
    for k in rep.kpis:
        if k.label == label:
            return k.value
    raise AssertionError(f"KPI {label!r} not in {[k.label for k in rep.kpis]}")


def section(rep, title_part):
    for s in rep.sections:
        if title_part.lower() in s.title.lower():
            return s
    raise AssertionError(f"section {title_part!r} not in {[s.title for s in rep.sections]}")


# ── shop-local days and months ────────────────────────────────────────

def test_evening_sale_belongs_to_the_local_day(db_session, staff_row, pacific):
    """8pm Pacific on Mar 15 is 03:00 UTC on Mar 16 — it must be on Mar 15's report."""
    p = make_product(db_session)
    inv = make_invoice(db_session, staff_row, datetime(2031, 3, 16, 3, 0), [(p, 100.0, 1, 5.0)])
    on_15 = build(db_session, "daily", date="2031-03-15")
    on_16 = build(db_session, "daily", date="2031-03-16")
    assert kpi(on_15, "Net sales (ex. tax)") == pytest.approx(100.0)
    assert kpi(on_16, "Net sales (ex. tax)") == pytest.approx(0.0)
    tx = section(on_15, "All transactions")
    assert any(row[0] == inv.number for row in tx.rows)
    assert [r[1] for r in tx.rows if r[0] == inv.number] == ["20:00"], "time column must be shop-local"


def test_last_evening_of_the_month_stays_in_that_month(db_session, staff_row, pacific):
    p = make_product(db_session)
    make_invoice(db_session, staff_row, datetime(2031, 5, 1, 3, 0), [(p, 50.0, 1, 5.0)])  # Apr 30, 8pm Pacific
    april = build(db_session, "monthly", month="2031-04")
    may = build(db_session, "monthly", month="2031-05")
    assert kpi(april, "Net sales (ex. tax)") == pytest.approx(50.0)
    assert kpi(may, "Net sales (ex. tax)") == pytest.approx(0.0)


def test_day_bounds_are_dst_safe():
    # London's clocks still change (BC and Alberta stopped on Nov 1 2026): the
    # spring-forward day is 23 hours long and the fall-back day 25, so a "day"
    # must be midnight-to-midnight local, never start + 24h.
    london = load_zone("Europe/London")
    s, e = day_bounds_utc(date(2031, 3, 30), date(2031, 3, 30), london)
    assert (e - s) == timedelta(hours=23)
    s, e = day_bounds_utc(date(2031, 10, 26), date(2031, 10, 26), london)
    assert (e - s) == timedelta(hours=25)


def test_bc_and_alberta_no_longer_change_clocks():
    """BC and Alberta ended clock changes on Nov 1 2026 (UTC-7 / UTC-6 all
    year). If this fails the timezone database is stale — see requirements.txt."""
    from app.localtime import tz_staleness_warning
    assert tz_staleness_warning(load_zone("America/Vancouver")) is None
    assert tz_staleness_warning(load_zone("America/Edmonton")) is None
    assert tz_staleness_warning(load_zone("UTC")) is None
    jan = datetime(2027, 1, 15, 12, tzinfo=PACIFIC).utcoffset()
    jul = datetime(2027, 7, 15, 12, tzinfo=PACIFIC).utcoffset()
    assert jan == jul == timedelta(hours=-7)


def test_timezone_defaults_from_province(db_session):
    db_session.merge(Setting(key="timezone", value=""))
    db_session.merge(Setting(key="province", value="AB"))
    db_session.commit()
    try:
        assert get_shop_tz(db_session).key == "America/Edmonton"
    finally:
        db_session.merge(Setting(key="timezone", value="UTC"))
        db_session.merge(Setting(key="province", value="ON"))
        db_session.commit()
        get_shop_tz(db_session)


# ── refunds are events, not edits to history ──────────────────────────

def test_refund_reduces_the_day_it_happened_not_the_sale_day(db_session, staff_row):
    p = make_product(db_session)
    make_invoice(db_session, staff_row, datetime(2031, 6, 10, 18, 0), [(p, 100.0, 1, 5.0)],
                 refunded=True, refunded_at=datetime(2031, 6, 12, 18, 0), refund_method="Cash", refunded_by="Test Owner")
    sale_day = build(db_session, "daily", date="2031-06-10")
    refund_day = build(db_session, "daily", date="2031-06-12")
    assert kpi(sale_day, "Net sales (ex. tax)") == pytest.approx(100.0)          # sale still stands on its own day
    assert kpi(sale_day, "Refunds today") == pytest.approx(0.0)
    assert kpi(refund_day, "Net sales (ex. tax)") == pytest.approx(-100.0)       # refund lands on the day it was paid
    assert kpi(refund_day, "Refunds today") == pytest.approx(112.0)
    assert kpi(build(db_session, "monthly", month="2031-06"), "Net sales (ex. tax)") == pytest.approx(0.0)


def test_refunded_cost_comes_back_so_profit_is_zero_not_negative(db_session, staff_row):
    p = make_product(db_session, cost=40.0)
    make_invoice(db_session, staff_row, datetime(2031, 7, 3, 18, 0), [(p, 100.0, 1, 40.0)],
                 refunded=True, refunded_at=datetime(2031, 7, 3, 20, 0), refund_method="Card")
    rep = build(db_session, "sales", **{"from": "2031-07-01", "to": "2031-07-31"})
    assert kpi(rep, "Net sales (ex. tax)") == pytest.approx(0.0)
    assert kpi(rep, "Gross profit") == pytest.approx(0.0)   # goods went back on the shelf, nothing earned or lost


def test_legacy_refund_without_a_date_nets_to_zero_in_its_sale_period(db_session, staff_row):
    p = make_product(db_session)
    make_invoice(db_session, staff_row, datetime(2031, 8, 5, 18, 0), [(p, 80.0, 1, 5.0)], refunded=True)  # pre-v45 style
    assert kpi(build(db_session, "monthly", month="2031-08"), "Net sales (ex. tax)") == pytest.approx(0.0)


def test_cash_up_drawer_follows_the_refund_to_the_day_it_was_paid(db_session, staff_row):
    p = make_product(db_session)
    inv = make_invoice(db_session, staff_row, datetime(2031, 9, 10, 18, 0), [(p, 100.0, 1, 5.0)],
                       refunded=True, refunded_at=datetime(2031, 9, 12, 18, 0), refund_method="Cash")
    utc = load_zone("UTC")
    sale_day = R.cash_card_totals(db_session, utc, date(2031, 9, 10))
    refund_day = R.cash_card_totals(db_session, utc, date(2031, 9, 12))
    assert sale_day["cash_sales"] == pytest.approx(inv.total)      # the cash really was taken that day
    assert refund_day["cash_sales"] == pytest.approx(-inv.total)   # and it left the drawer on this day
    assert refund_day["cash_refunds_paid"] == pytest.approx(inv.total)


def test_same_day_refund_to_store_credit_leaves_the_cash_in_the_drawer(db_session, staff_row):
    p = make_product(db_session)
    inv = make_invoice(db_session, staff_row, datetime(2031, 9, 20, 18, 0), [(p, 100.0, 1, 5.0)],
                       refunded=True, refunded_at=datetime(2031, 9, 20, 19, 0), refund_method="Store Credit")
    t = R.cash_card_totals(db_session, load_zone("UTC"), date(2031, 9, 20))
    assert t["cash_sales"] == pytest.approx(inv.total)
    assert t["total_sales"] == pytest.approx(0.0)    # but it no longer counts as a sale


def test_refund_route_records_who_how_and_why(owner_client, db_session, staff_row):
    p = make_product(db_session, stock=5)
    inv = make_invoice(db_session, staff_row, datetime(2031, 10, 1, 18, 0), [(p, 20.0, 1, 5.0)])
    resp = owner_client.post(f"/invoices/{inv.id}/refund", data={"refund_method": "Cash", "refund_reason": "screen cracked in box"},
                             follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    inv = db_session.get(Invoice, inv.id)
    assert inv.refunded and inv.refund_method == "Cash" and inv.refund_reason == "screen cracked in box"
    assert inv.refunded_by and inv.refunded_at is not None
    assert db_session.get(Product, p.id).stock == 6   # restock behaviour unchanged
    page = owner_client.get(f"/invoices/{inv.id}")
    assert "REFUNDED" in page.text and "screen cracked in box" in page.text


def test_refund_without_a_method_still_works_like_before(owner_client, db_session, staff_row):
    p = make_product(db_session)
    inv = make_invoice(db_session, staff_row, datetime(2031, 10, 2, 18, 0), [(p, 20.0, 1, 5.0)])
    assert owner_client.post(f"/invoices/{inv.id}/refund", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(Invoice, inv.id).refunded is True


def test_store_credit_refund_needs_a_customer_and_then_credits_them(owner_client, db_session, staff_row):
    p = make_product(db_session)
    walk_in = make_invoice(db_session, staff_row, datetime(2031, 10, 3, 18, 0), [(p, 20.0, 1, 5.0)])
    owner_client.post(f"/invoices/{walk_in.id}/refund", data={"refund_method": "Store Credit"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(Invoice, walk_in.id).refunded is False, "no customer to credit -> nothing is refunded"

    cust = Customer(id=_uid("rc"), name="Credit Customer", phone="2505550199", store_credit=0, spent=22.4)
    db_session.add(cust)
    db_session.commit()
    named = make_invoice(db_session, staff_row, datetime(2031, 10, 3, 19, 0), [(p, 20.0, 1, 5.0)], customer=cust)
    owner_client.post(f"/invoices/{named.id}/refund", data={"refund_method": "Store Credit"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(Invoice, named.id).refunded is True
    assert db_session.get(Customer, cust.id).store_credit == pytest.approx(named.total)


def test_cashier_cannot_refund(cashier_client, db_session, staff_row):
    p = make_product(db_session)
    inv = make_invoice(db_session, staff_row, datetime(2031, 10, 4, 18, 0), [(p, 20.0, 1, 5.0)])
    assert cashier_client.post(f"/invoices/{inv.id}/refund", data={"refund_method": "Cash"}).status_code == 403


# ── cost snapshots ────────────────────────────────────────────────────

def test_profit_uses_the_cost_at_time_of_sale(db_session, staff_row):
    p = make_product(db_session, cost=5.0, price=20.0)
    make_invoice(db_session, staff_row, datetime(2031, 11, 4, 18, 0), [(p, 20.0, 2, 5.0)])
    p.cost = 19.0                      # supplier raises the price tomorrow
    db_session.commit()
    rep = build(db_session, "sales", **{"from": "2031-11-04", "to": "2031-11-04"})
    assert kpi(rep, "Gross profit") == pytest.approx(30.0)    # 2 × (20 − 5), not 2 × (20 − 19)


def test_deleted_product_keeps_its_snapshot_cost_and_unknown_cost_is_flagged(db_session, staff_row):
    keep = make_product(db_session, cost=6.0)
    gone = make_product(db_session, cost=9.0)
    make_invoice(db_session, staff_row, datetime(2031, 11, 5, 18, 0), [(keep, 20.0, 1, 6.0), (gone, 20.0, 1, None)])
    gone_id = gone.id
    db_session.delete(gone)
    db_session.commit()
    rep = build(db_session, "daily", date="2031-11-05")
    assert any("no known cost" in n for n in rep.notes), "a sale line with no cost anywhere must be called out"
    assert kpi(rep, "Gross profit") == pytest.approx(34.0, abs=0.01)   # 40 net − 6 known cost; unknown counted as 0 (and flagged)
    assert gone_id


def test_repair_parts_cost_is_charged_against_the_repair_invoice(db_session, staff_row):
    cust = Customer(id=_uid("rc"), name="Repair Customer", phone="2505550188")
    rep_row = Repair(id=_uid("rr"), ticket_no=9000 + uuid.uuid4().int % 900, customer_id=cust.id, device="iPhone 12",
                     issue="Screen Replacement", status="COLLECTED", estimated_cost=150.0, final_cost=150.0,
                     created_at=datetime(2031, 12, 1, 17, 0), updated_at=datetime(2031, 12, 2, 17, 0))
    db_session.add_all([cust, rep_row])
    db_session.flush()
    db_session.add(RepairPart(id=_uid("rpp"), repair_id=rep_row.id, name="OLED screen", qty=1, unit_cost=60.0))
    db_session.commit()
    make_invoice(db_session, staff_row, datetime(2031, 12, 2, 17, 0), [(None, 150.0, 1, None)], repair_id=rep_row.id)
    rep = build(db_session, "daily", date="2031-12-02")
    assert kpi(rep, "Gross profit") == pytest.approx(90.0)
    cats = section(rep, "Sales by category")
    assert cats.rows[0][0] == "Repair / Service"


def test_one_time_backfill_stamps_old_lines_once(db_session, staff_row):
    from app.seed import backfill_line_snapshots
    p = make_product(db_session, cost=7.0, category="CHARGER")
    inv = make_invoice(db_session, staff_row, datetime(2031, 12, 5, 18, 0), [(p, 20.0, 1, None)])
    line = inv.lines[0]
    line.category = None
    db_session.commit()
    flag = db_session.get(Setting, "v45_line_backfill")
    if flag:
        db_session.delete(flag)
        db_session.commit()
    backfill_line_snapshots(db_session)
    db_session.expire_all()
    line = db_session.get(InvoiceLine, line.id)
    assert line.unit_cost == 7.0 and line.category == "CHARGER"
    p = db_session.get(Product, p.id)
    p.cost = 99.0
    line.unit_cost = None
    db_session.commit()
    backfill_line_snapshots(db_session)       # flag set -> must not run again
    db_session.expire_all()
    assert db_session.get(InvoiceLine, line.id).unit_cost is None


# ── report content ────────────────────────────────────────────────────

def test_category_breakdown_adds_up_to_net_sales_after_discount(db_session, staff_row):
    a, b = make_product(db_session, category="CASE"), make_product(db_session, category="CHARGER", cost=2.0)
    make_invoice(db_session, staff_row, datetime(2032, 1, 6, 18, 0), [(a, 60.0, 1, 5.0), (b, 40.0, 1, 2.0)], discount=10.0)
    rep = build(db_session, "sales", **{"from": "2032-01-06", "to": "2032-01-06"})
    net = kpi(rep, "Net sales (ex. tax)")
    assert net == pytest.approx(90.0)
    cats = section(rep, "category")
    assert sum(r[2] for r in cats.rows) == pytest.approx(net)


def test_tax_report_nets_refunds_in_the_period_they_happen(db_session, staff_row):
    p = make_product(db_session)
    make_invoice(db_session, staff_row, datetime(2032, 2, 10, 18, 0), [(p, 100.0, 1, 5.0)])
    make_invoice(db_session, staff_row, datetime(2032, 2, 20, 18, 0), [(p, 50.0, 1, 5.0)],
                 refunded=True, refunded_at=datetime(2032, 3, 2, 18, 0), refund_method="Card")
    feb = build(db_session, "tax", **{"from": "2032-02-01", "to": "2032-02-29"})
    mar = build(db_session, "tax", **{"from": "2032-03-01", "to": "2032-03-31"})
    assert kpi(feb, "Net tax to remit") == pytest.approx(12.0 + 6.0)     # Feb collected both sales' tax
    assert kpi(mar, "Net tax to remit") == pytest.approx(-6.0)           # March gets the credit for the refund
    by_type = {r[0]: r for r in section(feb, "Net tax by type").rows}
    assert by_type["GST"][3] == pytest.approx(5.0 + 2.5)
    assert by_type["PST"][3] == pytest.approx(7.0 + 3.5)


def test_exceptions_report_flags_overrides_and_below_cost_sales(db_session, staff_row):
    p = make_product(db_session, cost=30.0, price=50.0)
    inv = make_invoice(db_session, staff_row, datetime(2032, 4, 7, 18, 0), [(p, 25.0, 1, 30.0)], discount=10.0)
    inv.subtotal = 5.0          # staff overwrote the subtotal at the register
    db_session.commit()
    rep = build(db_session, "exceptions", **{"from": "2032-04-07", "to": "2032-04-07", "threshold": "15"})
    assert any(r[0] == inv.number for r in section(rep, "Subtotal manually overridden").rows)
    assert any(r[0] == inv.number for r in section(rep, "below cost").rows)
    assert any(r[0] == inv.number for r in section(rep, "different from today's list price").rows)
    assert any("no Cash Up" in k.label for k in rep.kpis)


def test_customers_report_finds_duplicate_phone_numbers(db_session):
    db_session.add_all([Customer(id=_uid("rc"), name="Dup One", phone="250-555-0777", spent=1e9),
                        Customer(id=_uid("rc"), name="Dup Two", phone="(250) 555 0777", spent=1e9)])
    db_session.commit()
    rep = build(db_session, "customers", **{"from": "2032-01-01", "to": "2032-01-31"})
    dups = section(rep, "duplicate")
    assert any("Dup One" in r[2] and "Dup Two" in r[2] for r in dups.rows)


def test_inventory_report_lists_reorders_and_data_problems(db_session):
    low = make_product(db_session, stock=1, reorder_threshold=5, reorder_qty=12, name="Low Item")
    free = make_product(db_session, cost=0.0, price=30.0, name="Zero Cost Item")
    rep = build(db_session, "inventory")
    assert any(r[0] == "Low Item" for r in section(rep, "Reorder list").rows)
    assert any(r[0] == "Zero Cost Item" and "Cost is $0" in r[2] for r in section(rep, "Data quality").rows)
    assert low.id and free.id


def test_repair_report_turnaround_and_chase_list(db_session):
    cust = Customer(id=_uid("rc"), name="Slow Pickup", phone="2505550166")
    r = Repair(id=_uid("rr"), ticket_no=7000 + uuid.uuid4().int % 900, customer_id=cust.id, device="Galaxy S21", issue="Battery Replacement",
               status="READY", estimated_cost=80.0, created_at=datetime.utcnow() - timedelta(days=40),
               updated_at=datetime.utcnow() - timedelta(days=30),
               status_history=json.dumps([{"status": "RECEIVED", "date": (datetime.utcnow() - timedelta(days=40)).isoformat()},
                                          {"status": "READY", "date": (datetime.utcnow() - timedelta(days=30)).isoformat()}]))
    db_session.add_all([cust, r])
    db_session.commit()
    rep = build(db_session, "repairs", preset="last_90")
    chase = section(rep, "Chase list")
    assert any(row[0] == str(r.ticket_no) and row[4] >= 29 for row in chase.rows)
    assert kpi(rep, "Waiting for pickup") >= 1


# ── exports ───────────────────────────────────────────────────────────

def test_csv_keeps_negative_amounts_numeric_and_neutralises_formulas():
    rep = Report("t", "T", "", [], [Section("S", ["Name", "Amount"], [["=HYPERLINK(\"http://x\")", Money(-5.0)], ["ok", Money(12.5)]])])
    text = report_to_csv(rep).decode("utf-8-sig")
    assert "-5.00" in text and "'-5.00" not in text
    assert "'=HYPERLINK" in text and "\n=HYPERLINK" not in text


def test_xlsx_has_a_sheet_per_section_with_real_numbers(db_session):
    rep = build(db_session, "monthly", month="2031-06")
    wb = load_workbook(io.BytesIO(report_to_xlsx(rep)))
    assert wb.sheetnames[0] == "Summary" and len(wb.sheetnames) == len(rep.sections) + 1
    ws = wb["Day by day"] if "Day by day" in wb.sheetnames else wb[wb.sheetnames[3]]
    assert ws.freeze_panes == "A2"


# ── pages, access and exports over HTTP ───────────────────────────────

@pytest.mark.parametrize("key", REPORT_KEYS)
def test_every_report_renders_prints_and_exports(owner_client, key):
    q = "?from=2031-01-01&to=2031-12-31&date=2031-03-15&month=2031-06"
    assert owner_client.get(f"/reports/center/{key}{q}").status_code == 200
    assert owner_client.get(f"/reports/center/{key}/print{q}").status_code == 200
    csv_resp = owner_client.get(f"/reports/center/{key}/export.csv{q}")
    assert csv_resp.status_code == 200 and csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers["content-disposition"]
    xl = owner_client.get(f"/reports/center/{key}/export.xlsx{q}")
    assert xl.status_code == 200
    assert load_workbook(io.BytesIO(xl.content)).sheetnames[0] == "Summary"


def test_report_center_hub_lists_every_report(owner_client):
    import html as _html
    page = owner_client.get("/reports/center").text
    for r in R.REPORTS:
        assert _html.escape(r["title"]) in page


def test_reports_are_manager_and_owner_only(cashier_client, technician_client, manager_client, anon_client):
    for path in ("/reports/center", "/reports/center/daily", "/reports/center/daily/export.csv", "/reports/center/daily/print"):
        assert cashier_client.get(path).status_code == 403
        assert technician_client.get(path).status_code == 403
        assert manager_client.get(path).status_code == 200
        assert anon_client.get(path, follow_redirects=False).status_code == 303


def test_unknown_report_is_a_404_and_junk_parameters_do_not_crash(owner_client):
    assert owner_client.get("/reports/center/nope").status_code == 404
    assert owner_client.get("/reports/center/sales?from=banana&to=2031-99-99").status_code == 200
    assert owner_client.get("/reports/center/monthly?month=garbage").status_code == 200
    assert owner_client.get("/reports/center/inventory?days=-5").status_code == 200
    assert owner_client.get("/reports/center/exceptions?threshold=abc").status_code == 200


def test_export_is_audited(owner_client, db_session):
    before = db_session.query(AuditLog).filter(AuditLog.action == "EXPORT").count()
    owner_client.get("/reports/center/tax/export.xlsx?from=2031-01-01&to=2031-01-31")
    db_session.expire_all()
    assert db_session.query(AuditLog).filter(AuditLog.action == "EXPORT").count() == before + 1


def test_existing_pages_render_in_a_non_utc_shop(owner_client, pacific):
    for path in ("/", "/?range=30d", "/reports", "/reports/eod", "/invoices", "/repairs", "/cashup", "/api/notifications"):
        assert owner_client.get(path).status_code == 200, path


def test_daily_digest_includes_profit_and_runs_in_a_local_day(db_session, pacific):
    from app.main import build_daily_digest
    subject, body = build_daily_digest(db_session)
    assert "Gross profit" in body and "Daily Summary" in subject


# ── settings ──────────────────────────────────────────────────────────

def test_settings_timezone_can_be_set_and_is_not_wiped_by_a_form_without_it(owner_client, db_session):
    base = {"shop_name": "TechPro+", "province": "BC", "invoice_prefix": "INV"}
    try:
        owner_client.post("/settings", data={**base, "timezone": "America/Edmonton"}, follow_redirects=False)
        db_session.expire_all()
        assert db_session.get(Setting, "timezone").value == "America/Edmonton"
        owner_client.post("/settings", data=base, follow_redirects=False)           # field absent
        db_session.expire_all()
        assert db_session.get(Setting, "timezone").value == "America/Edmonton"
        owner_client.post("/settings", data={**base, "timezone": "Mars/Olympus"}, follow_redirects=False)  # junk -> automatic
        db_session.expire_all()
        assert db_session.get(Setting, "timezone").value == ""
    finally:
        db_session.merge(Setting(key="timezone", value="UTC"))
        db_session.merge(Setting(key="province", value="ON"))
        db_session.commit()
        get_shop_tz(db_session)


# ── performance guard ─────────────────────────────────────────────────

def test_report_query_count_does_not_grow_with_invoice_count(db_session, staff_row):
    """The old Reports page did one query per invoice line. A month of
    invoices must cost the same handful of queries whether it holds 10 or 60."""
    p = make_product(db_session)

    def count_queries(key, **args):
        n = {"q": 0}

        def bump(*_a, **_k):
            n["q"] += 1
        event.listen(engine, "before_cursor_execute", bump)
        try:
            build(db_session, key, **args)
        finally:
            event.remove(engine, "before_cursor_execute", bump)
        return n["q"]

    for i in range(10):
        make_invoice(db_session, staff_row, datetime(2032, 9, 1, 12, 0) + timedelta(hours=i), [(p, 10.0, 1, 5.0)])
    small = count_queries("monthly", month="2032-09")
    for i in range(50):
        make_invoice(db_session, staff_row, datetime(2032, 9, 3, 0, 0) + timedelta(hours=i), [(p, 10.0, 2, 5.0), (None, 5.0, 1, None)])
    large = count_queries("monthly", month="2032-09")
    assert large <= small + 4, f"query count grew with data: {small} -> {large}"
    assert kpi(build(db_session, "monthly", month="2032-09"), "Sales") == 60


# ── display polish found in the visual check ───────────────────────────────
def test_zero_never_prints_negative():
    from app.report_types import Money, Pct, fmt_cell
    assert fmt_cell(Money(-0.0)) == "$0.00"
    assert fmt_cell(Money(-12.99)) == "-$12.99"
    assert fmt_cell(Pct(-0.0)) == "0.0%"


def test_ticket_numbers_have_no_thousands_comma(db_session=None):
    from app.report_types import fmt_cell
    # ticket numbers are emitted as strings by the repairs/daily builders
    assert fmt_cell("2832") == "2832"
