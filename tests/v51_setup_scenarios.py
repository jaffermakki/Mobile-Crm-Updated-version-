"""Helper for tests/test_v51_setup.py — NOT a test file.

First-run setup is decided when the app starts on an EMPTY database, so each scenario runs in its own process against
its own throwaway SQLite file. Usage: python v51_setup_scenarios.py <scenario> <db-file>; prints one 'RESULT:{json}' line.
"""
import json
import os
import sys
from pathlib import Path

scenario, db_file = sys.argv[1], sys.argv[2]
os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
os.environ["SESSION_SECRET"] = "setup-scenario-secret"
os.environ["SETTINGS_ENCRYPTION_KEY"] = "kQqjXGz0Yb3g8h5m1nR7sT2vC4wA6dF9eH0iJ3kL5mN="
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402,F401  (its presence switches the CSRF check off, exactly as in the real suite)
from starlette.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app import setup as S  # noqa: E402
from app.models import AuditLog, Invoice, InvoiceLine, Product, Setting, Staff  # noqa: E402

db = SessionLocal()
out: dict = {}
BASE = dict(shop_name="TechPro+ Red Deer", province="AB", owner_name="Mo", pin="739204", pin_confirm="739204", remove_samples="1",
            shop_phone="403-555-0100", shop_address="1 Main St", shop_gst="123456789RT0001")


def loc(resp):
    return resp.headers.get("location")


def setting(key):
    db.expire_all()
    row = db.get(Setting, key)
    return row.value if row is not None else None


def post(client, **kw):
    data = dict(BASE)
    data.update(kw)
    return client.post("/setup", data=data, follow_redirects=False)


if scenario == "flow":
    code = setting("setup_code")
    out["code_shape"] = code
    out["pending"] = S.is_pending()
    c = TestClient(app)
    out["gate"] = {u: [c.get(u, follow_redirects=False).status_code, loc(c.get(u, follow_redirects=False))]
                   for u in ("/", "/login", "/repairs", "/pos", "/forgot-pin", "/help", "/reports")}
    out["open"] = {"healthz": c.get("/healthz").status_code, "static": c.get("/static/style.css").status_code,
                   "setup": c.get("/setup").status_code}
    out["setup_page_has_code_box"] = 'name="setup_code"' in c.get("/setup").text
    out["old_pin_login"] = [c.post("/login", data={"pin": "1234"}, follow_redirects=False).status_code,
                            loc(c.post("/login", data={"pin": "1234"}, follow_redirects=False))]
    out["post_other"] = c.post("/pos/scan", data={"sku": "x"}, follow_redirects=False).status_code
    out["wrong_code"] = post(c, setup_code="WRONG-CODE-0000").status_code
    out["no_code"] = post(c, setup_code="").status_code
    out["weak_pin"] = [post(c, setup_code=code, pin="1234", pin_confirm="1234").status_code,
                       post(c, setup_code=code, pin="111111", pin_confirm="111111").status_code]
    out["short_pin"] = post(c, setup_code=code, pin="73920", pin_confirm="73920").status_code
    out["letters_pin"] = post(c, setup_code=code, pin="73920a", pin_confirm="73920a").status_code
    out["mismatch"] = post(c, setup_code=code, pin_confirm="739205").status_code
    out["bad_province"] = post(c, setup_code=code, province="ZZ").status_code
    out["no_shop_name"] = post(c, setup_code=code, shop_name="  ").status_code
    out["no_owner_name"] = post(c, setup_code=code, owner_name="").status_code
    out["still_pending_after_failures"] = S.refresh(db)
    out["failed_form_keeps_answers"] = "TechPro+ Red Deer" in post(c, setup_code=code, pin="1234", pin_confirm="1234").text
    ok = post(c, setup_code=code.lower().replace("-", " "))          # the code is forgiving about case, spaces and dashes
    out["success"] = [ok.status_code, loc(ok)]
    out["help_after"] = c.get("/help?welcome=1").status_code
    out["home_after"] = c.get("/").status_code
    out["setup_again"] = [c.get("/setup", follow_redirects=False).status_code, loc(c.get("/setup", follow_redirects=False))]
    again = c.post("/setup", data=dict(BASE, setup_code=code, pin="918273", pin_confirm="918273"), follow_redirects=False)
    out["post_again"] = [again.status_code, loc(again)]
    fresh = TestClient(app)
    out["old_pin_after"] = loc(fresh.post("/login", data={"pin": "1234"}, follow_redirects=False))
    out["second_pin_after"] = loc(fresh.post("/login", data={"pin": "918273"}, follow_redirects=False))
    out["new_pin_after"] = loc(fresh.post("/login", data={"pin": "739204"}, follow_redirects=False))
    db.expire_all()
    out["settings"] = {k: setting(k) for k in ("shop_name", "province", "shop_phone", "shop_address", "shop_gst", "setup_pending", "setup_code")}
    out["owner"] = [(s.name, s.role, s.pin_len) for s in db.query(Staff).all()]
    out["samples_left"] = db.query(Product).count()
    out["audit"] = [a.detail for a in db.query(AuditLog).filter(AuditLog.action == "SETUP").all()]
    out["pending_after"] = S.is_pending()

elif scenario == "keep_samples":
    code = setting("setup_code")
    c = TestClient(app)
    out["before"] = db.query(Product).count()
    out["success"] = post(c, setup_code=code, remove_samples="").status_code
    db.expire_all()
    out["after"] = db.query(Product).count()

elif scenario == "sold_sample":
    code = setting("setup_code")
    sold = db.query(Product).filter(Product.sku == "SCRN-IP13").one()
    inv = Invoice(number="INV-SETUP-1", total=10.0)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, product_id=sold.id, name=sold.name, sku=sold.sku, qty=1, price=10.0))
    db.commit()
    out["before"] = db.query(Product).count()
    c = TestClient(app)
    out["success"] = post(c, setup_code=code, remove_samples="1").status_code
    db.expire_all()
    out["left"] = sorted(p.sku for p in db.query(Product).all())

elif scenario == "env_code":
    out["code_row"] = setting("setup_code")
    out["pending"] = S.is_pending()
    c = TestClient(app)
    out["page_mentions_variable"] = "SETUP_CODE" in c.get("/setup").text
    out["random_code_refused"] = post(c, setup_code="ABCD-EFGH-JKLM").status_code
    out["right_code"] = post(c, setup_code="my own code 77").status_code      # case / spaces forgiven

elif scenario == "env_code_too_short":
    out["code_row"] = setting("setup_code")
    out["short_refused"] = post(TestClient(app), setup_code="abc").status_code

elif scenario == "existing_shop":
    # the database already has staff, so it must never turn into "pending"
    out["pending"] = S.is_pending(db)
    out["staff"] = db.query(Staff).count()
    out["flag"] = setting("setup_pending")
    c = TestClient(app)
    out["login_page"] = c.get("/login", follow_redirects=False).status_code
    out["setup_redirect"] = loc(c.get("/setup", follow_redirects=False))
    out["pin_1234_still_works"] = c.post("/login", data={"pin": "1234"}, follow_redirects=False).status_code

db.close()
print("RESULT:" + json.dumps(out, default=str))
