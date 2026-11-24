"""
v52 — the Help page covers the new screens (returns, repair quotes, cash out), and every button it tells people to tap
really exists on those screens.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"


def text_of(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


V52_GUIDE_LABELS = [
    ("invoice_detail.html", "Return items"),
    ("return_form.html", "Back on the shelf"),
    ("return_form.html", "Review the refund"),
    ("return_preview.html", "Confirm return"),
    ("invoice_detail.html", "Process Refund"),
    ("_quote_card.html", "Quote approval"),
    ("_quote_form.html", "Text the quote to"),
    ("_quote_form.html", "Just make the link"),
    ("_quote_card.html", "Withdraw"),
    ("_quote_card.html", "They approved"),
    ("_quote_card.html", "They declined"),
    ("quote_public.html", "Approve this quote"),
    ("quote_public.html", "Decline"),
    ("expenses.html", "Record cash out"),
    ("expense_form.html", "Cash from the till"),   # only checked below against app/expenses.py; the form renders PAID_FROM
    ("base.html", "Expenses"),
]


@pytest.mark.parametrize("template,label", V52_GUIDE_LABELS)
def test_the_new_guide_sections_only_name_buttons_that_exist(template, label):
    if label == "Cash from the till":
        from app.expenses import PAID_FROM
        assert PAID_FROM["drawer"] == label
        return
    assert label in text_of(template), f"{label!r} is gone from {template}"


def test_the_guide_uses_those_labels():
    src = text_of("help.html")
    for label in ("Return items", "Back on the shelf", "Review the refund", "Confirm return &amp; refund", "Quote approval",
                  "Just make the link", "Withdraw", "They approved", "They declined", "Approve this quote",
                  "Record cash out / expense", "Cash from the till"):
        assert label in src, label


def test_the_returns_section_is_for_owner_and_manager_only(owner_client, manager_client, cashier_client, technician_client):
    for client in (owner_client, manager_client):
        page = client.get("/help").text
        assert 'id="returns"' in page and 'href="#returns"' in page
        assert "Taking back some of the items on a sale" in page
    for client in (cashier_client, technician_client):
        page = client.get("/help").text
        assert 'id="returns"' not in page and 'href="#returns"' not in page
        assert "Taking back some of the items on a sale" not in page


@pytest.mark.parametrize("client_fixture", ["owner_client", "manager_client", "cashier_client", "technician_client"])
def test_everyone_sees_the_quote_and_cash_out_guidance(request, client_fixture):
    page = request.getfixturevalue(client_fixture).get("/help").text
    assert 'id="quotes"' in page and 'href="#quotes"' in page
    assert "Asking the customer to approve a repair price" in page
    assert "Took cash out of the drawer today" in page
    assert 'href="/expenses"' in page


def test_the_repair_step_lists_the_new_device_families(owner_client):
    page = owner_client.get("/help").text
    for word in ("iPads", "Galaxy M", "Pixel 10", "Pixel Tablet"):
        assert word in page, word


def test_the_help_page_is_still_clean_and_all_its_links_work(owner_client, cashier_client):
    for client in (owner_client, cashier_client):
        page = client.get("/help").text
        assert "{{" not in page and "{%" not in page and "None" not in page.replace("None of", "")
        for anchor in re.findall(r'href="#([a-z]+)"', page):
            assert f'id="{anchor}"' in page, anchor
        for link in sorted(set(re.findall(r'href="(/[^"#]*)"', page))):
            if link in ("/logout", "/static/style.css"):
                continue
            assert client.get(link, follow_redirects=False).status_code in (200, 303), link


def test_the_cash_out_step_comes_before_counting_the_drawer(owner_client):
    page = owner_client.get("/help").text
    close = page.split('id="close"')[1]
    assert close.index("Took cash out of the drawer today") < close.index("Actual Cash Counted")
