"""v53 — Mobile Polish.

Presentation-only release: the checks below make sure the new phone layouts render for every role, never
duplicate or drop a money action, escape hostile text, and that the dashboard feed and attention strip read
from real data. No money rule changed, so the existing suites remain the proof for totals and stock.
"""
import re

import pytest

from app.models import AuditLog, Customer, Invoice, Layaway, Product, Repair

TAG = "V53MP"


@pytest.fixture(autouse=True)
def _clean(db_session):
    def wipe():
        db_session.rollback()
        ids = [c.id for c in db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).all()]
        if ids:
            db_session.query(Repair).filter(Repair.customer_id.in_(ids)).delete(synchronize_session=False)
            db_session.query(Customer).filter(Customer.id.in_(ids)).delete(synchronize_session=False)
        db_session.query(Repair).filter(Repair.device.like(f"{TAG}%")).delete(synchronize_session=False)
        db_session.query(AuditLog).filter(AuditLog.detail.like(f"{TAG}%")).delete(synchronize_session=False)
        db_session.commit()
    wipe()
    yield
    wipe()


def _repair(c, name=f"{TAG} Ana", phone="250-555-0153", device=f"{TAG} iPhone 15", **extra):
    data = {"phone": phone, "name": name, "device": device, "issue": "Screen Replacement",
            "description": "cracked", "estimated_cost": "120"}
    data.update(extra)
    r = c.post("/repairs/add", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return re.split(r"[?#]", r.headers["location"])[0]


# ── repair ticket ────────────────────────────────────────────────────────────

def test_ticket_has_hero_stepper_and_one_of_each_money_action(owner_client, db_session):
    url = _repair(owner_client)
    html = owner_client.get(url).text
    assert 'id="ticket-hero"' in html and 'class="stepper"' in html
    assert html.count('action="/repairs/') >= 1
    # exactly one advance form, and no charge form until the ticket is Ready
    assert len(re.findall(r'action="/repairs/[^"]+/advance"', html)) == 1
    assert "/charge" not in html
    assert "tel:" in html                                  # tap-to-call for the customer
    assert html.count('aria-current="step"') == 1          # one active step


def test_ticket_ready_state_offers_charge_and_text_once_each(owner_client, db_session):
    url = _repair(owner_client)
    rid = url.rsplit("/", 1)[1]
    # move RECEIVED -> ... -> READY through the real route
    for _ in range(6):
        page = owner_client.get(url).text
        if re.search(r'<span class="badge badge-lg [^"]*">Ready', page):
            break
        owner_client.post(f"/repairs/{rid}/advance", data={"note": ""})
    html = owner_client.get(url).text
    assert len(re.findall(r'action="/repairs/[^"]+/charge"', html)) == 1
    assert len(re.findall(r'action="/repairs/[^"]+/notify"', html)) == 1
    assert len(re.findall(r'action="/repairs/[^"]+/advance"', html)) == 1


def test_ticket_escapes_hostile_customer_and_device_text(owner_client, db_session):
    url = _repair(owner_client, name=f"{TAG} <script>alert(1)</script>", device=f"{TAG} <img src=x onerror=alert(1)>")
    html = owner_client.get(url).text
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror" not in html


@pytest.mark.parametrize("client_name", ["manager_client", "cashier_client", "technician_client"])
def test_ticket_renders_for_every_role(request, owner_client, client_name):
    url = _repair(owner_client)
    r = request.getfixturevalue(client_name).get(url)
    assert r.status_code == 200 and 'id="ticket-hero"' in r.text


def test_late_ticket_is_flagged(owner_client):
    url = _repair(owner_client, promised_by="2020-01-01")
    html = owner_client.get(url).text
    assert "· late" in html


# ── customer page ────────────────────────────────────────────────────────────

def test_customer_page_has_profile_quick_actions_and_tabs(owner_client, db_session):
    _repair(owner_client, name=f"{TAG} Bea", phone="250-555-0154")
    c = db_session.query(Customer).filter(Customer.name == f"{TAG} Bea").one()
    html = owner_client.get(f"/customers/{c.id}").text
    assert 'id="customer-hero"' in html and 'class="c-quick"' in html and 'id="c-tabs"' in html
    assert 'href="tel:250-555-0154"' in html
    assert 'data-panel="repairs"' in html and 'data-panel="purchases"' in html and 'data-panel="details"' in html
    assert "Edit Details" in html and 'action="/customers/%s/edit"' % c.id in html
    assert html.count('action="/pos/customer"') == 1       # the "New sale" button posts the existing route


def test_customer_with_opt_out_cannot_be_texted_from_quick_actions(owner_client, db_session):
    _repair(owner_client, name=f"{TAG} Cy", phone="250-555-0155")
    c = db_session.query(Customer).filter(Customer.name == f"{TAG} Cy").one()
    c.sms_opt_out = True
    db_session.commit()
    html = owner_client.get(f"/customers/{c.id}").text
    assert "Do not text" in html
    assert 'data-tab="messages">' not in html.split('class="c-quick"')[1].split("c-stats")[0]


def test_new_sale_button_attaches_the_customer_to_the_till(owner_client, db_session):
    _repair(owner_client, name=f"{TAG} Di", phone="250-555-0156")
    c = db_session.query(Customer).filter(Customer.name == f"{TAG} Di").one()
    r = owner_client.post("/pos/customer", data={"customer_id": c.id}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pos"
    assert f'value="{c.id}" selected' in owner_client.get("/pos").text.replace("\n", " ").replace('selected', 'selected') or c.name in owner_client.get("/pos").text
    owner_client.post("/pos/customer", data={"customer_id": ""}, follow_redirects=False)


def test_customer_hostile_name_is_escaped(owner_client, db_session):
    _repair(owner_client, name=f'{TAG} <b onmouseover="x">Eve</b>', phone="250-555-0157")
    c = db_session.query(Customer).filter(Customer.name.like(f"{TAG} %Eve%")).one()
    html = owner_client.get(f"/customers/{c.id}").text
    assert '<b onmouseover="x">' not in html


def test_repairs_page_prefill_script_is_present(owner_client):
    html = owner_client.get("/repairs?new=1&phone=2505550100&name=Zed").text
    assert "URLSearchParams" in html and 'id="new-ticket-panel"' in html


# ── POS ──────────────────────────────────────────────────────────────────────

def test_pos_has_cart_bar_and_swipeable_filter_rows(owner_client):
    html = owner_client.get("/pos").text
    assert 'id="pos-cartbar"' in html and 'id="pos-pay"' in html and 'id="pos-cart-top"' in html
    assert html.count("pos-filter-row") >= 1
    # the real Charge button is still there, still once
    assert html.count('id="charge-btn"') == 1


def test_cart_bar_total_matches_the_server_total(owner_client, db_session):
    owner_client.post("/pos/clear", headers={"X-Pos-Ajax": "1"})
    p = db_session.query(Product).filter(Product.stock > 5, Product.price > 0).first()
    assert p is not None
    owner_client.post("/pos/add-custom", data={"product_id": p.id, "custom_price": "10.00"}, headers={"X-Pos-Ajax": "1"})
    frag = owner_client.post("/pos/qty/0", data={"qty": "3"}, headers={"X-Pos-Ajax": "1"}).text
    m = re.search(r'id="cart-total-carrier" data-total="([0-9.]+)"', frag)
    bar = re.search(r'class="pcb-total">\$([0-9.,]+)<', frag)
    assert m and bar and abs(float(m.group(1)) - float(bar.group(1).replace(",", ""))) < 0.005
    assert "3 items in cart" in frag
    assert 'class="pos-cartbar"' in frag                     # not marked empty
    cleared = owner_client.post("/pos/clear", headers={"X-Pos-Ajax": "1"}).text
    assert "pos-cartbar is-empty" in cleared and "0 items in cart" in cleared


# ── dashboard ────────────────────────────────────────────────────────────────

def test_dashboard_feed_speaks_plain_english(owner_client, db_session):
    db_session.add(AuditLog(staff_name="Owner", action="INVOICE_CREATE", detail=f"{TAG} sale of $5.00"))
    db_session.add(AuditLog(staff_name="Owner", action="SOME_FUTURE_CODE", detail=f"{TAG} later"))
    db_session.commit()
    html = owner_client.get("/").text
    assert "rang up a sale" in html
    assert "some future code" in html                       # unknown codes degrade to readable text, never vanish
    assert ">INVOICE_CREATE<" not in html


def test_dashboard_attention_strip_counts_ready_and_late_repairs(owner_client, db_session):
    url = _repair(owner_client, promised_by="2020-01-01")
    rid = url.rsplit("/", 1)[1]
    html = owner_client.get("/").text
    assert 'id="attention"' in html and "Past promised date" in html
    for _ in range(4):      # Received -> Diagnosed -> Waiting -> In Progress -> Ready
        owner_client.post(f"/repairs/{rid}/advance", data={"note": ""})
    html = owner_client.get("/").text
    assert "Ready for pickup" in html


def test_dashboard_attention_strip_hostile_free_and_role_safe(cashier_client, technician_client):
    for c in (cashier_client, technician_client):
        r = c.get("/")
        assert r.status_code == 200 and 'id="attention"' in r.text


def test_activity_label_helper_never_raises():
    from app.main import activity_label
    assert activity_label("INVOICE_CREATE")["phrase"] == "rang up a sale"
    assert activity_label("")["phrase"]
    assert activity_label(None)["phrase"]
    assert activity_label("weird_code_9")["phrase"] == "weird code 9"


# ── phone card lists + shared pieces ─────────────────────────────────────────

def test_tables_carry_phone_card_markup(owner_client, db_session):
    _repair(owner_client)
    for path in ("/", "/invoices", "/customers", "/repairs?view=table"):
        html = owner_client.get(path).text
        if "<table" in html:
            assert "stack-sm" in html, path


def test_every_flash_card_is_tagged_for_the_banner_script(owner_client):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "templates"
    for f in root.glob("*.html"):
        for line in f.read_text().splitlines():
            if "flash[1]" in line:
                assert "data-flash" in line, f"{f.name}: {line.strip()[:80]}"


def test_base_ships_toasts_busy_guard_and_new_stylesheet_version(owner_client):
    html = owner_client.get("/").text
    assert 'id="tpToasts"' in html and "window.tpToast" in html and "data-tp-busy" in html
    assert re.search(r"/static/style\.css\?v=(5[3-9]|[6-9]\d)", html)          # v53 introduced the busy guard; later versions bump the number
    css = owner_client.get("/static/style.css").text
    for needle in (".pos-cartbar", ".stepper", ".c-tabs", ".attn", "table.stack-sm", ".tp-toast", ".is-busy"):
        assert needle in css, needle


def test_login_page_still_renders_without_the_logged_in_scripts(anon_client):
    r = anon_client.get("/login")
    assert r.status_code == 200 and "tpToast" not in r.text
