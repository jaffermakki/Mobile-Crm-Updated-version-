from sqlalchemy import inspect, text, Boolean
from .database import Base, engine, SessionLocal
from .models import Staff, Product, Setting
from .auth import hash_pin
from . import setup as _setup
from .localtime import get_shop_tz, local_today

# Sensible defaults for columns that might get added to existing tables
# later — used only when a column doesn't exist yet in an older crm.db.
_COLUMN_DEFAULTS = {
    "subcategory": "''", "variant_group": "''", "reorder_threshold": "5", "reorder_qty": "10",
    "loyalty_pts_used": "0", "store_credit_used": "0", "tendered": "0", "change_given": "0",
    "sku": "''", "refunded": "0", "exchange_note": "''", "marketing_consent": "0",
    "imei": "''", "phone_brand": "''", "phone_model": "''", "color": "''",
    "cash_amount": "0", "card_amount": "0", "card_reference": "''",
    "number": "''",
    "refunded_by": "''", "refund_method": "''", "refund_reason": "''",
    "sms_opt_out": "0", "seq": "0", "attempts": "0", "cash_rounding": "0",
    "pin_len": "4", "totp_secret": "''", "totp_enabled": "0", "totp_last_step": "0", "recovery_codes": "''",
    "status_token": "''", "paid_out": "0", "intake_json": "''", "barcode": "''", "reason": "''",
}


