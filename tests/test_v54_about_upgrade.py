"""v54 — the About / health page (item 7), the support text, and upgrading from v53 (backups and databases without the new tables)."""
import html as _html
import re
import struct
import zlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app import backup as B
from app import health as H
from app import version as V
from app.models import (Customer, Product, RepairPhoto, RepairPrice, Setting, ShopLogo, Staff)

TAG = "V54AB"
KEYS = ("last_backup", "backup_email_enabled", "backup_email", "digest_email", "backup_password", "twilio_sid", "twilio_token",
        "twilio_from", "email_method", "smtp_host", "brevo_api_key", "brevo_from_email", "smtp_from", "brand_hours", "brand_show_photos")


@pytest.fixture(autouse=True)
def _tidy(db_session):
    saved = {k: (db_session.get(Setting, k).value if db_session.get(Setting, k) else None) for k in KEYS}

    def restore():
        db_session.rollback()
        for k, v in saved.items():
            row = db_session.get(Setting, k)
            if v is None and row:
                db_session.delete(row)
            elif v is not None:
                if row:
                    row.value = v
                else:
                    db_session.add(Setting(key=k, value=v))
        db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).delete(synchronize_session=False)
        db_session.query(RepairPhoto).filter(RepairPhoto.repair_id.like(f"{TAG}%")).delete(synchronize_session=False)
        db_session.query(RepairPrice).delete(synchronize_session=False)
        db_session.query(ShopLogo).delete(synchronize_session=False)
        db_session.commit()
    restore()
    yield
    restore()


def _set(db, key, value):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def _check(report, key):
    return next(c for c in report["checks"] if c["key"] == key)


# ── access ──────────────────────────────────────────────────────────────────

def test_about_is_for_owner_and_manager_only(owner_client, manager_client, cashier_client, technician_client, anon_client):
    assert owner_client.get("/about").status_code == 200
    assert manager_client.get("/about").status_code == 200
    assert cashier_client.get("/about").status_code == 403 and technician_client.get("/about").status_code == 403
    assert anon_client.get("/about", follow_redirects=False).status_code == 303
    assert owner_client.get("/about").headers["cache-control"] == "no-store"


def test_back_up_now_is_the_owners_button_and_the_menu_has_the_link(owner_client, manager_client):
    o, m = owner_client.get("/about").text, manager_client.get("/about").text
    assert 'href="/export/backup"' in o and "Back up now" in o and "Copy support info" in o
    assert 'href="/export/backup"' not in m and "Back up now" not in m and "Copy support info" in m
    assert 'href="/about"' in o and 'href="/about"' in m


def test_back_up_now_really_makes_a_backup_and_the_page_notices(owner_client, db_session):
    assert _check(H.collect(db_session, True), "backup")["state"] in ("bad", "ok", "warn")
    r = owner_client.get("/export/backup")
    assert r.status_code == 200 and len(r.content) > 100
    db_session.expire_all()
    assert _check(H.collect(db_session, True), "backup")["state"] == "ok"
    page = owner_client.get("/about").text
    assert "today" in page


# ── the checks ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days,state", [(0, "ok"), (7, "ok"), (8, "warn"), (30, "warn"), (31, "bad"), (400, "bad")])
def test_backup_age_states(db_session, days, state):
    now = datetime(2026, 11, 23, 12, 0)
    _set(db_session, "last_backup", (now - timedelta(days=days)).isoformat())
    assert _check(H.collect(db_session, True, now=now), "backup")["state"] == state


def test_never_backed_up_and_garbled_dates(db_session):
    row = db_session.get(Setting, "last_backup")
    if row:
        db_session.delete(row)
        db_session.commit()
    c = _check(H.collect(db_session, True), "backup")
    assert c["state"] == "bad" and "No backup" in c["value"]
    _set(db_session, "last_backup", "not a date")
    assert _check(H.collect(db_session, True), "backup")["state"] == "bad"
    _set(db_session, "last_backup", (datetime.utcnow() + timedelta(days=3)).isoformat())          # a clock set in the future
    assert _check(H.collect(db_session, True), "backup")["state"] == "ok"


def test_help_text_for_a_manager_does_not_point_at_a_button_they_do_not_have(db_session):
    r = H.collect(db_session, True, can_backup=False)
    assert "Press Back up now" not in " ".join(c["help"] for c in r["checks"])


