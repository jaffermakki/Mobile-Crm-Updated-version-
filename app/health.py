"""
v54: the shop's health check, shown on the About page and copied into "support info".

Everything here is read-only and safe to show: counts, yes/no answers and dates. Never a password, key, phone number,
customer name or any other customer data, so the support text can be pasted into an email or a chat.

Each check is a dict: {"key", "label", "state", "value", "help"} where state is
  ok    everything is fine           warn  worth a look           bad  needs fixing          info  just a fact
"""
from __future__ import annotations

import os
import platform
import sys
from datetime import datetime

from sqlalchemy import func, inspect

from . import version as V
from .database import Base, engine
from .models import (Customer, Invoice, Product, Repair, RepairPhoto, RepairPrice, Setting, ShopLogo)

BACKUP_FRESH_DAYS = 7
BACKUP_STALE_DAYS = 30
PHOTO_WARN_MB = 500


def _setting(db, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row and row.value is not None else default


def _check(key, label, state, value, help_="") -> dict:
    return {"key": key, "label": label, "state": state, "value": value, "help": help_}


def structure(db_engine=None) -> dict:
    """Compares what the program expects (every table and column) with what the live database really has.
    A problem here means an upgrade did not finish and some screens will fail."""
    eng = db_engine or engine
    insp = inspect(eng)
    have = set(insp.get_table_names())
    missing_tables, missing_columns = [], []
    for table in Base.metadata.sorted_tables:
        if table.name not in have:
            missing_tables.append(table.name)
            continue
        cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in cols:
                missing_columns.append(f"{table.name}.{col.name}")
    return {"tables": len(Base.metadata.sorted_tables), "missing_tables": missing_tables,
            "missing_columns": missing_columns, "ok": not missing_tables and not missing_columns}


def database_kind(db_engine=None) -> str:
    name = (db_engine or engine).dialect.name
    return {"postgresql": "PostgreSQL", "sqlite": "SQLite"}.get(name, name)


def _on_hosted_platform(env) -> bool:
    return any(k.startswith("RAILWAY_") for k in env)


def backup_age_days(db, now=None):
    raw = _setting(db, "last_backup")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return max(0, ((now or datetime.utcnow()) - when).days)


def collect(db, https: bool, env=None, now=None, db_engine=None, can_backup: bool = True) -> dict:
    env = os.environ if env is None else env
    checks = []
    act = "Press Back up now" if can_backup else "Ask the owner to make a backup"

    # version
    build = (env.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7]
    checks.append(_check("version", "Version", "info", f"{V.LABEL} ({V.VERSION})" + (f" · build {build}" if build else ""),
                         f"{V.NAME}."))

    # database
    kind = database_kind(db_engine)
    st = structure(db_engine)
    hosted = _on_hosted_platform(env)
    if not st["ok"]:
        problems = st["missing_tables"] + st["missing_columns"]
        checks.append(_check("structure", "Database structure", "bad",
                             f"{len(problems)} thing{'s' if len(problems) != 1 else ''} missing: " + ", ".join(problems[:8]) + (" …" if len(problems) > 8 else ""),
                             "An upgrade did not finish. Restart the app once; if this stays, send the support info to your developer."))
    else:
        checks.append(_check("structure", "Database structure", "ok", f"All {st['tables']} tables and their columns are in place."))
    if kind == "SQLite" and hosted:
        checks.append(_check("database", "Database", "bad", "SQLite file on a hosted server",
                             "A hosted server's disk can be wiped when the app restarts. Move the shop to PostgreSQL, or keep a recent backup."))
    elif kind == "SQLite":
        checks.append(_check("database", "Database", "info", "SQLite file on this computer",
                             "Fine for one computer. A shop used from several phones should run on PostgreSQL."))
    else:
        checks.append(_check("database", "Database", "ok", kind))

    # counts (no names, no amounts)
    open_repairs = db.query(func.count(Repair.id)).filter(Repair.status.notin_(["COMPLETED", "COLLECTED"])).scalar() or 0
    counts = {"products": db.query(func.count(Product.id)).scalar() or 0, "customers": db.query(func.count(Customer.id)).scalar() or 0,
              "invoices": db.query(func.count(Invoice.id)).scalar() or 0, "open repairs": open_repairs,
              "repair price lines": db.query(func.count(RepairPrice.id)).scalar() or 0}
    checks.append(_check("counts", "What is stored", "info", " · ".join(f"{v:,} {k}" for k, v in counts.items())))

    # backups
    age = backup_age_days(db, now)
    if age is None:
        checks.append(_check("backup", "Last backup", "bad", "No backup has been made yet",
                             f"{act}. A backup is the only way back if the server is lost."))
    elif age <= BACKUP_FRESH_DAYS:
        checks.append(_check("backup", "Last backup", "ok", "today" if age == 0 else f"{age} day{'s' if age != 1 else ''} ago"))
    elif age <= BACKUP_STALE_DAYS:
        checks.append(_check("backup", "Last backup", "warn", f"{age} days ago", f"{act}, or turn on the weekly emailed backup in Settings."))
    else:
        checks.append(_check("backup", "Last backup", "bad", f"{age} days ago", f"That is a long time. {act}."))
    weekly = _setting(db, "backup_email_enabled", "false") == "true" and bool(_setting(db, "backup_email") or _setting(db, "digest_email"))
    checks.append(_check("backup_weekly", "Weekly emailed backup", "ok" if weekly else "warn", "On" if weekly else "Off",
                         "" if weekly else "Turn it on in Settings → Notifications so a backup arrives without anyone remembering."))
    locked = bool(_setting(db, "backup_password"))
    checks.append(_check("backup_lock", "Backup password", "ok" if locked else "warn", "Set" if locked else "Not set",
                         "" if locked else "Without it a backup file can be read by anyone who gets it. Set one in Settings → Data."))

    # texting and email
    sms = bool(_setting(db, "twilio_sid") and _setting(db, "twilio_token") and _setting(db, "twilio_from"))
    checks.append(_check("sms", "Texting customers", "ok" if sms else "warn", "Connected" if sms else "Not connected",
                         "" if sms else "Quotes, ready-for-pickup texts and reminders will not send until Twilio is set up (Settings → Notifications)."))
    method = _setting(db, "email_method", "smtp")
    if method == "brevo_api":
        mail_ok = bool(_setting(db, "brevo_api_key") and (_setting(db, "brevo_from_email") or _setting(db, "smtp_from")))
        how = "Brevo"
    else:
        mail_ok = bool(_setting(db, "smtp_host"))
        how = "SMTP"
    checks.append(_check("email", "Email", "ok" if mail_ok else "warn", f"{how}, set up" if mail_ok else f"{how}, not set up",
                         "" if mail_ok else "Emailed receipts and backups will not send (Settings → Notifications)."))

    # photos
    n, total = db.query(func.count(RepairPhoto.id), func.coalesce(func.sum(RepairPhoto.size), 0)).one()
    mb = round((total or 0) / 1_000_000, 1)
    checks.append(_check("photos", "Repair photo storage", "warn" if mb >= PHOTO_WARN_MB else "ok", f"{int(n or 0):,} photos · {mb} MB",
                         "That is a lot for the database. Download the photos from Settings → Data, then delete old ones." if mb >= PHOTO_WARN_MB
                         else "Photos are not in the backup file; download them from Settings → Data now and then."))

    # customer pages
    has_logo = db.query(func.count(ShopLogo.id)).scalar() or 0
    has_hours = bool(_setting(db, "brand_hours"))
    bits = [("logo set" if has_logo else "no logo"), ("hours set" if has_hours else "no hours"),
            ("photos shown" if _setting(db, "brand_show_photos") == "true" else "photos hidden")]
    checks.append(_check("customer_pages", "Customer pages", "ok" if (has_logo and has_hours) else "info", ", ".join(bits),
                         "" if (has_logo and has_hours) else "Add a logo and your hours under Settings → Customer pages."))

    # security basics (yes/no only)
    checks.append(_check("https", "Secure connection", "ok" if https else "warn", "HTTPS" if https else "Not HTTPS",
                         "" if https else "Open the shop from its https:// address. PINs travel over this connection."))
    key_ok = bool(env.get("SETTINGS_ENCRYPTION_KEY"))
    checks.append(_check("enc_key", "Settings encryption key", "ok" if key_ok else "warn", "Set" if key_ok else "Not set",
                         "" if key_ok else "Passwords for email and texting are stored without encryption until SETTINGS_ENCRYPTION_KEY is set on the server."))
    secret_ok = bool(env.get("SESSION_SECRET"))
    checks.append(_check("session_secret", "Sign-in secret", "ok" if secret_ok else "warn", "Set" if secret_ok else "Not set",
                         "" if secret_ok else "Set SESSION_SECRET on the server so staff stay signed in after a restart."))

    worst = "ok"
    for c in checks:
        if c["state"] == "bad":
            worst = "bad"
            break
        if c["state"] == "warn":
            worst = "warn"
    return {"checks": checks, "overall": worst, "version": V.VERSION,
            "problems": sum(1 for c in checks if c["state"] == "bad"), "warnings": sum(1 for c in checks if c["state"] == "warn")}


def support_text(report: dict, db, now=None) -> str:
    """Plain text for 'Copy support info'. Built only from `report`, so it can never hold more than the page shows."""
    stamp = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"TechPro+ CRM support info ({stamp})",
             f"Python {sys.version.split()[0]} on {platform.system()}"]
    marks = {"ok": "OK  ", "warn": "WARN", "bad": "FIX ", "info": "    "}
    for c in report["checks"]:
        lines.append(f"[{marks[c['state']]}] {c['label']}: {c['value']}")
    lines.append(f"Summary: {report['problems']} to fix, {report['warnings']} to look at")
    return "\n".join(lines)
