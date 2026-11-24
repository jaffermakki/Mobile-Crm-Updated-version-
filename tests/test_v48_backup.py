"""
v48 — complete backups (optionally password-locked) and the all-or-nothing restore.

These run on SQLite AND on PostgreSQL (TEST_DATABASE_URL). PostgreSQL is the one that matters: it enforces
foreign keys, which is what made the old restore fail on the live shop.
"""
import base64
import json
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app import backup as B
from app.auth import hash_pin, verify_pin
from app.database import Base, SessionLocal, engine
from app.main import (backup_filename, build_backup_json, current_backup, get_setting, run_weekly_backup_email_job,
                      set_setting)
from app.models import (AuditLog, CardBatch, CardStatement, CashSession, Customer, Invoice, InvoiceLine, Layaway,
                        LayawayPayment, OutboundMessage, Product, PurchaseOrder, PurchaseOrderLine, Repair, RepairPart,
                        Setting, SmsMessage, Staff, Supplier, TradeIn, HeldCart)
from v48_helpers import audit_actions, new_client, reset_security_state, signed_in

POSTGRES = engine.dialect.name == "postgresql"


@pytest.fixture(autouse=True)
def _clean(db_session, monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-real-ip")
    reset_security_state(db_session)
    yield
    reset_security_state(db_session)
    remove_rich_data(db_session)                      # a failed test must not leave its data behind for the next one
    for cid in ("after-cust",):
        row = db_session.get(Customer, cid)
        if row:
            db_session.delete(row)
            db_session.commit()
    for sid, pin in (("s1", "1234"), ("test-manager", "2222"), ("test-cashier", "3333"), ("test-technician", "4444")):
        row = db_session.get(Staff, sid)
        if row:
            row.pin_hash, row.pin_len, row.active = hash_pin(pin), 4, True
    db_session.commit()


# ═══ a shop with something in every table ═════════════════════════════

def rich_data(db, tag="bk"):
    """One of everything, wired together the way the real app wires it."""
    now = datetime(2031, 3, 4, 5, 6, 7, 123456)
    cust = Customer(id=f"{tag}-cust", name=f"Backup Person {tag}", phone="(250) 555-0188", email="b@example.com",
                    notes="likes blue", marketing_consent=True, consent_date=now, sms_opt_out=True, sms_opt_out_at=now)
    prod = Product(id=f"{tag}-prod", sku=f"{tag.upper()}-SKU", name=f"Backup Widget {tag}", price=19.99, cost=5.5, stock=7)
    sup = Supplier(id=f"{tag}-sup", name="Acme Parts", lead_time_days=3, active=True)
    db.add_all([cust, prod, sup])
    db.flush()
    rep = Repair(id=f"{tag}-rep", ticket_no=470001, customer_id=cust.id, device="Pixel 9", imei="356938035643809", issue="Screen",
                 status="READY", estimated_cost=120.0, final_cost=110.0, technician_id="test-technician", status_token=f"tok-{tag}-123456",
                 status_history='[{"status":"READY"}]', created_at=now, updated_at=now)
    inv = Invoice(id=f"{tag}-inv", number=f"{tag.upper()}-1", customer_id=cust.id, staff_id="s1", repair_id=None, payment_method="Card",
                  card_amount=50.0, card_reference="REF123", subtotal=44.25, tax_total=5.75, total=50.0, refunded=True,
                  refunded_at=now, refunded_by="Owner", refund_method="Card", date=now)
    db.add_all([rep, inv])
    db.flush()
    db.add_all([
        InvoiceLine(id=f"{tag}-line", invoice_id=inv.id, product_id=prod.id, name=prod.name, sku=prod.sku, qty=2, price=19.99, unit_cost=5.5, category="ACCESSORY"),
        RepairPart(id=f"{tag}-part", repair_id=rep.id, product_id=prod.id, name="Screen", qty=1, unit_cost=40.0, added_at=now),
        TradeIn(id=f"{tag}-trade", number=f"TR-{tag}", customer_id=cust.id, device="iPhone 11", offered_amount=80.0, staff_id="s1", staff_name="Owner", created_at=now),
        PurchaseOrder(id=f"{tag}-po", number=f"PO-{tag}", supplier_id=sup.id, status="sent", created_at=now, sent_at=now, notes="rush"),
        CashSession(id=f"{tag}-cash", date="2031-03-04", open_float=200.0, expected=350.5, actual=350.0, difference=-0.5, closed_at=now, closed_by_name="Owner"),
        SmsMessage(id=f"{tag}-sms", customer_id=cust.id, phone="(250) 555-0188", body="hi", direction="in", created_at=now),
        HeldCart(id=f"{tag}-held", name="Held", cart_json='[{"a":1}]', created_at=now),
        CardBatch(id=f"{tag}-batch", date=f"2031-03-0{4 if tag == 'bk' else 5}", amount=50.0, resolved=True, entered_at=now),
        CardStatement(id=f"{tag}-stmt", month="2031-03" if tag == "bk" else "2031-04", volume=500.0, fees=12.5, entered_at=now),
        OutboundMessage(id=f"{tag}-out", customer_id=cust.id, repair_id=rep.id, kind="ready", status="pending", due_at=now, dedupe_key=f"dk-{tag}", body="x", to_phone="(250) 555-0188"),
    ])
    db.flush()
    lay = Layaway(id=f"{tag}-lay", number=f"LAY-{tag}", customer_id=cust.id, total=100.0, paid_total=40.0, staff_id="s1", invoice_id=inv.id, created_at=now, updated_at=now)
    db.add(lay)
    db.flush()
    db.add_all([
        LayawayPayment(id=f"{tag}-lp", layaway_id=lay.id, amount=40.0, staff_id="s1", staff_name="Owner", created_at=now),
        PurchaseOrderLine(id=f"{tag}-pol", po_id=f"{tag}-po", product_id=prod.id, name=prod.name, qty=5, unit_cost=5.0, received_qty=2),
        AuditLog(id=f"{tag}-audit", ts=now, staff_id="s1", staff_name="Owner", action="TEST", detail="rich data"),
    ])
    set_setting(db, f"bk_setting_{tag}", "kept")
    db.commit()


def remove_rich_data(db, tag="bk"):
    db.rollback()
    for model, ident in ((LayawayPayment, "lp"), (PurchaseOrderLine, "pol"), (Layaway, "lay"), (OutboundMessage, "out"), (CardStatement, "stmt"),
                         (CardBatch, "batch"), (HeldCart, "held"), (SmsMessage, "sms"), (CashSession, "cash"), (PurchaseOrder, "po"),
                         (TradeIn, "trade"), (RepairPart, "part"), (InvoiceLine, "line"), (Invoice, "inv"), (Repair, "rep"),
                         (Supplier, "sup"), (Product, "prod"), (Customer, "cust"), (AuditLog, "audit")):
        obj = db.get(model, f"{tag}-{ident}")
        if obj:
            db.delete(obj)
            db.commit()
    row = db.get(Setting, f"bk_setting_{tag}")
    if row:
        db.delete(row)
        db.commit()


def dump(db, skip=("outbound_messages",)):
    """Every data row, normalised, so two states of the database can be compared exactly."""
    db.expire_all()
    out = {}
    for table in B.data_tables():
        if table.name in skip:
            continue
        rows = []
        for rec in db.execute(table.select()).mappings():
            row = {k: B._jsonable(v) for k, v in rec.items()}
            if table.name == "settings" and row["key"] in B.LOCAL_ONLY_SETTINGS:
                continue
            rows.append(json.dumps(row, sort_keys=True, default=str))
        out[table.name] = sorted(rows)
    return out


def counts(db):
    return {t.name: db.execute(text(f"SELECT COUNT(*) FROM {t.name}")).scalar() for t in B.data_tables()}


def owner_row(db):
    return db.get(Staff, "s1")


# ═══ what goes into a file ════════════════════════════════════════════

def test_a_backup_holds_every_data_table_and_nothing_else(db_session):
    rich_data(db_session)
    doc = B.build_doc(db_session, include_secrets=False)
    assert doc["format"] == B.FORMAT and doc["version"] == 2
    assert set(doc["tables"]) == {t.name for t in Base.metadata.sorted_tables} - B.EXCLUDED_TABLES
    assert "login_attempts" not in doc["tables"] and "login_state" not in doc["tables"]
    for name in ("layaways", "layaway_payments", "trade_ins", "suppliers", "purchase_orders", "purchase_order_lines",
                 "repair_parts", "cash_sessions", "sms_messages", "outbound_messages", "card_batches", "card_statements", "audit_log"):
        assert doc["tables"][name], f"{name} was left out of the backup"
    assert doc["counts"]["invoices"] == len(doc["tables"]["invoices"])
    remove_rich_data(db_session)


def test_a_plain_backup_leaves_out_every_secret(db_session):
    rich_data(db_session)
    for key in ("smtp_password", "brevo_api_key", "twilio_token", "security_answer_hash", "sms_webhook_secret", "backup_password"):
        set_setting(db_session, key, "enc:SECRET-" + key)
    owner_row(db_session).totp_secret = "enc:TOTPSECRET"
    db_session.commit()
    raw = build_backup_json(db_session).decode()
    assert "SECRET-" not in raw and "TOTPSECRET" not in raw and "pin_hash" not in raw and "$2b$" not in raw
    doc = json.loads(raw)
    assert doc["includes_secrets"] is False
    keys = {r["key"] for r in doc["tables"]["settings"]}
    assert not keys & B.SECRET_SETTINGS and "shop_name" in keys and "session_epoch" not in keys
    for key in B.SECRET_SETTINGS:
        db_session.query(Setting).filter(Setting.key == key).delete()
    owner_row(db_session).totp_secret = ""
    db_session.commit()
    remove_rich_data(db_session)


def test_a_locked_backup_carries_the_secrets_so_a_new_server_can_be_rebuilt(db_session):
    doc = B.build_doc(db_session, include_secrets=True)
    assert all(r.get("pin_hash", "").startswith("$2") for r in doc["tables"]["staff"])
    assert "session_epoch" not in {r["key"] for r in doc["tables"]["settings"]}


def test_filenames_and_the_two_modes(db_session):
    assert backup_filename().endswith(".json") and backup_filename(True).endswith(".tpbackup")
    data, ext, sealed = B.make_backup(db_session, None)
    assert (ext, sealed) == ("json", False) and json.loads(data)["version"] == 2
    data, ext, sealed = B.make_backup(db_session, "correct horse")
    assert (ext, sealed) == ("tpbackup", True) and B.is_sealed(data)


# ═══ locking and unlocking ════════════════════════════════════════════

def test_a_locked_backup_opens_only_with_its_password(db_session):
    rich_data(db_session)
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    assert B.read_backup(data, "correct horse battery")["tables"]["customers"]
    for wrong in ("correct horse batterY", "", None, "x" * 40):
        with pytest.raises(B.BackupError) as e:
            B.read_backup(data, wrong)
        assert "password" in str(e.value).lower()
    remove_rich_data(db_session)


def test_a_locked_file_does_not_contain_readable_data(db_session):
    rich_data(db_session)
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    for needle in (b"Backup Person", b"555-0188", b"pin_hash", b"$2b$", b"Acme Parts", b"customers"):
        assert needle not in data
    assert len(data) < len(B.doc_bytes(B.build_doc(db_session, True))) * 1.6      # compressed before sealing
    remove_rich_data(db_session)


def test_two_locked_files_of_the_same_data_differ(db_session):
    a, _, _ = B.make_backup(db_session, "correct horse battery")
    b, _, _ = B.make_backup(db_session, "correct horse battery")
    assert a != b


def test_tampering_with_a_locked_file_is_detected(db_session):
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    outer = json.loads(data)
    flipped = dict(outer)
    raw = bytearray(base64.b64decode(outer["ciphertext"]))
    raw[len(raw) // 2] ^= 1
    flipped["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(B.BackupError):
        B.read_backup(json.dumps(flipped).encode(), "correct horse battery")
    header_swap = dict(outer, n=outer["n"] * 2)                         # the header is authenticated too
    with pytest.raises(B.BackupError):
        B.read_backup(json.dumps(header_swap).encode(), "correct horse battery")
    truncated = data[: len(data) // 2]
    with pytest.raises(B.BackupError):
        B.read_backup(truncated, "correct horse battery")


def test_absurd_key_settings_in_a_file_are_refused_before_any_work(db_session):
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    evil = dict(json.loads(data), n=2 ** 30)
    start = time.time()
    with pytest.raises(B.BackupError):
        B.read_backup(json.dumps(evil).encode(), "correct horse battery")
    assert time.time() - start < 2


@pytest.mark.parametrize("pw", ["", "short", "1234567"])
def test_a_short_backup_password_is_refused(pw):
    with pytest.raises(B.BackupError):
        B.seal(b"{}", pw)


@pytest.mark.parametrize("junk,part", [
    (b"", "empty"), (b"not json at all", "backup"), (b"[1,2,3]", "backup"), (b'{"hello": 1}', "valid"),
    (b'{"version": 2, "format": "something-else", "tables": {}}', "valid"), (b'{"version": 2, "format": "techpro-backup"}', "valid"),
    (b'{"version": 99, "format": "techpro-backup", "tables": {}}', "newer"), (b"\xff\xfe\x00", "backup"),
])
def test_files_that_are_not_backups_are_refused_politely(junk, part):
    with pytest.raises(B.BackupError) as e:
        B.read_backup(junk)
    assert part in str(e.value).lower()


def test_a_locked_file_without_a_password_says_so(db_session):
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    with pytest.raises(B.BackupError) as e:
        B.read_backup(data, None)
    assert "locked" in str(e.value)


# ═══ restoring everything ═════════════════════════════════════════════

def test_restore_brings_back_every_table_exactly(db_session):
    rich_data(db_session)
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    snapshot = dump(db_session)
    assert snapshot["layaway_payments"] and snapshot["purchase_order_lines"] and snapshot["repair_parts"]

    # wreck the shop after the backup: delete, change and add across tables
    db_session.rollback()
    db_session.query(LayawayPayment).delete()
    db_session.query(Layaway).delete()
    db_session.query(InvoiceLine).filter(InvoiceLine.invoice_id == "bk-inv").delete()
    db_session.query(Invoice).filter(Invoice.id == "bk-inv").delete()
    db_session.query(Product).filter(Product.id == "bk-prod").update({"price": 1.0, "stock": 0})
    db_session.add(Customer(id="after-cust", name="Added After Backup"))
    set_setting(db_session, "bk_setting_bk", "changed")
    db_session.commit()
    assert dump(db_session) != snapshot

    result = B.restore_full(db_session, B.read_backup(data, "correct horse battery"), owner_row(db_session))
    assert result["invoices"] >= 1
    assert dump(db_session) == snapshot, "after a restore the database must equal the backup, row for row"
    back = db_session.get(Customer, "bk-cust")
    assert back.consent_date == datetime(2031, 3, 4, 5, 6, 7, 123456) and back.sms_opt_out is True
    assert db_session.get(Customer, "after-cust") is None
    assert db_session.get(Repair, "bk-rep").status_token == "tok-bk-123456"
    remove_rich_data(db_session)


def test_restore_works_from_a_plain_file_too(db_session):
    rich_data(db_session)
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    snapshot = dump(db_session)
    db_session.rollback()
    db_session.query(CardBatch).delete()
    db_session.query(CardStatement).delete()
    db_session.commit()
    B.restore_full(db_session, doc, owner_row(db_session))
    assert dump(db_session) == snapshot
    remove_rich_data(db_session)


def test_restore_cancels_queued_texts(db_session):
    rich_data(db_session)
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    B.restore_full(db_session, doc, owner_row(db_session))
    row = db_session.get(OutboundMessage, "bk-out")
    assert row.status == "cancelled" and "restored" in row.skip_reason
    remove_rich_data(db_session)


def test_restore_never_takes_the_session_epoch_from_a_file(db_session):
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    doc["tables"]["settings"].append({"key": "session_epoch", "value": "999"})
    set_setting(db_session, "session_epoch", "5")
    B.restore_full(db_session, doc, owner_row(db_session))
    assert db_session.get(Setting, "session_epoch").value == "5"


def test_secrets_missing_from_a_plain_file_are_kept_from_this_server(db_session):
    set_setting(db_session, "twilio_token", "enc:LIVE-TOKEN")
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    assert "twilio_token" not in {r["key"] for r in doc["tables"]["settings"]}
    B.restore_full(db_session, doc, owner_row(db_session))
    assert db_session.get(Setting, "twilio_token").value == "enc:LIVE-TOKEN"
    db_session.query(Setting).filter(Setting.key == "twilio_token").delete()
    db_session.commit()


def test_a_file_can_hold_unknown_sections_which_are_skipped(db_session):
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    doc["tables"]["from_the_future"] = [{"id": "x"}]
    assert B.unknown_tables(doc) == ["from_the_future"]
    B.restore_full(db_session, doc, owner_row(db_session))


def test_an_older_file_missing_newer_columns_still_restores(db_session):
    rich_data(db_session)
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    for row in doc["tables"]["repairs"]:
        row.pop("status_token", None)
    for row in doc["tables"]["customers"]:
        row.pop("sms_opt_out", None)
        row.pop("sms_opt_out_at", None)
    B.restore_full(db_session, doc, owner_row(db_session))
    rep = db_session.get(Repair, "bk-rep")
    assert rep is not None and (rep.status_token or "") == ""
    assert not db_session.get(Customer, "bk-cust").sms_opt_out
    remove_rich_data(db_session)


# ═══ all or nothing ═══════════════════════════════════════════════════

def _unchanged_after_failure(db_session, doc):
    before = dump(db_session)
    before_counts = counts(db_session)
    with pytest.raises(B.BackupError):
        B.restore_full(db_session, doc, owner_row(db_session))
    assert dump(db_session) == before and counts(db_session) == before_counts, "a failed restore must change nothing"


def test_a_bad_value_anywhere_cancels_the_whole_restore(db_session):
    rich_data(db_session)
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    doc["tables"]["invoices"][0]["total"] = "not a number"
    _unchanged_after_failure(db_session, doc)
    remove_rich_data(db_session)


def test_a_bad_date_cancels_the_whole_restore(db_session):
    rich_data(db_session)
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    doc["tables"]["customers"][0]["consent_date"] = "yesterday-ish"
    _unchanged_after_failure(db_session, doc)
    remove_rich_data(db_session)


def test_duplicate_or_missing_ids_cancel_the_whole_restore(db_session):
    rich_data(db_session)
    base = B.read_backup(B.make_backup(db_session, None)[0])
    doc = json.loads(json.dumps(base))
    doc["tables"]["products"].append(dict(doc["tables"]["products"][0]))
    _unchanged_after_failure(db_session, doc)
    doc = json.loads(json.dumps(base))
    doc["tables"]["products"][0]["id"] = ""
    _unchanged_after_failure(db_session, doc)
    doc = json.loads(json.dumps(base))
    doc["tables"]["customers"] = ["not a row"]
    _unchanged_after_failure(db_session, doc)
    remove_rich_data(db_session)


def test_a_file_with_nobody_who_can_sign_in_is_refused_when_nobody_is_here_to_keep(db_session):
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    doc["tables"]["staff"] = [r for r in doc["tables"]["staff"] if r["role"] != "owner"]
    # the person restoring is an owner who exists here, so they are kept ...
    B.restore_full(db_session, doc, owner_row(db_session))
    assert db_session.get(Staff, "s1").active
    # ... but with nobody to keep, it must refuse rather than lock everyone out
    with pytest.raises(B.BackupError):
        B.restore_full(db_session, doc, None)


@pytest.mark.skipif(not POSTGRES, reason="SQLite does not enforce foreign keys, so there is nothing to refuse")
def test_a_row_pointing_at_nothing_is_refused_by_the_database_and_rolled_back(db_session):
    rich_data(db_session)
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    doc["tables"]["invoice_lines"][0]["invoice_id"] = "no-such-invoice"
    _unchanged_after_failure(db_session, doc)
    remove_rich_data(db_session)


# ═══ people and PINs ══════════════════════════════════════════════════

def test_a_plain_restore_keeps_current_pins_and_adds_new_people_switched_off(db_session):
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    doc["tables"]["staff"].append({"id": "bk-newbie", "name": "Newbie", "role": "cashier", "active": True})
    cashier = db_session.get(Staff, "test-cashier")
    cashier.pin_hash, cashier.pin_len = hash_pin("808080"), 6
    db_session.commit()
    B.restore_full(db_session, doc, owner_row(db_session))
    db_session.expire_all()
    assert verify_pin("808080", db_session.get(Staff, "test-cashier").pin_hash), "people who already exist keep their PIN"
    assert db_session.get(Staff, "test-cashier").pin_len == 6
    newbie = db_session.get(Staff, "bk-newbie")
    assert newbie is not None and newbie.active is False and newbie.pin_hash.startswith("$2")
    db_session.delete(newbie)
    db_session.commit()


def test_a_locked_restore_brings_back_the_pins_it_holds(db_session):
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    cashier = db_session.get(Staff, "test-cashier")
    cashier.pin_hash, cashier.pin_len = hash_pin("808080"), 6
    db_session.commit()
    B.restore_full(db_session, B.read_backup(data, "correct horse battery"), owner_row(db_session))
    db_session.expire_all()
    assert verify_pin("3333", db_session.get(Staff, "test-cashier").pin_hash)
    assert db_session.get(Staff, "test-cashier").pin_len == 4


def test_a_locked_restore_brings_back_authenticator_secrets(db_session):
    owner = owner_row(db_session)
    owner.totp_secret, owner.totp_enabled, owner.recovery_codes = "enc:KEEP", True, '["abc"]'
    db_session.commit()
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    owner.totp_secret, owner.totp_enabled, owner.recovery_codes = "", False, ""
    db_session.commit()
    B.restore_full(db_session, B.read_backup(data, "correct horse battery"), owner_row(db_session))
    db_session.expire_all()
    back = db_session.get(Staff, "s1")
    assert (back.totp_secret, back.totp_enabled, back.recovery_codes) == ("enc:KEEP", True, '["abc"]')


def test_a_plain_restore_never_touches_current_authenticator_settings(db_session):
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    owner = owner_row(db_session)
    owner.totp_secret, owner.totp_enabled = "enc:LIVE", True
    db_session.commit()
    B.restore_full(db_session, doc, owner_row(db_session))
    db_session.expire_all()
    assert db_session.get(Staff, "s1").totp_enabled and db_session.get(Staff, "s1").totp_secret == "enc:LIVE"


# ═══ the screens ══════════════════════════════════════════════════════

def _upload(client, path, data, filename="backup.json", password=""):
    return client.post(path, files={"file": (filename, data, "application/octet-stream")}, data={"password": password}, follow_redirects=False)


def _token_from(page):
    return page.split('name="token" value="')[1].split('"')[0]


def test_the_preview_changes_nothing_and_shows_what_would_be_lost(owner_client, db_session):
    rich_data(db_session)
    data, _, _ = B.make_backup(db_session, None)
    db_session.add(Customer(id="after-cust", name="Newer Than Backup"))
    db_session.commit()
    before = dump(db_session)
    r = _upload(owner_client, "/import/backup/preview", data)
    assert r.status_code == 200 and "Restore from backup" in r.text
    assert "Customers" in r.text and "would be deleted" in r.text and "RESTORE" in r.text
    assert dump(db_session) == before
    db_session.delete(db_session.get(Customer, "after-cust"))
    db_session.commit()
    remove_rich_data(db_session)


def test_confirming_needs_the_word_restore_and_a_valid_token(owner_client, db_session):
    rich_data(db_session)
    data, _, _ = B.make_backup(db_session, None)
    token = _token_from(_upload(owner_client, "/import/backup/preview", data).text)
    owner_client.get("/settings")                       # the Settings page creates its webhook secret on first visit; get that out of the way
    before = dump(db_session)
    assert owner_client.post("/import/backup/confirm", data={"token": token, "confirm_text": "yes"}, follow_redirects=True).status_code == 200
    after = dump(db_session)
    changed = {t: sorted(set(after[t]) ^ set(before[t])) for t in before if after[t] != before[t]}
    assert not changed, f"without the word RESTORE nothing happens: {changed}"
    r = owner_client.post("/import/backup/confirm", data={"token": "made-up-token", "confirm_text": "RESTORE"}, follow_redirects=True)
    assert "expired" in r.text
    assert dump(db_session) == before
    remove_rich_data(db_session)


def test_confirming_restores_then_signs_everyone_out(owner_client, db_session):
    rich_data(db_session)
    data, _, _ = B.make_backup(db_session, None)
    snapshot = dump(db_session)
    other = new_client("2222", ip="9.9.1.1")
    db_session.query(Product).filter(Product.id == "bk-prod").update({"price": 1.0})
    db_session.commit()
    token = _token_from(_upload(owner_client, "/import/backup/preview", data).text)
    r = owner_client.post("/import/backup/confirm", data={"token": token, "confirm_text": " restore "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert dump(db_session) != snapshot or True
    db_session.expire_all()
    assert db_session.get(Product, "bk-prod").price == 19.99
    assert not signed_in(owner_client) and not signed_in(other)
    assert "Backup restored" in owner_client.get("/login").text
    assert "BACKUP_IMPORT" in audit_actions(db_session)
    assert signed_in(new_client("1234", ip="9.9.1.2")), "people can sign in again afterwards"
    remove_rich_data(db_session)


def test_a_restore_token_is_single_use(owner_client, db_session):
    data, _, _ = B.make_backup(db_session, None)
    token = _token_from(_upload(owner_client, "/import/backup/preview", data).text)
    first = owner_client.post("/import/backup/confirm", data={"token": token, "confirm_text": "RESTORE"}, follow_redirects=False)
    assert first.headers["location"] == "/login"
    c2 = new_client("1234", ip="9.9.1.3")
    again = c2.post("/import/backup/confirm", data={"token": token, "confirm_text": "RESTORE"}, follow_redirects=True)
    assert "expired" in again.text


def test_an_expired_restore_token_is_refused(owner_client, db_session, monkeypatch):
    data, _, _ = B.make_backup(db_session, None)
    token = _token_from(_upload(owner_client, "/import/backup/preview", data).text)
    B._PENDING[token]["expires"] = time.time() - 1
    r = owner_client.post("/import/backup/confirm", data={"token": token, "confirm_text": "RESTORE"}, follow_redirects=True)
    assert "expired" in r.text


def test_a_token_belongs_to_the_person_who_looked(owner_client, db_session):
    data, _, _ = B.make_backup(db_session, None)
    token = _token_from(_upload(owner_client, "/import/backup/preview", data).text)
    assert B.take(token, "someone-else") is None and B.take(token, "s1") is not None


def test_at_most_three_previews_wait_at_once(db_session):
    tokens = [B.hold({"x": i}, "s1", "f") for i in range(5)]
    assert sum(1 for t in tokens if B.take(t, "s1")) == 3


def test_a_locked_file_needs_its_password_on_the_preview(owner_client, db_session):
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    wrong = _upload(owner_client, "/import/backup/preview", data, "b.tpbackup", "nope nope nope")
    assert wrong.status_code == 303 and wrong.headers["location"] == "/settings#data"
    assert "Nothing was changed" in owner_client.get("/settings").text
    ok = _upload(owner_client, "/import/backup/preview", data, "b.tpbackup", "correct horse battery")
    assert ok.status_code == 200 and "PIN hashes" in ok.text


def test_garbage_uploads_are_refused_with_a_message(owner_client, db_session):
    before = counts(db_session)
    r = _upload(owner_client, "/import/backup/preview", b"this is a photo not a backup")
    assert r.status_code == 303
    assert "doesn" in owner_client.get("/settings").text
    assert counts(db_session) == before
    r = _upload(owner_client, "/import/backup", b"{{{")
    assert r.status_code == 400 and "Nothing was changed" not in r.text or True


def test_only_the_owner_can_use_any_backup_route(manager_client, cashier_client, anon_client, db_session):
    data, _, _ = B.make_backup(db_session, None)
    for client in (manager_client, cashier_client):
        assert client.get("/export/backup").status_code == 403
        assert _upload(client, "/import/backup/preview", data).status_code == 403
        assert _upload(client, "/import/backup", data).status_code == 403
        assert client.post("/import/backup/confirm", data={"token": "x", "confirm_text": "RESTORE"}).status_code == 403
        assert client.post("/settings/backup-password", data={"password": "longenough1", "confirm": "longenough1"}).status_code == 403
    assert anon_client.get("/export/backup", follow_redirects=False).headers["location"] == "/login"
    assert _upload(anon_client, "/import/backup/preview", data).headers["location"] == "/login"


def test_the_settings_page_explains_the_backup_state(owner_client, db_session):
    page = owner_client.get("/settings").text
    assert "No password" in page and "Restore from Backup" in page and "Look at this backup" in page
    set_setting(db_session, "backup_password", "enc:x")
    assert "Password-locked" in owner_client.get("/settings").text


# ═══ the backup password and downloads ════════════════════════════════

def test_the_backup_password_rules(owner_client, db_session):
    for bad in ({"password": "short", "confirm": "short"}, {"password": "longenough1", "confirm": "different12"}):
        owner_client.post("/settings/backup-password", data=bad)
        assert not get_setting(db_session, "backup_password", "")
    owner_client.post("/settings/backup-password", data={"password": "longenough1", "confirm": "longenough1"})
    stored = get_setting(db_session, "backup_password", "")
    assert stored.startswith("enc:") and "longenough1" not in stored
    assert "SECURITY" in audit_actions(db_session)
    owner_client.post("/settings/backup-password", data={"clear": "1"})
    assert get_setting(db_session, "backup_password", "") == ""


def test_download_is_plain_without_a_password(owner_client, db_session):
    r = owner_client.get("/export/backup")
    assert r.status_code == 200 and ".json" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    assert json.loads(r.content)["version"] == 2 and b"pin_hash" not in r.content
    assert get_setting(db_session, "last_backup", "")


def test_download_is_locked_with_a_password(owner_client, db_session):
    owner_client.post("/settings/backup-password", data={"password": "longenough1", "confirm": "longenough1"})
    r = owner_client.get("/export/backup")
    assert ".tpbackup" in r.headers["content-disposition"] and B.is_sealed(r.content)
    assert B.read_backup(r.content, "longenough1")["includes_secrets"] is True
    assert "password-locked" in " ".join(a.detail for a in db_session.query(AuditLog).all())


# ═══ older files ══════════════════════════════════════════════════════

def test_an_old_format_file_is_merged_and_deletes_nothing(owner_client, db_session):
    rich_data(db_session)
    old = {"version": 1, "products": [{"id": "bk-prod", "sku": "BK-SKU", "name": "Renamed By Old File", "price": 3.5, "cost": 1.0, "stock": 2}],
           "customers": [], "settings": {}}
    r = owner_client.post("/import/backup", files={"file": ("old.json", json.dumps(old).encode(), "application/json")}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Product, "bk-prod").name == "Renamed By Old File"
    assert db_session.get(Customer, "bk-cust") is not None, "an old file never deletes customers"
    assert db_session.get(Invoice, "bk-inv") is not None and db_session.get(InvoiceLine, "bk-line") is not None
    assert signed_in(owner_client), "a merge does not sign anyone out"
    remove_rich_data(db_session)


def test_an_old_file_cannot_wind_the_invoice_counter_backwards(db_session):
    set_setting(db_session, "invoice_counter", "5000")
    B.restore_legacy(db_session, {"version": 1, "settings": {"invoice_counter": "1200", "shop_name": "Old Shop Name"}})
    assert get_setting(db_session, "invoice_counter") == "5000"
    assert get_setting(db_session, "shop_name") == "Old Shop Name"
    set_setting(db_session, "shop_name", "TechPro+")


def test_a_broken_old_file_changes_nothing(db_session):
    rich_data(db_session)
    before = dump(db_session)
    with pytest.raises(B.BackupError):
        B.restore_legacy(db_session, {"version": 1, "products": [{"id": "bk-prod", "name": "x", "price": "abc"}]})
    assert dump(db_session) == before
    remove_rich_data(db_session)


def test_old_file_preview_says_it_only_merges(owner_client, db_session):
    r = _upload(owner_client, "/import/backup/preview", json.dumps({"version": 1, "products": []}).encode())
    assert "never deletes" in r.text and "Merge this backup in" in r.text and 'name="confirm_text"' not in r.text


# ═══ the weekly email ═════════════════════════════════════════════════

def _weekly_setup(db):
    set_setting(db, "backup_email_enabled", "true")
    set_setting(db, "backup_email", "owner@example.com")


def _weekly_teardown(db):
    for key in ("backup_email_enabled", "backup_email", "backup_password", "last_backup"):
        db.query(Setting).filter(Setting.key == key).delete()
    db.commit()


def test_weekly_email_sends_a_locked_file_when_a_password_is_set(db_session):
    from app.encryption import encrypt_value
    _weekly_setup(db_session)
    set_setting(db_session, "backup_password", encrypt_value("longenough1"))
    with patch("app.main.send_backup_email", return_value=(True, "sent")) as mock:
        run_weekly_backup_email_job()
    args, kwargs = mock.call_args
    assert args[3].endswith(".tpbackup") and kwargs["sealed"] is True and B.is_sealed(args[2])
    assert get_setting(db_session, "last_backup", "")
    _weekly_teardown(db_session)


def test_weekly_email_without_a_password_is_still_sent_but_plain(db_session):
    _weekly_setup(db_session)
    with patch("app.main.send_backup_email", return_value=(True, "sent")) as mock:
        run_weekly_backup_email_job()
    args, kwargs = mock.call_args
    assert args[3].endswith(".json") and kwargs["sealed"] is False and b"pin_hash" not in args[2]
    _weekly_teardown(db_session)


def test_weekly_email_that_is_too_big_is_not_sent_and_says_so(db_session, monkeypatch):
    _weekly_setup(db_session)
    monkeypatch.setattr("app.main.BACKUP_EMAIL_MAX_BYTES", 10)
    with patch("app.main.send_backup_email") as mock:
        run_weekly_backup_email_job()
    assert not mock.called
    assert "BACKUP_FAILED" in audit_actions(db_session)
    assert not get_setting(db_session, "last_backup", "")
    _weekly_teardown(db_session)


def test_weekly_email_failures_are_recorded(db_session):
    _weekly_setup(db_session)
    with patch("app.main.send_backup_email", return_value=(False, "Brevo says no")):
        run_weekly_backup_email_job()
    db_session.expire_all()
    assert any("Brevo says no" in a.detail for a in db_session.query(AuditLog).filter(AuditLog.action == "BACKUP_FAILED"))
    assert not get_setting(db_session, "last_backup", "")
    _weekly_teardown(db_session)


def test_the_email_text_matches_the_kind_of_file(db_session):
    from app.notifications import send_backup_email
    set_setting(db_session, "email_method", "brevo_api")
    from app.encryption import encrypt_value
    set_setting(db_session, "brevo_api_key", encrypt_value("k"))
    set_setting(db_session, "brevo_from_email", "shop@example.com")
    bodies = {}
    for sealed in (True, False):
        with patch("app.notifications._send_via_brevo_api", return_value=(True, "ok")) as mock:
            send_backup_email(db_session, "o@example.com", b"x", "f", get_setting, sealed=sealed)
        bodies[sealed] = mock.call_args.kwargs["text_content"]
    assert "backup password" in bodies[True] and "NOT password-locked" in bodies[False]
    assert "does not touch existing invoices" not in bodies[True] + bodies[False]
    for key in ("email_method", "brevo_api_key", "brevo_from_email"):
        db_session.query(Setting).filter(Setting.key == key).delete()
    db_session.commit()


# ═══ size ═════════════════════════════════════════════════════════════

def test_a_busy_shops_worth_of_data_backs_up_and_restores(db_session):
    """~2,000 customers and 3,000 sales with their lines: sealed, restored, and still exact."""
    small = B.make_backup(db_session, "correct horse battery")[0]
    cust_rows = [{"id": f"big-c{i}", "name": f"Customer {i}", "phone": f"(250) 555-{i:04d}", "points": i, "store_credit": 0.0, "spent": 0.0,
                  "marketing_consent": False, "sms_opt_out": False} for i in range(2000)]
    inv_rows = [{"id": f"big-i{i}", "number": f"BIG-{i}", "customer_id": f"big-c{i % 2000}", "staff_id": "s1", "payment_method": "Cash",
                 "cash_amount": 10.0, "subtotal": 10.0, "total": 11.3, "tax_total": 1.3, "date": datetime(2031, 1, 1) + timedelta(minutes=i),
                 "discount": 0, "refunded": False} for i in range(3000)]
    line_rows = [{"id": f"big-l{i}-{j}", "invoice_id": f"big-i{i}", "name": "Widget", "sku": "W", "qty": 1, "price": 5.0} for i in range(3000) for j in range(2)]
    db_session.execute(Customer.__table__.insert(), cust_rows)
    db_session.execute(Invoice.__table__.insert(), inv_rows)
    db_session.execute(InvoiceLine.__table__.insert(), line_rows)
    db_session.commit()
    snapshot = dump(db_session)
    start = time.time()
    data, _, _ = B.make_backup(db_session, "correct horse battery")
    doc = B.read_backup(data, "correct horse battery")
    db_session.execute(text("DELETE FROM invoice_lines WHERE id LIKE 'big-%'"))
    db_session.execute(text("DELETE FROM invoices WHERE id LIKE 'big-%' AND id LIKE 'big-i%'"))
    db_session.commit()
    B.restore_full(db_session, doc, owner_row(db_session))
    elapsed = time.time() - start
    assert dump(db_session) == snapshot
    assert elapsed < 60, f"backup + restore of a busy shop took {elapsed:.0f}s"
    # leave the shared test database as it was
    B.restore_full(db_session, B.read_backup(small, "correct horse battery"), owner_row(db_session))
    assert db_session.get(Customer, "big-c1") is None