def test_texting_email_weekly_backup_and_password_states(db_session):
    for k in ("twilio_sid", "twilio_token", "twilio_from"):
        _set(db_session, k, "")
    r = H.collect(db_session, True)
    assert _check(r, "sms")["state"] == "warn" and _check(r, "backup_weekly")["state"] == "warn" and _check(r, "backup_lock")["state"] == "warn"
    for k in ("twilio_sid", "twilio_token", "twilio_from"):
        _set(db_session, k, "x")
    _set(db_session, "backup_email_enabled", "true")
    _set(db_session, "backup_email", "owner@example.test")
    _set(db_session, "backup_password", "enc:whatever")
    r = H.collect(db_session, True)
    assert _check(r, "sms")["state"] == "ok" and _check(r, "backup_weekly")["state"] == "ok" and _check(r, "backup_lock")["state"] == "ok"
    _set(db_session, "email_method", "brevo_api")
    _set(db_session, "brevo_api_key", "k")
    _set(db_session, "brevo_from_email", "")
    _set(db_session, "smtp_from", "")
    assert _check(H.collect(db_session, True), "email")["state"] == "warn"
    _set(db_session, "brevo_from_email", "shop@example.test")
    assert _check(H.collect(db_session, True), "email")["state"] == "ok"
    _set(db_session, "email_method", "smtp")
    _set(db_session, "smtp_host", "")
    assert _check(H.collect(db_session, True), "email")["state"] == "warn"
    _set(db_session, "smtp_host", "smtp.example.test")
    assert _check(H.collect(db_session, True), "email")["state"] == "ok"


def test_weekly_backup_needs_a_recipient_to_count(db_session):
    _set(db_session, "backup_email_enabled", "true")
    for k in ("backup_email", "digest_email"):
        _set(db_session, k, "")
    assert _check(H.collect(db_session, True), "backup_weekly")["state"] == "warn"


def test_photo_storage_warns_when_large(db_session):
    db_session.add(RepairPhoto(repair_id=f"{TAG}-1", label="front", mime="image/jpeg", data=b"x", size=H.PHOTO_WARN_MB * 1_000_000 + 1))
    db_session.commit()
    c = _check(H.collect(db_session, True), "photos")
    assert c["state"] == "warn" and "photos" in c["value"]
    db_session.query(RepairPhoto).filter(RepairPhoto.repair_id == f"{TAG}-1").delete()
    db_session.commit()
    assert _check(H.collect(db_session, True), "photos")["state"] == "ok"


def test_customer_pages_line_follows_the_branding_settings(db_session):
    assert _check(H.collect(db_session, True), "customer_pages")["state"] == "info"
    db_session.add(ShopLogo(id="logo", mime="image/png", width=1, height=1, data=b"x", size=1))
    db_session.commit()
    _set(db_session, "brand_hours", "Mon 10-6")
    c = _check(H.collect(db_session, True), "customer_pages")
    assert c["state"] == "ok" and "logo set" in c["value"] and "hours set" in c["value"] and "photos hidden" in c["value"]


def test_https_and_key_checks(db_session):
    assert _check(H.collect(db_session, True, env={"SETTINGS_ENCRYPTION_KEY": "k", "SESSION_SECRET": "s"}), "https")["state"] == "ok"
    r = H.collect(db_session, False, env={})
    assert _check(r, "https")["state"] == "warn" and _check(r, "enc_key")["state"] == "warn" and _check(r, "session_secret")["state"] == "warn"


def test_https_is_read_from_the_proxy_header(owner_client):
    assert "Not HTTPS" in owner_client.get("/about").text
    assert "HTTPS</div>" in owner_client.get("/about", headers={"x-forwarded-proto": "https"}).text.replace("Not HTTPS", "")


def test_sqlite_on_a_hosted_server_is_flagged(db_session):
    assert H.database_kind() in ("SQLite", "PostgreSQL")
    r = H.collect(db_session, True, env={"RAILWAY_ENVIRONMENT": "production"})
    if H.database_kind() == "SQLite":
        assert _check(r, "database")["state"] == "bad" and r["overall"] == "bad"
    else:
        assert _check(r, "database")["state"] == "ok"


def test_counts_line_never_lists_names_or_money(db_session):
    db_session.add(Customer(name=f"{TAG} Secret Name", phone="2505550111"))
    db_session.commit()
    c = _check(H.collect(db_session, True), "counts")["value"]
    assert "customers" in c and "Secret" not in c and "$" not in c


# ── structure: what an unfinished upgrade looks like ────────────────────────

def test_structure_is_clean_on_the_live_database():
    st = H.structure()
    assert st["ok"] and st["tables"] >= 30 and not st["missing_tables"] and not st["missing_columns"]


def test_structure_names_missing_tables_and_columns():
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE repairs (id TEXT PRIMARY KEY)"))              # an old table without most columns
    st = H.structure(eng)
    assert not st["ok"] and "repair_prices" in st["missing_tables"] and "shop_logo" in st["missing_tables"]
    assert "repairs.device" in st["missing_columns"] and "repairs.status_token" in st["missing_columns"]


def test_a_broken_structure_turns_the_page_red(db_session):
    eng = create_engine("sqlite://")
    r = H.collect(db_session, True, db_engine=eng)
    c = _check(r, "structure")
    assert c["state"] == "bad" and "missing" in c["value"] and r["overall"] == "bad" and r["problems"] >= 1


# ── the support text ────────────────────────────────────────────────────────

