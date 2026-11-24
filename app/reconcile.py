"""
Moneris reconciliation (v47).

The shop rings every sale on the Moneris terminal AND again in the CRM, so
the risk is a sale that exists in only one of them. This module:

  * lists the card transactions the CRM expects for a day (`crm_card_rows`),
  * reads a Moneris transaction export (CSV pasted or uploaded) without
    knowing its exact layout (`parse_bank_csv`), and
  * pairs the two sides up (`match_transactions`) so what is left over —
    "charged on the terminal but not in the CRM" and "in the CRM but not on
    the terminal" — is exactly what needs a look,
  * plus the day-end checks (`day_end_checks`) that flag a missed sale, an
    unentered batch or an unclosed cash-up.

The parsing/matching functions are pure (no database) so they can be tested
directly. All money here is handled in integer cents.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta

from sqlalchemy import func

from .localtime import day_bounds_utc, local_today, to_local, to_local_date
from .models import CardBatch, CashSession, Invoice, LayawayPayment, Repair, SaleReturn, Setting
from .money import to_cents

# ── numbers and dates ─────────────────────────────────────────────────

_MONEY = re.compile(r"^\(?-?\$?\s*-?[\d,]*\.?\d+\)?-?$")


def parse_money(s) -> int | None:
    """'$1,234.56' -> 123456, '(12.00)' / '-12.00' / '12.00-' -> negative,
    None when the cell is not a number."""
    if s is None:
        return None
    t = str(s).strip().replace(" ", " ")
    if not t or not _MONEY.match(t.replace(" ", "")):
        return None
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-") or t.endswith("-") or "-$" in t
    digits = re.sub(r"[^\d.]", "", t)
    if digits.count(".") > 1 or not re.search(r"\d", digits):
        return None
    if len(digits.split(".")[0]) > 10:          # beyond $10 billion it is not a dollar amount (and float would overflow)
        return None
    c = to_cents(float(digits))
    return -c if neg else c


def _amountish(cell: str) -> bool:
    """A cell that is clearly a dollar amount (has a decimal point, comma or $),
    as opposed to a plain digit string that may be a reference number."""
    return parse_money(cell) is not None and any(ch in str(cell) for ch in ".,$")


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m-%d-%Y", "%b %d, %Y", "%d %b %Y", "%Y%m%d", "%m/%d/%y")


def parse_date_cell(s) -> date | None:
    t = str(s or "").strip()
    if not t:
        return None
    t = t.split("T")[0]
    head = t.split(" ")[0] if re.match(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", t) else t
    for cand in (head, t):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
        # "Nov 20, 2026 14:32" — drop a trailing time and retry
        m = re.match(r"^(.*?\d{4})\s+\d{1,2}:\d{2}", cand)
        if m:
            for fmt in _DATE_FORMATS:
                try:
                    return datetime.strptime(m.group(1), fmt).date()
                except ValueError:
                    continue
    return None


def norm_ref(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


# ── reading a Moneris export ──────────────────────────────────────────

_AMT_WORDS = ("amount", "amt", "total", "sale", "price")
_AMT_BAD = ("tax", "tip", "fee", "surcharge", "cashback", "cash back", "count", "discount", "balance", "batch", "number", "no.", "#")
_DATE_WORDS = ("date", "time")
_TYPE_WORDS = ("type", "transaction", "trans", "txn", "action")
_HEADER_HINTS = _AMT_WORDS + _DATE_WORDS + ("ref", "auth", "approval", "order", "card", "batch", "terminal", "type", "id")


def _sniff(text: str) -> str:
    head = "\n".join(text.splitlines()[:20])
    counts = {d: head.count(d) for d in (",", "\t", ";", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def read_table(text: str) -> list[list[str]]:
    """Any pasted/uploaded delimited text -> rows of cells (blank rows dropped)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not text.strip():
        return []
    delim = _sniff(text)
    rows = []
    for r in csv.reader(io.StringIO(text), delimiter=delim):
        cells = [c.strip() for c in r]
        if any(cells):
            rows.append(cells)
    return rows


def _looks_like_header(cells: list[str]) -> bool:
    if len(cells) < 2:
        return False
    low = [c.lower() for c in cells]
    if any(parse_money(c) is not None and ("." in c or c.strip("-").isdigit()) for c in cells):
        return False
    return any(any(h in c for h in _HEADER_HINTS) for c in low)


def find_header(rows: list[list[str]]) -> int | None:
    """Index of the header row (Moneris exports can start with title lines)."""
    for i, cells in enumerate(rows[:25]):
        if _looks_like_header(cells):
            low = " ".join(c.lower() for c in cells)
            if any(w in low for w in _AMT_WORDS):
                return i
    for i, cells in enumerate(rows[:25]):
        if _looks_like_header(cells):
            return i
    return None


