import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Text, Index, LargeBinary, UniqueConstraint
from sqlalchemy.orm import relationship
from .database import Base


def gen_id():
    return uuid.uuid4().hex[:12]


class Staff(Base):
    __tablename__ = "staff"
    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False, default="cashier")  # owner | manager | cashier | technician
    pin_hash = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    pin_len = Column(Integer, default=4)            # v48: PINs may be 4-8 digits; the login keypad needs to know how long they are
    totp_secret = Column(String, default="")        # v48: authenticator-app secret (stored encrypted when SETTINGS_ENCRYPTION_KEY is set)
    totp_enabled = Column(Boolean, default=False)
    totp_last_step = Column(Integer, default=0)     # the last 30-second step accepted, so one code cannot be used twice
    recovery_codes = Column(Text, default="")       # JSON list of SHA-256 hashes of the unused one-time recovery codes


class Product(Base):
    __tablename__ = "products"
    id = Column(String, primary_key=True, default=gen_id)
    sku = Column(String, unique=True, index=True)
    name = Column(String, nullable=False)
    category = Column(String, default="")
    subcategory = Column(String, default="")  # case manufacturer/"Brand" in the UI (OtterBox, UAG, Generic...) — NOT the phone's brand, see phone_brand below
    variant_group = Column(String, default="")  # e.g. "Clear Silicone Case" — groups model/color variants together for browsing, without changing how stock is tracked per-SKU
    phone_brand = Column(String, default="")  # the PHONE's brand this variant fits — Apple, Samsung, Google, Motorola... (distinct from subcategory, which is the case's own brand)
    phone_model = Column(String, default="")  # e.g. "iPhone 15 Pro Max"
    color = Column(String, default="")
    price = Column(Float, default=0)
    cost = Column(Float, default=0)
    stock = Column(Integer, default=0)
    reorder_threshold = Column(Integer, default=5)  # flag for reorder when stock <= this
    reorder_qty = Column(Integer, default=10)        # suggested quantity to reorder
    barcode = Column(String, default="", index=True)  # v50: the UPC/EAN printed on the manufacturer's package ("" = none). Our own shelf labels still print the SKU.


class Customer(Base):
    __tablename__ = "customers"
    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    phone = Column(String, default="")
    email = Column(String, default="")
    notes = Column(Text, default="")
    points = Column(Integer, default=0)
    store_credit = Column(Float, default=0)
    spent = Column(Float, default=0)
    last_visit = Column(String, default="")
    # CASL (Canada's Anti-Spam Law): transactional messages — receipts,
    # repair-ready notifications — don't require this. Anything
    # promotional (review requests, marketing, newsletters) does, and
    # should check this flag before sending. Not enforced anywhere yet
    # since this app doesn't send promotional messages yet — this is the
    # data-capture groundwork for whenever it does.
    marketing_consent = Column(Boolean, default=False)
    consent_date = Column(DateTime, nullable=True)
    # v46: the customer replied STOP (or staff recorded that they asked not
    # to be texted). Blocks EVERY automatic text, transactional or not.
    sms_opt_out = Column(Boolean, default=False)
    sms_opt_out_at = Column(DateTime, nullable=True)


class Repair(Base):
    __tablename__ = "repairs"
    id = Column(String, primary_key=True, default=gen_id)
    ticket_no = Column(Integer, default=1001)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    device = Column(String, default="")
    imei = Column(String, default="")  # IMEI/serial of the device being repaired — warranty claims, insurance/police stolen-phone lookups, proof of condition at intake
    issue = Column(String, default="")
    description = Column(Text, default="")
    status = Column(String, default="RECEIVED")
    estimated_cost = Column(Float, nullable=True)
    final_cost = Column(Float, nullable=True)
    warranty_days = Column(Integer, default=90)
    promised_by = Column(String, default="")
    technician_id = Column(String, ForeignKey("staff.id"), nullable=True)
    status_history = Column(Text, default="[]")  # JSON list of {status, note, date}
    status_token = Column(String, default="", index=True)  # v48: private, unguessable link token for the customer status page
    intake_json = Column(Text, default="")  # v49: the drop-off condition checklist (see app/intake.py); "" = none was filled in
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    customer = relationship("Customer")
    technician = relationship("Staff")


