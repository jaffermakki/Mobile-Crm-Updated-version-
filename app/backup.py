"""
v48 backups: one complete file, optionally locked with a password, and a restore that works on PostgreSQL.

What was wrong before: the backup held only products, customers, repairs, invoices and a few extras, so it
could not rebuild a shop (no layaways, trade-ins, suppliers, purchase orders, repair parts, cash-ups, texts…),
and restoring deleted rows in an order PostgreSQL refuses (foreign keys), so on the live database it failed.

Now:
  * every data table is written, in one JSON document (`version` 2, `tables` → rows);
  * with a backup password the document is gzip-compressed and sealed with AES-256-GCM (key from scrypt) —
    a stolen email attachment or cloud-drive copy is unreadable without the password. The sealed file also
    carries the PIN hashes and sign-in secrets, so a restore on a fresh server brings everyone back;
  * without a password the file is plain JSON and leaves out every secret (PIN hashes, authenticator keys,
    API passwords) — it is still customer data, so treat it carefully;
  * restore = "make the database exactly this file": all rows are removed children-first and re-inserted
    parents-first inside ONE transaction. Any error rolls everything back and nothing changes.
Old (version 1) files still restore, merging by id and deleting nothing.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import secrets
import time
from datetime import date, datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, text
from sqlalchemy.orm import Session

from .database import Base
from .models import Setting, Staff

FORMAT = "techpro-backup"
SEALED_FORMAT = "techpro-backup-sealed"
VERSION = 2
from .version import LABEL as APP_LABEL        # v54: one place names the version (app/version.py)

# Sign-in history is not business data; the old shop-wide lock counter is obsolete. Repair photos are real data
# but far too big for a JSON file: they have their own owner-only .zip download (Settings → Data).
EXCLUDED_TABLES = {"login_attempts", "login_state", "repair_photos"}
# Settings that hold secrets: left out of a password-less backup; carried over from the live database when
# a file doesn't have them.
SECRET_SETTINGS = {"smtp_password", "brevo_api_key", "twilio_token", "security_answer_hash",
                   "sms_webhook_secret", "backup_password"}
# Never taken from a file: it belongs to this running server.
LOCAL_ONLY_SETTINGS = {"session_epoch"}
STAFF_SECRET_COLUMNS = ("pin_hash", "totp_secret", "totp_enabled", "totp_last_step", "recovery_codes")

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MIN_PASSWORD_LENGTH = 8


class BackupError(Exception):
    """A problem that can be shown to the owner as-is."""


# ═══ building a backup ═════════════════════════════════════════════════

def data_tables() -> list:
    """Every data table, parents before children (the order rows must be inserted in)."""
    return [t for t in Base.metadata.sorted_tables if t.name not in EXCLUDED_TABLES]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode()}
    return value


def build_doc(db: Session, include_secrets: bool) -> dict:
    tables: dict[str, list] = {}
    for table in data_tables():
        rows = []
        for rec in db.execute(table.select()).mappings():
            row = {k: _jsonable(v) for k, v in rec.items()}
            if table.name == "staff" and not include_secrets:
                for col in STAFF_SECRET_COLUMNS:
                    row.pop(col, None)
            if table.name == "settings":
                if row.get("key") in LOCAL_ONLY_SETTINGS:
                    continue
                if not include_secrets and row.get("key") in SECRET_SETTINGS:
                    continue
            rows.append(row)
        tables[table.name] = rows
    return {
        "format": FORMAT, "version": VERSION, "app": APP_LABEL,
        "exported_at": datetime.utcnow().isoformat(), "includes_secrets": include_secrets,
        "counts": {name: len(rows) for name, rows in tables.items()}, "tables": tables,
    }


def doc_bytes(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), default=str).encode("utf-8")


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def seal(payload: bytes, password: str) -> bytes:
    """Compress and lock a document. The header is authenticated too, so it can't be swapped."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise BackupError(f"The backup password must be at least {MIN_PASSWORD_LENGTH} characters.")
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {"format": SEALED_FORMAT, "version": VERSION, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R,
              "p": SCRYPT_P, "cipher": "AES-256-GCM", "compression": "gzip",
              "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode()}
    aad = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    key = _derive_key(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    sealed = AESGCM(key).encrypt(nonce, gzip.compress(payload, 6), aad)
    header["ciphertext"] = base64.b64encode(sealed).decode()
    return json.dumps(header, separators=(",", ":")).encode("utf-8")


def make_backup(db: Session, password: str | None) -> tuple[bytes, str, bool]:
    """(file bytes, file extension, sealed?) — the one place that decides what a backup file is."""
    if password:
        return seal(doc_bytes(build_doc(db, include_secrets=True)), password), "tpbackup", True
    return doc_bytes(build_doc(db, include_secrets=False)), "json", False


def backup_filename(sealed: bool, extension: str | None = None) -> str:
    ext = extension or ("tpbackup" if sealed else "json")
    return f"techpro_backup_{datetime.utcnow().strftime('%Y-%m-%d')}.{ext}"


# ═══ reading a backup ══════════════════════════════════════════════════

def is_sealed(raw: bytes) -> bool:
    head = raw[:400].lstrip()
    return head.startswith(b"{") and SEALED_FORMAT.encode() in raw[:400]


def read_backup(raw: bytes, password: str | None = None) -> dict:
    """Parses (and unlocks) a backup file. Raises BackupError with a plain-English reason."""
    if not raw:
        raise BackupError("That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise BackupError("That file is too large to be a backup of this app.")
    try:
        outer = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("This doesn't look like a backup file from this app.") from None
    if not isinstance(outer, dict):
        raise BackupError("This doesn't look like a backup file from this app.")

    if outer.get("format") == SEALED_FORMAT:
        if not password:
            raise BackupError("This backup is locked. Type the backup password it was made with.")
        try:
            header = {k: v for k, v in outer.items() if k != "ciphertext"}
            n, r, p = int(outer["n"]), int(outer["r"]), int(outer["p"])
            if not (2 ** 12 <= n <= 2 ** 20 and 1 <= r <= 16 and 1 <= p <= 4):
                raise ValueError("unreasonable scrypt settings")
            salt, nonce = base64.b64decode(outer["salt"]), base64.b64decode(outer["nonce"])
            sealed = base64.b64decode(outer["ciphertext"])
            aad = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
            key = _derive_key(password, salt, n, r, p)
            payload = gzip.decompress(AESGCM(key).decrypt(nonce, sealed, aad))
        except InvalidTag:
            raise BackupError("That password doesn't open this backup — or the file was damaged. "
                              "Nothing was changed.") from None
        except Exception:   # noqa: BLE001 — anything else means the file isn't intact
            raise BackupError("This backup file is damaged and can't be opened.") from None
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BackupError("This backup file is damaged and can't be opened.") from None
    else:
        doc = outer

    if not isinstance(doc, dict) or not doc.get("version"):
        raise BackupError("This doesn't look like a valid backup file.")
    if doc["version"] == 1:
        return doc
    if doc.get("format") != FORMAT or not isinstance(doc.get("tables"), dict):
        raise BackupError("This doesn't look like a valid backup file.")
    if int(doc["version"]) > VERSION:
        raise BackupError("This backup was made by a newer version of the app. Update the app first, then restore it.")
    return doc


# ═══ understanding what a restore would do ═════════════════════════════

TABLE_LABELS = {
    "invoices": "Invoices (sales)", "invoice_lines": "Invoice lines", "customers": "Customers", "products": "Products",
    "repairs": "Repairs", "repair_parts": "Repair parts used", "staff": "Staff accounts", "layaways": "Layaways",
    "layaway_payments": "Layaway payments", "trade_ins": "Trade-ins", "suppliers": "Suppliers",
    "purchase_orders": "Purchase orders", "purchase_order_lines": "Purchase order lines",
    "cash_sessions": "Cash-ups", "card_batches": "Moneris batches", "card_statements": "Moneris statements",
    "outbound_messages": "Automatic texts", "sms_messages": "Text messages", "audit_log": "Audit log",
    "held_carts": "Held carts", "settings": "Settings",
    "stock_counts": "Stock counts", "stock_count_lines": "Stock count lines",
    "sale_returns": "Returned items (partial returns)", "sale_return_lines": "Returned item lines",
    "expenses": "Expenses & till paid-outs", "quote_approvals": "Repair quote approvals",
    "repair_prices": "Repair price list", "shop_logo": "Shop logo (customer pages)",
}
_HIDDEN_WHEN_EMPTY = True


def summarize(db: Session, doc: dict) -> list[dict]:
    """Rows for the 'are you sure?' screen: what the file holds against what is in the database right now."""
    if doc.get("version") == 1:
        keys = [("products", "Products"), ("customers", "Customers"), ("repairs", "Repairs"),
                ("card_batches", "Moneris batches"), ("card_statements", "Moneris statements")]
        out = []
        for key, label in keys:
            if key in doc:
                table = Base.metadata.tables[key]
                now = db.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar() or 0
                out.append({"label": label, "backup": len(doc[key]), "now": now, "lost": 0})
        return out
    out = []
    for table in data_tables():
        in_file = len(doc["tables"].get(table.name, []))
        now = db.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar() or 0
        if not in_file and not now:
            continue
        out.append({"label": TABLE_LABELS.get(table.name, table.name.replace("_", " ").capitalize()),
                    "backup": in_file, "now": now, "lost": max(0, now - in_file), "name": table.name})
    out.sort(key=lambda r: (r["label"] not in ("Invoices (sales)", "Repairs", "Customers", "Products"), r["label"]))
    return out


def photo_effect(db: Session, doc: dict) -> dict | None:
    """What a full restore does to repair photos, for the 'are you sure?' screen. They are not in the file:
    photos of repairs that are in the file stay; the rest are deleted with their repairs. None = no photos."""
    if doc.get("version") == 1 or "tables" not in doc:
        return None
    total = db.execute(text("SELECT COUNT(*) FROM repair_photos")).scalar() or 0
    if not total:
        return None
    ids = [r.get("id") for r in doc["tables"].get("repairs", []) if r.get("id")]
    kept = 0
    for i in range(0, len(ids), 500):                    # in chunks: stays under any bind-parameter limit
        chunk = ids[i:i + 500]
        marks = ", ".join(f":p{n}" for n in range(len(chunk)))
        kept += db.execute(text(f"SELECT COUNT(*) FROM repair_photos WHERE repair_id IN ({marks})"),
                           {f"p{n}": v for n, v in enumerate(chunk)}).scalar() or 0
    return {"total": total, "kept": kept, "removed": total - kept}


def unknown_tables(doc: dict) -> list[str]:
    known = {t.name for t in Base.metadata.sorted_tables}
    return sorted(set(doc.get("tables", {})) - known)


# ═══ turning JSON back into database values ════════════════════════════

def _coerce_value(table, column, value):
    if value is None:
        return None
    ctype = column.type
    try:
        if isinstance(value, dict) and "__b64__" in value:
            return base64.b64decode(value["__b64__"])
        if isinstance(ctype, DateTime):
            return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if isinstance(ctype, Date):
            return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])
        if isinstance(ctype, Boolean):
            return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "t", "yes")
        if isinstance(ctype, Integer):
            return int(value)
        if isinstance(ctype, Float):
            return float(value)
    except (TypeError, ValueError):
        raise BackupError(f"A value in “{table.name}.{column.name}” isn't in the right format ({str(value)[:40]!r}).") from None
    return value