def test_support_text_has_no_secrets_no_customers_no_addresses(owner_client, db_session):
    _set(db_session, "twilio_token", "SECRET-TOKEN-ABC123")
    _set(db_session, "twilio_sid", "SID-XYZ")
    _set(db_session, "twilio_from", "+12505550199")
    _set(db_session, "backup_password", "enc:SUPERSECRETPASS")
    _set(db_session, "brevo_api_key", "BREVO-KEY-999")
    _set(db_session, "backup_email", "owner-private@example.test")
    db_session.add(Customer(name=f"{TAG} Hidden Person", phone="2505550123", email="hidden@example.test"))
    db_session.commit()
    page = owner_client.get("/about").text
    box = _html.unescape(re.search(r'<textarea id="support-text".*?>(.*?)</textarea>', page, re.S).group(1))
    for secret in ("SECRET-TOKEN-ABC123", "SID-XYZ", "+12505550199", "SUPERSECRETPASS", "BREVO-KEY-999", "owner-private@example.test",
                   "Hidden Person", "2505550123", "hidden@example.test"):
        assert secret not in page and secret not in box, secret
    assert V.LABEL in box and "Summary:" in box and "Python" in box
    assert box.count("\n") >= 10


def test_page_wording_matches_the_summary(owner_client, db_session):
    page = owner_client.get("/about").text
    assert V.LABEL in page and V.VERSION in page and 'data-check="structure"' in page and 'data-check="backup"' in page
    assert "/static/about.js" in page and 'id="copy-support"' in page


# ── upgrading from v53 ──────────────────────────────────────────────────────

V54_TABLES = ("repair_prices", "shop_logo")


def _doc(db_session):
    db_session.expire_all()
    return B.read_backup(B.make_backup(db_session, None)[0])


def test_backup_label_follows_the_version_file(db_session):
    assert B.APP_LABEL == V.LABEL and _doc(db_session)["app"] == V.LABEL and V.LABEL.endswith(V.MAJOR)


def test_a_v53_backup_without_the_new_tables_restores_and_the_app_still_works(owner_client, db_session):
    p = Product(sku=f"{TAG}-OLD", name=f"{TAG} Old Product", category="SCREEN", price=10, cost=4, stock=3)
    db_session.add(p)
    db_session.commit()
    doc = _doc(db_session)
    for name in V54_TABLES:
        doc["tables"].pop(name, None)
    doc["app"] = "TechPro+ CRM v53"
    db_session.add(RepairPrice(model="X", model_key="x", repair="Y", repair_key="y", price=1))
    db_session.commit()
    result = B.restore_full(db_session, doc, db_session.get(Staff, "s1") or db_session.query(Staff).filter(Staff.role == "owner").first())
    db_session.expire_all()
    assert db_session.query(Product).filter(Product.sku == f"{TAG}-OLD").count() == 1
    assert db_session.query(RepairPrice).count() == 0               # the old file had no price list: restore makes the database match it
    for url in ("/repair-prices", "/branding", "/about", "/repairs", "/status", "/brand/logo"):
        assert owner_client.get(url, follow_redirects=False).status_code in (200, 404), url
    db_session.query(Product).filter(Product.sku == f"{TAG}-OLD").delete()
    db_session.commit()


def test_a_v54_backup_brings_back_prices_and_the_logo(owner_client, db_session):
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    owner_client.post("/repair-prices/add", data={"device": "Pixel 8", "repair": "Screen Replacement", "price": "149.5", "minutes": "30"})
    db_session.add(ShopLogo(id="logo", mime="image/png", width=4, height=4, data=png + b"\x00" * 20, size=len(png) + 20))
    db_session.commit()
    doc = _doc(db_session)
    assert doc["tables"]["repair_prices"] and doc["tables"]["shop_logo"]
    db_session.query(RepairPrice).delete()
    db_session.query(ShopLogo).delete()
    db_session.commit()
    B.restore_full(db_session, doc, db_session.query(Staff).filter(Staff.role == "owner").first())
    db_session.expire_all()
    row = db_session.query(RepairPrice).one()
    assert row.price == 149.5 and row.minutes == 30
    logo = db_session.get(ShopLogo, "logo")
    assert logo.mime == "image/png" and bytes(logo.data).startswith(b"\x89PNG")
    assert owner_client.get("/brand/logo").status_code == 200


def test_restore_summary_names_the_new_tables():
    for name in V54_TABLES:
        assert name in B.TABLE_LABELS, name


def test_a_database_made_by_v53_gets_the_new_tables_when_v54_opens_it(tmp_path):
    """Build a v53-shaped database (every table EXCEPT the two new ones), then run the same startup steps the app runs."""
    from app import seed
    from app.database import Base
    from app import models  # noqa: F401
    eng = create_engine(f"sqlite:///{tmp_path / 'v53.db'}")
    old = [t for t in Base.metadata.sorted_tables if t.name not in V54_TABLES]
    Base.metadata.create_all(eng, tables=old)
    assert not (set(V54_TABLES) & set(__import__("sqlalchemy").inspect(eng).get_table_names()))
    Base.metadata.create_all(eng)                                  # what init_db() does first
    names = set(__import__("sqlalchemy").inspect(eng).get_table_names())
    assert set(V54_TABLES) <= names
    st = H.structure(eng)
    assert st["ok"], st