class RepairPhoto(Base):
    """v49: a photo of the device (at drop-off, damage close-ups, after the repair).

    `repair_id` is deliberately NOT a foreign key: photos are left out of backup files (they are large) and a
    restore replaces the repairs table, so a constraint would make the database refuse the restore. A restore
    removes the photos whose repair no longer exists instead (backup.restore_full)."""
    __tablename__ = "repair_photos"
    id = Column(String, primary_key=True, default=gen_id)
    repair_id = Column(String, nullable=False, index=True)
    label = Column(String, default="other")        # front | back | damage | label | after | other
    mime = Column(String, default="image/jpeg")
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    data = Column(LargeBinary, nullable=False)
    thumb = Column(LargeBinary, nullable=True)      # a small copy for the gallery; the full picture is used when missing
    size = Column(Integer, default=0)
    staff_name = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(String, primary_key=True, default=gen_id)
    number = Column(String, unique=True)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    staff_id = Column(String, ForeignKey("staff.id"), nullable=True)
    repair_id = Column(String, ForeignKey("repairs.id"), nullable=True)
    payment_method = Column(String, default="Cash")
    cash_amount = Column(Float, default=0)     # portion of `total` actually put in the cash drawer
    cash_rounding = Column(Float, default=0)   # v47: 5c cash-rounding adjustment inside cash_amount (+ = customer paid a little more). Revenue and tax stay on `total`.
    card_amount = Column(Float, default=0)     # portion of `total` actually run through the card terminal
    card_reference = Column(String, default="")  # approval/reference code from the terminal receipt, staff-entered
    subtotal = Column(Float, default=0)
    discount = Column(Float, default=0)
    loyalty_pts_used = Column(Integer, default=0)
    store_credit_used = Column(Float, default=0)
    tendered = Column(Float, default=0)
    change_given = Column(Float, default=0)
    tax_breakdown = Column(Text, default="")  # JSON string of [{label, amount}]
    tax_total = Column(Float, default=0)
    total = Column(Float, default=0)
    refunded = Column(Boolean, default=False)
    # When (UTC) and by whom the refund happened. Before v45 a refund was only
    # a True/False flag, so reports could not tell *when* money went back out
    # and rewrote the original sale day instead. NULL on legacy refunds —
    # reports fall back to the invoice date for those.
    refunded_at = Column(DateTime, nullable=True)
    refunded_by = Column(String, default="")
    refund_method = Column(String, default="")   # Cash | Card | Store Credit | E-Transfer — how the money went back
    refund_reason = Column(String, default="")
    date = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer")
    staff = relationship("Staff")
    repair = relationship("Repair")
    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")
    # v52: items taken back from this sale without refunding the whole invoice (see app/returns.py)
    returns = relationship("SaleReturn", back_populates="invoice", cascade="all, delete-orphan",
                           order_by="SaleReturn.seq")


class HeldCart(Base):
    __tablename__ = "held_carts"
    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, default="Held Cart")
    cart_json = Column(Text, default="[]")
    customer_id = Column(String, nullable=True)
    disc_mode = Column(String, default="$")
    disc_value = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id = Column(String, primary_key=True, default=gen_id)
    invoice_id = Column(String, ForeignKey("invoices.id"))
    product_id = Column(String, nullable=True)
    name = Column(String)
    sku = Column(String, default="")
    qty = Column(Integer, default=1)
    price = Column(Float, default=0)
    imei = Column(String, default="")  # IMEI/serial of the specific unit sold — only meaningful for qty=1 serialized devices (phones), left blank for accessories
    exchange_note = Column(Text, default="")  # e.g. "Exchanged from: Black Case (was $15.00)"
    # Snapshots taken at the moment of sale so profit reports stay correct
    # after the product's cost is edited or the product is deleted. NULL on
    # lines created before v45 (backfilled once from the product at upgrade).
    unit_cost = Column(Float, nullable=True)
    category = Column(String, nullable=True)

    invoice = relationship("Invoice", back_populates="lines")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(String, primary_key=True, default=gen_id)
    ts = Column(DateTime, default=datetime.utcnow)
    staff_id = Column(String, nullable=True)
    staff_name = Column(String, default="System")
    action = Column(String)
    detail = Column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text)