def _coerce_row(table, rec: dict) -> dict:
    if not isinstance(rec, dict):
        raise BackupError(f"The “{table.name}” section of this backup is malformed.")
    out = {}
    for column in table.columns:
        if column.name in rec:
            out[column.name] = _coerce_value(table, column, rec[column.name])
    return out


def _insert_rows(db: Session, table, rows: list[dict]) -> None:
    """executemany needs every row to carry the same keys, so rows are grouped by their key set."""
    groups: dict[frozenset, list[dict]] = {}
    for row in rows:
        groups.setdefault(frozenset(row), []).append(row)
    for batch in groups.values():
        for i in range(0, len(batch), 500):
            db.execute(table.insert(), batch[i:i + 500])


# ═══ restoring ═════════════════════════════════════════════════════════

def restore_full(db: Session, doc: dict, acting_staff) -> dict:
    """Replace every data table with the contents of `doc` — all or nothing."""
    from .auth import hash_pin   # imported here: auth ↔ security ↔ backup stay free of import cycles

    tables = data_tables()
    file_tables = doc["tables"]
    includes_secrets = bool(doc.get("includes_secrets"))

    # 1. Read what must survive from the live database BEFORE anything is deleted.
    live_staff = {r["id"]: dict(r) for r in db.execute(Staff.__table__.select()).mappings()}
    live_settings = {r["key"]: r["value"] for r in db.execute(Setting.__table__.select()).mappings()}
    acting_id = acting_staff.id if acting_staff else None

    # 2. Convert and validate the whole file first, so a bad value is found before the first delete.
    prepared: dict[str, list[dict]] = {}
    for table in tables:
        rows = [_coerce_row(table, rec) for rec in file_tables.get(table.name, [])]
        pk = [c.name for c in table.primary_key.columns]
        for row in rows:
            if any(row.get(k) in (None, "") for k in pk):
                raise BackupError(f"A row in “{table.name}” has no id — this backup is damaged.")
        if len({tuple(r[k] for k in pk) for r in rows}) != len(rows):
            raise BackupError(f"“{table.name}” lists the same record twice — this backup is damaged.")
        prepared[table.name] = rows

    staff_rows = prepared["staff"]
    for row in staff_rows:
        if includes_secrets and row.get("pin_hash"):
            row.setdefault("pin_len", 4)
            continue
        old = live_staff.get(row["id"])
        if old:                                          # same person: keep the PIN they already have
            for col in STAFF_SECRET_COLUMNS + ("pin_len",):
                row[col] = old.get(col)
        else:                                            # a person this server has never seen: needs a new PIN
            row["pin_hash"] = hash_pin(secrets.token_hex(16))
            row["pin_len"] = 4
            row["active"] = False
            row["totp_enabled"], row["totp_secret"], row["totp_last_step"], row["recovery_codes"] = False, "", 0, ""
    # Whoever is doing the restore must still be able to sign in afterwards.
    if acting_id and acting_id not in {r["id"] for r in staff_rows} and acting_id in live_staff:
        staff_rows.append(dict(live_staff[acting_id]))
    if not any(r.get("active") and r.get("role") == "owner" and r.get("pin_hash") for r in staff_rows):
        if acting_id and acting_id in live_staff:
            staff_rows = [r for r in staff_rows if r["id"] != acting_id] + [dict(live_staff[acting_id])]
        else:
            raise BackupError("This backup has no active Owner account, so nobody could sign in afterwards.")
    prepared["staff"] = staff_rows

    settings_rows = {r["key"]: r.get("value") for r in prepared["settings"]}
    for key in LOCAL_ONLY_SETTINGS:
        if key in live_settings:
            settings_rows[key] = live_settings[key]
    for key in SECRET_SETTINGS:
        if key not in settings_rows and key in live_settings:
            settings_rows[key] = live_settings[key]
    prepared["settings"] = [{"key": k, "value": v} for k, v in settings_rows.items()]

    # 3. One transaction: delete children first, insert parents first.
    try:
        for table in reversed(tables):
            db.execute(table.delete())
        for table in tables:
            if prepared[table.name]:
                _insert_rows(db, table, prepared[table.name])
        # Photos are not in the file. Keep the ones whose repair still exists; a photo whose repair is gone would
        # never be shown again, so it is removed here rather than left behind.
        db.execute(text("DELETE FROM repair_photos WHERE repair_id NOT IN (SELECT id FROM repairs)"))
        db.execute(text(
            "UPDATE outbound_messages SET status = 'cancelled', skip_reason = :why "
            "WHERE status IN ('pending', 'sending')"), {"why": "Cancelled when a backup was restored"})
        db.commit()
    except BackupError:
        db.rollback()
        raise
    except Exception as exc:    # noqa: BLE001 — whatever the database says, undo everything
        db.rollback()
        raise BackupError("The database refused part of this backup, so nothing was changed "
                          f"({type(exc).__name__}). Your current data is untouched.") from exc
    db.expire_all()
    return {name: len(rows) for name, rows in prepared.items() if rows}