def migrate_schema():
    """SQLAlchemy's create_all() only creates NEW tables — it never adds
    columns to tables that already exist. Since this app has gone through
    several rounds of new fields (Product.subcategory, Invoice.tendered,
    etc.), an older crm.db file would be missing those columns entirely,
    which surfaces as a 500 error the moment any query touches them. This
    inspects the live database and adds whatever's missing, so upgrading
    the app code doesn't require deleting your data.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand-new table — create_all() already handles this
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                default = _COLUMN_DEFAULTS.get(col.name, "NULL")
                if isinstance(col.type, Boolean) and default in ("0", "1") and engine.dialect.name == "postgresql":
                    # PostgreSQL refuses `BOOLEAN DEFAULT 0` — it wants FALSE/TRUE. (SQLite is happy with 0/1.)
                    default = "TRUE" if default == "1" else "FALSE"
                # The column's SQL type (e.g. VARCHAR, FLOAT) must be included
                # explicitly — SQLite tolerates ADD COLUMN without one, but
                # PostgreSQL treats it as a syntax error and refuses the
                # statement entirely, which would crash the app on startup.
                col_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type} DEFAULT {default}'))
                if col.index:      # create_all() never adds an index to a table that already exists
                    conn.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{table.name}_{col.name} ON {table.name} ({col.name})'))


def backfill_line_snapshots(db):
    """One-time (v45): invoice lines now freeze the product's cost and
    category at the moment of sale, so profit reports can't be rewritten by
    a later cost edit or a deleted product. Lines sold BEFORE this version
    have nothing frozen, so stamp them once with the product's current
    cost/category — the best information that still exists — and never
    touch them again. Lines whose product was already deleted stay NULL and
    the reports flag them as 'cost unknown' instead of guessing."""
    if db.get(Setting, "v45_line_backfill"):
        return
    db.execute(text(
        "UPDATE invoice_lines SET unit_cost = (SELECT p.cost FROM products p WHERE p.id = invoice_lines.product_id) "
        "WHERE unit_cost IS NULL AND product_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM products p WHERE p.id = invoice_lines.product_id)"))
    db.execute(text(
        "UPDATE invoice_lines SET category = (SELECT p.category FROM products p WHERE p.id = invoice_lines.product_id) "
        "WHERE category IS NULL AND product_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM products p WHERE p.id = invoice_lines.product_id)"))
    db.add(Setting(key="v45_line_backfill", value="done"))
    db.commit()


def _emergency_reset_2fa(db):
    """v48: lost the phone AND the recovery codes? Set RESET_2FA=1 in the server's environment variables and
    restart: the authenticator step is switched off for every account at start-up, so the PIN signs in again.
    Whoever can edit the server's variables is the real owner of the shop. Remove the variable afterwards,
    otherwise it switches 2-step off again at every restart."""
    import os
    import logging
    if os.environ.get("RESET_2FA", "").strip().lower() not in ("1", "true", "yes"):
        return
    rows = db.query(Staff).filter((Staff.totp_enabled == True) | (Staff.totp_secret != "")).all()  # noqa: E712
    for s in rows:
        s.totp_enabled, s.totp_secret, s.totp_last_step, s.recovery_codes = False, "", 0, ""
    if rows:
        from .models import AuditLog
        db.add(AuditLog(action="SECURITY", staff_name="System",
                        detail=f"2-step sign-in switched off for {len(rows)} account(s) by RESET_2FA at start-up"))
        db.commit()
    logging.getLogger("techpro.security").warning(
        "RESET_2FA is set: 2-step sign-in is off for all accounts. Remove the variable once you are back in.")


def init_db():
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    db = SessionLocal()
    try:
        if not db.query(Staff).first():
            # v51: a brand-new database no longer starts with a known PIN. Its owner claims it on the setup screen
            # (app/setup.py). A database that already has staff is an existing shop and is left exactly as it is.
            _setup.begin(db)
        existing_name = db.query(Setting).filter_by(key="shop_name").first()
        if not existing_name:
            db.add(Setting(key="shop_name", value="TechPro+"))
        elif existing_name.value == "TechPro Repairs":
            # Migrate old default name to the new brand name
            existing_name.value = "TechPro+"
        if not db.query(Setting).filter_by(key="province").first():
            db.add(Setting(key="province", value="ON"))
        if not db.query(Setting).filter_by(key="invoice_prefix").first():
            db.add(Setting(key="invoice_prefix", value="INV"))
        if not db.query(Setting).filter_by(key="invoice_counter").first():
            db.add(Setting(key="invoice_counter", value="1000"))
        if not db.query(Setting).filter_by(key="points_per_dollar").first():
            db.add(Setting(key="points_per_dollar", value="1"))
        if not db.query(Setting).filter_by(key="points_redeem_rate").first():
            db.add(Setting(key="points_redeem_rate", value="100"))  # 100 points = $1
        if not db.query(Setting).filter_by(key="cash_float").first():
            db.add(Setting(key="cash_float", value="200"))
        if not db.query(Setting).filter_by(key="recon_start").first():
            # v47: the Moneris/cash-up day-end checks only judge days from the day they were
            # first switched on, so an upgraded shop's old history never shows up as "missing".
            try:
                db.flush()
                db.add(Setting(key="recon_start", value=local_today(get_shop_tz(db)).isoformat()))
            except Exception:
                db.add(Setting(key="recon_start", value=__import__("datetime").date.today().isoformat()))
        if not db.query(Setting).filter_by(key="shop_gst").first():
            db.add(Setting(key="shop_gst", value=""))
        if not db.query(Setting).filter_by(key="shop_pst").first():
            db.add(Setting(key="shop_pst", value=""))
        if not db.query(Setting).filter_by(key="digest_enabled").first():
            db.add(Setting(key="digest_enabled", value="false"))
        if not db.query(Setting).filter_by(key="digest_email").first():
            db.add(Setting(key="digest_email", value=""))
        if not db.query(Setting).filter_by(key="digest_hour").first():
            db.add(Setting(key="digest_hour", value="21"))  # 24h, server-local time
        if not db.query(Setting).filter_by(key="layaway_prefix").first():
            db.add(Setting(key="layaway_prefix", value="LAY"))
        if not db.query(Setting).filter_by(key="layaway_counter").first():
            db.add(Setting(key="layaway_counter", value="1000"))
        if not db.query(Product).first():
            db.add_all([
                Product(sku="SCRN-IP13", name="iPhone 13 Screen Protector", category="ACCESSORY", subcategory="Screen Protector", price=14.99, cost=3.50, stock=40),
                Product(sku="CASE-IP13", name="iPhone 13 Case", category="CASE", subcategory="OtterBox", price=19.99, cost=5.00, stock=25),
                Product(sku="CHRG-USBC", name="USB-C Fast Charger", category="CHARGER", subcategory="Anker", price=24.99, cost=8.00, stock=30),
                Product(sku="BATT-IP12", name="iPhone 12 Replacement Battery", category="BATTERY", subcategory="OEM Grade", price=49.99, cost=18.00, stock=10),
            ])
        db.commit()
        backfill_line_snapshots(db)
        _emergency_reset_2fa(db)
        _setup.announce(db)
    finally:
        db.close()