class LoginState(Base):
    """Tracks shared PIN-pad lockout state (mirrors the brute-force
    protection from the JS version) — single row, id='global'."""
    __tablename__ = "login_state"
    id = Column(String, primary_key=True, default="global")
    fail_count = Column(Integer, default=0)
    lock_until = Column(DateTime, nullable=True)


class LoginAttempt(Base):
    """v48: one row per sign-in attempt (PIN, second step, PIN recovery, public status lookup).
    It is the security history the owner sees AND what the per-device throttling is computed from."""
    __tablename__ = "login_attempts"
    id = Column(String, primary_key=True, default=gen_id)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    ip = Column(String, default="", index=True)
    kind = Column(String, default="pin")      # pin | 2fa | recovery | status
    success = Column(Boolean, default=False)
    staff_id = Column(String, nullable=True)
    staff_name = Column(String, default="")
    device = Column(String, default="")       # short browser/OS summary, e.g. "Chrome on Android"
    note = Column(String, default="")


class CashSession(Base):
    __tablename__ = "cash_sessions"
    id = Column(String, primary_key=True, default=gen_id)
    date = Column(String)  # YYYY-MM-DD — one session per day, like the original
    open_float = Column(Float, default=0)
    expected = Column(Float, default=0)
    actual = Column(Float, default=0)
    difference = Column(Float, default=0)
    card_expected = Column(Float, default=0)   # what the CRM recorded as card/debit sales
    card_batch = Column(Float, default=0)      # what the bank terminal's batch/settlement report shows
    card_difference = Column(Float, default=0)
    notes = Column(Text, default="")
    closed_at = Column(DateTime, default=datetime.utcnow)
    closed_by_id = Column(String, nullable=True)
    closed_by_name = Column(String, default="")
    paid_out = Column(Float, default=0)        # v52: cash taken out of the till for expenses / deposits, frozen at close


class SmsMessage(Base):
    """Logs both directions of SMS — outgoing (receipts, repair-ready
    notices) and incoming (customer replies via the Twilio webhook).
    Matched to a customer by phone number since that's all Twilio gives
    us on an inbound message."""
    __tablename__ = "sms_messages"
    id = Column(String, primary_key=True, default=gen_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    phone = Column(String, default="")
    body = Column(Text, default="")
    direction = Column(String, default="out")  # "out" or "in"
    staff_name = Column(String, default="")  # who sent it, for outgoing messages
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer")


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String)
    contact_name = Column(String, default="")
    email = Column(String, default="")
    phone = Column(String, default="")
    lead_time_days = Column(Integer, default=7)
    notes = Column(Text, default="")
    active = Column(Boolean, default=True)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(String, primary_key=True, default=gen_id)
    number = Column(String, default="")
    supplier_id = Column(String, ForeignKey("suppliers.id"), nullable=True)
    status = Column(String, default="draft")  # draft, sent, received, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, nullable=True)
    created_by_name = Column(String, default="")
    notes = Column(Text, default="")

    supplier = relationship("Supplier")
    lines = relationship("PurchaseOrderLine", back_populates="po", cascade="all, delete-orphan")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    id = Column(String, primary_key=True, default=gen_id)
    po_id = Column(String, ForeignKey("purchase_orders.id"))
    product_id = Column(String, ForeignKey("products.id"), nullable=True)
    name = Column(String, default="")
    sku = Column(String, default="")
    qty = Column(Integer, default=1)
    unit_cost = Column(Float, default=0)
    received_qty = Column(Integer, default=0)

    po = relationship("PurchaseOrder", back_populates="lines")


