"""v54 — branded customer pages (item 3): logo, colour, hours, progress steps, "text the shop", and the photo privacy rules."""
import html as _html
import re
import struct
import zlib

import pytest

from app import brand as B
from app import quotes as Q
from app.models import AuditLog, Customer, LoginAttempt, QuoteApproval, Repair, RepairPhoto, Setting, ShopLogo

TAG = "V54BR"
PHONE = "250-555-0187"
KEYS = ("brand_color", "brand_hours", "brand_show_photos", "shop_phone", "shop_address", "shop_name")


def png(w=40, h=20):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


@pytest.fixture(autouse=True)
def _tidy(db_session, monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-real-ip")
    saved = {k: (db_session.get(Setting, k).value if db_session.get(Setting, k) else None) for k in KEYS}

    def wipe():
        db_session.rollback()
        cids = [c.id for c in db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).all()]
        rids = [r.id for r in db_session.query(Repair).filter(Repair.customer_id.in_(cids)).all()] if cids else []
        if rids:
            db_session.query(RepairPhoto).filter(RepairPhoto.repair_id.in_(rids)).delete(synchronize_session=False)
            db_session.query(QuoteApproval).filter(QuoteApproval.repair_id.in_(rids)).delete(synchronize_session=False)
            db_session.query(Repair).filter(Repair.id.in_(rids)).delete(synchronize_session=False)
        if cids:
            db_session.query(Customer).filter(Customer.id.in_(cids)).delete(synchronize_session=False)
        db_session.query(ShopLogo).delete(synchronize_session=False)
        db_session.query(LoginAttempt).filter(LoginAttempt.kind == "status").delete(synchronize_session=False)
        db_session.query(AuditLog).filter(AuditLog.action == "BRAND_CHANGE").delete(synchronize_session=False)
        for k, v in saved.items():
            row = db_session.get(Setting, k)
            if v is None and row:
                db_session.delete(row)
            elif v is not None:
                if row:
                    row.value = v
                else:
                    db_session.add(Setting(key=k, value=v))
        db_session.commit()
    wipe()
    yield
    wipe()


def _set(db, key, value):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


_n = [0]


def _ticket(db, status="IN_PROGRESS", device="Pixel 8"):
    _n[0] += 1
    c = Customer(name=f"{TAG} Bea {_n[0]}", phone=PHONE)
    db.add(c)
    db.flush()
    r = Repair(ticket_no=770000 + _n[0], customer_id=c.id, device=device, issue="Battery Replacement", status=status, promised_by="Fri")
    db.add(r)
    db.commit()
    from app import security as sec
    sec.ensure_status_token(db, r)
    db.commit()
    db.refresh(r)
    return r


def _photo(db, repair, label="front"):
    p = RepairPhoto(repair_id=repair.id, label=label, mime="image/png", width=40, height=20, data=png(), thumb=png(), size=300, staff_name="t")
    db.add(p)
    db.commit()
    return p


# ── helpers and rules ───────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["red", "#12345", "#1234567", "#gggggg", "javascript:alert(1)", "#123456;color:red", "url(x)", "rgb(1,2,3)", "#12 456", "<b>"])
def test_colour_must_be_exactly_six_hex_digits(bad):
    with pytest.raises(B.BrandError):
        B.clean_color(bad)


def test_colour_is_normalised_and_blank_means_default():
    assert B.clean_color("1A73E8") == "#1a73e8" and B.clean_color(" #FFC53D ") == "#ffc53d" and B.clean_color("") == ""


def test_text_on_colour_is_readable_both_ways():
    for hex_color in ("#ffffff", "#ffc53d", "#f5f5f5", "#ffff00"):
        assert B.on_color(hex_color) == "#14110a"
    for hex_color in ("#000000", "#1a73e8", "#2d3748", "#8b0000"):
        assert B.on_color(hex_color) == "#ffffff"


def test_hours_are_trimmed_and_limited():
    assert B.clean_hours("  Mon-Fri   10-6 \r\n\r\n Sat 10-4 ") == "Mon-Fri 10-6\nSat 10-4"
    with pytest.raises(B.BrandError):
        B.clean_hours("\n".join(f"line {i}" for i in range(B.MAX_HOURS_LINES + 1)))
    with pytest.raises(B.BrandError):
        B.clean_hours("x" * (B.MAX_HOURS_CHARS + 1))


