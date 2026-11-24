"""
v49 — repair check-in (condition checklist) and device photos.

Runs on SQLite and on PostgreSQL. Pictures are made by hand (a few header bytes, or a tiny real PNG built with
zlib) so the suite needs no imaging library; the browser pass uses real phone-sized JPEGs.
"""
import csv
import io
import json
import re
import struct
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect

from app import backup as B
from app import intake as I
from app.database import engine
from app.main import get_setting, set_setting
from app.models import AuditLog, Customer, Repair, RepairPhoto, Setting, Staff

ROOT = Path(__file__).resolve().parent.parent


# ═══ pictures made by hand ════════════════════════════════════════════

def png(w=4, h=3, pad=0):
    """A real, decodable PNG (grey), optionally padded with an extra chunk to reach a size."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(raw))
    if pad:
        out += chunk(b"tEXt", b"Comment\x00" + b"x" * pad)
    return out + chunk(b"IEND", b"")


def jpeg(w=640, h=480, progressive=False, app_segments=2):
    """Header-only JPEG: enough for the server to read its size; the browser pass uses real ones."""
    out = b"\xff\xd8"
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    for _ in range(app_segments):
        out += b"\xff\xe1" + struct.pack(">H", 10) + b"Exif\x00\x00\x00\x00"
    sof = b"\xff\xc2" if progressive else b"\xff\xc0"
    out += sof + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", h, w) + b"\x01\x01\x11\x00"
    out += b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00" + b"\x00" * 16 + b"\xff\xd9"
    return out


def webp(kind="VP8X", w=300, h=200):
    if kind == "VP8X":
        body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    elif kind == "VP8L":
        bits = (w - 1) | ((h - 1) << 14)
        body = b"VP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", bits)
    else:   # lossy "VP8 "
        body = b"VP8 " + struct.pack("<I", 14) + b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", w, h) + b"\x00" * 4
    body += b"\x00" * 16
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


# ═══ helpers ═════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean(db_session):
    yield
    db_session.rollback()
    for repair in db_session.query(Repair).filter(Repair.device.like("V49%") | Repair.device.like("=V49%")).all():
        db_session.query(RepairPhoto).filter(RepairPhoto.repair_id == repair.id).delete()
        db_session.delete(repair)
    db_session.commit()
    db_session.query(RepairPhoto).filter(RepairPhoto.repair_id.like("v49-%")).delete(synchronize_session=False)
    for key in ("repair_terms",):
        row = db_session.get(Setting, key)
        if row:
            db_session.delete(row)
    db_session.commit()
    db_session.expire_all()


FULL_CHECKIN = {
    "chk_powers_on": "ok", "chk_screen": "issue", "chk_body": "issue", "chk_charging": "ok", "chk_camera": "na",
    "acc_case": "1", "acc_sim": "1",
    "lock_passcode": "given", "lock_find_my": "off", "intake_data_warning": "1", "intake_notes": "Small dent top left",
}


def make_ticket(client, db, device="V49 Phone", phone="555-4900", extra=None, follow=False):
    data = {"phone": phone, "name": "V49 Customer", "device": device, "issue": "Cracked screen", "description": "dropped",
            "estimated_cost": "120", "warranty_days": "90"}
    data.update(extra or {})
    resp = client.post("/repairs/add", data=data, follow_redirects=follow)
    db.expire_all()
    repair = db.query(Repair).filter(Repair.device == device).order_by(Repair.ticket_no.desc()).first()
    return resp, repair


def upload(client, repair_id, data=None, name="shot.jpg", mime="image/jpeg", label="front", thumb=None):
    files = {"file": (name, data if data is not None else png(), mime)}
    if thumb is not None:
        files["thumb"] = ("thumb.jpg", thumb, "image/png")
    return client.post(f"/repairs/{repair_id}/photos", data={"label": label}, files=files)


def raw_photo(db, repair_id, label="front", data=None, thumb=None, **kw):
    data = data or png()
    photo = RepairPhoto(repair_id=repair_id, label=label, mime="image/png", width=4, height=3, data=data, thumb=thumb,
                        size=len(data), staff_name="Test", **kw)
    db.add(photo)
    db.commit()
    return photo.id


def actions(db):
    db.expire_all()
    return [a.action for a in db.query(AuditLog).order_by(AuditLog.ts).all()]


# ═══ the checklist: turning form fields into clean data ═══════════════

def test_nothing_filled_in_means_no_checklist():
    assert I.normalise({}) is None
    assert I.normalise({"chk_screen": "", "lock_passcode": "", "intake_notes": "   "}) is None
    assert I.normalise({"chk_screen": "bogus", "lock_passcode": "1234", "lock_find_my": "maybe"}) is None


def test_a_full_checklist_is_cleaned_and_stamped():
    now = datetime(2031, 5, 6, 7, 8, 9)
    data = I.normalise(dict(FULL_CHECKIN), "Mohammed", now)
    assert data["checks"] == {"powers_on": "ok", "screen": "issue", "body": "issue", "charging": "ok", "camera": "na"}
    assert data["left"] == ["case", "sim"]
    assert data["passcode"] == "given" and data["find_my"] == "off" and data["data_warning"] is True
    assert data["notes"] == "Small dent top left" and data["by"] == "Mohammed" and data["at"] == "2031-05-06T07:08:09"


def test_unknown_or_hostile_values_are_ignored():
    data = I.normalise({"chk_screen": "issue", "chk_nonsense": "issue", "acc_gun": "1", "acc_case": "off", "acc_sim": "false",
                        "lock_passcode": "<script>", "lock_find_my": "ON", "intake_data_warning": "0"})
    assert data["checks"] == {"screen": "issue"}
    assert data["left"] == [] and data["passcode"] == "" and data["find_my"] == "" and data["data_warning"] is False


def test_the_passcode_itself_can_never_be_stored():
    data = I.normalise({"lock_passcode": "482913", "chk_screen": "ok"})
    assert data["passcode"] == ""                                  # only given / none / not given are accepted
    assert "482913" not in json.dumps(data)


def test_notes_are_one_line_limited_and_card_numbers_are_masked():
    data = I.normalise({"intake_notes": "line one\n\n   line two\t" + "x" * 900})
    assert "\n" not in data["notes"] and data["notes"].startswith("line one line two") and len(data["notes"]) == I.NOTES_MAX
    masked = I.normalise({"intake_notes": "customer paid with 4111 1111 1111 1111 earlier"})["notes"]
    assert "4111 1111 1111 1111" not in masked and "1111" in masked


def test_load_survives_missing_or_damaged_data():
    assert I.load("") == I.empty()
    assert I.load("{not json") == I.empty()
    assert I.load("[1, 2]") == I.empty()
    assert I.load(json.dumps({"checks": "oops", "left": "oops", "at": "2031-01-01T00:00:00"}))["checks"] == {}
    original = I.normalise(dict(FULL_CHECKIN), "Z")
    assert I.load(json.dumps(original)) == original
    assert I.has_checklist(original) and not I.has_checklist(I.empty())


def test_rows_come_in_a_fixed_order_with_plain_words():
    data = I.normalise({"chk_screen": "issue", "chk_powers_on": "ok", "chk_battery": "weird"})
    rows = I.rows(data)
    assert [r["key"] for r in rows] == [c[0] for c in I.CHECKS] and len(rows) == 9
    by_key = {r["key"]: r for r in rows}
    assert by_key["screen"]["state"] == "issue" and by_key["screen"]["words"] == I.CHECKS[1][3]
    assert by_key["powers_on"]["state"] == "ok"
    assert by_key["battery"]["state"] == "na" and by_key["battery"]["words"] == "Not tested"
    assert I.problems(data) == [I.CHECKS[1][1]]


def test_labels_for_accessories_passcode_and_find_my():
    data = I.normalise({"acc_charger": "1", "acc_case": "1", "lock_passcode": "none", "lock_find_my": "unknown"})
    assert I.accessory_labels(data) == ["Case", "Charger / cable"]                 # always in the form's order
    assert I.passcode_label(data) == "No passcode" and I.find_my_label(data) == "Don't know"
    assert I.passcode_label(I.empty()) == "" and I.find_my_label(I.empty()) == ""


# ═══ telling what is really a picture ═════════════════════════════════

def test_png_jpeg_and_webp_are_recognised_with_their_size():
    assert I.sniff_image(png(40, 30)) == ("image/png", 40, 30)
    assert I.sniff_image(jpeg(1600, 1200)) == ("image/jpeg", 1600, 1200)
    assert I.sniff_image(jpeg(800, 600, progressive=True)) == ("image/jpeg", 800, 600)
    assert I.sniff_image(jpeg(321, 123, app_segments=6)) == ("image/jpeg", 321, 123)
    assert I.sniff_image(webp("VP8X", 300, 200)) == ("image/webp", 300, 200)
    assert I.sniff_image(webp("VP8L", 300, 200)) == ("image/webp", 300, 200)
    assert I.sniff_image(webp("VP8 ", 300, 200)) == ("image/webp", 300, 200)


@pytest.mark.parametrize("blob", [
    b"", b"GIF89a" + b"\x00" * 40, b"<html><script>alert(1)</script></html>" + b" " * 30, b"%PDF-1.4" + b"\x00" * 40,
    b"MZ" + b"\x00" * 60, b"\x89PNG\r\n\x1a\n" + b"\x00" * 30,                      # PNG signature but no IHDR
    b"\xff\xd8\xff" + b"\x00" * 30,                                                  # JPEG start, no frame header
    b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x00" * 30, jpeg()[:20],
])
def test_things_that_are_not_pictures_are_refused(blob):
    assert I.sniff_image(blob) is None


def test_absurd_picture_sizes_are_refused():
    assert I.sniff_image(png(9000, 10)) is None                  # wider than the limit
    assert I.sniff_image(png(10, 9000)) is None
    assert I.sniff_image(jpeg(30000, 30000)) is None
    assert I.sniff_image(jpeg(0, 0)) is None
    assert I.sniff_image(png(I.MAX_SIDE, 1)) is not None


def test_download_file_names_are_safe():
    name = I.photo_file_name(1234, "../../etc/passwd", "abcdef0123456789", "image/png")
    assert name.startswith("ticket-1234/") and ".." not in name and name.endswith(".png") and "/" not in name.split("/", 1)[1]
    assert I.photo_file_name(5, "", "xyz", "weird/type").endswith(".bin")


# ═══ creating a ticket with a check-in ════════════════════════════════

def test_new_ticket_with_checkin_is_saved_and_sent_to_the_ticket(owner_client, db_session):
    resp, repair = make_ticket(owner_client, db_session, extra=FULL_CHECKIN)
    assert resp.status_code == 303 and resp.headers["location"] == f"/repairs/{repair.id}#photos"
    data = I.load(repair)
    owner_name = db_session.query(Staff).filter(Staff.role == "owner").first().name
    assert data["checks"]["screen"] == "issue" and data["left"] == ["case", "sim"] and data["by"] == owner_name
    assert data["at"] and data["notes"] == "Small dent top left"
    assert any("check-in recorded" in (a.detail or "") for a in db_session.query(AuditLog).order_by(AuditLog.ts.desc()).limit(8))


def test_new_ticket_without_checkin_still_works_and_stores_nothing(owner_client, db_session):
    resp, repair = make_ticket(owner_client, db_session, device="V49 Plain", phone="555-4901")
    assert resp.status_code == 303 and resp.headers["location"] == f"/repairs/{repair.id}#photos"
    assert (repair.intake_json or "") == ""
    page = owner_client.get(f"/repairs/{repair.id}")
    assert page.status_code == 200 and "No check-in was recorded for this ticket." in page.text and 'id="photos"' in page.text


def test_ticket_page_shows_the_checkin_and_the_photo_area(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Page", phone="555-4902", extra=FULL_CHECKIN)
    html = owner_client.get(f"/repairs/{repair.id}").text
    assert 'id="photos"' in html and 'id="checkin"' in html and 'id="lightbox"' in html
    assert "/static/intake.js" in html and f'data-repair="{repair.id}"' in html
    assert "Small dent top left" in html
    assert "Find My" in html


def test_find_my_on_shows_a_red_warning(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Locked", phone="555-4903",
                            extra={"chk_screen": "ok", "lock_find_my": "on"})
    html = owner_client.get(f"/repairs/{repair.id}").text
    assert "Find My / Google lock is ON" in html and "badge-red" in html
    _, calm = make_ticket(owner_client, db_session, device="V49 Unlocked", phone="555-4904",
                          extra={"chk_screen": "ok", "lock_find_my": "off"})
    assert "Find My / Google lock is ON" not in owner_client.get(f"/repairs/{calm.id}").text


def test_card_number_in_a_new_ticket_note_is_masked(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Pan", phone="555-4905",
                            extra={"chk_screen": "ok", "intake_notes": "pay 4111111111111111 later"})
    assert "4111111111111111" not in repair.intake_json


def test_the_new_ticket_form_offers_the_checklist(owner_client):
    html = owner_client.get("/repairs").text
    for key, *_ in I.CHECKS:
        assert f'name="chk_{key}"' in html
    assert 'name="lock_passcode"' in html and 'name="lock_find_my"' in html and 'name="intake_notes"' in html


# ═══ editing the check-in later ═══════════════════════════════════════

def test_checkin_can_be_edited_and_cleared(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Edit", phone="555-4906")
    resp = owner_client.post(f"/repairs/{repair.id}/intake", data=dict(FULL_CHECKIN), follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/repairs/{repair.id}#checkin"
    db_session.expire_all()
    assert I.load(db_session.get(Repair, repair.id))["checks"]["screen"] == "issue"
    assert "REPAIR_INTAKE" in actions(db_session)
    owner_client.post(f"/repairs/{repair.id}/intake", data={"chk_screen": "ok"}, follow_redirects=False)
    db_session.expire_all()
    assert I.load(db_session.get(Repair, repair.id))["checks"] == {"screen": "ok"}
    owner_client.post(f"/repairs/{repair.id}/intake", data={}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(Repair, repair.id).intake_json == ""


def test_checkin_routes_need_a_login_and_a_real_ticket(anon_client, owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Auth", phone="555-4907")
    resp = anon_client.post(f"/repairs/{repair.id}/intake", data=dict(FULL_CHECKIN), follow_redirects=False)
    assert resp.status_code == 303 and "/login" in resp.headers["location"]
    db_session.expire_all()
    assert (db_session.get(Repair, repair.id).intake_json or "") == ""
    resp = owner_client.post("/repairs/does-not-exist/intake", data=dict(FULL_CHECKIN), follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/repairs"


@pytest.mark.parametrize("client_name", ["cashier_client", "technician_client", "manager_client"])
def test_every_role_can_record_a_checkin(request, owner_client, db_session, client_name):
    client = request.getfixturevalue(client_name)
    _, repair = make_ticket(owner_client, db_session, device=f"V49 {client_name}", phone="555-4908")
    resp = client.post(f"/repairs/{repair.id}/intake", data={"chk_screen": "issue"}, follow_redirects=False)
    assert resp.status_code == 303 and "login" not in resp.headers["location"]
    db_session.expire_all()
    assert I.load(db_session.get(Repair, repair.id))["checks"] == {"screen": "issue"}


# ═══ uploading photos ═════════════════════════════════════════════════

def test_a_photo_can_be_uploaded_with_a_thumbnail(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Up", phone="555-4910")
    body = png(40, 30)
    small = png(8, 6)
    r = upload(owner_client, repair.id, body, label="damage", thumb=small)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["count"] == 1 and out["max"] == I.MAX_PHOTOS and out["id"]
    assert r.headers["cache-control"] == "no-store"
    db_session.expire_all()
    photo = db_session.get(RepairPhoto, out["id"])
    assert bytes(photo.data) == body and bytes(photo.thumb) == small
    assert (photo.mime, photo.width, photo.height, photo.label, photo.size) == ("image/png", 40, 30, "damage", len(body))
    assert photo.repair_id == repair.id and photo.staff_name
    assert "REPAIR_PHOTO" in actions(db_session)


def test_the_type_is_decided_by_the_bytes_not_the_file_name(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Sniff", phone="555-4911")
    r = upload(owner_client, repair.id, png(), name="holiday.pdf", mime="application/pdf")
    assert r.status_code == 200
    photo = db_session.get(RepairPhoto, r.json()["id"])
    assert photo.mime == "image/png"
    for data in (b"<html><body>hello</body></html>" * 3, b"MZ" + b"\x00" * 100, b"plain text" * 10):
        bad = upload(owner_client, repair.id, data, name="x.jpg", mime="image/jpeg")
        assert bad.status_code == 400 and bad.json()["ok"] is False
    db_session.expire_all()
    assert db_session.query(RepairPhoto).filter(RepairPhoto.repair_id == repair.id).count() == 1


def test_unknown_labels_fall_back_to_other(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Lbl", phone="555-4912")
    r = upload(owner_client, repair.id, png(), label="<script>")
    assert r.status_code == 200 and db_session.get(RepairPhoto, r.json()["id"]).label == "other"


def test_a_thumbnail_that_is_not_a_picture_is_dropped_but_the_photo_is_kept(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Thumb", phone="555-4913")
    files = {"file": ("a.png", png(), "image/png"), "thumb": ("t.jpg", b"<html>nope</html>" * 4, "image/jpeg")}
    r = owner_client.post(f"/repairs/{repair.id}/photos", data={"label": "front"}, files=files)
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(RepairPhoto, r.json()["id"]).thumb is None


def test_a_ticket_holds_at_most_twelve_photos(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Cap", phone="555-4914")
    for n in range(I.MAX_PHOTOS):
        r = upload(owner_client, repair.id, png(4 + n, 3))
        assert r.status_code == 200 and r.json()["count"] == n + 1
    r = upload(owner_client, repair.id, png())
    assert r.status_code == 409 and r.json()["ok"] is False and "12" in r.json()["error"]
    assert db_session.query(RepairPhoto).filter(RepairPhoto.repair_id == repair.id).count() == I.MAX_PHOTOS


def test_photos_over_six_megabytes_are_refused(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Big", phone="555-4915")
    big = png(10, 10, pad=I.MAX_PHOTO_BYTES + 10)
    assert len(big) > I.MAX_PHOTO_BYTES
    r = upload(owner_client, repair.id, big)
    assert r.status_code == 413 and r.json()["ok"] is False
    ok = png(10, 10, pad=I.MAX_PHOTO_BYTES - 1000)
    assert len(ok) <= I.MAX_PHOTO_BYTES
    assert upload(owner_client, repair.id, ok).status_code == 200


def test_uploads_to_a_missing_ticket_or_when_signed_out_are_refused(anon_client, owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Gone", phone="555-4916")
    r = upload(owner_client, "no-such-ticket")
    assert r.status_code == 404 and r.json()["ok"] is False
    r = upload(anon_client, repair.id)
    assert r.status_code == 401 and r.json()["ok"] is False
    assert db_session.query(RepairPhoto).filter(RepairPhoto.repair_id == repair.id).count() == 0


@pytest.mark.parametrize("client_name", ["cashier_client", "technician_client", "manager_client"])
def test_any_signed_in_role_can_add_photos(request, owner_client, db_session, client_name):
    client = request.getfixturevalue(client_name)
    _, repair = make_ticket(owner_client, db_session, device=f"V49 role {client_name}", phone="555-4917")
    assert upload(client, repair.id).status_code == 200


# ═══ showing photos ═══════════════════════════════════════════════════

def test_photos_are_only_shown_to_signed_in_staff(anon_client, owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Show", phone="555-4920")
    body, small = png(40, 30), png(8, 6)
    pid = upload(owner_client, repair.id, body, thumb=small).json()["id"]
    assert anon_client.get(f"/repairs/{repair.id}/photos/{pid}").status_code == 401
    assert anon_client.get(f"/repairs/{repair.id}/photos/{pid}/thumb").status_code == 401
    full = owner_client.get(f"/repairs/{repair.id}/photos/{pid}")
    assert full.status_code == 200 and full.content == body and full.headers["content-type"] == "image/png"
    assert full.headers["x-content-type-options"] == "nosniff" and "private" in full.headers["cache-control"]
    assert "public" not in full.headers["cache-control"]
    thumb = owner_client.get(f"/repairs/{repair.id}/photos/{pid}/thumb")
    assert thumb.status_code == 200 and thumb.content == small


def test_a_missing_thumbnail_falls_back_to_the_full_picture(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 NoThumb", phone="555-4921")
    pid = raw_photo(db_session, repair.id, data=png(12, 9), thumb=None)
    r = owner_client.get(f"/repairs/{repair.id}/photos/{pid}/thumb")
    assert r.status_code == 200 and r.content == png(12, 9)


def test_a_photo_cannot_be_read_through_another_ticket(owner_client, db_session):
    _, a = make_ticket(owner_client, db_session, device="V49 A", phone="555-4922")
    _, b = make_ticket(owner_client, db_session, device="V49 B", phone="555-4923")
    pid = raw_photo(db_session, a.id)
    assert owner_client.get(f"/repairs/{b.id}/photos/{pid}").status_code == 404
    assert owner_client.get(f"/repairs/{a.id}/photos/nope").status_code == 404
    assert owner_client.get(f"/repairs/{a.id}/photos/{pid}").status_code == 200


def test_ticket_page_lists_thumbnails_without_loading_the_pictures(owner_client, db_session):
    from app.intake_routes import photo_list
    _, repair = make_ticket(owner_client, db_session, device="V49 Defer", phone="555-4924")
    pid = raw_photo(db_session, repair.id, data=png(30, 20), thumb=png(6, 4))
    db_session.expire_all()
    photos = photo_list(db_session, repair.id)
    assert [p.id for p in photos] == [pid]
    assert {"data", "thumb"} <= set(sa_inspect(photos[0]).unloaded)
    html = owner_client.get(f"/repairs/{repair.id}").text
    assert f"/repairs/{repair.id}/photos/{pid}/thumb" in html and f"/repairs/{repair.id}/photos/{pid}" in html


# ═══ deleting photos ═════════════════════════════════════════════════

def test_only_owner_and_manager_can_delete_photos(owner_client, manager_client, cashier_client, technician_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Del", phone="555-4930")
    pid = raw_photo(db_session, repair.id)
    for client in (cashier_client, technician_client):
        r = client.post(f"/repairs/{repair.id}/photos/{pid}/delete", follow_redirects=False)
        assert r.status_code == 403
    db_session.expire_all()
    assert db_session.get(RepairPhoto, pid) is not None
    r = manager_client.post(f"/repairs/{repair.id}/photos/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/repairs/{repair.id}#photos"
    db_session.expire_all()
    assert db_session.get(RepairPhoto, pid) is None
    assert "REPAIR_PHOTO" in actions(db_session)
    pid2 = raw_photo(db_session, repair.id)
    assert owner_client.post(f"/repairs/{repair.id}/photos/{pid2}/delete", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(RepairPhoto, pid2) is None


def test_deleting_a_photo_that_is_already_gone_is_harmless(owner_client, anon_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Del2", phone="555-4931")
    r = owner_client.post(f"/repairs/{repair.id}/photos/nope/delete", follow_redirects=False)
    assert r.status_code == 303
    pid = raw_photo(db_session, repair.id)
    r = anon_client.post(f"/repairs/{repair.id}/photos/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    db_session.expire_all()
    assert db_session.get(RepairPhoto, pid) is not None


def test_the_delete_button_is_only_offered_to_those_who_may_use_it(owner_client, cashier_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Btn", phone="555-4932")
    raw_photo(db_session, repair.id)
    owner_html = owner_client.get(f"/repairs/{repair.id}").text
    cashier_html = cashier_client.get(f"/repairs/{repair.id}").text
    assert 'id="lb-delete"' in owner_html and 'id="lb-delete"' not in cashier_html


# ═══ one zip of everything ════════════════════════════════════════════

def test_owner_can_download_every_photo_in_one_zip(owner_client, db_session):
    _, a = make_ticket(owner_client, db_session, device="V49 ZipA", phone="555-4940")
    _, b = make_ticket(owner_client, db_session, device="V49 ZipB", phone="555-4941")
    body_a, body_b = png(9, 9), jpeg(500, 400)
    raw_photo(db_session, a.id, label="front", data=body_a)
    pid_b = raw_photo(db_session, b.id, label="damage", data=body_b)
    db_session.get(RepairPhoto, pid_b).mime = "image/jpeg"
    db_session.commit()
    r = owner_client.get("/export/repair-photos")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/zip")
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.testzip() is None
    names = z.namelist()
    assert "index.csv" in names
    files = {n: z.read(n) for n in names if n != "index.csv"}
    assert body_a in files.values() and body_b in files.values()
    assert any(n.startswith(f"ticket-{a.ticket_no}/front-") and n.endswith(".png") for n in files)
    assert any(n.startswith(f"ticket-{b.ticket_no}/damage-") and n.endswith(".jpg") for n in files)
    index = list(csv.reader(io.StringIO(z.read("index.csv").decode("utf-8-sig"))))
    assert index[0][:3] == ["file", "ticket", "device"]
    assert any("V49 ZipA" in row for row in index[1:]) and any("V49 ZipB" in row for row in index[1:])
    assert "EXPORT" in actions(db_session)


def test_the_zip_is_owner_only(manager_client, cashier_client, technician_client, anon_client):
    assert manager_client.get("/export/repair-photos").status_code == 403
    assert cashier_client.get("/export/repair-photos").status_code == 403
    assert technician_client.get("/export/repair-photos").status_code == 403
    r = anon_client.get("/export/repair-photos", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_the_zip_works_with_no_photos_at_all(owner_client, db_session):
    db_session.query(RepairPhoto).delete()
    db_session.commit()
    r = owner_client.get("/export/repair-photos")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["index.csv"]


def test_csv_formulas_in_a_device_or_customer_name_cannot_run_from_the_index(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="=V49 HYPERLINK(1)", phone="555-4942",
                            extra={"name": "+cmd|' /C calc'!A0"})
    assert repair is not None
    raw_photo(db_session, repair.id)
    z = zipfile.ZipFile(io.BytesIO(owner_client.get("/export/repair-photos").content))
    rows = list(csv.reader(io.StringIO(z.read("index.csv").decode("utf-8-sig"))))
    mine = [r for r in rows if "V49 HYPERLINK" in r[2]]
    assert mine and mine[0][2].startswith("'=") and mine[0][3].startswith("'+")


# ═══ backups and restores ═════════════════════════════════════════════

def test_photos_are_left_out_of_the_backup_file(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Bk", phone="555-4950", extra=FULL_CHECKIN)
    raw_photo(db_session, repair.id, data=png(20, 20, pad=50000))
    assert "repair_photos" in B.EXCLUDED_TABLES
    doc = B.build_doc(db_session, include_secrets=False)
    assert "repair_photos" not in doc["tables"]
    assert b"\x89PNG" not in B.doc_bytes(doc) and b"iVBOR" not in B.doc_bytes(doc)          # no picture data in the file
    row = next(r for r in doc["tables"]["repairs"] if r["id"] == repair.id)
    assert I.load(row["intake_json"])["checks"]["screen"] == "issue"          # the checklist travels with the repair
    assert doc["app"].startswith("TechPro+ CRM v")


def test_restore_keeps_photos_of_repairs_that_survive_and_removes_the_rest(owner_client, db_session):
    _, keep = make_ticket(owner_client, db_session, device="V49 Keep", phone="555-4951")
    _, drop = make_ticket(owner_client, db_session, device="V49 Drop", phone="555-4952")
    kept_id = raw_photo(db_session, keep.id)
    gone_id = raw_photo(db_session, drop.id)
    stray_id = raw_photo(db_session, "v49-ghost-repair")                              # already an orphan
    doc = B.build_doc(db_session, include_secrets=False)
    doc["tables"]["repairs"] = [r for r in doc["tables"]["repairs"] if r["id"] != drop.id]   # that ticket is "newer than the backup"
    drop_id = drop.id
    effect = B.photo_effect(db_session, doc)
    assert effect["total"] >= 3 and effect["kept"] >= 1 and effect["removed"] >= 2
    owner = db_session.get(Staff, "s1") or db_session.query(Staff).filter(Staff.role == "owner").first()
    B.restore_full(db_session, doc, owner)
    db_session.expire_all()
    assert db_session.get(RepairPhoto, kept_id) is not None
    assert db_session.get(RepairPhoto, gone_id) is None
    assert db_session.get(RepairPhoto, stray_id) is None
    assert db_session.get(Repair, drop_id) is None and db_session.get(Repair, keep.id) is not None


def test_restore_preview_tells_the_owner_what_happens_to_photos(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Prev", phone="555-4953")
    raw_photo(db_session, repair.id)
    raw_photo(db_session, "v49-ghost-two")
    data, _, _ = B.make_backup(db_session, None)
    r = owner_client.post("/import/backup/preview", files={"file": ("b.json", data, "application/json")}, data={"password": ""})
    assert r.status_code == 200
    assert "Repair photos are not stored in backup files" in r.text and "will be deleted" in r.text


def test_photo_effect_is_none_when_there_are_no_photos(db_session):
    db_session.query(RepairPhoto).delete()
    db_session.commit()
    assert B.photo_effect(db_session, B.build_doc(db_session, False)) is None
    assert B.photo_effect(db_session, {"version": 1}) is None


# ═══ the printable sheet ══════════════════════════════════════════════

def test_the_sheet_prints_the_checkin_with_the_standard_terms(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Sheet", phone="555-4960", extra=FULL_CHECKIN)
    r = owner_client.get(f"/repairs/{repair.id}/intake-sheet")
    assert r.status_code == 200
    html = r.text
    assert f"#{repair.ticket_no}" in html and "V49 Sheet" in html and "V49 Customer" in html and "555-4960" in html
    assert "Small dent top left" in html and "Customer signature" in html and 'window.print()' in html
    assert I.DEFAULT_TERMS.split(";")[0][:40] in html


def test_the_sheet_escapes_anything_typed_into_it(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 <b>Bold</b>", phone="555-4961",
                            extra={"chk_screen": "issue", "intake_notes": "<script>alert(1)</script> & more"})
    html = owner_client.get(f"/repairs/{repair.id}/intake-sheet").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "<b>Bold</b>" not in html


def test_the_sheet_uses_the_owners_own_wording_when_set(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Terms", phone="555-4962", extra={"chk_screen": "ok"})
    r = owner_client.post("/settings/repair-terms", data={"terms": "Pay on pickup.   No refunds\non screens."}, follow_redirects=False)
    assert r.status_code == 303
    assert get_setting(db_session, "repair_terms", "") == "Pay on pickup. No refunds on screens."
    html = owner_client.get(f"/repairs/{repair.id}/intake-sheet").text
    assert "Pay on pickup. No refunds on screens." in html and "back up your data" not in html


def test_the_sheet_shows_at_most_six_photos_and_never_the_after_shots(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Six", phone="555-4963", extra={"chk_screen": "ok"})
    ids = [raw_photo(db_session, repair.id, label="front", data=png(4 + n, 3)) for n in range(8)]
    after = raw_photo(db_session, repair.id, label="after", data=png(30, 3))
    html = owner_client.get(f"/repairs/{repair.id}/intake-sheet").text
    shown = re.findall(r"/photos/([^/]+)/thumb", html)
    assert len(shown) == 6 and after not in shown and set(shown) <= set(ids)


def test_the_sheet_for_a_ticket_with_no_checkin_leaves_room_to_write(owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 Blank", phone="555-4964")
    html = owner_client.get(f"/repairs/{repair.id}/intake-sheet").text
    assert "No condition check-in was recorded" in html and "Customer signature" in html


def test_the_sheet_needs_a_login_and_a_real_ticket(anon_client, owner_client, db_session):
    _, repair = make_ticket(owner_client, db_session, device="V49 SheetAuth", phone="555-4965")
    r = anon_client.get(f"/repairs/{repair.id}/intake-sheet", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    r = owner_client.get("/repairs/nope/intake-sheet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/repairs"


# ═══ the wording setting ═════════════════════════════════════════════

def test_only_the_owner_can_change_the_sheet_wording(manager_client, cashier_client, anon_client, owner_client, db_session):
    assert manager_client.post("/settings/repair-terms", data={"terms": "hacked"}).status_code == 403
    assert cashier_client.post("/settings/repair-terms", data={"terms": "hacked"}).status_code == 403
    r = anon_client.post("/settings/repair-terms", data={"terms": "hacked"}, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert get_setting(db_session, "repair_terms", "") == ""
    owner_client.post("/settings/repair-terms", data={"terms": "Mine."}, follow_redirects=False)
    assert get_setting(db_session, "repair_terms", "") == "Mine."
    assert "SETTINGS" in actions(db_session)


def test_blank_wording_resets_to_the_standard_text_and_long_wording_is_refused(owner_client, db_session):
    owner_client.post("/settings/repair-terms", data={"terms": "Mine."}, follow_redirects=False)
    owner_client.post("/settings/repair-terms", data={"terms": "   "}, follow_redirects=False)
    assert get_setting(db_session, "repair_terms", "") == ""
    owner_client.post("/settings/repair-terms", data={"terms": "Kept."}, follow_redirects=False)
    owner_client.post("/settings/repair-terms", data={"terms": "x" * 1300}, follow_redirects=False)
    assert get_setting(db_session, "repair_terms", "") == "Kept."


def test_settings_page_has_the_photo_download_and_the_wording_box(owner_client, db_session):
    raw_photo(db_session, "v49-count-a")
    html = owner_client.get("/settings").text
    assert 'href="/export/repair-photos"' in html and 'action="/settings/repair-terms"' in html
    assert "photo" in html and "Photos are <strong>not</strong> inside the Full Backup file" in html
    assert I.DEFAULT_TERMS[:30] in html          # shown as the grey example


# ═══ database upgrade ═════════════════════════════════════════════════

def test_database_has_the_new_column_and_table_with_no_foreign_key():
    insp = sa_inspect(engine)
    assert "intake_json" in {c["name"] for c in insp.get_columns("repairs")}
    cols = {c["name"]: c for c in insp.get_columns("repair_photos")}
    assert {"id", "repair_id", "label", "mime", "width", "height", "data", "thumb", "size", "staff_name", "created_at"} <= set(cols)
    assert cols["data"]["nullable"] is False and cols["repair_id"]["nullable"] is False
    assert insp.get_foreign_keys("repair_photos") == []                     # so a restore can never be refused over a photo
    from app.seed import _COLUMN_DEFAULTS
    assert _COLUMN_DEFAULTS.get("intake_json") == "''"


# ═══ the pages and script that make it work on a phone ═════════════════

def test_every_form_in_the_new_pages_carries_the_security_token():
    for name in ("repair_detail.html", "repairs.html", "settings.html", "restore_confirm.html"):
        html = (ROOT / "templates" / name).read_text()
        forms = len(re.findall(r'<form[^>]*method="post"', html))
        assert forms <= html.count("csrf_token()"), name
    js = (ROOT / "static" / "intake.js").read_text()
    assert "X-CSRF-Token" in js and "data-csrf" in js
    assert 'data-csrf="{{ csrf_token() }}"' in (ROOT / "templates" / "repair_detail.html").read_text()


def test_the_script_only_talks_to_this_server_and_stores_nothing_in_the_browser():
    js = (ROOT / "static" / "intake.js").read_text()
    assert "http://" not in js and "https://" not in js
    assert "localStorage" not in js and "sessionStorage" not in js and "eval(" not in js


def test_printing_leaves_out_the_weak_pin_banner_and_never_prints_a_dark_page():
    base = (ROOT / "templates" / "base.html").read_text()
    banner = [line for line in base.splitlines() if "Your PIN is easy to guess" in line]
    assert banner and all("no-print" in line for line in banner)          # it used to print on every receipt and sheet
    css = (ROOT / "static" / "style.css").read_text()
    assert ":root{color-scheme:light!important}" in css[css.index("@media print{"):]