class RepairPart(Base):
    """A product consumed on a repair job — deducts stock immediately when
    added and lets the repair's true margin (charge minus parts cost) be
    calculated, instead of parts silently vanishing from inventory counts."""
    __tablename__ = "repair_parts"
    id = Column(String, primary_key=True, default=gen_id)
    repair_id = Column(String, ForeignKey("repairs.id"))
    product_id = Column(String, ForeignKey("products.id"), nullable=True)
    name = Column(String, default="")
    qty = Column(Integer, default=1)
    unit_cost = Column(Float, default=0)  # snapshot of product cost at time of use
    added_at = Column(DateTime, default=datetime.utcnow)

    repair = relationship("Repair", backref="parts")


class TradeIn(Base):
    """A used device accepted from a customer in exchange for store
    credit or cash, toward a new purchase or repair. Deliberately kept
    separate from Product/Invoice — a trade-in isn't inventory being
    resold (yet), it's a one-off transaction with its own condition
    assessment and payout, and most shops either scrap/refurbish
    trade-ins off-books or list them later as a distinct product."""
    __tablename__ = "trade_ins"
    id = Column(String, primary_key=True, default=gen_id)
    number = Column(String, default="")  # e.g. "TR-1004" — for a proper receipt, not a raw internal id
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    device = Column(String, default="")  # e.g. "iPhone 12, 64GB, Blue"
    imei = Column(String, default="")
    condition = Column(String, default="")  # e.g. "Good — minor scratches, screen fine"
    offered_amount = Column(Float, default=0)
    payout_method = Column(String, default="store_credit")  # store_credit | cash
    status = Column(String, default="accepted")  # accepted | resold | scrapped
    notes = Column(Text, default="")
    staff_id = Column(String, ForeignKey("staff.id"), nullable=True)
    staff_name = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer")
    staff = relationship("Staff")


class Layaway(Base):
    """A held sale paid off in installments. Cart contents are snapshotted
    as JSON at creation time (same pattern as HeldCart) since the actual
    products/prices shouldn't drift if a price changes while a customer
    is still paying it off. Stock is deducted at creation (reserved for
    this customer) rather than at payoff, so the same item can't be sold
    twice — see layaway_new() and layaway_cancel() in main.py."""
    __tablename__ = "layaways"
    id = Column(String, primary_key=True, default=gen_id)
    number = Column(String, default="")  # e.g. "LAY-1000"
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)
    cart_json = Column(Text, default="[]")  # snapshot: [{product_id, name, sku, price, qty}]
    subtotal = Column(Float, default=0)
    tax_breakdown = Column(Text, default="")  # JSON string of [{label, amount}] — locked in at creation, same as Invoice
    tax_total = Column(Float, default=0)
    total = Column(Float, default=0)
    paid_total = Column(Float, default=0)
    status = Column(String, default="active")  # active | completed | cancelled | forfeited
    due_date = Column(String, default="")
    notes = Column(Text, default="")
    staff_id = Column(String, ForeignKey("staff.id"), nullable=True)
    invoice_id = Column(String, ForeignKey("invoices.id"), nullable=True)  # set once completed and converted to a real sale
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer")
    staff = relationship("Staff")
    invoice = relationship("Invoice")
    payments = relationship("LayawayPayment", back_populates="layaway", cascade="all, delete-orphan", order_by="LayawayPayment.created_at")


class LayawayPayment(Base):
    __tablename__ = "layaway_payments"
    id = Column(String, primary_key=True, default=gen_id)
    layaway_id = Column(String, ForeignKey("layaways.id"))
    amount = Column(Float, default=0)
    method = Column(String, default="Cash")
    staff_id = Column(String, ForeignKey("staff.id"), nullable=True)
    staff_name = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    layaway = relationship("Layaway", back_populates="payments")