def detect_columns(headers: list[str], body: list[list[str]]) -> dict:
    """Best guess at which columns hold the amount, the transaction type and
    the date. Anything that can't be told is left None — the screen then
    lets the owner pick."""
    cols = {"amount": None, "type": None, "date": None}
    low = [h.lower() for h in headers]

    def score_amount(h):
        if any(b in h for b in _AMT_BAD):
            return 0
        if h in ("amount", "transaction amount", "txn amount", "total amount", "sale amount", "trans amount"):
            return 5
        if "amount" in h or "amt" in h:
            return 4
        if "total" in h:
            return 2
        return 1 if any(w in h for w in _AMT_WORDS) else 0

    best = max(range(len(low)), key=lambda i: score_amount(low[i]), default=None) if low else None
    if best is not None and score_amount(low[best]) > 0:
        cols["amount"] = best
    best_date = 0
    for i, h in enumerate(low):
        if cols["type"] is None and "card" not in h and ("type" in h or h in ("action", "trans action", "transaction")):
            cols["type"] = i
        if any(w in h for w in _DATE_WORDS) and not any(b in h for b in ("settle", "deposit", "batch", "post")):
            sc = 3 if any(w in h for w in ("transaction", "trans", "txn")) else 2
            if sc > best_date:
                cols["date"], best_date = i, sc
    # headerless / unrecognised: pick the column most cells of which look like money with cents
    if cols["amount"] is None and body:
        width = max(len(r) for r in body)
        best_i, best_n = None, 0
        for i in range(width):
            n = sum(1 for r in body if i < len(r) and parse_money(r[i]) is not None and "." in r[i])
            if n > best_n:
                best_i, best_n = i, n
        if best_i is not None and best_n >= max(1, len(body) // 2):
            cols["amount"] = best_i
    if cols["date"] is None and body:
        width = max(len(r) for r in body)
        for i in range(width):
            n = sum(1 for r in body if i < len(r) and parse_date_cell(r[i]) is not None)
            if n >= max(1, len(body) // 2):
                cols["date"] = i
                break
    return cols


_REFUND_WORDS = ("refund", "return")
_VOID_WORDS = ("void", "reversal", "cancel", "correction", "decline", "denied", "error", "fail")


def parse_bank_csv(text: str, day: date | None = None, all_dates: bool = False, cols_override: dict | None = None) -> dict:
    """Turns a pasted/uploaded Moneris transaction export into comparable rows.

    Returns {headers, cols, rows, voids, other_dates, error}. `rows` are
    sales/refunds for `day` (or every row when no date column can be read or
    `all_dates` is set); each carries amount_c (always positive), kind
    ('sale'|'refund'), the cell tokens used for reference matching, and a
    short label for display."""
    table = read_table(text)
    if not table:
        return {"headers": [], "cols": {}, "rows": [], "voids": [], "other_dates": 0, "error": "Nothing to read — paste the Moneris transaction list or upload its CSV."}
    hi = find_header(table)
    headers = table[hi] if hi is not None else [f"Column {i + 1}" for i in range(max(len(r) for r in table))]
    body = table[hi + 1:] if hi is not None else table
    cols = detect_columns(headers, body)
    for k, v in (cols_override or {}).items():
        if k in cols and v not in (None, ""):
            try:
                cols[k] = int(v)
            except (TypeError, ValueError):
                pass
    if cols["amount"] is None:
        return {"headers": headers, "cols": cols, "rows": [], "voids": [], "other_dates": 0,
                "error": "Couldn't tell which column holds the amount — pick it below."}

    rows, voids, other = [], [], 0
    skip = {cols["amount"], cols["type"], cols["date"]}
    for idx, cells in enumerate(body):
        if cols["amount"] >= len(cells):
            continue
        amt = parse_money(cells[cols["amount"]])
        if amt is None or amt == 0:
            continue  # subtotal lines, notes, blank amounts
        d = parse_date_cell(cells[cols["date"]]) if cols["date"] is not None and cols["date"] < len(cells) else None
        if day and d and d != day and not all_dates:
            other += 1
            continue
        typ = cells[cols["type"]].lower() if cols["type"] is not None and cols["type"] < len(cells) else ""
        row_text = " ".join(c.lower() for i, c in enumerate(cells) if i not in (cols["amount"], cols["date"])) if not typ else typ
        label = " · ".join(c for i, c in enumerate(cells) if c and i != cols["amount"])[:90]
        tokens = []
        for i, c in enumerate(cells):
            if i in skip or not c:
                continue
            t = norm_ref(c)
            if len(t) >= 4 and not _amountish(c):
                tokens.append(t)
        base = {"idx": idx, "amount_c": abs(amt), "tokens": tokens, "label": label, "date": d, "raw": cells}
        if any(w in typ for w in _VOID_WORDS):
            voids.append(dict(base, kind="void"))
            continue
        if any(w in typ for w in _REFUND_WORDS) or amt < 0:
            rows.append(dict(base, kind="refund"))
        else:
            rows.append(dict(base, kind="sale"))
    return {"headers": headers, "cols": cols, "rows": rows, "voids": voids, "other_dates": other, "error": None}


# ── what the CRM expects for a day ────────────────────────────────────

def crm_card_rows(db, tz, d: date) -> list[dict]:
    """Every card transaction the CRM expects the terminal to show for local
    day `d`. The sum of the non-optional rows (sales minus refunds) equals
    reporting.cash_card_totals(...)['card_sales'], i.e. what Cash Up
    compares to the batch.

    A sale refunded the same day is `optional`: depending on whether it was
    voided before the batch closed or refunded after, the terminal may show
    both lines or neither, so it is never reported as missing."""
    s, e = day_bounds_utc(d, d, tz)
    out = []
    keep_methods = ("Store Credit", "E-Transfer")
    for inv in db.query(Invoice).filter(Invoice.date >= s, Invoice.date < e, Invoice.card_amount > 0).order_by(Invoice.date).all():
        same_day_refund = bool(inv.refunded and (inv.refunded_at is None or inv.refunded_at < e))
        optional = same_day_refund and inv.refund_method not in keep_methods
        who = inv.customer.name if inv.customer else "Walk-in"
        out.append({"key": f"inv:{inv.id}", "kind": "sale", "label": inv.number, "href": f"/invoices/{inv.id}",
                    "when": to_local(inv.date, tz), "who": who, "amount_c": to_cents(inv.card_amount),
                    "ref": inv.card_reference or "", "optional": optional,
                    "note": "refunded the same day" if optional else ("refunded later" if inv.refunded else "")})
        if optional and inv.refund_method == "Card":
            out.append({"key": f"invref:{inv.id}", "kind": "refund", "label": f"Refund of {inv.number}", "href": f"/invoices/{inv.id}",
                        "when": to_local(inv.refunded_at or inv.date, tz), "who": who, "amount_c": to_cents(inv.total),
                        "ref": "", "optional": True, "note": "same-day card refund"})
    # refunds paid out today for EARLIER days' sales
    for inv in (db.query(Invoice).filter(Invoice.refunded.is_(True), Invoice.refunded_at.isnot(None),
                                         Invoice.refunded_at >= s, Invoice.refunded_at < e, Invoice.date < s,
                                         Invoice.refund_method == "Card").order_by(Invoice.refunded_at).all()):
        out.append({"key": f"invref:{inv.id}", "kind": "refund", "label": f"Refund of {inv.number}", "href": f"/invoices/{inv.id}",
                    "when": to_local(inv.refunded_at, tz), "who": inv.customer.name if inv.customer else "Walk-in",
                    "amount_c": to_cents(inv.total), "ref": "", "optional": False, "note": "refund of an earlier sale"})
    # v52: items taken back from a sale and refunded to the card (the sale itself stays a normal sale)
    for r in (db.query(SaleReturn).filter(SaleReturn.refunded_at >= s, SaleReturn.refunded_at < e, SaleReturn.refund_method == "Card")
              .order_by(SaleReturn.refunded_at).all()):
        out.append({"key": f"ret:{r.id}", "kind": "refund", "label": f"Return {r.number}", "href": f"/returns/{r.id}",
                    "when": to_local(r.refunded_at, tz), "who": r.customer.name if r.customer else "Walk-in",
                    "amount_c": to_cents(r.total), "ref": "", "optional": False, "note": "part of a sale returned"})
    for p in db.query(LayawayPayment).filter(LayawayPayment.created_at >= s, LayawayPayment.created_at < e,
                                              LayawayPayment.method.in_(("Debit", "Credit"))).order_by(LayawayPayment.created_at).all():
        out.append({"key": f"lay:{p.id}", "kind": "sale", "label": "Layaway payment", "href": f"/layaway/{p.layaway_id}",
                    "when": to_local(p.created_at, tz), "who": (p.layaway.customer.name if p.layaway and getattr(p.layaway, "customer", None) else "Layaway"),
                    "amount_c": to_cents(p.amount), "ref": "", "optional": False, "note": f"{p.method} instalment"})
    out.sort(key=lambda r: r["when"])
    return out


def crm_net_cents(rows: list[dict]) -> int:
    return sum((r["amount_c"] if r["kind"] == "sale" else -r["amount_c"]) for r in rows if not r["optional"])


# ── pairing the two sides ─────────────────────────────────────────────

def _ref_hit(crm_ref: str, tokens: list[str]) -> bool:
    r = norm_ref(crm_ref)
    if len(r) < 4:
        return False
    for t in tokens:
        if t == r:
            return True
        if len(r) >= 5 and len(t) >= 5 and (r in t or t in r):
            return True
    return False


def match_transactions(crm_rows: list[dict], bank_rows: list[dict]) -> dict:
    """Pairs CRM rows with terminal rows.

      1. same kind + the CRM reference is found on the terminal row
            -> 'ok' (amounts agree) or 'amount' (reference agrees, money differs)
      2. same kind + same amount, reference not usable
            -> 'noref'    the CRM sale has no reference (matched by amount only)
            -> 'refcheck' the CRM has a reference but the terminal row doesn't show it
               (typo in the reference, or the export uses another code)

    What is left is `crm_only` (optional same-day-refund rows are dropped from
    it) and `bank_only` (charged on the terminal, not in the CRM)."""
    pairs, used_c, used_b = [], set(), set()

    for ci, c in enumerate(crm_rows):
        if not c["ref"]:
            continue
        hits = [bi for bi, b in enumerate(bank_rows)
                if bi not in used_b and b["kind"] == c["kind"] and _ref_hit(c["ref"], b["tokens"])]
        if not hits:
            continue
        # a short reference (e.g. last four digits) can hit several rows — prefer the one with the same amount
        bi = next((h for h in hits if bank_rows[h]["amount_c"] == c["amount_c"]), hits[0])
        b = bank_rows[bi]
        pairs.append({"crm": c, "bank": b, "status": "ok" if c["amount_c"] == b["amount_c"] else "amount"})
        used_c.add(ci); used_b.add(bi)

    for ci, c in enumerate(crm_rows):
        if ci in used_c:
            continue
        for bi, b in enumerate(bank_rows):
            if bi in used_b or b["kind"] != c["kind"] or b["amount_c"] != c["amount_c"]:
                continue
            pairs.append({"crm": c, "bank": b, "status": "refcheck" if c["ref"] else "noref"})
            used_c.add(ci); used_b.add(bi)
            break

    crm_only = [c for ci, c in enumerate(crm_rows) if ci not in used_c and not c["optional"]]
    bank_only = [b for bi, b in enumerate(bank_rows) if bi not in used_b]
    bank_net = sum((b["amount_c"] if b["kind"] == "sale" else -b["amount_c"]) for b in bank_rows)
    return {"pairs": pairs, "crm_only": crm_only, "bank_only": bank_only,
            "bank_net_c": bank_net, "crm_net_c": crm_net_cents(crm_rows),
            "clean": not (crm_only or bank_only or any(p["status"] in ("amount", "refcheck") for p in pairs))}


# ── day-end checks ────────────────────────────────────────────────────

def recon_start(db, tz) -> date:
    row = db.get(Setting, "recon_start")
    d = parse_date_cell(row.value) if row and row.value else None
    return d or local_today(tz)


def _fmt_day(d: date) -> str:
    return d.strftime("%b %d").replace(" 0", " ")


def day_end_checks(db, tz, full: bool = False, lookback_days: int = 14) -> list[dict]:
    """Things that look like a missed or unconfirmed sale. Each item:
    {kind, severity ('bad'|'warn'), message, href, date}.

      * a repair marked Collected with money attached but no sale
      * a past day with card activity and no Moneris batch entered
      * a past day with sales and no Cash Up closed
      * (full=True) a past day whose entered batch differs from the CRM's card total

    Only days from the day this feature was first switched on (recon_start)
    are judged, so old history never shows up as 'missing'."""
    today = local_today(tz)
    start = max(recon_start(db, tz), today - timedelta(days=lookback_days))
    items: list[dict] = []

    # 1) repairs collected but never rung up
    cutoff = datetime.utcnow() - timedelta(days=30)
    collected = db.query(Repair).filter(Repair.status == "COLLECTED", Repair.updated_at >= cutoff).all()
    if collected:
        linked = {rid for (rid,) in db.query(Invoice.repair_id).filter(Invoice.repair_id.in_([r.id for r in collected])).all()}
        # only repairs collected since the check was switched on
        s_utc, _ = day_bounds_utc(recon_start(db, tz), recon_start(db, tz), tz)
        for r in sorted(collected, key=lambda r: r.ticket_no or 0):
            if r.id in linked or (r.updated_at and r.updated_at < s_utc):
                continue
            amount = r.final_cost if r.final_cost is not None else (r.estimated_cost or 0)
            if (amount or 0) > 0:
                items.append({"kind": "repair_no_sale", "severity": "bad", "date": None, "href": f"/repairs/{r.id}",
                              "message": f"Repair #{r.ticket_no} ({r.device or 'device'}) is Collected but has no sale — ${amount:.2f} may not have been rung up."})

    # 2) + 3) past days: card batch / cash-up
    s_utc, _ = day_bounds_utc(start, start, tz)
    _, e_utc = day_bounds_utc(today, today, tz)
    invs = db.query(Invoice).filter(Invoice.date >= s_utc, Invoice.date < e_utc).all()
    traded, card_days = set(), set()
    for i in invs:
        d = to_local_date(i.date, tz)
        traded.add(d)
        if (i.card_amount or 0) > 0:
            card_days.add(d)
    for p in db.query(LayawayPayment).filter(LayawayPayment.created_at >= s_utc, LayawayPayment.created_at < e_utc):
        d = to_local_date(p.created_at, tz)
        traded.add(d)
        if p.method in ("Debit", "Credit"):
            card_days.add(d)
    for i in db.query(Invoice).filter(Invoice.refunded.is_(True), Invoice.refund_method == "Card",
                                      Invoice.refunded_at >= s_utc, Invoice.refunded_at < e_utc):
        card_days.add(to_local_date(i.refunded_at, tz))
    for r in db.query(SaleReturn).filter(SaleReturn.refund_method == "Card", SaleReturn.refunded_at >= s_utc, SaleReturn.refunded_at < e_utc):
        card_days.add(to_local_date(r.refunded_at, tz))      # v52: a card return also shows on that day's terminal batch
    batches = {b.date: b for b in db.query(CardBatch).filter(CardBatch.date >= start.isoformat()).all()}
    sessions = {c.date for c in db.query(CashSession).filter(CashSession.date >= start.isoformat()).all()}

    for d in sorted(card_days):
        if d >= today:
            continue
        if d.isoformat() not in batches:
            items.append({"kind": "batch_missing", "severity": "warn", "date": d, "href": f"/reconcile?date={d.isoformat()}",
                          "message": f"Moneris batch for {_fmt_day(d)} hasn't been entered — card sales can't be checked."})
    for d in sorted(traded):
        if d >= today:
            continue
        if d.isoformat() not in sessions:
            items.append({"kind": "cashup_missing", "severity": "warn", "date": d, "href": "/cashup",
                          "message": f"Cash Up wasn't closed for {_fmt_day(d)}."})

    # 4) entered batches that don't agree (needs the day's card total, so only on the full page)
    if full:
        from .reporting import cash_card_totals
        for ds, b in sorted(batches.items()):
            d = date.fromisoformat(ds)
            if d >= today or b.resolved:
                continue
            crm = cash_card_totals(db, tz, d)["card_sales"]
            diff = round((b.amount or 0) - crm, 2)
            if abs(diff) > 0.009:
                items.append({"kind": "batch_differs", "severity": "bad", "date": d, "href": f"/reconcile?date={ds}",
                              "message": f"{_fmt_day(d)}: Moneris batch ${b.amount:,.2f} vs CRM ${crm:,.2f} — off by ${diff:+,.2f}."})
    items.sort(key=lambda x: (0 if x["severity"] == "bad" else 1, x["date"] or today))
    return items


def summarize_checks(items: list[dict], cap: int = 3) -> list[dict]:
    """Collapses the day-end check items into a few readable lines for the
    dashboard: repairs/differences individually (up to `cap`), missing
    batches and cash-ups grouped into one line each."""
    out: list[dict] = []
    for kind in ("repair_no_sale", "batch_differs"):
        group = [i for i in items if i["kind"] == kind]
        for i in group[:cap]:
            out.append({"severity": i["severity"], "message": i["message"], "href": i["href"]})
        if len(group) > cap:
            out.append({"severity": group[0]["severity"], "message": f"…and {len(group) - cap} more — open Moneris Check.", "href": "/reconcile"})
    for kind, noun, href in (("batch_missing", "Moneris batch not entered for", None), ("cashup_missing", "Cash Up not closed for", "/cashup")):
        group = [i for i in items if i["kind"] == kind]
        if group:
            days = ", ".join(_fmt_day(i["date"]) for i in group[:4]) + (" …" if len(group) > 4 else "")
            out.append({"severity": "warn", "message": f"{noun} {days}.", "href": href or group[0]["href"]})
    return out