def restore_moneris_records(db: Session, data: dict) -> int:
    """Moneris batches / statements are hand-entered, so a legacy restore only ADDS the ones that are missing
    (matched by day / month, or by id); it never overwrites or removes what is already recorded."""
    from .models import CardBatch, CardStatement
    added = 0
    for key, model, uniq in (("card_batches", CardBatch, "date"), ("card_statements", CardStatement, "month")):
        if key not in data:
            continue
        have = {getattr(m, uniq) for m in db.query(model).all()}
        for rec in data[key]:
            if rec.get(uniq) in have or (rec.get("id") and db.get(model, rec["id"]) is not None):
                continue
            row = _coerce_row(model.__table__, rec)
            if isinstance(row.get("entered_at"), str):
                row["entered_at"] = datetime.utcnow()
            db.add(model(**row))
            have.add(rec.get(uniq))
            added += 1
    return added


def restore_legacy(db: Session, doc: dict) -> dict:
    """Version-1 files: add what is missing and overwrite what matches by id — never delete. (They only ever held
    the catalogue, customers, repairs and Moneris records, and deleting those is what PostgreSQL refused.)"""
    from . import models
    counts: dict[str, int] = {}
    try:
        for key, model in (("products", models.Product), ("customers", models.Customer), ("repairs", models.Repair)):
            rows = doc.get(key)
            if rows is None:
                continue
            table = model.__table__
            n = 0
            for rec in rows:
                row = _coerce_row(table, rec)
                if not row.get("id"):
                    raise BackupError(f"A row in “{key}” has no id — this backup is damaged.")
                db.merge(model(**row))
                n += 1
            counts[key] = n
        if "card_batches" in doc or "card_statements" in doc:
            counts["moneris records added"] = restore_moneris_records(db, doc)
        for key, value in (doc.get("settings") or {}).items():
            if key in LOCAL_ONLY_SETTINGS:
                continue
            current = db.get(Setting, key)
            if key.endswith("_counter"):          # numbering must never go backwards
                try:
                    value = str(max(int(value), int(current.value if current else 0)))
                except (TypeError, ValueError):
                    pass
            if current:
                current.value = value
            else:
                db.add(Setting(key=key, value=value))
        db.execute(text(
            "UPDATE outbound_messages SET status = 'cancelled', skip_reason = :why "
            "WHERE status IN ('pending', 'sending')"), {"why": "Cancelled when a backup was restored"})
        db.commit()
    except BackupError:
        db.rollback()
        raise
    except Exception as exc:    # noqa: BLE001
        db.rollback()
        raise BackupError("The backup file doesn't match this app's data format, so nothing was changed "
                          f"({type(exc).__name__}). Your current data is untouched.") from exc
    db.expire_all()
    return counts


# ═══ the two-step restore (preview, then confirm) ══════════════════════
# The parsed backup waits in memory for a few minutes between the two screens. This app runs as one process;
# if the server restarts in between, the owner simply uploads the file again.

_PENDING: dict[str, dict] = {}
PENDING_SECONDS = 15 * 60


def hold(doc: dict, staff_id: str, filename: str) -> str:
    now = time.time()
    for token in [t for t, v in _PENDING.items() if v["expires"] < now]:
        _PENDING.pop(token, None)
    while len(_PENDING) >= 3:
        _PENDING.pop(min(_PENDING, key=lambda t: _PENDING[t]["expires"]), None)
    token = secrets.token_urlsafe(18)
    _PENDING[token] = {"doc": doc, "staff_id": staff_id, "filename": filename, "expires": now + PENDING_SECONDS}
    return token


def take(token: str, staff_id: str) -> dict | None:
    item = _PENDING.get(token)
    if not item or item["expires"] < time.time() or item["staff_id"] != staff_id:
        return None
    return item


def release(token: str) -> None:
    _PENDING.pop(token, None)