class OutboundMessage(Base):
    """v46: the automatic-text queue and its history, in one table.

    A row is created when something happens (a repair becomes Ready, a
    phone is collected, ...) and is sent later by a scheduled job, so that
    quiet hours, retries, a daily cap and "is this still wanted?" checks
    all happen at send time. `dedupe_key` is UNIQUE, which is what makes
    sending the same text twice impossible even if two workers race.

    status: pending -> sending -> sent | failed ; or skipped / cancelled.
    """
    __tablename__ = "outbound_messages"
    id = Column(String, primary_key=True, default=gen_id)
    kind = Column(String, default="ready", index=True)      # ready | reminder | review | winback
    seq = Column(Integer, default=0)                        # reminder number (1, 2, 3); 0 = n/a
    repair_id = Column(String, ForeignKey("repairs.id"), nullable=True, index=True)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True, index=True)
    dedupe_key = Column(String, unique=True, nullable=False)
    status = Column(String, default="pending")
    source = Column(String, default="auto")                 # auto | manual | campaign
    due_at = Column(DateTime, default=datetime.utcnow)      # naive UTC like every timestamp in this app
    attempts = Column(Integer, default=0)
    claimed_at = Column(DateTime, nullable=True)
    to_phone = Column(String, default="")                   # E.164 actually used
    body = Column(Text, default="")                         # exact text sent (blank until then)
    provider_sid = Column(String, default="")
    error_code = Column(String, default="")
    last_error = Column(Text, default="")
    skip_reason = Column(String, default="")
    staff_name = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)

    repair = relationship("Repair")
    customer = relationship("Customer")


Index("ix_outbound_status_due", OutboundMessage.status, OutboundMessage.due_at)


class CardBatch(Base):
    """v47: what the Moneris terminal's end-of-day batch said for one
    shop-local day, entered by hand (or typed at Cash Up). Kept apart from
    CashSession so a past day's batch can be entered or corrected later, and
    so "was the batch ever entered?" is a real yes/no rather than a guess."""
    __tablename__ = "card_batches"
    id = Column(String, primary_key=True, default=gen_id)
    date = Column(String, unique=True, index=True)   # YYYY-MM-DD, shop-local
    amount = Column(Float, default=0)                # net batch total from the terminal (sales minus refunds)
    note = Column(Text, default="")
    resolved = Column(Boolean, default=False)        # a difference was investigated and explained
    entered_at = Column(DateTime, default=datetime.utcnow)


class CardStatement(Base):
    """v47: one month of the Moneris statement — the volume they processed
    and the fees they charged — so profit reports can subtract real card
    fees and the CRM's card sales can be checked against what Moneris says."""
    __tablename__ = "card_statements"
    id = Column(String, primary_key=True, default=gen_id)
    month = Column(String, unique=True, index=True)  # YYYY-MM
    volume = Column(Float, nullable=True)            # net card volume on the statement (sales minus refunds)
    fees = Column(Float, default=0)                  # total fees charged that month (incl. any tax on fees)
    note = Column(Text, default="")
    entered_at = Column(DateTime, default=datetime.utcnow)


class StockCount(Base):
    """v50: one stock-take session ("Cases shelf, Nov 22"). Lines hold what was physically counted; nothing in
    `products` changes until the count is applied."""
    __tablename__ = "stock_counts"
    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, default="")
    scope_category = Column(String, default="")     # "" = any product; otherwise only this category is reviewed / zero-filled
    status = Column(String, default="open", index=True)   # open | applied | cancelled
    note = Column(Text, default="")
    created_by = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_by = Column(String, default="")
    closed_at = Column(DateTime, nullable=True)
    items_changed = Column(Integer, default=0)       # filled when applied: products whose stock changed
    units_net = Column(Integer, default=0)           # ...net units added (+) or removed (-)
    value_net = Column(Float, default=0.0)           # ...the same at cost price
    zero_filled = Column(Boolean, default=False)     # were uncounted products in scope set to zero?
    lines = relationship("StockCountLine", back_populates="count", cascade="all, delete-orphan")


