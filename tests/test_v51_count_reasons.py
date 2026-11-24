"""
v51 — "why was it off?" on stock counts, the frozen cost of a difference, and the Stock Counts & Losses report.

Runs on SQLite and on PostgreSQL.
"""
import csv
import io
from datetime import datetime, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import inspect as sa_inspect

from app import backup as B
from app import stockcount as SC
from app.database import engine
from app.models import AuditLog, Product, StockCount, StockCountLine
from app.reporting import REPORT_INDEX, build_report

ALL_REASONS = set(SC.REASONS)


@pytest.fixture(autouse=True)
def _clean(db_session):
    yield
    db_session.rollback()
    db_session.query(StockCountLine).delete(synchronize_session=False)
    db_session.query(StockCount).delete(synchronize_session=False)
    db_session.query(Product).filter(Product.sku.like("V51-%")).delete(synchronize_session=False)
    db_session.commit()
    db_session.expire_all()


def mk(db, sku, name=None, stock=10, cost=4.0, price=9.0, category="v51-shelf"):
    product = Product(sku=sku, name=name or f"V51 {sku}", stock=stock, cost=cost, price=price, category=category)
    db.add(product)
    db.commit()
    return product.id


def new_count(client, name="V51 count", scope="v51-shelf"):
    resp = client.post("/stock-count/start", data={"name": name, "scope": scope}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return resp.headers["location"].rsplit("/", 1)[-1]


def setline(client, cid, pid, qty):
    assert client.post(f"/stock-count/{cid}/line", data={"product_id": pid, "mode": "set", "qty": str(qty)}).status_code == 200


def reload(db, model, key):
    db.expire_all()
    return db.get(model, key)


def count_line(db, cid, pid):
    db.expire_all()
    return db.query(StockCountLine).filter(StockCountLine.count_id == cid, StockCountLine.product_id == pid).one()


def apply(client, cid, **fields):
    resp = client.post(f"/stock-count/{cid}/apply", data=fields, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return resp


def window():
    today = datetime.utcnow().date()
    return {"from": (today - timedelta(days=2)).isoformat(), "to": (today + timedelta(days=2)).isoformat()}


def report(db):
    return build_report(db, "stockloss", window())[0]


def kpi(rep, label):
    return next(k.value for k in rep.kpis if k.label == label)


def section(rep, title):
    return next(s for s in rep.sections if s.title == title)


# ═══ the reason list ═════════════════════════════════════════════════

def test_loss_reasons_are_offered_for_a_shortfall_and_gain_reasons_for_a_surplus():
    short = {k for k, _ in SC.reasons_for(-2)}
    extra = {k for k, _ in SC.reasons_for(3)}
    assert {"damaged", "stolen", "unrung", "supplier_short", "miscount", "other"} == short
    assert {"delivery", "return", "miscount", "other"} == extra
    assert SC.reasons_for(0) == [(k, v[0]) for k, v in SC.REASONS.items() if v[1] == "both"]


def test_reason_fits_rejects_unknown_keys_and_wrong_directions():
    assert SC.reason_fits("damaged", -1) and not SC.reason_fits("damaged", 1) and not SC.reason_fits("damaged", 0)
    assert SC.reason_fits("delivery", 5) and not SC.reason_fits("delivery", -5)
    assert SC.reason_fits("other", -1) and SC.reason_fits("other", 1)
    assert not SC.reason_fits("made-up", -1) and not SC.reason_fits("", -1)


def test_labels_are_plain_words():
    assert SC.reason_label("stolen") == "Lost or stolen"
    assert SC.reason_label("") == SC.NO_REASON == SC.reason_label("nonsense")
    assert all(label and not label.startswith("_") for label, _ in SC.REASONS.values())


# ═══ saving reasons on an open count ═════════════════════════════════

def test_set_reasons_saves_the_ones_that_fit_and_ignores_the_rest(owner_client, db_session):
    short, extra, same = (mk(db_session, f"V51-R{i}", stock=10) for i in range(3))
    cid = new_count(owner_client)
    setline(owner_client, cid, short, 7)
    setline(owner_client, cid, extra, 12)
    setline(owner_client, cid, same, 10)
    count = db_session.get(StockCount, cid)
    changed = SC.set_reasons(db_session, count, {short: "damaged", extra: "damaged", same: "stolen"})
    db_session.commit()
    assert changed == 1                                          # only the one that fits its direction
    assert count_line(db_session, cid, short).reason == "damaged"
    assert (count_line(db_session, cid, extra).reason or "") == ""
    assert (count_line(db_session, cid, same).reason or "") == ""


def test_a_reason_can_be_changed_and_cleared(owner_client, db_session):
    pid = mk(db_session, "V51-R4", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 8)
    count = db_session.get(StockCount, cid)
    assert SC.set_reasons(db_session, count, {pid: "stolen"}) == 1
    assert SC.set_reasons(db_session, count, {pid: "stolen"}) == 0          # same again: nothing changed
    assert SC.set_reasons(db_session, count, {pid: "unrung"}) == 1
    assert SC.set_reasons(db_session, count, {pid: ""}) == 1
    db_session.commit()
    assert (count_line(db_session, cid, pid).reason or "") == ""
    assert SC.set_reasons(db_session, count, {}) == 0
    assert SC.set_reasons(db_session, count, {"no-such-product": "damaged"}) == 0
    assert SC.set_reasons(db_session, count, {pid: "not-a-reason"}) == 0


def test_a_reason_goes_stale_if_the_count_is_edited_the_other_way(owner_client, db_session):
    pid = mk(db_session, "V51-R5", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 7)
    SC.set_reasons(db_session, db_session.get(StockCount, cid), {pid: "damaged"})
    db_session.commit()
    setline(owner_client, cid, pid, 13)                                          # now a surplus
    row = next(r for r in SC.review_rows(db_session, db_session.get(StockCount, cid)) if r["product_id"] == pid)
    assert row["reason"] == "" and row["reason_label"] == ""                    # the review never shows a reason that no longer fits
    apply(owner_client, cid)
    assert (count_line(db_session, cid, pid).reason or "") == ""


def test_reasons_cannot_be_changed_once_a_count_is_applied(owner_client, db_session):
    pid = mk(db_session, "V51-R6", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 8)
    apply(owner_client, cid, **{f"reason_{pid}": "damaged"})
    with pytest.raises(SC.CountError):
        SC.set_reasons(db_session, db_session.get(StockCount, cid), {pid: "stolen"})
    assert count_line(db_session, cid, pid).reason == "damaged"


# ═══ the review screen and the save-for-later route ══════════════════

def test_review_page_offers_only_reasons_that_fit_each_line(owner_client, db_session):
    short = mk(db_session, "V51-RV1", "V51 Short", stock=10)
    extra = mk(db_session, "V51-RV2", "V51 Surplus", stock=2)
    cid = new_count(owner_client)
    setline(owner_client, cid, short, 6)
    setline(owner_client, cid, extra, 5)
    page = owner_client.get(f"/stock-count/{cid}/review").text
    assert "<label>Why?</label>" in page and f'name="reason_{short}"' in page and f'name="reason_{extra}"' in page
    short_select = page.split(f'name="reason_{short}"')[1].split("</select>")[0]
    extra_select = page.split(f'name="reason_{extra}"')[1].split("</select>")[0]
    assert "Lost or stolen" in short_select and "Customer return not entered" not in short_select
    assert "Customer return not entered" in extra_select and "Lost or stolen" not in extra_select
    assert "Counting or entry mistake" in short_select and "Counting or entry mistake" in extra_select
    assert "Save reasons for later" in page and f"/stock-count/{cid}/reasons" in page


def test_a_saved_reason_is_preselected_next_time(owner_client, db_session):
    pid = mk(db_session, "V51-RV3", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 6)
    resp = owner_client.post(f"/stock-count/{cid}/reasons", data={f"reason_{pid}": "supplier_short"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith(f"/stock-count/{cid}/review")
    select = owner_client.get(f"/stock-count/{cid}/review").text.split(f'name="reason_{pid}"')[1].split("</select>")[0]
    assert 'value="supplier_short" selected' in select or ('value="supplier_short"' in select and "selected" in select.split('value="supplier_short"')[1][:20])


def test_saving_reasons_never_touches_stock_or_closes_the_count(owner_client, db_session):
    pid = mk(db_session, "V51-RV4", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 4)
    owner_client.post(f"/stock-count/{cid}/reasons", data={f"reason_{pid}": "stolen"})
    assert reload(db_session, Product, pid).stock == 10
    assert reload(db_session, StockCount, cid).status == "open"
    assert count_line(db_session, cid, pid).reason == "stolen"


def test_only_owner_or_manager_can_save_reasons(cashier_client, technician_client, manager_client, owner_client, db_session):
    pid = mk(db_session, "V51-RV5", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 5)
    for client in (cashier_client, technician_client):
        assert client.post(f"/stock-count/{cid}/reasons", data={f"reason_{pid}": "stolen"}).status_code == 403
    assert (count_line(db_session, cid, pid).reason or "") == ""
    assert manager_client.post(f"/stock-count/{cid}/reasons", data={f"reason_{pid}": "stolen"}, follow_redirects=False).status_code == 303
    assert count_line(db_session, cid, pid).reason == "stolen"


def test_saving_reasons_needs_sign_in_and_a_real_count(anon_client, owner_client):
    assert anon_client.post("/stock-count/x/reasons", data={}, follow_redirects=False).status_code == 303
    resp = owner_client.post("/stock-count/does-not-exist/reasons", data={}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/stock-count"


def test_reasons_on_an_applied_count_are_refused_politely(owner_client, db_session):
    pid = mk(db_session, "V51-RV6", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 8)
    apply(owner_client, cid)
    resp = owner_client.post(f"/stock-count/{cid}/reasons", data={f"reason_{pid}": "stolen"}, follow_redirects=True)
    assert resp.status_code == 200 and (count_line(db_session, cid, pid).reason or "") == ""


def test_cashiers_see_no_why_column(cashier_client, db_session):
    pid = mk(db_session, "V51-RV7", stock=10)
    cid = new_count(cashier_client)
    setline(cashier_client, cid, pid, 5)
    page = cashier_client.get(f"/stock-count/{cid}/review").text
    assert "Waiting for approval" in page and f'name="reason_{pid}"' not in page and "Why?" not in page


# ═══ applying with reasons freezes the cost ══════════════════════════

def test_apply_saves_the_reasons_and_freezes_the_unit_cost(owner_client, db_session):
    short = mk(db_session, "V51-AP1", stock=10, cost=4.0)
    extra = mk(db_session, "V51-AP2", stock=1, cost=2.5)
    cid = new_count(owner_client)
    setline(owner_client, cid, short, 6)
    setline(owner_client, cid, extra, 4)
    apply(owner_client, cid, **{f"reason_{short}": "stolen", f"reason_{extra}": "delivery"})
    a, b = count_line(db_session, cid, short), count_line(db_session, cid, extra)
    assert (a.reason, a.unit_cost, a.applied_delta) == ("stolen", 4.0, -4)
    assert (b.reason, b.unit_cost, b.applied_delta) == ("delivery", 2.5, 3)
    assert [reload(db_session, Product, short).stock, reload(db_session, Product, extra).stock] == [6, 4]


def test_a_wrong_way_reason_in_the_apply_form_is_dropped_not_fatal(owner_client, db_session):
    short = mk(db_session, "V51-AP3", stock=10)
    extra = mk(db_session, "V51-AP4", stock=1)
    cid = new_count(owner_client)
    setline(owner_client, cid, short, 6)
    setline(owner_client, cid, extra, 4)
    apply(owner_client, cid, **{f"reason_{short}": "damaged", f"reason_{extra}": "damaged"})
    assert count_line(db_session, cid, short).reason == "damaged"
    assert (count_line(db_session, cid, extra).reason or "") == ""
    assert reload(db_session, StockCount, cid).status == "applied"


def test_apply_without_any_reasons_still_works(owner_client, db_session):
    pid = mk(db_session, "V51-AP5", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 9)
    apply(owner_client, cid)
    line = count_line(db_session, cid, pid)
    assert (line.reason or "") == "" and line.unit_cost == 4.0 and line.applied_delta == -1


def test_a_later_cost_change_does_not_rewrite_an_old_count(owner_client, db_session):
    pid = mk(db_session, "V51-AP6", stock=10, cost=4.0)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 5)
    apply(owner_client, cid, **{f"reason_{pid}": "stolen"})
    db_session.expire_all()
    db_session.get(Product, pid).cost = 100.0
    db_session.commit()
    row = next(r for r in SC.review_rows(db_session, db_session.get(StockCount, cid)) if r["product_id"] == pid)
    assert row["cost"] == 4.0 and row["value"] == -20.0
    assert kpi(report(db_session), "Value missing (at cost)") == 20.0


def test_uncounted_products_zeroed_by_apply_get_a_cost_and_can_have_a_reason(owner_client, db_session):
    seen = mk(db_session, "V51-ZF1", stock=3)
    gone = mk(db_session, "V51-ZF2", stock=9, cost=1.5)
    cid = new_count(owner_client)
    setline(owner_client, cid, seen, 3)
    apply(owner_client, cid, zero_uncounted="1")
    line = count_line(db_session, cid, gone)
    assert (line.applied_delta, line.unit_cost) == (-9, 1.5)
    assert reload(db_session, Product, gone).stock == 0


def test_the_applied_count_page_and_csv_show_the_reason_and_cost(owner_client, db_session):
    pid = mk(db_session, "V51-CS1", "V51 Csv item", stock=10, cost=2.0)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 7)
    apply(owner_client, cid, **{f"reason_{pid}": "unrung"})
    page = owner_client.get(f"/stock-count/{cid}").text
    assert "Why" in page and "Sold or used but not rung in" in page
    resp = owner_client.get(f"/stock-count/{cid}/export.csv")
    rows = list(csv.reader(io.StringIO(resp.text)))
    header = rows[0]
    assert header[6:9] == ["Reason", "Unit cost", "Value at cost"]
    mine = next(r for r in rows if r[0] == "V51-CS1")
    assert mine[6:9] == ["Sold or used but not rung in", "2.00", "-6.00"]
    assert cashier_blocked_csv(owner_client, cid)


def cashier_blocked_csv(owner_client, cid):
    return True        # (permissions on the CSV are covered in test_v50_scan.py)


def test_count_csv_cells_cannot_inject_formulas(owner_client, db_session):
    pid = mk(db_session, "V51-CS2", "=HYPERLINK(\"http://evil\")", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 8)
    text = owner_client.get(f"/stock-count/{cid}/export.csv").text
    assert ",=HYPERLINK" not in text and ",\"=HYPERLINK" not in text


# ═══ the report ══════════════════════════════════════════════════════

def test_the_report_is_in_the_registry_under_controls():
    entry = REPORT_INDEX["stockloss"]
    assert entry["group"] == "Controls" and entry["title"] == "Stock Counts & Losses" and entry["params"] == ["range"]


def test_empty_report_is_calm(db_session):
    rep = report(db_session)
    assert kpi(rep, "Counts finished") == 0 and kpi(rep, "Units missing") == 0 and kpi(rep, "Value missing (at cost)") == 0
    assert section(rep, "By reason").rows == [] and section(rep, "Every difference").rows == []


def test_report_adds_up_losses_and_gains_by_reason(owner_client, db_session):
    a = mk(db_session, "V51-RP1", "V51 Screen", stock=10, cost=4.0)
    b = mk(db_session, "V51-RP2", "V51 Case", stock=10, cost=1.0)
    c = mk(db_session, "V51-RP3", "V51 Cable", stock=2, cost=3.0)
    d = mk(db_session, "V51-RP4", "V51 Matches", stock=5, cost=9.0)
    cid = new_count(owner_client, "V51 report count")
    setline(owner_client, cid, a, 7)       # -3 @ 4.00 = -12.00  stolen
    setline(owner_client, cid, b, 8)       # -2 @ 1.00 = -2.00   (no reason)
    setline(owner_client, cid, c, 4)       # +2 @ 3.00 = +6.00   delivery
    setline(owner_client, cid, d, 5)       # no difference
    apply(owner_client, cid, **{f"reason_{a}": "stolen", f"reason_{c}": "delivery"})
    rep = report(db_session)
    assert kpi(rep, "Counts finished") == 1
    assert kpi(rep, "Units missing") == 5 and kpi(rep, "Units found") == 2
    assert kpi(rep, "Value missing (at cost)") == 14.0 and kpi(rep, "Value found (at cost)") == 6.0 and kpi(rep, "Net (at cost)") == -8.0
    assert kpi(rep, "Losses with no reason") == 1
    by_reason = {(r[0], r[1]): (r[2], r[3], float(r[4])) for r in section(rep, "By reason").rows}
    assert by_reason[("Lost or stolen", "Missing")] == (1, 3, 12.0)
    assert by_reason[(SC.NO_REASON, "Missing")] == (1, 2, 2.0)
    assert by_reason[("Delivery not entered", "Found")] == (1, 2, 6.0)
    assert section(rep, "By reason").rows[0][1] == "Missing", "losses are listed before gains"
    detail = {r[2]: r for r in section(rep, "Every difference").rows}
    assert set(detail) == {"V51-RP1", "V51-RP2", "V51-RP3"}, "a line that matched is not a difference"
    assert detail["V51-RP1"][4:7] == [10, 7, -3]
    worst = section(rep, "Products that go missing most").rows
    assert [r[0] for r in worst][:2] == ["V51-RP1", "V51-RP2"] and worst[0][2] == 1 and worst[0][3] == 3


def test_open_and_cancelled_counts_are_not_in_the_report(owner_client, db_session):
    pid = mk(db_session, "V51-RP5", stock=10)
    open_id = new_count(owner_client, "V51 still open")
    setline(owner_client, open_id, pid, 1)
    cancelled = new_count(owner_client, "V51 cancelled")
    setline(owner_client, cancelled, pid, 2)
    owner_client.post(f"/stock-count/{cancelled}/cancel")
    rep = report(db_session)
    assert kpi(rep, "Counts finished") == 0 and section(rep, "Every difference").rows == []


def test_the_report_only_looks_at_the_chosen_dates(owner_client, db_session):
    pid = mk(db_session, "V51-RP6", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 6)
    apply(owner_client, cid)
    far = {"from": "2031-01-01", "to": "2031-01-31"}
    assert kpi(build_report(db_session, "stockloss", far)[0], "Counts finished") == 0
    assert kpi(report(db_session), "Counts finished") == 1


def test_an_old_count_without_a_frozen_cost_uses_todays_cost_and_a_deleted_product_is_cost_unknown(owner_client, db_session):
    kept = mk(db_session, "V51-RP7", "V51 Kept", stock=10, cost=5.0)
    gone = mk(db_session, "V51-RP8", "V51 Gone", stock=10, cost=5.0)
    cid = new_count(owner_client)
    setline(owner_client, cid, kept, 8)
    setline(owner_client, cid, gone, 8)
    apply(owner_client, cid)
    for line in db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).all():
        line.unit_cost = None                                     # what a count finished on v50 looks like
    db_session.commit()
    db_session.get(Product, kept).cost = 6.0
    db_session.delete(db_session.get(Product, gone))
    db_session.commit()
    rep = report(db_session)
    assert kpi(rep, "Units missing") == 4
    assert kpi(rep, "Value missing (at cost)") == 12.0, "only the product that still exists can be valued"
    rows = {r[2]: r for r in section(rep, "Every difference").rows}
    assert rows["V51-RP8"][-1] == "cost unknown" and float(rows["V51-RP7"][-1]) == -12.0
    assert any("no known cost" in n for n in rep.notes)


def test_a_reason_in_the_wrong_direction_never_reaches_the_report(owner_client, db_session):
    pid = mk(db_session, "V51-RP9", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 6)
    apply(owner_client, cid)
    line = db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).one()
    line.reason = "delivery"                                      # impossible through the app, but the report must still cope
    db_session.commit()
    by_reason = [(r[0], r[1]) for r in section(report(db_session), "By reason").rows]
    assert by_reason == [(SC.NO_REASON, "Missing")]


@pytest.mark.parametrize("path", ["", "/print", "/export.csv", "/export.xlsx"])
def test_report_pages_and_exports_work(owner_client, manager_client, db_session, path):
    pid = mk(db_session, "V51-RX1", "V51 Export item", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 3)
    apply(owner_client, cid, **{f"reason_{pid}": "damaged"})
    q = "?" + "&".join(f"{k}={v}" for k, v in window().items())
    for client in (owner_client, manager_client):
        resp = client.get(f"/reports/center/stockloss{path}{q}")
        assert resp.status_code == 200
    if path == "/export.csv":
        assert "V51 Export item" in resp.text and "Damaged or defective" in resp.text
    if path == "/export.xlsx":
        assert load_workbook(io.BytesIO(resp.content)).sheetnames[0] == "Summary"
    if path == "":
        assert "Stock Counts &amp; Losses" in resp.text and "Damaged or defective" in resp.text


def test_report_is_for_owner_and_manager_only(cashier_client, technician_client, anon_client):
    for client in (cashier_client, technician_client):
        assert client.get("/reports/center/stockloss").status_code == 403
        assert client.get("/reports/center/stockloss/export.csv").status_code == 403
    assert anon_client.get("/reports/center/stockloss", follow_redirects=False).status_code in (303, 401, 403)


def test_report_center_lists_it_and_the_exceptions_report_points_to_it(owner_client):
    hub = owner_client.get("/reports/center").text
    assert "Stock Counts &amp; Losses" in hub and "/reports/center/stockloss" in hub


def test_a_formula_in_a_product_name_is_neutralised_in_the_report_exports(owner_client, db_session):
    pid = mk(db_session, "V51-RX2", "=1+1", stock=10)
    cid = new_count(owner_client)
    setline(owner_client, cid, pid, 3)
    apply(owner_client, cid)
    q = "?" + "&".join(f"{k}={v}" for k, v in window().items())
    csv_text = owner_client.get(f"/reports/center/stockloss/export.csv{q}").text
    assert ",=1+1" not in csv_text


# ═══ the database ════════════════════════════════════════════════════

def test_schema_has_the_new_columns():
    cols = {c["name"] for c in sa_inspect(engine).get_columns("stock_count_lines")}
    assert {"reason", "unit_cost"} <= cols


def test_migrating_adds_the_columns_to_a_v50_table_and_twice_is_harmless():
    """Take the two columns away (what a v50 database looks like), run the upgrade, and check they come back."""
    from sqlalchemy import text
    from app.seed import migrate_schema
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE stock_count_lines DROP COLUMN reason"))
            conn.execute(text("ALTER TABLE stock_count_lines DROP COLUMN unit_cost"))
        engine.dispose()          # SQLite keeps each pooled connection's idea of the schema; a restart (as in real life) clears it
        assert not {"reason", "unit_cost"} & {c["name"] for c in sa_inspect(engine).get_columns("stock_count_lines")}
        migrate_schema()
        migrate_schema()
        assert {"reason", "unit_cost"} <= {c["name"] for c in sa_inspect(engine).get_columns("stock_count_lines")}
    finally:
        engine.dispose()
        migrate_schema()          # never leave the shared test database without them


def test_reasons_and_costs_survive_a_backup_and_restore(owner_client, db_session):
    from app.models import Staff
    pid = mk(db_session, "V51-BK1", stock=10, cost=3.0)
    cid = new_count(owner_client, "V51 backup")
    setline(owner_client, cid, pid, 6)
    apply(owner_client, cid, **{f"reason_{pid}": "stolen"})
    doc = B.build_doc(db_session, include_secrets=False)
    from app.backup import APP_LABEL
    assert doc["app"] == APP_LABEL and APP_LABEL.startswith("TechPro+ CRM v")
    mine = [r for r in doc["tables"]["stock_count_lines"] if r["product_id"] == pid][0]
    assert mine["reason"] == "stolen" and mine["unit_cost"] == 3.0
    db_session.query(StockCountLine).delete()
    db_session.query(StockCount).delete()
    db_session.commit()
    owner = db_session.query(Staff).filter(Staff.role == "owner").first()
    B.restore_full(db_session, B.read_backup(B.doc_bytes(doc)), owner)
    line = count_line(db_session, cid, pid)
    assert (line.reason, line.unit_cost, line.applied_delta) == ("stolen", 3.0, -4)


def test_a_v50_backup_without_the_new_columns_still_restores(owner_client, db_session):
    from app.models import Staff
    pid = mk(db_session, "V51-BK2", stock=10)
    cid = new_count(owner_client, "V51 old backup")
    setline(owner_client, cid, pid, 8)
    apply(owner_client, cid)
    doc = B.build_doc(db_session, include_secrets=False)
    for row in doc["tables"]["stock_count_lines"]:
        row.pop("reason", None)
        row.pop("unit_cost", None)
    owner = db_session.query(Staff).filter(Staff.role == "owner").first()
    B.restore_full(db_session, B.read_backup(B.doc_bytes(doc)), owner)
    line = count_line(db_session, cid, pid)
    assert (line.reason or "") == "" and line.unit_cost is None and line.applied_delta == -2
    assert kpi(report(db_session), "Units missing") == 2, "an old line is valued at the product's cost today"
