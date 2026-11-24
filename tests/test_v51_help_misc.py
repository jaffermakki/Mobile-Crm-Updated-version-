"""
v51 — the staff Help page (what it says must match the real screens), and barcodes in the product list.
"""
import re
from pathlib import Path

import pytest

from app.models import Product

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"


def text_of(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


# ═══ the Help page ═══════════════════════════════════════════════════

@pytest.mark.parametrize("client_fixture", ["owner_client", "manager_client", "cashier_client", "technician_client"])
def test_everyone_signed_in_can_open_help(request, client_fixture):
    resp = request.getfixturevalue(client_fixture).get("/help")
    assert resp.status_code == 200
    for heading in ("Selling at the counter", "Taking in a repair", "Using the phone camera to scan", "Counting stock", "End of the day", "Staying safe"):
        assert heading in resp.text, heading


def test_the_receiving_section_is_for_owner_and_manager_only(owner_client, manager_client, cashier_client, technician_client):
    for client in (owner_client, manager_client):
        page = client.get("/help").text
        assert "Receiving a purchase order" in page and 'href="#po"' in page
    for client in (cashier_client, technician_client):
        page = client.get("/help").text
        assert "Receiving a purchase order" not in page and 'href="#po"' not in page
        assert "tell the owner or a manager" in page, "staff who cannot approve a count are told what to do"
        assert "Stock Counts &amp; Losses" not in page


def test_help_needs_sign_in(anon_client):
    resp = anon_client.get("/help", follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (303, "/login")


def test_the_welcome_banner_only_shows_after_setup(owner_client):
    assert "is set up" not in owner_client.get("/help").text
    assert "is set up" in owner_client.get("/help?welcome=1").text
    assert "is set up" not in owner_client.get("/help?welcome=yes").text


def test_the_sidebar_links_to_help_on_every_page(owner_client, cashier_client):
    for client in (owner_client, cashier_client):
        for url in ("/", "/pos", "/repairs", "/products"):
            assert 'href="/help"' in client.get(url).text, url


def test_every_link_in_help_goes_somewhere(owner_client):
    page = owner_client.get("/help").text
    links = set(re.findall(r'href="(/[^"#]*)"', page))
    assert {"/pos", "/repairs", "/trade-ins", "/stock-count", "/purchase-orders", "/cashup", "/reconcile", "/security", "/settings"} <= links
    for link in sorted(links):
        if link in ("/logout", "/static/style.css"):
            continue
        assert owner_client.get(link, follow_redirects=False).status_code in (200, 303), link
    for anchor in re.findall(r'href="#([a-z]+)"', page):
        assert f'id="{anchor}"' in page, anchor


def test_help_has_no_leftover_template_syntax(owner_client):
    page = owner_client.get("/help").text
    assert "{{" not in page and "{%" not in page and "None" not in page.replace("None of", "")


# What the guide tells people to tap must exist on the real screens. If a button is ever renamed, this fails and the
# guide gets fixed in the same change.
GUIDE_LABELS = [
    ("po_detail.html", "Scan items as they arrive"),
    ("po_detail.html", "Back to the full order"),
    ("po_detail.html", "Confirm Receipt — Add to Stock"),
    ("repair_detail.html", "Record check-in"),
    ("repair_detail.html", "Print check-in sheet"),
    ("repair_detail.html", "Photos"),
    ("stock_count_list.html", "Start counting"),
    ("stock_count.html", "Scan with camera"),
    ("stock_count.html", "Review &amp; finish"),
    ("stock_count_review.html", "Save reasons for later"),
    ("stock_count_review.html", "Why?"),
    ("settings.html", "Download Full Backup"),
    ("cashup.html", "Actual Cash Counted"),
    ("base.html", "Point of Sale"),
    ("base.html", "Moneris Check"),
    ("base.html", "Stock Count"),
    ("base.html", "Purchasing"),
    ("base.html", "Trade-Ins"),
]


@pytest.mark.parametrize("template,label", GUIDE_LABELS)
def test_the_guide_only_names_buttons_that_exist(template, label):
    assert label in text_of(template), f"{label!r} is gone from {template}"


def test_the_guides_own_labels_appear_in_the_guide():
    src = text_of("help.html")
    for label in ("Scan items as they arrive", "Confirm Receipt — Add to Stock", "Record check-in", "Print check-in sheet", "Start counting",
                  "Scan with camera", "Save reasons for later", "Download Full Backup", "Actual Cash Counted", "Back to the full order"):
        assert label in src, label
    assert "Review &amp; finish" in src


def test_the_guide_names_the_reasons_staff_will_actually_see():
    from app import stockcount as SC
    src = text_of("help.html")
    assert "Stock Counts &amp; Losses" in src
    assert "Report Center" in src
    assert "Why?" in src and "Lost or stolen" in SC.REASONS["stolen"][0]


# ═══ barcodes in the product list ════════════════════════════════════

def test_the_product_list_shows_the_barcode_under_the_sku(owner_client, db_session):
    db_session.add_all([Product(sku="V51-BC1", name="V51 Has Barcode", barcode="0036000291452", stock=3, cost=1, price=2, category="cases"),
                        Product(sku="V51-BC2", name="V51 No Barcode", barcode="", stock=3, cost=1, price=2, category="cases")])
    db_session.commit()
    try:
        page = owner_client.get("/products", params={"q": "V51-BC"}).text
        assert 'class="p-barcode"' in page and "0036000291452" in page
        assert page.count('class="p-barcode"') >= 1
        row2 = page.split("V51 No Barcode")[1].split("</tr>")[0] if "V51 No Barcode" in page else ""
        assert "p-barcode" not in row2, "a product without a barcode shows no empty barcode line"
    finally:
        db_session.query(Product).filter(Product.sku.like("V51-BC%")).delete(synchronize_session=False)
        db_session.commit()


def test_a_barcode_is_escaped_in_the_product_list(owner_client, db_session):
    db_session.add(Product(sku="V51-BC3", name="V51 Escape", barcode="<b>x</b>1234", stock=1, cost=1, price=2, category="cases"))
    db_session.commit()
    try:
        page = owner_client.get("/products", params={"q": "V51-BC3"}).text
        assert "<b>x</b>1234" not in page
    finally:
        db_session.query(Product).filter(Product.sku == "V51-BC3").delete(synchronize_session=False)
        db_session.commit()