class StockCountLine(Base):
    """v50: what was counted for one product. `stock_at_count` is the system stock the moment the line was last
    changed; applying sets stock to `counted` plus whatever was sold or received since, so a sale rung through while
    the shelf was being counted is not undone."""
    __tablename__ = "stock_count_lines"
    __table_args__ = (UniqueConstraint("count_id", "product_id", name="uq_stock_count_line"),)
    id = Column(String, primary_key=True, default=gen_id)
    count_id = Column(String, ForeignKey("stock_counts.id"), nullable=False, index=True)
    product_id = Column(String, nullable=False, index=True)   # deliberately not a foreign key: a deleted product must not block anything
    sku = Column(String, default="")
    name = Column(String, default="")
    counted = Column(Integer, default=0)
    stock_at_count = Column(Integer, default=0)
    counted_by = Column(String, default="")
    first_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    applied_delta = Column(Integer, nullable=True)   # units added (+) / removed (-) when applied
    reason = Column(String, default="")              # v51: why the count differed from the system (see stockcount.REASONS); "" = not said
    unit_cost = Column(Float, nullable=True)         # v51: the product's cost when the count was applied, so a later cost edit can't rewrite the loss
    count = relationship("StockCount", back_populates="lines")


class SaleReturn(Base):
    """v52: a PARTIAL return: some items (or some of the quantity) of one invoice go back and the customer is refunded
    for just those. The invoice itself stays a normal paid sale; reports treat each return as a refund event on the
    day it happened, with the same fields a whole-invoice refund has (so the report code reads both the same way)."""
    __tablename__ = "sale_returns"
    __table_args__ = (UniqueConstraint("invoice_id", "seq", name="uq_sale_return_seq"),)
    id = Column(String, primary_key=True, default=gen_id)
    invoice_id = Column(String, ForeignKey("invoices.id"), nullable=False, index=True)
    seq = Column(Integer, default=1)                     # 1, 2, 3 ... per invoice
    number = Column(String, default="", index=True)      # "INV-1042-R1"
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    date = Column(DateTime, default=datetime.utcnow)     # when the ORIGINAL sale happened (reports show it as "Sold")
    refunded_at = Column(DateTime, default=datetime.utcnow, index=True)   # when the items came back / money went out
    refunded_by = Column(String, default="")
    refund_method = Column(String, default="")           # Cash | Card | Store Credit | E-Transfer
    refund_reason = Column(String, default="")
    subtotal = Column(Float, default=0)                  # list price x qty of the returned items
    discount = Column(Float, default=0)                  # the share of the invoice's discount that goes back with them
    tax_breakdown = Column(Text, default="")             # JSON [{label, amount}] — tax refunded
    tax_total = Column(Float, default=0)
    total = Column(Float, default=0)                     # what goes back to the customer (net + tax)
    cash_paid = Column(Float, default=0)                 # cash that physically left the drawer (5c-rounded); 0 unless method is Cash
    invoice = relationship("Invoice", back_populates="returns")
    customer = relationship("Customer")
    lines = relationship("SaleReturnLine", back_populates="sale_return", cascade="all, delete-orphan")

    # The reporting code was written around whole-invoice refunds and reads these off a refund event.
    refunded = True
    repair_id = None


class SaleReturnLine(Base):
    __tablename__ = "sale_return_lines"
    id = Column(String, primary_key=True, default=gen_id)
    return_id = Column(String, ForeignKey("sale_returns.id"), nullable=False, index=True)
    invoice_line_id = Column(String, nullable=True, index=True)   # deliberately not a foreign key: nothing may block a restore
    product_id = Column(String, nullable=True)
    name = Column(String, default="")
    sku = Column(String, default="")
    imei = Column(String, default="")
    qty = Column(Integer, default=1)
    price = Column(Float, default=0)                     # unit list price, as sold
    unit_cost = Column(Float, nullable=True)
    category = Column(String, nullable=True)
    restocked = Column(Boolean, default=False)           # did these units go back on the shelf?
    sale_return = relationship("SaleReturn", back_populates="lines")