def test_phone_links_carry_digits_only():
    assert B.phone_digits("(250) 555-0100") == "2505550100" and B.phone_digits("+1 250 555 0100") == "+12505550100"
    assert B.phone_digits('250"><script>') == "250" and B.phone_digits("call us") == ""


def test_progress_steps_for_every_status():
    done = lambda s: [x["state"] for x in B.progress(s)["steps"]]
    assert done("RECEIVED") == ["current", "todo", "todo", "todo", "todo"]
    assert done("DIAGNOSED") == ["done", "current", "todo", "todo", "todo"]
    assert done("WAITING") == ["done", "current", "todo", "todo", "todo"] and B.progress("WAITING")["waiting"]
    assert done("IN_PROGRESS") == ["done", "done", "current", "todo", "todo"]
    assert done("READY") == done("COMPLETED") == ["done", "done", "done", "current", "todo"]
    assert done("COLLECTED") == ["done"] * 5
    assert B.progress("SOMETHING_ELSE")["steps"][0]["state"] == "current"          # an unknown status never crashes


# ── the logo ────────────────────────────────────────────────────────────────

def test_logo_upload_serve_replace_remove_with_audit(owner_client, anon_client, db_session):
    assert anon_client.get("/brand/logo").status_code == 404
    r = owner_client.post("/branding/logo", files={"file": ("a.png", png(), "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    got = anon_client.get("/brand/logo")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png" and got.content == png()
    assert got.headers["x-content-type-options"] == "nosniff" and "sandbox" in got.headers["content-security-policy"]
    owner_client.post("/branding/logo", files={"file": ("b.png", png(80, 40), "image/png")})
    assert db_session.query(ShopLogo).count() == 1
    db_session.expire_all()
    assert db_session.get(ShopLogo, "logo").width == 80
    owner_client.post("/branding/logo/remove")
    assert anon_client.get("/brand/logo").status_code == 404
    db_session.expire_all()
    assert db_session.query(AuditLog).filter(AuditLog.action == "BRAND_CHANGE").count() >= 3


@pytest.mark.parametrize("case", ["svg", "html-as-png", "gif", "truncated-png", "empty", "too-big", "too-wide"])
def test_bad_logo_files_are_refused_and_the_old_logo_stays(owner_client, db_session, case):
    name, body, why = {
        "svg": ("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "not a PNG"),
        "html-as-png": ("x.png", b"<html><script>alert(1)</script></html>", "not a PNG"),
        "gif": ("x.png", b"GIF89a" + b"\x00" * 40, "not a PNG"),
        "truncated-png": ("x.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 5, "not a PNG"),
        "empty": ("x.png", b"", "Choose a picture"),
        "too-big": ("x.png", png() + b"\x00" * (B.MAX_LOGO_BYTES + 10), "most is"),
        "too-wide": ("x.png", png(B.MAX_LOGO_SIDE + 1, 10), "longest side"),
    }[case]
    owner_client.post("/branding/logo", files={"file": ("ok.png", png(), "image/png")})
    page = owner_client.post("/branding/logo", files={"file": (name, body, "image/png")}).text       # the redirect is followed
    assert "Logo not changed" in page and why in page
    db_session.expire_all()
    assert db_session.get(ShopLogo, "logo").data == png()


def test_a_script_hidden_in_a_png_is_served_as_a_harmless_image(owner_client, anon_client):
    sneaky = png() + b"<script>alert(1)</script>"
    owner_client.post("/branding/logo", files={"file": ("s.png", sneaky, "text/html")})
    r = anon_client.get("/brand/logo")
    assert r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"


# ── who can change it ───────────────────────────────────────────────────────

def test_only_the_owner_changes_the_customer_pages(owner_client, manager_client, cashier_client, technician_client, anon_client, db_session):
    for c in (manager_client, cashier_client, technician_client):
        assert c.get("/branding").status_code == 403
        assert c.post("/branding/save", data={"color": "#112233", "hours": "x"}).status_code == 403
        assert c.post("/branding/logo", files={"file": ("a.png", png(), "image/png")}).status_code == 403
        assert c.post("/branding/logo/remove").status_code == 403
        assert c.get("/branding/preview").status_code == 403
    assert anon_client.get("/branding", follow_redirects=False).status_code == 303
    assert anon_client.post("/branding/save", data={"color": "#112233"}, follow_redirects=False).status_code in (303, 401, 403)
    db_session.expire_all()
    assert db_session.get(Setting, "brand_color") is None or db_session.get(Setting, "brand_color").value != "#112233"
    assert db_session.query(ShopLogo).count() == 0


def test_saving_validates_everything_before_writing_anything(owner_client, db_session):
    owner_client.post("/branding/save", data={"color": "#112233", "hours": "Mon 10-6"})
    r = owner_client.post("/branding/save", data={"color": "not-a-colour", "hours": "Changed", "show_photos": "1"}, follow_redirects=True)
    assert "Not saved" in r.text
    db_session.expire_all()
    assert db_session.get(Setting, "brand_color").value == "#112233" and db_session.get(Setting, "brand_hours").value == "Mon 10-6"
    assert db_session.get(Setting, "brand_show_photos").value == "false"
    r = owner_client.post("/branding/save", data={"color": "", "hours": "x\n" * 20}, follow_redirects=True)
    assert "Not saved" in r.text


def test_save_audits_what_changed(owner_client, db_session):
    owner_client.post("/branding/save", data={"color": "#112233", "hours": "Mon 10-6", "show_photos": "1"})
    d = [a.detail for a in db_session.query(AuditLog).filter(AuditLog.action == "BRAND_CHANGE").all()]
    assert any("colour" in x and "opening hours" in x and "photos shown" in x for x in d)


# ── the status page ─────────────────────────────────────────────────────────

def test_status_link_shows_logo_colour_hours_stepper_and_contact(owner_client, anon_client, db_session):
    owner_client.post("/branding/logo", files={"file": ("a.png", png(), "image/png")})
    _set(db_session, "shop_phone", "(250) 555-0100")
    _set(db_session, "shop_address", "1 Main St, Vernon BC")
    owner_client.post("/branding/save", data={"color": "#1a73e8", "hours": "Mon-Fri 10-6\nSat 10-4"})
    r = _ticket(db_session, "IN_PROGRESS")
    html = anon_client.get(f"/s/{r.status_token}").text
    assert "/brand/logo?v=" in html and "--brand:#1a73e8" in html and "--on-brand:#ffffff" in html
    assert "Mon-Fri 10-6<br>Sat 10-4" in html and "1 Main St, Vernon BC" in html
    assert 'href="tel:2505550100"' in html and "sms:2505550100?&amp;body=" in html and "Text the shop" in html
    assert f"ticket%20%23{r.ticket_no}" in html
    assert html.count('aria-current="step"') == 1 and "Repairing" in html and "Picked up" in html
    assert "Still being worked on" in html and f"#{r.ticket_no}" in html and "Pixel 8" in html
    assert "Cost" not in html and "estimated" not in html.lower() and PHONE not in html          # nothing private leaks


def test_ready_and_collected_wording(anon_client, db_session):
    ready = _ticket(db_session, "READY")
    assert "Ready for pickup!" in anon_client.get(f"/s/{ready.status_token}").text
    done = _ticket(db_session, "COLLECTED")
    h = anon_client.get(f"/s/{done.status_token}").text
    assert "Picked up. Thank you!" in h and "Ready for pickup!" not in h and h.count("cp-done") >= 5
    waiting = _ticket(db_session, "WAITING")
    assert "waiting for a part" in anon_client.get(f"/s/{waiting.status_token}").text


def test_defaults_when_nothing_is_set_up(anon_client, db_session):
    for k in ("shop_phone", "shop_address", "brand_color", "brand_hours"):
        row = db_session.get(Setting, k)
        if row:
            db_session.delete(row)
    db_session.commit()
    r = _ticket(db_session)
    html = anon_client.get(f"/s/{r.status_token}").text
    assert "--brand:#FFC53D" in html and "/brand/logo" not in html and "tel:" not in html and "Hours" not in html
    assert "Please call or visit the shop" in html


def test_hostile_settings_are_escaped_on_the_public_page(owner_client, anon_client, db_session):
    evil = '<script>alert(1)</script>'
    _set(db_session, "shop_name", evil)
    _set(db_session, "shop_address", evil)
    owner_client.post("/branding/save", data={"hours": evil})
    r = _ticket(db_session)
    html = anon_client.get(f"/s/{r.status_token}").text
    assert evil not in html and "&lt;script&gt;" in html
    _set(db_session, "brand_color", '#123456"><script>alert(1)</script>')               # a hand-edited bad value falls back safely
    html = anon_client.get(f"/s/{r.status_token}").text
    assert "<script>alert(1)" not in html and "--brand:#FFC53D" in html


def test_ticket_plus_last4_lookup_still_works_and_never_shows_photos(owner_client, anon_client, db_session):
    owner_client.post("/branding/save", data={"show_photos": "1"})
    r = _ticket(db_session)
    _photo(db_session, r)
    ok = anon_client.post("/status", data={"ticket_no": str(r.ticket_no), "phone_last4": "0187"})
    assert ok.status_code == 200 and f"#{r.ticket_no}" in ok.text and "cp-steps" in ok.text and "/photo/" not in ok.text
    bad = anon_client.post("/status", data={"ticket_no": str(r.ticket_no), "phone_last4": "9999"})
    assert "couldn't find a matching ticket" in _html.unescape(bad.text) and "cp-steps" not in bad.text
    assert "Check Repair Status" in anon_client.get("/status").text


def test_unknown_link_is_the_same_branded_404(anon_client):
    r = anon_client.get("/s/not-a-real-code-1234")
    assert r.status_code == 404 and "isn't valid any more" in _html.unescape(r.text) and "cp-wrap" in r.text


# ── photos: the privacy rules ───────────────────────────────────────────────

def test_photos_are_off_by_default_and_unreachable(anon_client, db_session):
    r = _ticket(db_session)
    p = _photo(db_session, r)
    assert "/photo/" not in anon_client.get(f"/s/{r.status_token}").text
    assert anon_client.get(f"/s/{r.status_token}/photo/{p.id}").status_code == 404
    assert anon_client.get(f"/s/{r.status_token}/photo/{p.id}/thumb").status_code == 404


def test_photos_show_on_the_private_link_when_the_owner_allows_it(owner_client, anon_client, db_session):
    owner_client.post("/branding/save", data={"show_photos": "1"})
    r = _ticket(db_session)
    front, dmg, imei = _photo(db_session, r, "front"), _photo(db_session, r, "damage"), _photo(db_session, r, "label")
    html = anon_client.get(f"/s/{r.status_token}").text
    assert f"/photo/{front.id}/thumb" in html and f"/photo/{dmg.id}/thumb" in html and imei.id not in html
    full = anon_client.get(f"/s/{r.status_token}/photo/{front.id}")
    assert full.status_code == 200 and full.content == png() and full.headers["content-type"] == "image/png"
    assert full.headers["cache-control"] == "private, no-store" and full.headers["x-content-type-options"] == "nosniff"
    assert anon_client.get(f"/s/{r.status_token}/photo/{front.id}/thumb").status_code == 200
    assert anon_client.get(f"/s/{r.status_token}/photo/{imei.id}").status_code == 404           # the IMEI-sticker photo is never shown
    owner_client.post("/branding/save", data={})                                                 # switch off again
    assert anon_client.get(f"/s/{r.status_token}/photo/{front.id}").status_code == 404
    assert "/photo/" not in anon_client.get(f"/s/{r.status_token}").text


def test_the_full_photo_csp_lets_the_browsers_own_image_page_style_itself_but_nothing_else(owner_client, anon_client, db_session):
    """Found in the browser run: with default-src 'none' alone, Chrome logs a blocked inline style when a customer taps a photo."""
    owner_client.post("/branding/save", data={"show_photos": "1"})
    r = _ticket(db_session)
    front = _photo(db_session, r, "front")
    csp = anon_client.get(f"/s/{r.status_token}/photo/{front.id}").headers["content-security-policy"]
    assert "default-src 'none'" in csp and "style-src 'unsafe-inline'" in csp and "sandbox" in csp
    assert "script-src" not in csp and "img-src" not in csp


def test_call_button_keeps_the_number_in_one_piece(anon_client, db_session):
    _set(db_session, "shop_phone", "(250) 555-0100")
    html = anon_client.get("/status").text
    assert 'class="cp-num">(250) 555-0100</span>' in html


def test_one_customers_link_cannot_open_another_customers_photo(owner_client, anon_client, db_session):
    owner_client.post("/branding/save", data={"show_photos": "1"})
    a, b = _ticket(db_session), _ticket(db_session)
    pa = _photo(db_session, a)
    assert anon_client.get(f"/s/{b.status_token}/photo/{pa.id}").status_code == 404
    assert anon_client.get(f"/s/{a.status_token}/photo/{pa.id}").status_code == 200
    assert anon_client.get(f"/s/short/photo/{pa.id}").status_code == 404
    assert anon_client.get(f"/s/{a.status_token}/photo/no-such-photo").status_code == 404


def test_a_non_image_in_the_photo_table_is_never_served(owner_client, anon_client, db_session):
    owner_client.post("/branding/save", data={"show_photos": "1"})
    r = _ticket(db_session)
    p = RepairPhoto(repair_id=r.id, label="front", mime="text/html", width=1, height=1, data=b"<script>alert(1)</script>", size=10, staff_name="t")
    db_session.add(p)
    db_session.commit()
    assert anon_client.get(f"/s/{r.status_token}/photo/{p.id}").status_code == 404


def test_guessing_photo_links_trips_the_same_rate_limit(owner_client, anon_client, db_session):
    owner_client.post("/branding/save", data={"show_photos": "1"})
    codes = [anon_client.get(f"/s/wrong-code-{i:04d}/photo/x").status_code for i in range(40)]
    assert 429 in codes


# ── the quote page ──────────────────────────────────────────────────────────

def test_quote_page_is_branded_and_still_works(owner_client, anon_client, db_session):
    owner_client.post("/branding/logo", files={"file": ("a.png", png(), "image/png")})
    owner_client.post("/branding/save", data={"color": "#cc3300", "hours": "Open daily 9-5"})
    r = _ticket(db_session)
    q = Q.create(db_session, None, r, "129.99", "Battery swap", "90-day warranty", 7)
    db_session.commit()
    html = anon_client.get(f"/q/{q.token}").text
    assert "--brand:#cc3300" in html and "/brand/logo?v=" in html and "Open daily 9-5" in html
    assert "$129.99" in html and "Approve this quote" in html and "Decline" in html and "Your repair quote" in html
    ans = anon_client.post(f"/q/{q.token}", data={"decision": "approve", "comment": "ok"}, follow_redirects=True)
    assert "Approved" in ans.text and "cp-wrap" in ans.text
    nf = anon_client.get("/q/nope-nope-nope")
    assert nf.status_code == 404 and "couldn't find that quote" in _html.unescape(nf.text) and "cp-wrap" in nf.text


# ── preview ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("view", ["progress", "ready", "waiting", "received", "lookup", "quote", "quote-approved", "bogus"])
def test_every_preview_renders_with_sample_data_only(owner_client, db_session, view):
    r = _ticket(db_session)
    page = owner_client.get("/branding/preview", params={"view": view})
    assert page.status_code == 200 and "Preview:" in page.text and "cp-wrap" in page.text
    assert PHONE not in page.text and f"#{r.ticket_no}" not in page.text and "Bea" not in page.text
    if view in ("quote", "lookup"):
        assert "disabled" in page.text


def test_branding_screen_and_settings_link(owner_client):
    page = owner_client.get("/branding").text
    assert "Upload logo" in page and 'name="color"' in page and 'name="hours"' in page and 'name="show_photos"' in page
    assert "Off by default" in page and 'action="/branding/save"' in page
    assert "/branding" in owner_client.get("/settings").text


def test_logo_and_settings_travel_in_a_backup(owner_client, db_session):
    from app import backup as Bk
    owner_client.post("/branding/logo", files={"file": ("a.png", png(), "image/png")})
    doc = Bk.build_doc(db_session, include_secrets=False)
    assert "shop_logo" in doc["tables"] and len(doc["tables"]["shop_logo"]) == 1