class Expense(Base):
    """v52: money the shop spent, or cash taken out of the till. `paid_from` says whether it came out of the cash
    drawer (so Cash Up expects that much less) or was paid some other way (business card, bank, the owner's pocket)."""
    __tablename__ = "expenses"
    id = Column(String, primary_key=True, default=gen_id)
    spent_at = Column(DateTime, default=datetime.utcnow, index=True)
    amount = Column(Float, default=0)                    # total paid, tax included
    tax_paid = Column(Float, default=0)                  # GST/HST/PST included in `amount` (for input tax credits)
    category = Column(String, default="Other")
    description = Column(String, default="")
    payee = Column(String, default="")
    receipt_ref = Column(String, default="")
    paid_from = Column(String, default="drawer")         # drawer | other
    staff_id = Column(String, nullable=True)
    staff_name = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    voided = Column(Boolean, default=False)
    voided_at = Column(DateTime, nullable=True)
    voided_by = Column(String, default="")
    void_reason = Column(String, default="")


class QuoteApproval(Base):
    """v52: a repair quote sent to the customer for a yes / no, reached through a private link. One row per quote
    sent; sending a new quote for the same ticket retires the old one."""
    __tablename__ = "quote_approvals"
    id = Column(String, primary_key=True, default=gen_id)
    repair_id = Column(String, ForeignKey("repairs.id"), nullable=False, index=True)
    token = Column(String, unique=True, index=True)      # the unguessable part of the customer's link
    amount = Column(Float, default=0)                    # the quoted price before tax
    summary = Column(String, default="")                 # what the customer is agreeing to ("Screen replacement")
    note = Column(String, default="")                    # optional message from the shop shown with the quote
    status = Column(String, default="pending", index=True)   # pending | approved | declined | expired | replaced
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, default="")
    expires_at = Column(DateTime, nullable=True)
    sent_via = Column(String, default="")                # sms | link (copied by hand) | ""
    sent_to = Column(String, default="")                 # phone number, masked, for the record
    sms_note = Column(String, default="")                # what the text service said
    responded_at = Column(DateTime, nullable=True)
    responded_via = Column(String, default="")           # link | staff (recorded by phone / in person)
    responded_by = Column(String, default="")            # staff name when responded_via == staff
    response_note = Column(String, default="")           # the customer's comment, or staff's note
    responder_ip = Column(String, default="")
    responder_device = Column(String, default="")
    seen_at = Column(DateTime, nullable=True)            # staff opened the ticket after the answer came in
    repair = relationship("Repair")



class RepairPrice(Base):
    """v54: one line of the shop's repair price menu: "iPhone 15 + Screen Replacement = $129.00, uses part SCR-IP15".

    `model` is the phone model as the owner typed or picked it ("" means any phone, used for repairs that cost the same on
    every phone such as a software fix). `model_key` / `repair_key` are the same words lower-cased with the punctuation
    squeezed out, so "iPhone 15" and "iphone  15" are one row. The price is what the customer is charged BEFORE tax, the
    same meaning as a ticket's estimate.

    `product_id` points at the part the repair normally uses. It is deliberately NOT a foreign key: deleting a product
    must stay possible, and a backup restore replaces products and this table independently. A missing product just means
    "no part suggested"."""
    __tablename__ = "repair_prices"
    id = Column(String, primary_key=True, default=gen_id)
    model = Column(String, default="")
    model_key = Column(String, default="", index=True)
    repair = Column(String, nullable=False)
    repair_key = Column(String, nullable=False, index=True)
    price = Column(Float, default=0)
    product_id = Column(String, default="")
    minutes = Column(Integer, nullable=True)
    updated_by = Column(String, default="")
    updated_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("model_key", "repair_key", name="uq_repair_price_model_repair"),)


class ShopLogo(Base):
    """v54: the shop's own logo for the customer pages (status page, quote page). One row, id 'logo'.

    Only a JPEG, PNG or WebP that passed `intake.sniff_image` is stored (never SVG, which can carry script). It is small
    (a few hundred KB at most), so unlike repair photos it travels in the backup file with the rest of the shop's settings."""
    __tablename__ = "shop_logo"
    id = Column(String, primary_key=True, default="logo")
    mime = Column(String, default="image/png")
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    data = Column(LargeBinary, nullable=False)
    size = Column(Integer, default=0)
    updated_by = Column(String, default="")
    updated_at = Column(DateTime, default=datetime.utcnow)
