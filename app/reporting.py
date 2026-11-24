"""
Report Center engine — builds every report as a `report_types.Report`.

Accounting conventions (the same ones in every report, so numbers agree
across pages):

  * A sale belongs to the shop-local calendar day it was rung up on.
  * Net sales  = invoice total minus tax (tax is owed to the government,
    it is not revenue). Discounts, loyalty points and store credit applied
    are already out of that figure.
  * A refund is its own event, dated when it happened (`refunded_at`), and
    reduces the period it happened in — it does NOT rewrite the original
    sale day. Legacy refunds with no recorded date fall back to the sale
    date (so they net to zero inside one period, as before).
  * Cost of goods sold uses the cost frozen onto each invoice line at the
    time of sale (`unit_cost`), falling back to the product's current cost
    for older lines. Repair parts cost comes from RepairPart.unit_cost.
  * Refunded goods go back on the shelf (the refund route restocks), so
    their line cost is reversed; repair parts consumed are not.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import selectinload

from .localtime import (
    day_bounds_utc, get_shop_tz, local_now, local_today, month_bounds, parse_date,
    parse_month, to_local, to_local_date, tz_label,
)
from .models import (
    AuditLog, CardStatement, CashSession, Customer, Invoice, InvoiceLine, Layaway, LayawayPayment,
    OutboundMessage, Product, Repair, RepairPart, SaleReturn, SaleReturnLine, Setting, StockCount, StockCountLine, TradeIn,
)
from . import stockcount as _stockcount
from .money import cash_refund_amount
from . import returns as _returns
from . import expenses as _expenses
from .cardfees import card_fees_for_period, crm_card_month, month_key
from .product_const import CATEGORY_LABELS
from .repairs_const import STATUS_LABELS
from .report_types import Dec, Kpi, Money, Pct, Report, Section

CAT_NAMES = {k: v.split(" ", 1)[1] if " " in v else v for k, v in CATEGORY_LABELS.items()}
CAT_NAMES.update({"REPAIR": "Repair / Service", "UNCATEGORIZED": "Uncategorized", "OTHER": "Other / custom items"})

WIP_STATUSES = ("RECEIVED", "DIAGNOSED", "WAITING", "IN_PROGRESS")
PICKUP_STATUSES = ("READY", "COMPLETED")


# ── small helpers ─────────────────────────────────────────────────────

def r2(x) -> float:
    return round(float(x or 0), 2)


def pct(part, whole) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def chunked(seq, n=500):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def cat_name(code: str) -> str:
    return CAT_NAMES.get(code or "", (code or "Uncategorized").title())


def change_sub(cur, prev, label) -> str:
    if not prev:
        return f"no data for {label}" if not cur else f"new vs {label}"
    delta = (cur - prev) / abs(prev) * 100
    arrow = "▲" if delta >= 0 else "▼"
    return f"{arrow} {abs(delta):.0f}% vs {label}"


def tax_name(label: str) -> str:
    """'GST (5%)' -> 'GST' so a rate change doesn't split the filing line."""
    m = re.match(r"^(.*?)\s*\(", label or "")
    return (m.group(1) if m else label or "Tax").strip() or "Tax"


def tender_label(inv) -> str:
    pm = inv.payment_method or "Unknown"
    if pm.startswith("Split ("):
        return "Split (cash + card)"
    if pm.startswith("Layaway ("):
        return "Layaway payoff"
    return pm


def _hist_dates(repair) -> dict:
    """status -> first UTC datetime it was entered, from the repair's JSON history."""
    out = {}
    try:
        hist = json.loads(repair.status_history or "[]")
    except (ValueError, TypeError):
        hist = []
    for h in hist:
        st, dt = h.get("status"), h.get("date")
        if not st or not dt or st in out:
            continue
        try:
            out[st] = datetime.fromisoformat(dt)
        except ValueError:
            continue
    out.setdefault("RECEIVED", repair.created_at)
    return out


def device_brand(device: str) -> str:
    d = (device or "").strip()
    low = d.lower()
    table = [
        (("iphone", "ipad", "macbook", "imac", "apple watch", "airpods"), "Apple"),
        (("galaxy", "samsung"), "Samsung"), (("pixel", "google"), "Google"),
        (("moto", "motorola"), "Motorola"), (("oneplus",), "OnePlus"), (("lg ",), "LG"),
        (("switch", "nintendo"), "Nintendo"), (("playstation", "ps4", "ps5", "sony"), "Sony"),
        (("xbox",), "Microsoft"), (("surface",), "Microsoft"),
    ]
    for keys, brand in table:
        if any(low.startswith(k) or f" {k}" in f" {low}" for k in keys):
            return brand
    return d.split()[0].title() if d else "Unknown"


# ── invoice-level figures ─────────────────────────────────────────────

class SalesWindow:
    """All invoices sold, and refund events, between two local dates
    (inclusive), with per-invoice figures worked out once and reused by
    every breakdown below."""

    def __init__(self, db, tz, d1: date, d2: date):
        self.db, self.tz, self.d1, self.d2 = db, tz, d1, d2
        s, e = day_bounds_utc(d1, d2, tz)
        self.start_utc, self.end_utc = s, e
        self.products = {p.id: p for p in db.query(Product).all()}
        opts = (selectinload(Invoice.lines), selectinload(Invoice.staff), selectinload(Invoice.customer))
        self.sold = (db.query(Invoice).options(*opts)
                     .filter(Invoice.date >= s, Invoice.date < e).order_by(Invoice.date).all())
        self.refunds = (db.query(Invoice).options(*opts)
                        .filter(Invoice.refunded.is_(True),
                                or_(and_(Invoice.refunded_at.isnot(None), Invoice.refunded_at >= s, Invoice.refunded_at < e),
                                    and_(Invoice.refunded_at.is_(None), Invoice.date >= s, Invoice.date < e)))
                        .order_by(Invoice.date).all())
        # v52: a partial return is a refund event too (it duck-types the invoice fields reports read)
        self.refunds = sorted(self.refunds + _returns.in_window(db, s, e), key=self.refund_ts)
        repair_ids = {i.repair_id for i in self.sold + self.refunds if i.repair_id}
        self.parts_cost = {}
        for ids in chunked(repair_ids):
            for rid, cost in (db.query(RepairPart.repair_id, func.sum(RepairPart.qty * RepairPart.unit_cost))
                              .filter(RepairPart.repair_id.in_(ids)).group_by(RepairPart.repair_id)):
                self.parts_cost[rid] = float(cost or 0)
        self.unknown_cost_lines = 0
        self.fig = {}
        for inv in self.sold + self.refunds:
            if inv.id not in self.fig:
                self.fig[inv.id] = self._figures(inv)

    # one invoice -> numbers ------------------------------------------------
    def _line_cost(self, line):
        if line.unit_cost is not None:
            return float(line.unit_cost), True
        if not line.product_id:
            return 0.0, True          # custom / repair-labour line: no product cost by design
        p = self.products.get(line.product_id)
        if p is not None:
            return float(p.cost or 0), True
        return 0.0, False             # product deleted and cost never snapshotted

    def _line_cat(self, line, inv):
        if line.category:
            return line.category
        p = self.products.get(line.product_id) if line.product_id else None
        if p is not None and p.category:
            return p.category
        if (line.sku or "").startswith("RPR-") or inv.repair_id:
            return "REPAIR"
        return "UNCATEGORIZED" if line.product_id else "OTHER"

    def _figures(self, inv):
        lines = list(inv.lines)
        lines_gross = sum((l.price or 0) * (l.qty or 0) for l in lines)
        net = float(inv.total or 0) - float(inv.tax_total or 0)
        ratio = net / lines_gross if lines_gross else 0.0
        cogs_lines, per_cat, per_prod = 0.0, defaultdict(lambda: {"net": 0.0, "cost": 0.0, "units": 0}), {}
        for l in lines:
            cost, known = self._line_cost(l)
            if not known:
                self.unknown_cost_lines += 1
            qty = l.qty or 0
            line_net = (l.price or 0) * qty * ratio
            cat = self._line_cat(l, inv)
            # v52: a returned item that did NOT go back on the shelf (broken, opened) is a written-off
            # cost, so its cost is not reversed. Invoice lines have no `restocked` flag -> True.
            keep = cost * qty if getattr(l, "restocked", True) else 0.0
            cogs_lines += keep
            c = per_cat[cat]
            c["net"] += line_net; c["cost"] += keep; c["units"] += qty
            key = l.product_id or l.name or "custom"
            pr = per_prod.setdefault(key, {"name": l.name, "sku": l.sku, "cat": cat, "units": 0, "net": 0.0, "cost": 0.0})
            pr["units"] += qty; pr["net"] += line_net; pr["cost"] += keep
        parts = self.parts_cost.get(inv.repair_id, 0.0) if inv.repair_id else 0.0
        if parts:
            per_cat["REPAIR"]["cost"] += parts
        try:
            tax_lines = json.loads(inv.tax_breakdown or "[]")
        except (ValueError, TypeError):
            tax_lines = []
        return {
            "gross": float(inv.subtotal or 0), "discount": float(inv.discount or 0), "net": net,
            "tax": float(inv.tax_total or 0), "total": float(inv.total or 0),
            "cogs_lines": cogs_lines, "cogs_parts": parts, "lines_gross": lines_gross,
            "per_cat": dict(per_cat), "per_prod": per_prod, "tax_lines": tax_lines,
        }

    def refund_ts(self, inv) -> datetime:
        return inv.refunded_at or inv.date

    # aggregates -----------------------------------------------------------
    def totals(self) -> dict:
        t = defaultdict(float)
        for k in ("gross", "discount", "sold_net", "sold_tax", "sold_total", "cogs", "refund_net", "refund_tax",
                  "refund_total", "refund_cogs"):
            t[k] = 0.0
        for inv in self.sold:
            f = self.fig[inv.id]
            t["gross"] += f["gross"]; t["discount"] += f["discount"]
            t["sold_net"] += f["net"]; t["sold_tax"] += f["tax"]; t["sold_total"] += f["total"]
            t["cogs"] += f["cogs_lines"] + f["cogs_parts"]
        for inv in self.refunds:
            f = self.fig[inv.id]
            t["refund_net"] += f["net"]; t["refund_tax"] += f["tax"]; t["refund_total"] += f["total"]
            t["refund_cogs"] += f["cogs_lines"]
        t["count"] = len(self.sold)
        t["refund_count"] = len(self.refunds)
        t["net_sales"] = t["sold_net"] - t["refund_net"]
        t["tax_net"] = t["sold_tax"] - t["refund_tax"]
        t["takings"] = t["sold_total"] - t["refund_total"]
        t["cogs_net"] = t["cogs"] - t["refund_cogs"]
        t["gross_profit"] = t["net_sales"] - t["cogs_net"]
        t["margin"] = pct(t["gross_profit"], t["net_sales"])
        t["avg_ticket"] = t["sold_net"] / t["count"] if t["count"] else 0.0
        return dict(t)

    def by_day(self) -> dict:
        days = defaultdict(lambda: defaultdict(float))
        for inv in self.sold:
            d = to_local_date(inv.date, self.tz); f = self.fig[inv.id]; x = days[d]
            x["count"] += 1; x["gross"] += f["gross"]; x["discount"] += f["discount"]
            x["net"] += f["net"]; x["tax"] += f["tax"]; x["total"] += f["total"]
            x["cogs"] += f["cogs_lines"] + f["cogs_parts"]
        for inv in self.refunds:
            d = to_local_date(self.refund_ts(inv), self.tz); f = self.fig[inv.id]; x = days[d]
            x["refund_count"] += 1; x["refund_net"] += f["net"]; x["refund_tax"] += f["tax"]
            x["refund_total"] += f["total"]; x["cogs"] -= f["cogs_lines"]
        return days

    def by_hour(self) -> dict:
        out = defaultdict(lambda: [0, 0.0])
        for inv in self.sold:
            h = to_local(inv.date, self.tz).hour
            out[h][0] += 1; out[h][1] += self.fig[inv.id]["net"]
        return out

    def by_weekday(self) -> dict:
        out = defaultdict(lambda: [0, 0.0])
        for d, x in self.by_day().items():
            if x["count"]:                      # a refund-only day is not a trading day
                out[d.weekday()][0] += 1
            out[d.weekday()][1] += x["net"] - x["refund_net"]
        return out

    def by_category(self) -> dict:
        cats = defaultdict(lambda: {"net": 0.0, "cost": 0.0, "units": 0})
        for inv in self.sold:
            for c, v in self.fig[inv.id]["per_cat"].items():
                cats[c]["net"] += v["net"]; cats[c]["cost"] += v["cost"]; cats[c]["units"] += v["units"]
        for inv in self.refunds:
            for c, v in self.fig[inv.id]["per_cat"].items():
                cats[c]["net"] -= v["net"]
                if c != "REPAIR":
                    cats[c]["cost"] -= v["cost"]
                cats[c]["units"] -= v["units"]
        return cats

    def by_tender(self) -> dict:
        out = defaultdict(lambda: [0, 0.0, 0.0])
        for inv in self.sold:
            o = out[tender_label(inv)]
            o[0] += 1; o[1] += self.fig[inv.id]["net"]; o[2] += self.fig[inv.id]["total"]
        return out

    def by_staff(self) -> dict:
        out = defaultdict(lambda: defaultdict(float))
        for inv in self.sold:
            n = inv.staff.name if inv.staff else "Unknown"
            f = self.fig[inv.id]; o = out[n]
            o["count"] += 1; o["gross"] += f["gross"]; o["discount"] += f["discount"]; o["net"] += f["net"]
            o["cogs"] += f["cogs_lines"] + f["cogs_parts"]
        for inv in self.refunds:
            n = (inv.refunded_by or "").strip() or "Unrecorded (legacy)"
            o = out[n]; o["refund_count"] += 1; o["refund_total"] += self.fig[inv.id]["total"]
        return out

    def by_product(self) -> dict:
        out = {}
        for inv in self.sold:
            for k, v in self.fig[inv.id]["per_prod"].items():
                o = out.setdefault(k, {"name": v["name"], "sku": v["sku"], "cat": v["cat"], "units": 0, "net": 0.0, "cost": 0.0})
                o["units"] += v["units"]; o["net"] += v["net"]; o["cost"] += v["cost"]
        for inv in self.refunds:
            for k, v in self.fig[inv.id]["per_prod"].items():
                if k in out:
                    out[k]["units"] -= v["units"]; out[k]["net"] -= v["net"]; out[k]["cost"] -= v["cost"]
        return out

    def tax_by_label(self) -> dict:
        out = defaultdict(lambda: [0.0, 0.0])  # label -> [collected, refunded]
        for inv in self.sold:
            for l in self.fig[inv.id]["tax_lines"]:
                out[l.get("label", "Tax")][0] += float(l.get("amount", 0) or 0)
        for inv in self.refunds:
            for l in self.fig[inv.id]["tax_lines"]:
                out[l.get("label", "Tax")][1] += float(l.get("amount", 0) or 0)
        return out


# ── light-weight series for the dashboard ─────────────────────────────

def daily_totals(db, tz, d1: date, d2: date) -> dict:
    """{local date: {"total","count","refund_total"}} using column-only
    queries — cheap enough to call on every dashboard load."""
    s, e = day_bounds_utc(d1, d2, tz)
    out = defaultdict(lambda: {"total": 0.0, "count": 0, "refund_total": 0.0})
    for dt, total in db.query(Invoice.date, Invoice.total).filter(Invoice.date >= s, Invoice.date < e):
        x = out[to_local_date(dt, tz)]
        x["total"] += float(total or 0); x["count"] += 1
    refund_rows = (db.query(Invoice.date, Invoice.refunded_at, Invoice.total)
                   .filter(Invoice.refunded.is_(True),
                           or_(and_(Invoice.refunded_at.isnot(None), Invoice.refunded_at >= s, Invoice.refunded_at < e),
                               and_(Invoice.refunded_at.is_(None), Invoice.date >= s, Invoice.date < e))))
    for dt, rat, total in refund_rows:
        out[to_local_date(rat or dt, tz)]["refund_total"] += float(total or 0)
    for r in _returns.in_window(db, s, e):       # v52: partial returns come off the day they were paid back
        out[to_local_date(r.refunded_at, tz)]["refund_total"] += float(r.total or 0)
    return out


# ── cash drawer / card terminal (shared with Cash Up) ─────────────────

def cash_card_totals(db, tz, d: date) -> dict:
    """What Cash Up reconciles against physical reality for local day `d`.

    Same rules as the original todays_cash_card_totals(), now on the shop's
    local day, plus three refund fixes:
      * a sale refunded on a LATER day still counts on its own day (the cash
        really was taken that day) — previously the old day's numbers
        silently shrank the moment a refund was pressed;
      * a refund paid out today (Cash / Card) for an EARLIER day's sale
        comes off today's drawer / terminal — previously it vanished and the
        drawer came up short with no explanation;
      * a same-day refund returned as Store Credit or E-Transfer leaves the
        original cash/card in the drawer/terminal.
    Legacy refunds (no refunded_at) behave exactly as before: the sale is
    dropped from the day it was made."""
    s, e = day_bounds_utc(d, d, tz)
    day_invoices = db.query(Invoice).filter(Invoice.date >= s, Invoice.date < e).all()
    today_invoices = [i for i in day_invoices
                      if not i.refunded or (i.refunded_at is not None and i.refunded_at >= e)]
    kept_money = [i for i in day_invoices
                  if i.refunded and i.refunded_at is not None and i.refunded_at < e
                  and i.refund_method in ("Store Credit", "E-Transfer")]
    cash_sales = sum(i.cash_amount or 0 for i in today_invoices) + sum(i.cash_amount or 0 for i in kept_money)
    card_sales = sum(i.card_amount or 0 for i in today_invoices) + sum(i.card_amount or 0 for i in kept_money)
    etransfer_sales = sum(i.total or 0 for i in today_invoices if i.payment_method == "E-Transfer")
    total_sales = sum(i.total or 0 for i in today_invoices)

    for p in db.query(LayawayPayment).filter(LayawayPayment.created_at >= s, LayawayPayment.created_at < e):
        if p.method == "Cash":
            cash_sales += p.amount
        elif p.method in ("Debit", "Credit"):
            card_sales += p.amount

    for t in db.query(TradeIn).filter(TradeIn.created_at >= s, TradeIn.created_at < e):
        if t.payout_method == "cash":
            cash_sales -= t.offered_amount

    cash_refunds_paid = card_refunds_paid = 0.0
    for inv in (db.query(Invoice)
                .filter(Invoice.refunded.is_(True), Invoice.refunded_at.isnot(None),
                        Invoice.refunded_at >= s, Invoice.refunded_at < e, Invoice.date < s)):
        if inv.refund_method == "Cash":
            cash_refunds_paid += cash_refund_amount(db, inv.total or 0)   # what physically left the drawer (5c-rounded)
        elif inv.refund_method == "Card":
            card_refunds_paid += inv.total or 0
    # v52: partial returns. The invoice stays a normal sale on its own day, so a return paid out in
    # Cash / Card comes off the day it was paid (Store Credit / E-Transfer never touch drawer or terminal).
    cash_returns_paid = _returns.cash_out(db, s, e)
    card_returns_paid = _returns.sum_total(db, s, e, "Card")
    cash_sales -= cash_refunds_paid + cash_returns_paid
    card_sales -= card_refunds_paid + card_returns_paid

    # v52: cash taken out of the till for expenses / bank deposits on this local day
    cash_paid_out = _expenses.drawer_out(db, s, e)

    card_invoices_today = [i for i in today_invoices if (i.card_amount or 0) > 0]
    card_missing_ref = sum(1 for i in card_invoices_today if not i.card_reference)
    return {
        "cash_sales": round(cash_sales, 2), "card_sales": round(card_sales, 2),
        "etransfer_sales": round(etransfer_sales, 2), "total_sales": round(total_sales, 2),
        "invoice_count": len(today_invoices),
        "card_invoices_today": card_invoices_today, "card_missing_ref": card_missing_ref,
        "cash_refunds_paid": round(cash_refunds_paid, 2), "card_refunds_paid": round(card_refunds_paid, 2),
        "cash_returns_paid": round(cash_returns_paid, 2), "card_returns_paid": round(card_returns_paid, 2),
        # cash_sales is what customers' sales/refunds did to the drawer; cash_paid_out is money the shop took out;
        # cash_net is the change Cash Up expects since the opening float (expected = float + cash_net)
        "cash_paid_out": round(cash_paid_out, 2), "cash_net": round(cash_sales - cash_paid_out, 2),
        "invoice_cash": round(sum(i.cash_amount or 0 for i in today_invoices) + sum(i.cash_amount or 0 for i in kept_money), 2),
        # v47: the 5c cash-rounding adjustments already inside the cash figures above (informational)
        "cash_rounding": round(sum(i.cash_rounding or 0 for i in today_invoices) + sum(i.cash_rounding or 0 for i in kept_money), 2),
    }


# ── REPORT: daily ─────────────────────────────────────────────────────

def _sub_period(tz, d1, d2=None):
    d2 = d2 or d1
    lab = d1.strftime("%a %b %d, %Y") if d1 == d2 else f"{d1.strftime('%b %d, %Y')} – {d2.strftime('%b %d, %Y')}"
    return f"{lab} · times in {tz_label(tz)}"


def daily_report(db, tz, d: date) -> Report:
    w = SalesWindow(db, tz, d, d)
    t = w.totals()
    cc = cash_card_totals(db, tz, d)
    s, e = w.start_utc, w.end_utc
    session = db.query(CashSession).filter(CashSession.date == d.isoformat()).first()
    _cf = db.get(Setting, "cash_float")
    float_default = float((_cf.value if _cf else "200") or 0)
    open_float = session.open_float if session else float_default

    kpis = [
        Kpi("Net sales (ex. tax)", Money(r2(t["net_sales"])), f"{int(t['count'])} sale{'s' if t['count'] != 1 else ''}", "good"),
        Kpi("Tax collected", Money(r2(t["tax_net"])), "net of refunds"),
        Kpi("Total takings", Money(r2(t["takings"])), "incl. tax, net of refunds"),
        Kpi("Avg sale (ex. tax)", Money(r2(t["avg_ticket"]))),
        Kpi("Gross profit", Money(r2(t["gross_profit"])), f"{t['margin']:.0f}% margin", "good" if t["gross_profit"] >= 0 else "bad"),
        Kpi("Refunds today", Money(r2(t["refund_total"])), f"{int(t['refund_count'])} refund(s)", "bad" if t["refund_count"] else ""),
    ]

    # 1 — sales summary ladder
    rows = [
        ["Gross sales (before discounts)", Money(r2(t["gross"]))],
        ["Less: discounts, loyalty & store credit applied", Money(-r2(t["discount"]))],
        ["Net sales sold today (ex. tax)", Money(r2(t["sold_net"]))],
        ["Less: refunds processed today (ex. tax)", Money(-r2(t["refund_net"]))],
    ]
    for label, (col, ref) in sorted(w.tax_by_label().items()):
        rows.append([f"Tax — {label}", Money(r2(col - ref))])
    rows += [
        ["Total takings (incl. tax)", Money(r2(t["takings"]))],
        ["Cost of goods sold", Money(r2(t["cogs_net"]))],
        ["Gross profit", Money(r2(t["gross_profit"]))],
    ]
    sec_summary = Section("Sales summary", ["Line", "Amount"], rows)

    # 2 — money in by tender + expected drawer
    sold_tenders = w.by_tender()
    lay = defaultdict(float)
    for p in db.query(LayawayPayment).filter(LayawayPayment.created_at >= s, LayawayPayment.created_at < e):
        lay[p.method or "Other"] += p.amount
    tr_cash = sum(x.offered_amount for x in db.query(TradeIn).filter(TradeIn.created_at >= s, TradeIn.created_at < e, TradeIn.payout_method == "cash"))
    expected_drawer = round(open_float + cc["cash_net"], 2)
    tender_rows = [[k, v[0], Money(r2(v[2]))] for k, v in sorted(sold_tenders.items(), key=lambda kv: -kv[1][2])]
    sec_tender = Section("Sales by payment method", ["Method", "Sales", "Total (incl. tax)"], tender_rows,
                         foot=["Total", int(t["count"]), Money(r2(t["sold_total"]))])

    drawer_rows = [
        ["Opening float", Money(r2(open_float))],
        ["+ Cash from sales (net of change given)", Money(r2(cc["invoice_cash"]))],
        *([["   of which cash rounding to 5¢ (net)", Money(r2(cc["cash_rounding"]))]] if cc.get("cash_rounding") else []),
        ["+ Cash layaway payments", Money(r2(lay.get("Cash", 0)))],
        ["− Cash trade-in payouts", Money(-r2(tr_cash))],
        ["− Cash refunds paid (earlier days' sales)", Money(-r2(cc["cash_refunds_paid"]))],
        ["− Cash paid back on returned items", Money(-r2(cc["cash_returns_paid"]))],
        ["− Cash taken out of the till (expenses & deposits)", Money(-r2(cc["cash_paid_out"]))],
        ["= Expected cash in drawer", Money(expected_drawer)],
    ]
    if session:
        drawer_rows += [["Counted at close", Money(r2(session.actual))],
                        ["Over / (short)", Money(r2(session.difference))]]
    sec_drawer = Section("Cash drawer", ["Line", "Amount"], drawer_rows,
                         note="Closed by %s." % (session.closed_by_name or "staff") if session else "Cash Up has not been closed for this day yet.")
    card_rows = [["Card sales recorded in CRM", Money(r2(cc["card_sales"]))]]
    if session:
        card_rows += [["Terminal batch total entered", Money(r2(session.card_batch))],
                      ["Difference", Money(r2(session.card_difference))]]
    card_rows.append(["Card sales missing a terminal reference", cc["card_missing_ref"]])
    other_rows = [["E-Transfer sales", Money(r2(cc["etransfer_sales"]))],
                  ["Store credit applied to sales", Money(r2(sum(i.store_credit_used or 0 for i in w.sold)))],
                  ["Loyalty points redeemed", int(sum(i.loyalty_pts_used or 0 for i in w.sold))]]
    sec_card = Section("Card terminal & other tenders", ["Line", "Amount"], card_rows + other_rows)

    # 3 — hours, categories, top items, staff
    hours = w.by_hour()
    hour_rows = [[f"{h:02d}:00–{h:02d}:59", hours[h][0], Money(r2(hours[h][1]))] for h in sorted(hours)]
    sec_hours = Section("Sales by hour", ["Hour", "Sales", "Net sales"], hour_rows,
                        bars=[(f"{h:02d}h", r2(hours[h][1])) for h in sorted(hours)])
    cats = w.by_category()
    cat_rows = [[cat_name(c), v["units"], Money(r2(v["net"])), Money(r2(v["net"] - v["cost"]))]
                for c, v in sorted(cats.items(), key=lambda kv: -kv[1]["net"])]
    sec_cats = Section("Sales by category", ["Category", "Units", "Net sales", "Gross profit"], cat_rows,
                       note="Discounts are shared out across lines pro-rata, so these add up to net sales.")
    prods = sorted(w.by_product().values(), key=lambda p: -p["net"])[:10]
    sec_top = Section("Top items", ["Item", "SKU", "Units", "Net sales"],
                      [[p["name"], p["sku"], p["units"], Money(r2(p["net"]))] for p in prods])
    staff_rows = []
    for n, v in sorted(w.by_staff().items(), key=lambda kv: -kv[1]["net"]):
        staff_rows.append([n, int(v["count"]), Money(r2(v["net"])), Money(r2(v["net"] / v["count"])) if v["count"] else Money(0),
                           Money(r2(v["discount"]))])
    sec_staff = Section("By staff member", ["Staff", "Sales", "Net sales", "Avg sale", "Discounts given"], staff_rows)

    # 4 — repairs and other movements
    rep_rows, counts = [], defaultdict(int)
    for r in (db.query(Repair).options(selectinload(Repair.customer))
              .filter(or_(Repair.created_at >= s, Repair.updated_at >= s)).all()):
        hd = _hist_dates(r)
        for st in ("RECEIVED", "READY", "COMPLETED", "COLLECTED"):
            at = hd.get(st)
            if at and s <= at < e:
                counts[st] += 1
                rep_rows.append([str(r.ticket_no), r.device, STATUS_LABELS.get(st, st),
                                 to_local(at, tz).strftime("%H:%M"), r.customer.name if r.customer else "—"])
    rep_rows.sort(key=lambda x: (x[3], x[0]))
    sec_rep = Section("Repair activity", ["Ticket", "Device", "Event", "Time", "Customer"], rep_rows,
                      note=(f"{counts['RECEIVED']} intake · {counts['READY']} ready for pickup · "
                            f"{counts['COLLECTED']} collected"), empty="No repair activity today.")

    tis = db.query(TradeIn).filter(TradeIn.created_at >= s, TradeIn.created_at < e).all()
    issued = db.query(AuditLog).filter(AuditLog.ts >= s, AuditLog.ts < e, AuditLog.action == "STORE_CREDIT",
                                        AuditLog.detail.like("Issued%")).all()
    new_lay = db.query(Layaway).filter(Layaway.created_at >= s, Layaway.created_at < e).count()
    move_rows = [
        ["Trade-ins accepted", len(tis)],
        ["Trade-in cash paid out", Money(r2(tr_cash))],
        ["Trade-in store credit issued", Money(r2(sum(x.offered_amount for x in tis if x.payout_method == "store_credit")))],
        ["Store credit issued manually", len(issued)],
        ["Layaway payments received", Money(r2(sum(lay.values())))],
        ["New layaways started", new_lay],
    ]
    sec_move = Section("Other money movements", ["Item", "Value"], move_rows)

    # 5 — transaction list
    tx_rows = []
    for inv in w.sold:
        f = w.fig[inv.id]
        tx_rows.append([inv.number, to_local(inv.date, tz).strftime("%H:%M"), inv.customer.name if inv.customer else "Walk-in",
                        inv.staff.name if inv.staff else "—", tender_label(inv), Money(r2(f["net"])),
                        Money(r2(f["tax"])), Money(r2(f["total"])), "Refunded" if inv.refunded else ("Part returned" if getattr(inv, "returns", None) else "")])
    sec_tx = Section("All transactions", ["Invoice", "Time", "Customer", "Staff", "Payment", "Net", "Tax", "Total", "Status"],
                     tx_rows, empty="No sales recorded for this day.")

    notes = []
    if w.unknown_cost_lines:
        notes.append(f"{w.unknown_cost_lines} sale line(s) have no known cost (product deleted before cost tracking) — profit on those is overstated.")
    if not session and d <= local_today(tz) and t["count"]:
        notes.append("Cash Up has not been closed for this day.")
    return Report("daily", "Daily Report", _sub_period(tz, d), kpis,
                  [sec_summary, sec_tender, sec_drawer, sec_card, sec_hours, sec_cats, sec_top, sec_staff, sec_rep, sec_move, sec_tx], notes)


# ── REPORT: sales & profit over any period / monthly review ───────────

def _day_table(w: SalesWindow, d1: date, d2: date):
    days = w.by_day()
    rows, bars = [], []
    d = d1
    tot = defaultdict(float)
    while d <= d2:
        x = days.get(d)
        if x:
            net = x["net"] - x["refund_net"]
            gp = net - x["cogs"]
            rows.append([d.isoformat(), d.strftime("%a"), int(x["count"]), Money(r2(x["gross"])), Money(r2(x["discount"])),
                         Money(r2(x["refund_net"])), Money(r2(net)), Money(r2(x["tax"] - x["refund_tax"])), Money(r2(gp))])
            tot["count"] += x["count"]; tot["gross"] += x["gross"]; tot["disc"] += x["discount"]
            tot["ref"] += x["refund_net"]; tot["net"] += net; tot["tax"] += x["tax"] - x["refund_tax"]; tot["gp"] += gp
            bars.append((d.strftime("%d"), r2(net)))
        else:
            bars.append((d.strftime("%d"), 0))
        d += timedelta(days=1)
    foot = ["Total", "", int(tot["count"]), Money(r2(tot["gross"])), Money(r2(tot["disc"])), Money(r2(tot["ref"])),
            Money(r2(tot["net"])), Money(r2(tot["tax"])), Money(r2(tot["gp"]))]
    return rows, foot, bars


def _breakdown_sections(w: SalesWindow, t: dict):
    cats = w.by_category()
    cat_rows = []
    for c, v in sorted(cats.items(), key=lambda kv: -kv[1]["net"]):
        gp = v["net"] - v["cost"]
        cat_rows.append([cat_name(c), v["units"], Money(r2(v["net"])), Pct(pct(v["net"], t["net_sales"])),
                         Money(r2(gp)), Pct(pct(gp, v["net"]))])
    sec_cat = Section("Net sales & margin by category", ["Category", "Units", "Net sales", "% of sales", "Gross profit", "Margin"], cat_rows,
                      foot=["Total", sum(v["units"] for v in cats.values()), Money(r2(t["net_sales"])), Pct(100.0 if t["net_sales"] else 0.0),
                            Money(r2(t["gross_profit"])), Pct(t["margin"])],
                      note="Discounts and refunds are allocated to categories so this adds up to net sales.")
    ten_rows = [[k, v[0], Money(r2(v[1])), Money(r2(v[2]))] for k, v in sorted(w.by_tender().items(), key=lambda kv: -kv[1][2])]
    sec_ten = Section("Payment methods", ["Method", "Sales", "Net sales", "Total incl. tax"], ten_rows)
    st_rows = []
    for n, v in sorted(w.by_staff().items(), key=lambda kv: -kv[1]["net"]):
        st_rows.append([n, int(v["count"]), Money(r2(v["net"])), Money(r2(v["net"] / v["count"])) if v["count"] else Money(0),
                        Money(r2(v["discount"])), Pct(pct(v["discount"], v["gross"])), int(v["refund_count"])])
    sec_staff = Section("Staff", ["Staff", "Sales", "Net sales", "Avg sale", "Discounts", "Discount %", "Refunds processed"], st_rows)
    wd = w.by_weekday()
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    wd_rows = [[names[i], wd[i][0], Money(r2(wd[i][1])), Money(r2(wd[i][1] / wd[i][0])) if wd[i][0] else Money(0)] for i in range(7) if i in wd]
    sec_wd = Section("Busiest weekdays", ["Weekday", "Days traded", "Net sales", "Avg per day"], wd_rows,
                     bars=[(names[i][:3], r2(wd[i][1] / wd[i][0]) if wd[i][0] else 0) for i in range(7) if i in wd])
    hrs = w.by_hour()
    hr_rows = [[f"{h:02d}:00", hrs[h][0], Money(r2(hrs[h][1]))] for h in sorted(hrs)]
    sec_hr = Section("Busiest hours", ["Hour", "Sales", "Net sales"], hr_rows, bars=[(f"{h:02d}h", r2(hrs[h][1])) for h in sorted(hrs)])
    return sec_cat, sec_ten, sec_staff, sec_wd, sec_hr


def sales_report(db, tz, d1: date, d2: date, key="sales", title="Sales & Profit") -> Report:
    w = SalesWindow(db, tz, d1, d2)
    t = w.totals()
    span = (d2 - d1).days + 1
    prev_d2 = d1 - timedelta(days=1)
    prev_d1 = prev_d2 - timedelta(days=span - 1)
    tp = SalesWindow(db, tz, prev_d1, prev_d2).totals()
    kpis = [
        Kpi("Net sales (ex. tax)", Money(r2(t["net_sales"])), change_sub(t["net_sales"], tp["net_sales"], "previous period"), "good"),
        Kpi("Gross profit", Money(r2(t["gross_profit"])), f"{t['margin']:.1f}% margin", "good" if t["gross_profit"] >= 0 else "bad"),
        Kpi("Sales", int(t["count"]), change_sub(t["count"], tp["count"], "previous period")),
        Kpi("Avg sale (ex. tax)", Money(r2(t["avg_ticket"]))),
        Kpi("Tax collected", Money(r2(t["tax_net"])), "net of refunds"),
        Kpi("Discounts given", Money(r2(t["discount"])), f"{pct(t['discount'], t['gross']):.1f}% of gross"),
        Kpi("Refunds", Money(r2(t["refund_total"])), f"{int(t['refund_count'])} refund(s)", "bad" if t["refund_count"] else ""),
        Kpi("Days traded", len([1 for x in w.by_day().values() if x['count']]), f"of {span} days"),
    ]
    fees = card_fees_for_period(db, tz, d1, d2)
    if fees["basis"] != "none":
        after = t["gross_profit"] - fees["amount"]
        kpis.append(Kpi("Card fees", Money(r2(fees["amount"])), fees["note"]))
        kpis.append(Kpi("Profit after card fees", Money(r2(after)), "gross profit − card fees", "good" if after >= 0 else "bad"))
    rows, foot, bars = _day_table(w, d1, d2)
    sec_days = Section("Day by day", ["Date", "Day", "Sales", "Gross", "Discounts", "Refunds (ex. tax)", "Net sales", "Tax", "Gross profit"],
                       rows, foot=foot, bars=bars if span <= 62 else None)
    prods = sorted(w.by_product().values(), key=lambda p: -p["net"])[:15]
    sec_top = Section("Top 15 items by net sales", ["Item", "Category", "Units", "Net sales", "Gross profit", "Margin"],
                      [[p["name"], cat_name(p["cat"]), p["units"], Money(r2(p["net"])), Money(r2(p["net"] - p["cost"])), Pct(pct(p["net"] - p["cost"], p["net"]))] for p in prods])
    notes = []
    if w.unknown_cost_lines:
        notes.append(f"{w.unknown_cost_lines} sale line(s) have no known cost (product deleted before cost tracking) — profit on those is overstated.")
    if fees["basis"] == "none" and t["count"]:
        notes.append("Card processing fees are not included — enter your Moneris statement fees under Card Fees to see profit after fees.")
    return Report(key, title, _sub_period(tz, d1, d2), kpis, [sec_days, *_breakdown_sections(w, t), sec_top], notes)


def repair_summary(db, tz, d1: date, d2: date) -> dict:
    """Compact repair numbers for the monthly review."""
    s, e = day_bounds_utc(d1, d2, tz)
    opened = db.query(Repair).filter(Repair.created_at >= s, Repair.created_at < e).count()
    collected, turns = 0, []
    for r in db.query(Repair).filter(Repair.updated_at >= s):
        hd = _hist_dates(r)
        c = hd.get("COLLECTED")
        if c and s <= c < e:
            collected += 1
            if hd.get("RECEIVED"):
                turns.append((c - hd["RECEIVED"]).total_seconds() / 86400)
    return {"opened": opened, "collected": collected, "avg_days": sum(turns) / len(turns) if turns else 0.0}


def liabilities_snapshot(db) -> list:
    """Point-in-time balances the shop owes customers / holds in stock."""
    redeem = float((db.get(Setting, "points_redeem_rate").value if db.get(Setting, "points_redeem_rate") else "100") or 100)
    credit = float(db.query(func.coalesce(func.sum(Customer.store_credit), 0.0)).scalar() or 0)
    points = int(db.query(func.coalesce(func.sum(Customer.points), 0)).scalar() or 0)
    lay_dep = float(db.query(func.coalesce(func.sum(Layaway.paid_total), 0.0)).filter(Layaway.status == "active").scalar() or 0)
    lay_n = db.query(Layaway).filter(Layaway.status == "active").count()
    inv_cost = float(db.query(func.coalesce(func.sum(Product.cost * Product.stock), 0.0)).filter(Product.stock > 0).scalar() or 0)
    inv_retail = float(db.query(func.coalesce(func.sum(Product.price * Product.stock), 0.0)).filter(Product.stock > 0).scalar() or 0)
    return [
        ["Store credit owed to customers", Money(r2(credit)), "Liability — redeemable against future sales"],
        ["Loyalty points outstanding", points, f"≈ ${points / redeem:,.2f} if all redeemed" if redeem else ""],
        ["Layaway deposits held", Money(r2(lay_dep)), f"{lay_n} active layaway(s)"],
        ["Inventory value at cost", Money(r2(inv_cost)), "Stock on hand × cost"],
        ["Inventory value at retail", Money(r2(inv_retail)), "Stock on hand × price"],
    ]


def monthly_report(db, tz, year: int, month: int) -> Report:
    first, last = month_bounds(year, month)
    w = SalesWindow(db, tz, first, last)
    t = w.totals()
    pm_first, pm_last = month_bounds(*( (year - 1, 12) if month == 1 else (year, month - 1)))
    ly_first, ly_last = month_bounds(year - 1, month)
    tp = SalesWindow(db, tz, pm_first, pm_last).totals()
    ty = SalesWindow(db, tz, ly_first, ly_last).totals()
    rs = repair_summary(db, tz, first, last)
    s, e = w.start_utc, w.end_utc

    # new customers = first-ever activity falls inside this month
    first_seen = {}
    for cid, dt in db.query(Invoice.customer_id, func.min(Invoice.date)).filter(Invoice.customer_id.isnot(None)).group_by(Invoice.customer_id):
        first_seen[cid] = dt
    for cid, dt in db.query(Repair.customer_id, func.min(Repair.created_at)).filter(Repair.customer_id.isnot(None)).group_by(Repair.customer_id):
        if cid not in first_seen or dt < first_seen[cid]:
            first_seen[cid] = dt
    new_customers = sum(1 for dt in first_seen.values() if s <= dt < e)

    kpis = [
        Kpi("Net sales (ex. tax)", Money(r2(t["net_sales"])), change_sub(t["net_sales"], tp["net_sales"], "last month"), "good"),
        Kpi("Gross profit", Money(r2(t["gross_profit"])), f"{t['margin']:.1f}% margin", "good" if t["gross_profit"] >= 0 else "bad"),
        Kpi("Sales", int(t["count"]), change_sub(t["count"], tp["count"], "last month")),
        Kpi("Avg sale (ex. tax)", Money(r2(t["avg_ticket"]))),
        Kpi("Tax to remit (GST/PST)", Money(r2(t["tax_net"])), "before input tax credits"),
        Kpi("Repairs collected", rs["collected"], f"{rs['opened']} taken in"),
        Kpi("New customers", new_customers),
        Kpi("Refunds", Money(r2(t["refund_total"])), f"{int(t['refund_count'])} refund(s)", "bad" if t["refund_count"] else ""),
    ]
    pl = [
        ["Gross sales (before discounts)", Money(r2(t["gross"]))],
        ["Less: discounts, loyalty & store credit applied", Money(-r2(t["discount"]))],
        ["Less: refunds (ex. tax)", Money(-r2(t["refund_net"]))],
        ["Net sales", Money(r2(t["net_sales"]))],
        ["Cost of goods sold (products + repair parts)", Money(-r2(t["cogs_net"]))],
        ["Gross profit", Money(r2(t["gross_profit"]))],
        ["Gross margin", Pct(t["margin"])],
    ]
    fees = card_fees_for_period(db, tz, first, last)
    if fees["basis"] != "none":
        pl.append([f"Less: card processing fees ({fees['note']})", Money(-r2(fees["amount"]))])
        pl.append(["Profit after card fees", Money(r2(t["gross_profit"] - fees["amount"]))])
        kpis.append(Kpi("Profit after card fees", Money(r2(t["gross_profit"] - fees["amount"])), "gross profit − Moneris fees",
                        "good" if t["gross_profit"] - fees["amount"] >= 0 else "bad"))
        pl_note = "Gross profit less card fees — other operating expenses (rent, wages, utilities) are not recorded in the CRM, so this is not full net profit."
    else:
        pl_note = ("Gross profit only — operating expenses (rent, wages, utilities) are not recorded in the CRM, so this is not net profit. "
                   "Add your Moneris statement fees under Card Fees to include them.")
    exp = _expenses.business_total(db, s, e)
    if exp["count"]:
        ex_net = r2(exp["amount"] - exp["tax_paid"])
        pl.append([f"Less: expenses recorded (ex. tax, {exp['count']} entr{'y' if exp['count'] == 1 else 'ies'})", Money(-ex_net)])
        after = t["gross_profit"] - (fees["amount"] if fees["basis"] != "none" else 0.0) - ex_net
        pl.append(["Profit after recorded expenses", Money(r2(after))])
        kpis.append(Kpi("Profit after expenses", Money(r2(after)), "gross profit − card fees − recorded expenses", "good" if after >= 0 else "bad"))
        pl_note += " Expenses are only the ones entered on the Expenses page — rent, wages or bills paid another way count only if they were entered."
    sec_pl = Section("Profit & loss", ["Line", "Amount"], pl, note=pl_note)
    cmp_rows = []
    for label, key, kind in (("Net sales", "net_sales", Money), ("Gross profit", "gross_profit", Money), ("Sales count", "count", int),
                             ("Avg sale", "avg_ticket", Money), ("Discounts", "discount", Money), ("Refunds", "refund_total", Money),
                             ("Tax collected (net)", "tax_net", Money)):
        a, b, c = t[key], tp[key], ty[key]
        conv = (lambda v: kind(r2(v))) if kind is Money else (lambda v: int(v))
        cmp_rows.append([label, conv(a), conv(b), Pct(pct(a - b, b)) if b else None, conv(c), Pct(pct(a - c, c)) if c else None])
    sec_cmp = Section("Compared with last month and last year", ["Metric", first.strftime("%b %Y"), pm_first.strftime("%b %Y"), "vs last month", ly_first.strftime("%b %Y"), "vs last year"], cmp_rows)
    rows, foot, bars = _day_table(w, first, last)
    sec_days = Section("Day by day", ["Date", "Day", "Sales", "Gross", "Discounts", "Refunds (ex. tax)", "Net sales", "Tax", "Gross profit"], rows, foot=foot, bars=bars)
    sec_cat, sec_ten, sec_staff, sec_wd, _hr = _breakdown_sections(w, t)
    by_prod = w.by_product().values()
    best_gp = sorted(by_prod, key=lambda p: -(p["net"] - p["cost"]))[:10]
    sec_gp = Section("Top 10 items by gross profit", ["Item", "Units", "Net sales", "Gross profit", "Margin"],
                     [[p["name"], p["units"], Money(r2(p["net"])), Money(r2(p["net"] - p["cost"])), Pct(pct(p["net"] - p["cost"], p["net"]))] for p in best_gp])
    sec_rep = Section("Repairs", ["Measure", "Value"], [
        ["Tickets taken in", rs["opened"]], ["Tickets collected", rs["collected"]],
        ["Average turnaround (intake → collected)", Dec(rs["avg_days"])]],
        note="Full detail in the Repairs report.")
    # cash-up discipline
    sessions = db.query(CashSession).filter(CashSession.date >= first.isoformat(), CashSession.date <= last.isoformat()).all()
    traded = {d for d, x in w.by_day().items() if x["count"]}
    closed = {date.fromisoformat(x.date) for x in sessions}
    missing = sorted(d for d in traded if d not in closed and d <= local_today(tz))
    var = sum(x.difference or 0 for x in sessions)
    sec_cash = Section("Cash-up discipline", ["Measure", "Value"], [
        ["Days traded", len(traded)], ["Days with a Cash Up closed", len(traded & closed)],
        ["Days traded with NO Cash Up", len(missing)], ["Net cash over / (short) for the month", Money(r2(var))],
        ["Card terminal differences (sum of absolute)", Money(r2(sum(abs(x.card_difference or 0) for x in sessions)))]],
        note=("Missing: " + ", ".join(d.strftime("%b %d") for d in missing[:12]) + (" …" if len(missing) > 12 else "")) if missing else "")
    sec_tax = Section("Tax summary", ["Tax", "Collected", "Refunded", "Net to remit"],
                      [[k, Money(r2(v[0])), Money(r2(v[1])), Money(r2(v[0] - v[1]))] for k, v in sorted(w.tax_by_label().items())],
                      foot=["Total", Money(r2(t["sold_tax"])), Money(r2(t["refund_tax"])), Money(r2(t["tax_net"]))],
                      note="Use the Tax / Remittance report for filing-period detail.")
    sec_liab = Section("Balance-sheet snapshot (as of now)", ["Item", "Amount", "Note"], liabilities_snapshot(db))
    sections = [sec_pl, sec_cmp, sec_days, sec_cat, sec_ten, sec_staff, sec_wd, sec_gp, sec_rep, sec_cash, sec_tax, sec_liab]
    st = db.query(CardStatement).filter(CardStatement.month == month_key(first)).first()
    if st is not None:
        crm = crm_card_month(db, tz, first)
        rows_st = [["Card money taken (CRM)", Money(r2(crm["taken"]))], ["Card refunds paid (CRM)", Money(-r2(crm["refunds"]))],
                   ["CRM card total, net", Money(r2(crm["net"]))]]
        if st.volume is not None:
            rows_st += [["Moneris statement volume", Money(r2(st.volume))], ["Difference (statement − CRM)", Money(r2(st.volume - crm["net"]))]]
        rows_st.append(["Moneris fees charged", Money(r2(st.fees))])
        base = st.volume if st.volume else crm["net"]
        if base:
            rows_st.append(["Effective fee rate", Pct(pct(st.fees or 0, base))])
        sections.insert(10, Section("Moneris statement check", ["Measure", "Amount"], rows_st,
                                    note="A non-zero difference means the CRM and Moneris disagree about the month's card sales — use the Moneris Check page to find the days."))
    notes = []
    if w.unknown_cost_lines:
        notes.append(f"{w.unknown_cost_lines} sale line(s) have no known cost — profit on those is overstated.")
    return Report("monthly", f"Monthly Business Review — {first.strftime('%B %Y')}", _sub_period(tz, first, last), kpis,
                  sections, notes)


# ── REPORT: tax / remittance ──────────────────────────────────────────

def tax_report(db, tz, d1: date, d2: date) -> Report:
    w = SalesWindow(db, tz, d1, d2)
    t = w.totals()
    by_label = w.tax_by_label()
    by_name = defaultdict(lambda: [0.0, 0.0])
    for label, (c, rf) in by_label.items():
        by_name[tax_name(label)][0] += c; by_name[tax_name(label)][1] += rf
    names = sorted(by_name)
    kpis = [Kpi("Taxable sales (ex. tax)", Money(r2(t["net_sales"])), "net of refunds"),
            Kpi("Tax collected", Money(r2(t["sold_tax"]))), Kpi("Tax refunded", Money(r2(t["refund_tax"]))),
            Kpi("Net tax to remit", Money(r2(t["tax_net"])), "before input tax credits (ITCs)", "warn")]
    sec_type = Section("Net tax by type", ["Tax", "Collected", "Refunded", "Net to remit"],
                       [[n, Money(r2(by_name[n][0])), Money(r2(by_name[n][1])), Money(r2(by_name[n][0] - by_name[n][1]))] for n in names],
                       foot=["Total", Money(r2(t["sold_tax"])), Money(r2(t["refund_tax"])), Money(r2(t["tax_net"]))])
    sec_rate = Section("By rate (shows any mid-period rate change)", ["Tax line", "Collected", "Refunded", "Net"],
                       [[k, Money(r2(v[0])), Money(r2(v[1])), Money(r2(v[0] - v[1]))] for k, v in sorted(by_label.items())])
    # per-day by tax name
    day_tax = defaultdict(lambda: defaultdict(float)); day_net = defaultdict(float)
    for inv in w.sold:
        d = to_local_date(inv.date, tz); day_net[d] += w.fig[inv.id]["net"]
        for l in w.fig[inv.id]["tax_lines"]:
            day_tax[d][tax_name(l.get("label", ""))] += float(l.get("amount", 0) or 0)
    for inv in w.refunds:
        d = to_local_date(w.refund_ts(inv), tz); day_net[d] -= w.fig[inv.id]["net"]
        for l in w.fig[inv.id]["tax_lines"]:
            day_tax[d][tax_name(l.get("label", ""))] -= float(l.get("amount", 0) or 0)
    drows = []
    for d in sorted(day_net):
        row = [d.isoformat(), Money(r2(day_net[d]))] + [Money(r2(day_tax[d].get(n, 0))) for n in names] + [Money(r2(sum(day_tax[d].values())))]
        drows.append(row)
    sec_day = Section("Day by day", ["Date", "Net sales"] + names + ["Total tax"], drows,
                      foot=["Total", Money(r2(t["net_sales"]))] + [Money(r2(by_name[n][0] - by_name[n][1])) for n in names] + [Money(r2(t["tax_net"]))])
    # reconciliation checks
    mismatch, zero_tax = [], []
    for inv in w.sold:
        f = w.fig[inv.id]
        lines_sum = sum(float(l.get("amount", 0) or 0) for l in f["tax_lines"])
        if abs(lines_sum - f["tax"]) > 0.01:
            mismatch.append([inv.number, to_local_date(inv.date, tz).isoformat(), Money(r2(f["tax"])), Money(r2(lines_sum))])
        if f["tax"] == 0 and f["net"] > 0:
            zero_tax.append([inv.number, to_local_date(inv.date, tz).isoformat(), Money(r2(f["net"])), tender_label(inv)])
    sec_chk1 = Section("Check: invoices whose tax lines don't add up to the tax total", ["Invoice", "Date", "Tax total", "Sum of tax lines"], mismatch, empty="None — all invoices reconcile.")
    sec_chk2 = Section("Check: sales with no tax charged", ["Invoice", "Date", "Net sales", "Payment"], zero_tax, empty="None — tax was charged on every sale.")
    rr = [[inv.number, to_local_date(inv.date, tz).isoformat(), to_local_date(w.refund_ts(inv), tz).isoformat(),
           Money(r2(w.fig[inv.id]["tax"])), Money(r2(w.fig[inv.id]["total"]))] for inv in w.refunds]
    sec_ref = Section("Refunds in this period (tax reduces this period, not the original sale period)", ["Invoice", "Sold", "Refunded", "Tax refunded", "Total refunded"], rr, empty="No refunds in this period.")
    lay = (db.query(func.coalesce(func.sum(LayawayPayment.amount), 0.0))
           .filter(LayawayPayment.created_at >= w.start_utc, LayawayPayment.created_at < w.end_utc).scalar() or 0)
    itc = _expenses.business_total(db, w.start_utc, w.end_utc)
    notes = [
        "Net tax to remit is tax COLLECTED minus tax REFUNDED. Subtract your input tax credits (GST paid on business purchases) when you file. "
        + (f"Tax typed in on expenses recorded in this period: ${itc['tax_paid']:,.2f} across {itc['count']} entr{'y' if itc['count'] == 1 else 'ies'} "
           "(see the Expenses report) — that is only what was entered, so check it against your receipts and ask your accountant what you can claim."
           if itc["count"] else "No expenses with tax were recorded in this period (Expenses page) — add them there, or work out your credits from your receipts."),
        "Layaway sales are reported (and taxed) when the layaway is paid off, not when deposits are taken. "
        f"Layaway money received in this period: ${float(lay):,.2f}.",
        "Store credit applied to a sale currently reduces the taxable amount (see review notes) — confirm the treatment with your accountant.",
    ]
    return Report("tax", "Tax / Remittance Report", _sub_period(tz, d1, d2), kpis, [sec_type, sec_rate, sec_day, sec_ref, sec_chk1, sec_chk2], notes)


# ── REPORT: products & inventory ──────────────────────────────────────

def inventory_report(db, tz, days: int) -> Report:
    now = local_now(tz)
    today = now.date()
    cutoff30 = day_bounds_utc(today - timedelta(days=29), today, tz)[0]
    cutoff_n = day_bounds_utc(today - timedelta(days=days - 1), today, tz)[0]
    products = db.query(Product).all()

    def sales_since(cut):
        q = (db.query(InvoiceLine.product_id, func.sum(InvoiceLine.qty), func.sum(InvoiceLine.qty * InvoiceLine.price))
             .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
             .filter(Invoice.date >= cut, Invoice.refunded.isnot(True), InvoiceLine.product_id.isnot(None))
             .group_by(InvoiceLine.product_id))
        out = {pid: (int(q_ or 0), float(rev or 0)) for pid, q_, rev in q}
        # v52: units taken back from earlier sales since `cut` are not demand
        rq = (db.query(SaleReturnLine.product_id, func.sum(SaleReturnLine.qty), func.sum(SaleReturnLine.qty * SaleReturnLine.price))
              .join(SaleReturn, SaleReturn.id == SaleReturnLine.return_id)
              .filter(SaleReturn.refunded_at >= cut, SaleReturnLine.product_id.isnot(None)).group_by(SaleReturnLine.product_id))
        for pid, q_, rev in rq:
            if pid in out:
                out[pid] = (max(0, out[pid][0] - int(q_ or 0)), max(0.0, out[pid][1] - float(rev or 0)))
        return {pid: v for pid, v in out.items() if v[0] > 0}

    s30, sN = sales_since(cutoff30), sales_since(cutoff_n)
    last_sold = {pid: dt for pid, dt in db.query(InvoiceLine.product_id, func.max(Invoice.date))
                 .join(Invoice, Invoice.id == InvoiceLine.invoice_id).filter(InvoiceLine.product_id.isnot(None)).group_by(InvoiceLine.product_id)}

    in_stock = [p for p in products if (p.stock or 0) > 0]
    cost_val = sum((p.cost or 0) * p.stock for p in in_stock)
    retail_val = sum((p.price or 0) * p.stock for p in in_stock)
    low = [p for p in products if (p.stock or 0) <= (p.reorder_threshold or 0)]
    out = [p for p in products if (p.stock or 0) <= 0]
    dead = [p for p in in_stock if p.id not in sN]
    dead_val = sum((p.cost or 0) * p.stock for p in dead)
    kpis = [Kpi("Active products", len(products)), Kpi("Units on hand", sum(p.stock or 0 for p in in_stock)),
            Kpi("Stock value (cost)", Money(r2(cost_val)), f"retail {Money(r2(retail_val))!s}".replace("Money", "") if False else f"≈ ${retail_val:,.0f} at retail"),
            Kpi("Low / out of stock", len(low), f"{len(out)} out of stock", "warn" if low else "good"),
            Kpi(f"No sales in {days} days", len(dead), f"${dead_val:,.0f} tied up at cost", "warn" if dead else "good")]

    def cover(p):
        per_day = s30.get(p.id, (0, 0))[0] / 30
        return (p.stock / per_day) if per_day else None

    low_rows = []
    for p in sorted(low, key=lambda p: (p.stock or 0)):
        c = cover(p)
        low_rows.append([p.name, p.sku, p.stock, p.reorder_threshold, p.reorder_qty, s30.get(p.id, (0, 0))[0],
                         Dec(c) if c is not None else None, Money(r2((p.reorder_qty or 0) * (p.cost or 0)))])
    sec_low = Section("Reorder list (at or below reorder point)", ["Product", "SKU", "On hand", "Reorder at", "Suggested qty", "Sold last 30d", "Days of cover", "Est. cost to reorder"],
                      low_rows, empty="Nothing needs reordering.")
    fast = [p for p in products if cover(p) is not None and cover(p) < 7 and p.stock > (p.reorder_threshold or 0)]
    sec_fast = Section("Selling fast — under 7 days of cover (above reorder point)", ["Product", "SKU", "On hand", "Sold last 30d", "Days of cover"],
                       [[p.name, p.sku, p.stock, s30[p.id][0], Dec(cover(p))] for p in sorted(fast, key=cover)[:25]], empty="None.")
    dead_rows = []
    for p in sorted(dead, key=lambda p: -((p.cost or 0) * p.stock))[:40]:
        ls = last_sold.get(p.id)
        dead_rows.append([p.name, p.sku, p.stock, Money(r2((p.cost or 0) * p.stock)), to_local_date(ls, tz).isoformat() if ls else "never sold"])
    sec_dead = Section(f"Slow movers — stock on hand but no sales in {days} days (top 40 by cash tied up)", ["Product", "SKU", "On hand", "Cost tied up", "Last sold"], dead_rows, empty="None — everything has sold recently.")
    top = sorted(((p, sN[p.id]) for p in products if p.id in sN), key=lambda x: -x[1][1])[:20]
    sec_top = Section(f"Best sellers — last {days} days", ["Product", "SKU", "Units", "Sales (list)", "On hand", "Margin at current price"],
                      [[p.name, p.sku, u, Money(r2(rev)), p.stock, Pct(pct((p.price or 0) - (p.cost or 0), p.price or 0))] for p, (u, rev) in top])
    cat_val = defaultdict(lambda: [0, 0.0, 0.0])
    for p in in_stock:
        c = cat_val[p.category or "UNCATEGORIZED"]; c[0] += p.stock; c[1] += (p.cost or 0) * p.stock; c[2] += (p.price or 0) * p.stock
    sec_val = Section("Stock value by category", ["Category", "Units", "At cost", "At retail", "Potential margin"],
                      [[cat_name(k), v[0], Money(r2(v[1])), Money(r2(v[2])), Pct(pct(v[2] - v[1], v[2]))] for k, v in sorted(cat_val.items(), key=lambda kv: -kv[1][1])],
                      foot=["Total", sum(v[0] for v in cat_val.values()), Money(r2(cost_val)), Money(r2(retail_val)), Pct(pct(retail_val - cost_val, retail_val))])
    quality = []
    for p in products:
        if (p.cost or 0) <= 0 and (p.price or 0) > 0:
            quality.append([p.name, p.sku, "Cost is $0 — profit on this item is overstated", Money(r2(p.price)), Money(r2(p.cost))])
        elif (p.price or 0) < (p.cost or 0):
            quality.append([p.name, p.sku, "Price is below cost", Money(r2(p.price)), Money(r2(p.cost))])
        elif (p.price or 0) == 0:
            quality.append([p.name, p.sku, "Price is $0", Money(r2(p.price)), Money(r2(p.cost))])
    sec_q = Section("Data quality — fix these first, they distort profit reports", ["Product", "SKU", "Problem", "Price", "Cost"], quality[:60], empty="No pricing problems found.")
    return Report("inventory", "Products & Inventory", f"As of {now.strftime('%a %b %d, %Y %H:%M')} · times in {tz_label(tz)}", kpis,
                  [sec_low, sec_fast, sec_dead, sec_top, sec_val, sec_q],
                  ["Stock levels come from the Products screen; there is no stock-movement ledger yet, so shrinkage (loss/theft/miscounts) cannot be separated from sales."])


# ── REPORT: repairs ───────────────────────────────────────────────────

def repairs_report(db, tz, d1: date, d2: date) -> Report:
    s, e = day_bounds_utc(d1, d2, tz)
    now_utc = datetime.utcnow()
    today = local_today(tz)
    loaded = (db.query(Repair).options(selectinload(Repair.customer), selectinload(Repair.technician), selectinload(Repair.parts))
              .filter(or_(and_(Repair.created_at >= s, Repair.created_at < e), Repair.updated_at >= s)).all())
    hist = {r.id: _hist_dates(r) for r in loaded}
    opened = [r for r in loaded if s <= r.created_at < e]
    collected = [r for r in loaded if hist[r.id].get("COLLECTED") and s <= hist[r.id]["COLLECTED"] < e]
    inv_by_repair = defaultdict(float)
    for ids in chunked([r.id for r in collected]):
        for inv in db.query(Invoice).filter(Invoice.repair_id.in_(ids), Invoice.refunded.isnot(True)):
            inv_by_repair[inv.repair_id] += (inv.total or 0) - (inv.tax_total or 0)

    def days_between(a, b):
        return (b - a).total_seconds() / 86400 if a and b else None

    turn_ready = [x for x in (days_between(hist[r.id].get("RECEIVED"), hist[r.id].get("READY") or hist[r.id].get("COMPLETED")) for r in collected) if x is not None]
    turn_coll = [x for x in (days_between(hist[r.id].get("RECEIVED"), hist[r.id].get("COLLECTED")) for r in collected) if x is not None]
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    revenue = sum(inv_by_repair.values())
    parts_cost = sum(sum(p.qty * p.unit_cost for p in r.parts) for r in collected)

    # snapshot of what is open right now (independent of the date range)
    all_open = (db.query(Repair).options(selectinload(Repair.customer), selectinload(Repair.technician))
                .filter(Repair.status != "COLLECTED").all())
    wip = [r for r in all_open if r.status in WIP_STATUSES]
    pickup = [r for r in all_open if r.status in PICKUP_STATUSES]

    kpis = [Kpi("Tickets taken in", len(opened)), Kpi("Tickets collected", len(collected)),
            Kpi("Avg turnaround", Dec(avg(turn_coll)), "days, intake → collected"),
            Kpi("Repair revenue", Money(r2(revenue)), "ex. tax, collected in period"),
            Kpi("Parts cost", Money(r2(parts_cost))), Kpi("Repair margin", Money(r2(revenue - parts_cost)), f"{pct(revenue - parts_cost, revenue):.0f}% of revenue", "good" if revenue >= parts_cost else "bad"),
            Kpi("In progress now", len(wip)), Kpi("Waiting for pickup", len(pickup), "", "warn" if pickup else "")]

    def group(items, keyfn):
        g = defaultdict(list)
        for r in items:
            g[keyfn(r)].append(r)
        return g

    def grp_rows(items_opened, items_collected, keyfn):
        keys = set(group(items_opened, keyfn)) | set(group(items_collected, keyfn))
        go, gc = group(items_opened, keyfn), group(items_collected, keyfn)
        rows = []
        for k in keys:
            rev = sum(inv_by_repair.get(r.id, 0) for r in gc.get(k, []))
            tn = [x for x in (days_between(hist[r.id].get("RECEIVED"), hist[r.id].get("COLLECTED")) for r in gc.get(k, [])) if x is not None]
            rows.append([k or "—", len(go.get(k, [])), len(gc.get(k, [])), Money(r2(rev)),
                         Money(r2(rev / len(gc[k]))) if gc.get(k) else None, Dec(avg(tn)) if tn else None])
        return sorted(rows, key=lambda r: -(r[3] or 0)) if False else sorted(rows, key=lambda r: (-r[2], -r[1]))

    hdr = ["", "Taken in", "Collected", "Revenue", "Avg ticket", "Avg days"]
    sec_issue = Section("By repair type", ["Issue"] + hdr[1:], grp_rows(opened, collected, lambda r: r.issue))
    sec_brand = Section("By device brand", ["Brand"] + hdr[1:], grp_rows(opened, collected, lambda r: device_brand(r.device)))
    sec_tech = Section("By technician", ["Technician"] + hdr[1:], grp_rows(opened, collected, lambda r: r.technician.name if r.technician else "Unassigned"))

    wip_b = [("0–1 days", 0, 1), ("2–3 days", 2, 3), ("4–7 days", 4, 7), ("8–14 days", 8, 14), ("15+ days", 15, 10**6)]
    wip_rows = []
    for lab, lo, hi in wip_b:
        items = [r for r in wip if lo <= (now_utc - r.created_at).days <= hi]
        wip_rows.append([lab, len(items), Money(r2(sum((r.final_cost if r.final_cost is not None else r.estimated_cost) or 0 for r in items)))])
    sec_wip = Section("Work in progress — how long tickets have been open (now)", ["Age", "Tickets", "Quoted value"], wip_rows)

    pick_b = [("0–3 days", 0, 3), ("4–7 days", 4, 7), ("8–14 days", 8, 14), ("15–30 days", 15, 30), ("31–60 days", 31, 60), ("61–90 days", 61, 90), ("90+ days", 91, 10**6)]
    def ready_since(r):
        h = _hist_dates(r)
        return h.get("READY") or h.get("COMPLETED") or r.updated_at or r.created_at
    pick_rows = []
    for lab, lo, hi in pick_b:
        items = [r for r in pickup if lo <= (now_utc - ready_since(r)).days <= hi]
        pick_rows.append([lab, len(items), Money(r2(sum((r.final_cost if r.final_cost is not None else r.estimated_cost) or 0 for r in items)))])
    sec_pick = Section("Ready but not collected — aging (now)", ["Waiting", "Tickets", "Quoted value"], pick_rows,
                       note="Anything over 30 days is abandonment risk — call or text the customer; check your unclaimed-device policy.")
    old = sorted([r for r in pickup if (now_utc - ready_since(r)).days >= 14], key=ready_since)
    sec_old = Section("Chase list — ready 14+ days, not collected", ["Ticket", "Device", "Customer", "Phone", "Days waiting", "Quoted"],
                      [[str(r.ticket_no), r.device, r.customer.name if r.customer else "—", r.customer.phone if r.customer else "",
                        (now_utc - ready_since(r)).days, Money(r2((r.final_cost if r.final_cost is not None else r.estimated_cost) or 0))] for r in old[:40]],
                      empty="Nobody is overdue to collect.")
    late = sorted([r for r in wip if r.promised_by and r.promised_by < today.isoformat()], key=lambda r: r.promised_by)
    sec_late = Section("Past their promised date (still in progress)", ["Ticket", "Device", "Customer", "Promised", "Status", "Days late"],
                       [[str(r.ticket_no), r.device, r.customer.name if r.customer else "—", r.promised_by, STATUS_LABELS.get(r.status, r.status),
                         (today - parse_date(r.promised_by, today)).days] for r in late[:40]], empty="No tickets are running late.")

    # warranty comebacks: opened in window, same IMEI (or same customer + device) as an earlier collected repair still under warranty
    cids = {r.customer_id for r in opened if r.customer_id}
    prior = []
    for ids in chunked(cids):
        prior += db.query(Repair).filter(Repair.customer_id.in_(ids)).all()
    imei_prior = defaultdict(list); cust_prior = defaultdict(list)
    for p in prior:
        if p.imei:
            imei_prior[p.imei.strip().lower()].append(p)
        cust_prior[(p.customer_id, (p.device or "").strip().lower())].append(p)
    comeback = []
    for r in opened:
        cands = (imei_prior.get((r.imei or "").strip().lower(), []) if r.imei else []) or cust_prior.get((r.customer_id, (r.device or "").strip().lower()), [])
        for p in cands:
            if p.id == r.id or p.created_at >= r.created_at:
                continue
            coll = _hist_dates(p).get("COLLECTED")
            if coll and r.created_at <= coll + timedelta(days=p.warranty_days or 0):
                comeback.append([str(r.ticket_no), r.device, r.issue, str(p.ticket_no), p.issue, int((r.created_at - coll).days),
                                 p.technician.name if p.technician else "Unassigned"])
                break
    sec_cb = Section("Possible warranty comebacks (same device returned inside warranty)", ["New ticket", "Device", "Issue", "Original ticket", "Original issue", "Days after pickup", "Original technician"],
                     comeback, empty="No comebacks detected.")
    comeback_rate = pct(len(comeback), len(opened))

    var_rows, over = [], 0
    for r in collected:
        if r.estimated_cost is not None and r.final_cost is not None:
            d = r.final_cost - r.estimated_cost
            if abs(d) >= 0.01:
                var_rows.append(d)
            if d > 0.01:
                over += 1
    est_n = len([r for r in collected if r.estimated_cost is not None and r.final_cost is not None])
    sec_est = Section("Quote accuracy", ["Measure", "Value"], [
        ["Collected tickets with a quote and a final price", est_n],
        ["Final price came in higher than the quote", over],
        ["Average difference when the price changed", Money(r2(avg(var_rows)))]])
    notes = [f"Comeback rate this period: {comeback_rate:.1f}% of tickets taken in." if opened else "",
             "Turnaround uses the status history on each ticket; tickets created before history was recorded may be missing."]
    return Report("repairs", "Repairs Report", _sub_period(tz, d1, d2), kpis,
                  [sec_issue, sec_brand, sec_tech, sec_wip, sec_pick, sec_old, sec_late, sec_cb, sec_est], [n for n in notes if n])


# ── REPORT: customers ─────────────────────────────────────────────────

def customers_report(db, tz, d1: date, d2: date) -> Report:
    s, e = day_bounds_utc(d1, d2, tz)
    now_utc = datetime.utcnow()
    redeem = float((db.get(Setting, "points_redeem_rate").value if db.get(Setting, "points_redeem_rate") else "100") or 100)
    cust = {c.id: c for c in db.query(Customer).all()}
    in_win = (db.query(Invoice.customer_id, func.count(Invoice.id), func.sum(Invoice.total - Invoice.tax_total))
              .filter(Invoice.date >= s, Invoice.date < e, Invoice.refunded.isnot(True), Invoice.customer_id.isnot(None))
              .group_by(Invoice.customer_id).all())
    walkins = db.query(Invoice).filter(Invoice.date >= s, Invoice.date < e, Invoice.customer_id.is_(None), Invoice.refunded.isnot(True)).count()
    ret_by_cust = {cid: float(v or 0) for cid, v in
                   db.query(SaleReturn.customer_id, func.sum(SaleReturn.total - SaleReturn.tax_total))   # v52: spend net of items taken back
                   .filter(SaleReturn.refunded_at >= s, SaleReturn.refunded_at < e, SaleReturn.customer_id.isnot(None))
                   .group_by(SaleReturn.customer_id)}
    in_win = [(cid, n, float(spend or 0) - ret_by_cust.get(cid, 0.0)) for cid, n, spend in in_win]
    attached = sum(n for _, n, _ in in_win)
    first_seen = {}
    for cid, dt in db.query(Invoice.customer_id, func.min(Invoice.date)).filter(Invoice.customer_id.isnot(None)).group_by(Invoice.customer_id):
        first_seen[cid] = dt
    for cid, dt in db.query(Repair.customer_id, func.min(Repair.created_at)).filter(Repair.customer_id.isnot(None)).group_by(Repair.customer_id):
        if cid not in first_seen or dt < first_seen[cid]:
            first_seen[cid] = dt
    repair_custs = {cid for (cid,) in db.query(Repair.customer_id).filter(Repair.created_at >= s, Repair.created_at < e, Repair.customer_id.isnot(None)).distinct()}
    active = {cid for cid, _, _ in in_win} | repair_custs
    new = {cid for cid in active if cid in first_seen and s <= first_seen[cid] < e}
    returning = active - new
    last_inv = {cid: dt for cid, dt in db.query(Invoice.customer_id, func.max(Invoice.date)).filter(Invoice.customer_id.isnot(None)).group_by(Invoice.customer_id)}
    credit_total = sum(c.store_credit or 0 for c in cust.values())
    pts_total = sum(c.points or 0 for c in cust.values())
    kpis = [Kpi("Active customers", len(active), "bought or dropped off a repair"), Kpi("New customers", len(new), "first-ever visit in period", "good"),
            Kpi("Returning customers", len(returning), f"{pct(len(returning), len(active)):.0f}% of active"),
            Kpi("Sales linked to a customer", Pct(pct(attached, attached + walkins)), f"{walkins} walk-in sale(s) with no customer", "warn" if walkins > attached else ""),
            Kpi("Store credit owed", Money(r2(credit_total))), Kpi("Loyalty points outstanding", int(pts_total), f"≈ ${pts_total / redeem:,.0f}" if redeem else "")]
    top = sorted(in_win, key=lambda x: -(x[2] or 0))[:15]
    sec_top = Section("Top customers in this period", ["Customer", "Phone", "Visits", "Net spend", "Lifetime spend"],
                      [[cust[c].name, cust[c].phone, n, Money(r2(v)), Money(r2(cust[c].spent))] for c, n, v in top if c in cust])
    cutoff = now_utc - timedelta(days=90)
    dormant = sorted([c for c in cust.values() if (c.spent or 0) > 0 and c.id in last_inv and last_inv[c.id] < cutoff], key=lambda c: -(c.spent or 0))[:30]
    sec_dorm = Section("Win-back list — good customers not seen in 90+ days", ["Customer", "Phone", "Last purchase", "Lifetime spend", "May receive promotions (CASL)"],
                       [[c.name, c.phone, to_local_date(last_inv[c.id], tz).isoformat(), Money(r2(c.spent)), bool(c.marketing_consent)] for c in dormant],
                       note="Only message customers marked Yes with promotional offers. Transactional messages (receipts, repair-ready) are always fine.", empty="No lapsed customers yet.")
    cr = sorted([c for c in cust.values() if (c.store_credit or 0) > 0], key=lambda c: -c.store_credit)[:25]
    sec_cr = Section("Store credit owed (top 25)", ["Customer", "Phone", "Balance", "Last purchase"],
                     [[c.name, c.phone, Money(r2(c.store_credit)), to_local_date(last_inv[c.id], tz).isoformat() if c.id in last_inv else "—"] for c in cr],
                     foot=["Total owed (all customers)", "", Money(r2(credit_total)), ""], empty="No store credit outstanding.")
    # data quality: duplicates by phone
    by_phone = defaultdict(list)
    for c in cust.values():
        digits = re.sub(r"\D", "", c.phone or "")[-10:]
        if len(digits) >= 7:
            by_phone[digits].append(c)
    dups = [v for v in by_phone.values() if len(v) > 1]
    sec_dup = Section("Data quality — probable duplicate customers (same phone number)", ["Phone (digits)", "Records", "Names", "Combined spend"],
                      [[re.sub(r"\D", "", v[0].phone)[-10:], len(v), " / ".join(c.name for c in v), Money(r2(sum(c.spent or 0 for c in v)))] for v in sorted(dups, key=lambda v: (-sum(c.spent or 0 for c in v), -len(v)))[:40]],
                      note="Phones are stored exactly as typed, so '250-555-0101' and '(250) 555-0101' become two customers and split their history, points and credit.",
                      empty="No duplicates found.")
    no_phone = sum(1 for c in cust.values() if not (c.phone or "").strip())
    consent = sum(1 for c in cust.values() if c.marketing_consent)
    sec_base = Section("Customer base", ["Measure", "Value"], [["Total customers", len(cust)], ["Without a phone number", no_phone],
                                                                ["Opted in to promotions (CASL)", consent]])
    return Report("customers", "Customers Report", _sub_period(tz, d1, d2), kpis, [sec_top, sec_dorm, sec_cr, sec_dup, sec_base])


# ── REPORT: customer messaging (v46) ──────────────────────────────────

_KIND_NAMES = {"ready": "Ready for pickup", "reminder": "Pickup reminder", "review": "Review request", "winback": "Win-back offer"}


def messaging_report(db, tz, d1: date, d2: date) -> Report:
    """What the automatic texts did. 'Sent' means Twilio accepted the text —
    delivery receipts are not tracked, so a phone that was switched off still
    counts as sent."""
    s, e = day_bounds_utc(d1, d2, tz)
    M = OutboundMessage
    sent = db.query(M).filter(M.status == "sent", M.sent_at >= s, M.sent_at < e).all()
    # Failed / skipped / cancelled rows have no sent_at, so they belong to the day they were queued.
    other = db.query(M).filter(M.status.in_(("failed", "skipped", "cancelled")), M.created_at >= s, M.created_at < e).all()
    pending_now = db.query(func.count(M.id)).filter(M.status.in_(("pending", "sending"))).scalar() or 0
    auto_sent = [m for m in sent if m.source != "manual"]
    manual_sent = [m for m in sent if m.source == "manual"]
    failed = [m for m in other if m.status == "failed"]
    skipped = [m for m in other if m.status == "skipped"]
    cancelled = [m for m in other if m.status == "cancelled"]
    optouts = db.query(Customer).filter(Customer.sms_opt_out.is_(True), Customer.sms_opt_out_at >= s, Customer.sms_opt_out_at < e).all()

    # Repairs that got a reminder in this period and have since been collected.
    rem_repairs = {m.repair_id for m in auto_sent if m.kind == "reminder" and m.repair_id}
    collected_after = 0
    if rem_repairs:
        for chunk in chunked(list(rem_repairs)):
            collected_after += db.query(func.count(Repair.id)).filter(Repair.id.in_(chunk), Repair.status == "COLLECTED").scalar() or 0
    attempted = len(auto_sent) + len(failed)
    kpis = [
        Kpi("Automatic texts sent", len(auto_sent), f"+ {len(manual_sent)} sent by hand from a ticket" if manual_sent else "accepted by Twilio", "good" if auto_sent else ""),
        Kpi("Failed", len(failed), f"{pct(len(failed), attempted):.0f}% of attempts" if attempted else "", "bad" if failed else ""),
        Kpi("Skipped", len(skipped), "no longer needed / not allowed at send time"),
        Kpi("Cancelled", len(cancelled), "device collected or cancelled by staff"),
        Kpi("Waiting to send", int(pending_now), "right now (all dates)", "warn" if pending_now > 50 else ""),
        Kpi("New do-not-text", len(optouts), "customers who replied STOP", "warn" if optouts else ""),
        Kpi("Collected after a reminder", collected_after, f"of {len(rem_repairs)} phone(s) reminded", "good" if collected_after else ""),
    ]

    # By kind
    by_kind = []
    for k in ("ready", "reminder", "review", "winback"):
        ks = [m for m in auto_sent if m.kind == k]
        by_kind.append([_KIND_NAMES[k], len(ks), sum(1 for m in failed if m.kind == k), sum(1 for m in skipped if m.kind == k),
                        sum(1 for m in cancelled if m.kind == k)])
    sec_kind = Section("By type of text", ["Type", "Sent", "Failed", "Skipped", "Cancelled"], by_kind,
                       foot=["Total", sum(r[1] for r in by_kind), sum(r[2] for r in by_kind), sum(r[3] for r in by_kind), sum(r[4] for r in by_kind)])

    # By local day
    days = defaultdict(lambda: [0, 0])
    for m in auto_sent:
        days[to_local_date(m.sent_at, tz)][0] += 1
    for m in failed:
        days[to_local_date(m.created_at, tz)][1] += 1
    sec_day = Section("Day by day", ["Date", "Sent", "Failed"], [[d.isoformat(), v[0], v[1]] for d, v in sorted(days.items())],
                      bars=[(d.strftime("%b %d"), v[0]) for d, v in sorted(days.items())] if len(days) > 1 else None,
                      empty="No automatic texts in this period.")

    # Why things did not send
    why = defaultdict(int)
    for m in failed:
        why[("Failed", (m.last_error or m.error_code or "Unknown error")[:90])] += 1
    for m in skipped:
        why[("Skipped", (m.skip_reason or "No reason recorded")[:90])] += 1
    sec_why = Section("Why texts did not go out", ["Outcome", "Reason", "Count"],
                      [[o, r, n] for (o, r), n in sorted(why.items(), key=lambda kv: -kv[1])[:25]],
                      note="Skipped is normal: the device was collected, the customer asked not to be texted, or the text was too late to be useful.",
                      empty="Nothing failed or was skipped.")

    # Failures to act on
    recent_fail = sorted(failed, key=lambda m: m.created_at, reverse=True)[:25]
    reps = {r.id: r for r in db.query(Repair).filter(Repair.id.in_([m.repair_id for m in recent_fail if m.repair_id])).all()} if recent_fail else {}
    custs = {c.id: c for c in db.query(Customer).filter(Customer.id.in_([m.customer_id for m in recent_fail if m.customer_id])).all()} if recent_fail else {}
    sec_fail = Section("Failed texts to look at (latest 25)", ["Queued", "Type", "Ticket", "Customer", "Phone", "Attempts", "Error"],
                       [[to_local(m.created_at, tz).strftime("%Y-%m-%d %H:%M"), _KIND_NAMES.get(m.kind, m.kind),
                         str(reps[m.repair_id].ticket_no) if m.repair_id in reps else "—",
                         custs[m.customer_id].name if m.customer_id in custs else "—", m.to_phone or (custs[m.customer_id].phone if m.customer_id in custs else ""),
                         int(m.attempts or 0), (m.last_error or m.error_code or "")[:120]] for m in recent_fail],
                       note="A bad phone number is the usual cause — fix it on the customer, then press Retry on the Automatic texts page.",
                       empty="No failed texts in this period.")

    sec_out = Section("Customers who opted out in this period", ["Customer", "Phone", "When"],
                      [[c.name, c.phone, to_local(c.sms_opt_out_at, tz).strftime("%Y-%m-%d %H:%M")] for c in sorted(optouts, key=lambda c: c.sms_opt_out_at)[:50]],
                      note="They get no automatic texts and the manual text buttons are blocked. Use the toggle on the customer page if they ask to be texted again.",
                      empty="Nobody opted out in this period.")
    rep = Report("messaging", "Customer Messaging", _sub_period(tz, d1, d2), kpis, [sec_kind, sec_day, sec_why, sec_fail, sec_out])
    rep.notes.append("'Sent' means the text was accepted by Twilio. Delivery receipts are not tracked, so a text to a switched-off phone still counts as sent.")
    return rep


# ── REPORT: staff ─────────────────────────────────────────────────────

def staff_report(db, tz, d1: date, d2: date) -> Report:
    w = SalesWindow(db, tz, d1, d2)
    t = w.totals()
    s, e = w.start_utc, w.end_utc
    rows = []
    for n, v in sorted(w.by_staff().items(), key=lambda kv: -kv[1]["net"]):
        gp = v["net"] - v["cogs"]
        rows.append([n, int(v["count"]), Money(r2(v["net"])), Money(r2(v["net"] / v["count"])) if v["count"] else Money(0), Money(r2(gp)),
                     Pct(pct(gp, v["net"])), Money(r2(v["discount"])), Pct(pct(v["discount"], v["gross"])), int(v["refund_count"]), Money(r2(v["refund_total"]))])
    sec_sales = Section("Sales by staff", ["Staff", "Sales", "Net sales", "Avg sale", "Gross profit", "Margin", "Discounts", "Discount %", "Refunds processed", "Refund $"], rows)
    sessions = db.query(CashSession).filter(CashSession.date >= d1.isoformat(), CashSession.date <= d2.isoformat()).all()
    cu = defaultdict(lambda: [0, 0.0, 0.0])
    for x in sessions:
        o = cu[x.closed_by_name or "—"]; o[0] += 1; o[1] += x.difference or 0; o[2] += abs(x.difference or 0)
    sec_cu = Section("Cash Up closed by", ["Staff", "Days closed", "Net over/(short)", "Total absolute variance"],
                     [[k, v[0], Money(r2(v[1])), Money(r2(v[2]))] for k, v in sorted(cu.items())], empty="No Cash Ups closed in this period.")
    tech = defaultdict(lambda: [0, []])
    for r in db.query(Repair).options(selectinload(Repair.technician)).filter(Repair.updated_at >= s):
        hd = _hist_dates(r); c = hd.get("COLLECTED")
        if c and s <= c < e:
            o = tech[r.technician.name if r.technician else "Unassigned"]; o[0] += 1
            if hd.get("RECEIVED"):
                o[1].append((c - hd["RECEIVED"]).total_seconds() / 86400)
    sec_tech = Section("Repairs collected by technician", ["Technician", "Tickets", "Avg turnaround (days)"],
                       [[k, v[0], Dec(sum(v[1]) / len(v[1])) if v[1] else None] for k, v in sorted(tech.items(), key=lambda kv: -kv[1][0])], empty="No repairs collected in this period.")
    kpis = [Kpi("Net sales", Money(r2(t["net_sales"]))), Kpi("Sales", int(t["count"])), Kpi("Discounts", Money(r2(t["discount"])), f"{pct(t['discount'], t['gross']):.1f}% of gross"),
            Kpi("Refunds", Money(r2(t["refund_total"])), f"{int(t['refund_count'])} refund(s)")]
    return Report("staff", "Staff Performance", _sub_period(tz, d1, d2), kpis, [sec_sales, sec_cu, sec_tech],
                  ["Refunds before v45 did not record who processed them and show as 'Unrecorded (legacy)'."])


# ── REPORT: exceptions / loss prevention ──────────────────────────────

def exceptions_report(db, tz, d1: date, d2: date, disc_threshold: float) -> Report:
    w = SalesWindow(db, tz, d1, d2)
    s, e = w.start_utc, w.end_utc
    ref_rows = [[inv.number, to_local_date(inv.date, tz).isoformat(), to_local_date(w.refund_ts(inv), tz).isoformat(), inv.customer.name if inv.customer else "Walk-in",
                 Money(r2(inv.total)), inv.refund_method or "not recorded", inv.refunded_by or "not recorded", inv.refund_reason or ""] for inv in w.refunds]
    sec_ref = Section("Refunds", ["Invoice", "Sold", "Refunded", "Customer", "Amount", "Paid back via", "Processed by", "Reason"], ref_rows, empty="No refunds in this period.")
    big, over_rows, below, sub_over = [], [], [], []
    for inv in w.sold:
        f = w.fig[inv.id]
        if f["gross"] > 0 and f["discount"] / f["gross"] * 100 >= disc_threshold:
            big.append([inv.number, to_local_date(inv.date, tz).isoformat(), inv.staff.name if inv.staff else "—", Money(r2(f["gross"])), Money(r2(f["discount"])),
                        Pct(pct(f["discount"], f["gross"])), "store credit" if (inv.store_credit_used or 0) else ("loyalty" if inv.loyalty_pts_used else "manual")])
        if f["lines_gross"] and abs(f["lines_gross"] - f["gross"]) > 0.01:
            sub_over.append([inv.number, to_local_date(inv.date, tz).isoformat(), inv.staff.name if inv.staff else "—", Money(r2(f["lines_gross"])), Money(r2(f["gross"])), Money(r2(f["gross"] - f["lines_gross"]))])
        for l in inv.lines:
            p = w.products.get(l.product_id) if l.product_id else None
            cost, known = w._line_cost(l)
            if known and cost > 0 and (l.price or 0) < cost:
                below.append([inv.number, to_local_date(inv.date, tz).isoformat(), inv.staff.name if inv.staff else "—", l.name, Money(r2(l.price)), Money(r2(cost))])
            if p is not None and abs((l.price or 0) - (p.price or 0)) > 0.01 and not l.exchange_note:
                over_rows.append([inv.number, to_local_date(inv.date, tz).isoformat(), inv.staff.name if inv.staff else "—", l.name, Money(r2(p.price)), Money(r2(l.price)), Money(r2((l.price or 0) - (p.price or 0)))])
    sec_big = Section(f"Discounts of {disc_threshold:g}% or more", ["Invoice", "Date", "Staff", "Gross", "Discount", "Discount %", "Type"], big, empty="None at this threshold.")
    sec_sub = Section("Subtotal manually overridden (invoice subtotal ≠ sum of its lines)", ["Invoice", "Date", "Staff", "Lines add up to", "Subtotal used", "Difference"], sub_over,
                      note="The POS lets staff overwrite the whole subtotal. It is not logged anywhere else, so this is the only place it shows.", empty="None.")
    sec_over = Section("Sold at a price different from today's list price", ["Invoice", "Date", "Staff", "Item", "List price now", "Sold at", "Difference"], over_rows[:80],
                       note="Can also be a legitimate price change since the sale — look for repeats by one staff member.", empty="None.")
    sec_below = Section("Sold below cost", ["Invoice", "Date", "Staff", "Item", "Sold at", "Cost"], below[:80], empty="None.")
    sessions = db.query(CashSession).filter(CashSession.date >= d1.isoformat(), CashSession.date <= d2.isoformat()).order_by(CashSession.date).all()
    var = [[x.date, x.closed_by_name, Money(r2(x.expected)), Money(r2(x.actual)), Money(r2(x.difference)), Money(r2(x.card_difference)), x.notes or ""]
           for x in sessions if abs(x.difference or 0) >= 0.5 or abs(x.card_difference or 0) >= 0.5]
    sec_var = Section("Cash Up differences of $0.50 or more", ["Date", "Closed by", "Expected", "Counted", "Cash over/(short)", "Card diff", "Notes"], var, empty="No notable differences.")
    traded = {d for d, x in w.by_day().items() if x["count"]}
    missing = sorted(d for d in traded if d.isoformat() not in {x.date for x in sessions} and d < local_today(tz))
    sec_miss = Section("Days with sales but no Cash Up closed", ["Date", "Sales", "Net sales"],
                       [[d.isoformat(), int(w.by_day()[d]["count"]), Money(r2(w.by_day()[d]["net"]))] for d in missing], empty="Every trading day was closed out.")
    nocard = [[inv.number, to_local_date(inv.date, tz).isoformat(), inv.staff.name if inv.staff else "—", Money(r2(inv.card_amount))] for inv in w.sold if (inv.card_amount or 0) > 0 and not inv.card_reference]
    sec_nocard = Section("Card sales with no terminal reference entered", ["Invoice", "Date", "Staff", "Card amount"], nocard[:80], empty="All card sales have a reference.")
    audit = (db.query(AuditLog).filter(AuditLog.ts >= s, AuditLog.ts < e, AuditLog.action.in_(("REFUND", "RETURN", "EXPENSE_VOID", "EXCHANGE", "SECURITY_BLOCK", "PIN_CHANGE", "BACKUP_IMPORT", "BACKUP_EXPORT", "PRODUCT_DELETE", "STAFF_ADD", "STAFF_EDIT", "STAFF_TOGGLE", "SETTINGS", "EXPORT")), ~AuditLog.detail.like("Redeemed%"))
             .order_by(AuditLog.ts.desc()).limit(120).all())
    sec_audit = Section("Sensitive actions from the audit log", ["When", "Who", "Action", "Detail"],
                        [[to_local(a.ts, tz).strftime("%Y-%m-%d %H:%M"), a.staff_name, a.action, a.detail] for a in audit], empty="None.")
    tis = db.query(TradeIn).filter(TradeIn.created_at >= s, TradeIn.created_at < e, TradeIn.payout_method == "cash").all()
    sec_tr = Section("Cash trade-in payouts", ["Date", "Number", "Device", "Paid out", "Staff"],
                     [[to_local_date(x.created_at, tz).isoformat(), x.number, x.device, Money(r2(x.offered_amount)), x.staff_name] for x in tis], empty="None.")
    exp_all = _expenses.in_window(db, s, e, include_voided=True)          # v52: cash taken out of the till
    till_out = [x for x in exp_all if x.paid_from == "drawer" and not x.voided]
    exp_void = [x for x in exp_all if x.voided]
    sec_till = Section("Cash taken out of the till (expenses & deposits)", ["When", "Who", "Category", "What for", "Amount"],
                       [[to_local(x.spent_at, tz).strftime("%Y-%m-%d %H:%M"), x.staff_name or "—", x.category, x.description, Money(r2(x.amount))] for x in till_out[:120]],
                       foot=["Total", "", "", "", Money(r2(sum(x.amount or 0 for x in till_out)))] if till_out else None,
                       note="Cash Up expects the drawer to hold this much less. Look for round numbers, repeat entries by one person, or a category that doesn't fit.", empty="Nothing was taken out of the till.")
    sec_xvoid = Section("Expense entries that were voided", ["When", "Entered by", "Amount", "Voided by", "Why"],
                        [[to_local(x.spent_at, tz).strftime("%Y-%m-%d %H:%M"), x.staff_name or "—", Money(r2(x.amount)), x.voided_by or "—", x.void_reason or "—"] for x in exp_void],
                        empty="None.")
    flags = len(ref_rows) + len(big) + len(sub_over) + len(below) + len(var) + len(missing) + len(nocard) + len(exp_void)
    kpis = [Kpi("Items needing a look", flags, "", "warn" if flags else "good"), Kpi("Refunds", len(ref_rows)), Kpi("Heavy discounts", len(big)),
            Kpi("Subtotal overrides", len(sub_over)), Kpi("Sold below cost", len(below)), Kpi("Days with no Cash Up", len(missing)),
            Kpi("Till paid-outs", len(till_out), f"${sum(x.amount or 0 for x in till_out):,.2f}")]
    return Report("exceptions", "Exceptions & Loss Prevention", _sub_period(tz, d1, d2), kpis,
                  [sec_ref, sec_big, sec_sub, sec_over, sec_below, sec_var, sec_miss, sec_nocard, sec_till, sec_xvoid, sec_tr, sec_audit],
                  ["Stock changes made by finishing a stock count are in the Stock Counts & Losses report. Stock typed straight into a product on the Products screen is still not recorded with before/after quantities."])


# ── REPORT: stock counts & losses (v51) ───────────────────────────────

STOCKLOSS_DETAIL_CAP = 500


def stock_loss_report(db, tz, d1: date, d2: date) -> Report:
    """Every difference found by stock counts that were finished (applied) in the period.

    Only the lines that really moved stock count (a count that matched adds nothing). A line's cost is the one frozen
    when the count was applied; counts finished before v51 froze nothing, so they use the product's cost today, and a
    line whose product was deleted has no cost at all and is shown as "cost unknown" instead of guessed."""
    start, end = day_bounds_utc(d1, d2, tz)
    counts = (db.query(StockCount).filter(StockCount.status == "applied", StockCount.closed_at >= start, StockCount.closed_at < end)
              .order_by(StockCount.closed_at.desc()).all())
    by_id = {c.id: c for c in counts}
    lines = []
    if by_id:
        for chunk in chunked(list(by_id)):
            lines += (db.query(StockCountLine).filter(StockCountLine.count_id.in_(chunk), StockCountLine.applied_delta.isnot(None),
                                                      StockCountLine.applied_delta != 0).all())
    prod_ids = {l.product_id for l in lines if l.unit_cost is None}
    cost_now = {}
    for chunk in chunked(list(prod_ids)):
        cost_now.update({p.id: float(p.cost or 0) for p in db.query(Product).filter(Product.id.in_(chunk)).all()})

    recs = []
    for l in lines:
        c = by_id[l.count_id]
        delta = int(l.applied_delta)
        cost = float(l.unit_cost) if l.unit_cost is not None else cost_now.get(l.product_id)
        recs.append({"count": c, "line": l, "delta": delta, "cost": cost, "value": None if cost is None else round(delta * cost, 2),
                     "reason": l.reason if _stockcount.reason_fits(l.reason or "", delta) else ""})

    lost = [r for r in recs if r["delta"] < 0]
    found = [r for r in recs if r["delta"] > 0]
    units_lost, units_found = -sum(r["delta"] for r in lost), sum(r["delta"] for r in found)
    val_lost = -sum(r["value"] for r in lost if r["value"] is not None)
    val_found = sum(r["value"] for r in found if r["value"] is not None)
    unknown = sum(1 for r in recs if r["value"] is None)
    unreasoned = sum(1 for r in lost if not r["reason"])

    kpis = [
        Kpi("Counts finished", len(counts), "in this period"),
        Kpi("Units missing", units_lost, f"{len(lost)} product line{'' if len(lost) == 1 else 's'}", "bad" if units_lost else "good"),
        Kpi("Value missing (at cost)", Money(r2(val_lost)), "", "bad" if val_lost > 0.005 else "good"),
        Kpi("Units found", units_found, f"{len(found)} product line{'' if len(found) == 1 else 's'}"),
        Kpi("Value found (at cost)", Money(r2(val_found))),
        Kpi("Net (at cost)", Money(r2(val_found - val_lost)), "", "bad" if val_found - val_lost < -0.005 else "good"),
        Kpi("Losses with no reason", unreasoned, "ask who counted" if unreasoned else "", "warn" if unreasoned else "good"),
    ]

    groups: dict = {}
    for r in recs:
        key = (r["reason"], "Missing" if r["delta"] < 0 else "Found")
        g = groups.setdefault(key, {"lines": 0, "units": 0, "value": 0.0})
        g["lines"] += 1
        g["units"] += abs(r["delta"])
        g["value"] += abs(r["value"]) if r["value"] is not None else 0.0
    reason_rows = [[(_stockcount.reason_label(k[0]) if k[0] else _stockcount.NO_REASON), k[1], g["lines"], g["units"], Money(r2(g["value"]))]
                   for k, g in sorted(groups.items(), key=lambda kv: (kv[0][1] != "Missing", -kv[1]["value"], -kv[1]["units"]))]
    sec_reason = Section("By reason", ["Reason", "Direction", "Product lines", "Units", "At cost"], reason_rows,
                         note="“No reason given” means whoever finished the count skipped the “Why?” box for that line.",
                         bars=[(f"{r[0]} (missing)", float(r[4])) for r in reason_rows if r[1] == "Missing"][:8] or None,
                         empty="No stock counts were finished in this period.")

    count_rows = [[to_local(c.closed_at, tz).strftime("%Y-%m-%d %H:%M"), c.name, c.closed_by or "—", int(c.items_changed or 0), int(c.units_net or 0),
                   Money(r2(c.value_net or 0)), "Yes" if c.zero_filled else "No"] for c in counts]
    sec_counts = Section("Counts finished in this period", ["Finished", "Count", "By", "Products changed", "Net units", "Net at cost", "Uncounted set to 0"],
                         count_rows, empty="No stock counts were finished in this period.")

    per_product: dict = {}
    for r in lost:
        l = r["line"]
        g = per_product.setdefault(l.product_id, {"sku": l.sku, "name": l.name, "times": 0, "units": 0, "value": 0.0, "unknown": False})
        g["times"] += 1
        g["units"] += -r["delta"]
        if r["value"] is None:
            g["unknown"] = True
        else:
            g["value"] += -r["value"]
    worst = sorted(per_product.values(), key=lambda g: (-g["value"], -g["units"], g["name"].lower()))[:25]
    sec_worst = Section("Products that go missing most", ["SKU", "Product", "Times short", "Units missing", "At cost"],
                        [[g["sku"], g["name"], g["times"], g["units"], ("cost unknown" if g["unknown"] and not g["value"] else Money(r2(g["value"])))] for g in worst],
                        note="Worth a look at where these are kept: the same product coming up short count after count is rarely a miscount.",
                        empty="Nothing went missing in this period.")

    detail = sorted(recs, key=lambda r: (r["count"].closed_at, r["line"].name.lower()), reverse=True)
    detail_rows = [[to_local_date(r["count"].closed_at, tz).isoformat(), r["count"].name, r["line"].sku, r["line"].name, int(r["line"].stock_at_count), int(r["line"].counted),
                    r["delta"], (_stockcount.reason_label(r["reason"]) if r["reason"] else "—"), ("cost unknown" if r["value"] is None else Money(r2(r["value"])))]
                   for r in detail[:STOCKLOSS_DETAIL_CAP]]
    sec_detail = Section("Every difference", ["Date", "Count", "SKU", "Product", "System said", "Counted", "Stock moved", "Reason", "At cost"], detail_rows,
                         note=(f"Showing the newest {STOCKLOSS_DETAIL_CAP} of {len(detail)} lines. Narrow the dates to see the rest." if len(detail) > STOCKLOSS_DETAIL_CAP else ""),
                         empty="No differences in this period.")

    notes = ["Values use each product's cost at the moment the count was finished, so a later cost change doesn't rewrite a loss.",
             "A count that matched the system exactly changes nothing and doesn't appear here. Counts that were cancelled or are still open are not included."]
    if unknown:
        notes.append(f"{unknown} line{'' if unknown == 1 else 's'} {'has' if unknown == 1 else 'have'} no known cost (the product was deleted, or its cost was never recorded). They are shown as “cost unknown” and left out of the dollar totals.")
    return Report("stockloss", "Stock Counts & Losses", _sub_period(tz, d1, d2), kpis, [sec_reason, sec_counts, sec_worst, sec_detail], notes)



# ── REPORT: expenses & till paid-outs (v52) ───────────────────────────

def expenses_report(db, tz, d1: date, d2: date) -> Report:
    """Everything recorded as an expense or as cash taken out of the till in the period."""
    start, end = day_bounds_utc(d1, d2, tz)
    rows_all = _expenses.in_window(db, start, end, include_voided=True)
    live = [e for e in rows_all if not e.voided]
    voided = [e for e in rows_all if e.voided]
    biz = [e for e in live if e.category not in _expenses.NOT_AN_EXPENSE]
    movers = [e for e in live if e.category in _expenses.NOT_AN_EXPENSE]
    biz_total = r2(sum(e.amount or 0 for e in biz))
    tax_total = r2(sum(e.tax_paid or 0 for e in biz))
    till_total = r2(sum(e.amount or 0 for e in live if e.paid_from == "drawer"))
    other_total = r2(sum(e.amount or 0 for e in live if e.paid_from != "drawer"))
    moved_total = r2(sum(e.amount or 0 for e in movers))
    gp = SalesWindow(db, tz, d1, d2).totals()["gross_profit"]

    kpis = [
        Kpi("Business expenses", Money(biz_total), f"{len(biz)} entr{'y' if len(biz) == 1 else 'ies'}, tax included"),
        Kpi("Tax paid on them", Money(tax_total), "possible input tax credits — ask your accountant"),
        Kpi("Taken from the till", Money(till_total), "lowers the cash Cash Up expects"),
        Kpi("Paid another way", Money(other_total), "card, bank or own pocket"),
        Kpi("Deposits & owner draws", Money(moved_total), "cash that left the till but is not a cost"),
        Kpi("Gross profit", Money(r2(gp)), "from sales, before these expenses"),
        Kpi("Profit after expenses", Money(r2(gp - (biz_total - tax_total))), "gross profit less expenses (tax excluded)",
            "bad" if gp - (biz_total - tax_total) < 0 else "good"),
    ]

    cat_rows = [[c, n, Money(a), Money(t)] for c, n, a, t in _expenses.by_category(live)]
    sec_cat = Section("By category", ["Category", "Entries", "Total paid", "Tax in it"], cat_rows,
                      foot=["Total", len(live), Money(r2(sum(e.amount or 0 for e in live))), Money(r2(sum(e.tax_paid or 0 for e in live)))],
                      bars=[(r[0], float(r[2])) for r in cat_rows if r[0] not in _expenses.NOT_AN_EXPENSE][:8] or None,
                      empty="Nothing recorded in this period.")

    day_amt: dict = defaultdict(lambda: [0.0, 0.0])
    for e in live:
        d = to_local_date(e.spent_at, tz)
        day_amt[d][0] += e.amount or 0
        if e.paid_from == "drawer":
            day_amt[d][1] += e.amount or 0
    sec_day = Section("By day", ["Date", "Total recorded", "of which from the till"],
                      [[d.isoformat(), Money(r2(v[0])), Money(r2(v[1]))] for d, v in sorted(day_amt.items())],
                      empty="Nothing recorded in this period.")

    detail = [[to_local(e.spent_at, tz).strftime("%Y-%m-%d %H:%M"), e.staff_name or "—", e.category, e.payee or "—", e.description,
               _expenses.PAID_FROM.get(e.paid_from, e.paid_from), e.receipt_ref or "—", Money(r2(e.amount)), Money(r2(e.tax_paid))]
              for e in live]
    sec_detail = Section("Every entry", ["When", "Who", "Category", "Paid to", "What for", "How paid", "Receipt #", "Amount", "Tax"],
                         detail, empty="Nothing recorded in this period.")
    sec_void = Section("Voided entries (not counted anywhere)", ["When", "Who entered", "Category", "What for", "Amount", "Voided by", "Why"],
                       [[to_local(e.spent_at, tz).strftime("%Y-%m-%d %H:%M"), e.staff_name or "—", e.category, e.description,
                         Money(r2(e.amount)), e.voided_by or "—", e.void_reason or "—"] for e in voided],
                       empty="No entries were voided.")
    notes = ["Amounts include tax. “Tax paid” is only what was typed in on each entry — keep the receipts.",
             "Bank deposits and owner draws take cash out of the drawer but are not business costs, so they are left out of “Business expenses” and the profit figure.",
             "Profit after expenses only knows about costs recorded here. Rent, wages and anything else paid by cheque or bank transfer count only if they were entered."]
    return Report("expenses", "Expenses & Till Paid-Outs", _sub_period(tz, d1, d2), kpis, [sec_cat, sec_day, sec_detail, sec_void], notes)


# ── REPORT: cash-up history ───────────────────────────────────────────

def cashup_report(db, tz, year: int, month: int) -> Report:
    first, last = month_bounds(year, month)
    w = SalesWindow(db, tz, first, last)
    sessions = {x.date: x for x in db.query(CashSession).filter(CashSession.date >= first.isoformat(), CashSession.date <= last.isoformat())}
    days = w.by_day()
    rows, absvar, net_var, card_var = [], 0.0, 0.0, 0.0
    d = first
    while d <= last:
        x, x_sales = sessions.get(d.isoformat()), days.get(d)
        if x or (x_sales and x_sales["count"]):
            rows.append([d.isoformat(), d.strftime("%a"), int(x_sales["count"]) if x_sales else 0,
                         Money(r2(x.paid_out or 0)) if x else None,
                         Money(r2(x.expected)) if x else None, Money(r2(x.actual)) if x else None, Money(r2(x.difference)) if x else None,
                         Money(r2(x.card_expected)) if x else None, Money(r2(x.card_batch)) if x else None, Money(r2(x.card_difference)) if x else None,
                         x.closed_by_name if x else "NOT CLOSED", (x.notes or "") if x else ""])
            if x:
                absvar += abs(x.difference or 0); net_var += x.difference or 0; card_var += x.card_difference or 0
        d += timedelta(days=1)
    not_closed = sum(1 for r in rows if r[10] == "NOT CLOSED")
    sec = Section("Cash Up by day", ["Date", "Day", "Sales", "Paid out of till", "Cash expected", "Cash counted", "Over/(short)", "Card expected", "Card batch", "Card diff", "Closed by", "Notes"], rows,
                  empty="No trading days in this month.")
    kpis = [Kpi("Days closed", len(rows) - not_closed, f"of {len(rows)} trading days", "good" if not not_closed else "warn"),
            Kpi("Days NOT closed", not_closed, "", "bad" if not_closed else "good"),
            Kpi("Net cash over/(short)", Money(r2(net_var)), "", "good" if abs(net_var) < 5 else "warn"),
            Kpi("Total absolute variance", Money(r2(absvar))), Kpi("Net card difference", Money(r2(card_var)))]
    return Report("cashup", f"Cash-Up History — {first.strftime('%B %Y')}", _sub_period(tz, first, last), kpis, [sec])


# ── registry + parameter parsing ──────────────────────────────────────

RANGE_PRESETS = [
    ("today", "Today"), ("yesterday", "Yesterday"), ("this_week", "This week"), ("last_week", "Last week"),
    ("this_month", "This month"), ("last_month", "Last month"), ("this_quarter", "This quarter"), ("last_quarter", "Last quarter"),
    ("last_30", "Last 30 days"), ("last_90", "Last 90 days"), ("ytd", "Year to date"), ("last_year", "Last year"),
]


def preset_range(key: str, today: date):
    if key == "today":
        return today, today
    if key == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if key == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if key == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if key == "this_month":
        return month_bounds(today.year, today.month)[0], today
    if key == "last_month":
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return month_bounds(y, m)
    if key in ("this_quarter", "last_quarter"):
        q = (today.month - 1) // 3
        y = today.year
        if key == "last_quarter":
            q -= 1
            if q < 0:
                q, y = 3, y - 1
        first = date(y, q * 3 + 1, 1)
        last = month_bounds(y, q * 3 + 3)[1]
        return first, min(last, today) if key == "this_quarter" else last
    if key == "last_30":
        return today - timedelta(days=29), today
    if key == "last_90":
        return today - timedelta(days=89), today
    if key == "ytd":
        return date(today.year, 1, 1), today
    if key == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    return today, today


REPORTS = [
    {"key": "daily", "group": "Every day", "title": "Daily Report (Z-Report)", "icon": "calendar", "params": ["date"],
     "desc": "End-of-day close: net sales, tax, tenders, expected drawer vs counted, card terminal check, hourly sales, repairs activity, every transaction. Print it and clip it to the cash bag."},
    {"key": "cashup", "group": "Every day", "title": "Cash-Up History", "icon": "banknote", "params": ["month"],
     "desc": "Every trading day of the month: cash expected vs counted, card terminal differences, who closed, and any day nobody closed."},
    {"key": "monthly", "group": "Every month", "title": "Monthly Business Review", "icon": "trending-up", "params": ["month"],
     "desc": "The one to read on the 1st: gross P&L, vs last month and last year, categories, staff, tax to remit, cash-up discipline, store-credit and inventory balances."},
    {"key": "tax", "group": "Every month", "title": "Tax / Remittance Report", "icon": "receipt", "params": ["range"],
     "desc": "GST/PST collected, refunded and net to remit for any filing period, day by day, with reconciliation checks. Hand this to the accountant."},
    {"key": "sales", "group": "Every month", "title": "Sales & Profit (any period)", "icon": "bar-chart-3", "params": ["range"],
     "desc": "Pick any dates: day-by-day sales, margin by category, payment methods, staff, busiest weekdays and hours, best sellers."},
    {"key": "expenses", "group": "Every month", "title": "Expenses & Till Paid-Outs", "icon": "wallet", "params": ["range"],
     "desc": "Every expense and every dollar taken out of the till: by category, by day, tax paid (for input tax credits), voided entries, and profit after expenses."},
    {"key": "inventory", "group": "Running the shop", "title": "Products & Inventory", "icon": "package", "params": ["days"],
     "desc": "Reorder list, fast sellers about to run out, slow movers tying up cash, stock value, and pricing mistakes that distort profit."},
    {"key": "repairs", "group": "Running the shop", "title": "Repairs Report", "icon": "wrench", "params": ["range"],
     "desc": "Volume, turnaround, revenue and margin by repair type / brand / technician; open-ticket aging; chase list for uncollected phones; warranty comebacks."},
    {"key": "customers", "group": "Running the shop", "title": "Customers Report", "icon": "users", "params": ["range"],
     "desc": "New vs returning, top customers, win-back list (CASL-aware), store credit owed, and duplicate-customer clean-up."},
    {"key": "messaging", "group": "Running the shop", "title": "Customer Messaging", "icon": "message-circle", "params": ["range"],
     "desc": "Automatic texts sent, failed and skipped; reminders that got phones collected; who replied STOP; failures to fix."},
    {"key": "staff", "group": "Controls", "title": "Staff Performance", "icon": "user-round", "params": ["range"],
     "desc": "Sales, margin, discounts, refunds and Cash Up variance per person; repairs finished per technician."},
    {"key": "stockloss", "group": "Controls", "title": "Stock Counts & Losses", "icon": "package-check", "params": ["range"],
     "desc": "What every finished stock count found: units and dollars missing or found, why (damaged, lost or stolen, not rung in...), the products that go missing most, and every difference line."},
    {"key": "exceptions", "group": "Controls", "title": "Exceptions & Loss Prevention", "icon": "shield", "params": ["range", "threshold"],
     "desc": "Refunds, heavy discounts, overridden subtotals, below-cost sales, cash differences, days with no Cash Up, missing card references, sensitive audit events."},
]
REPORT_INDEX = {r["key"]: r for r in REPORTS}


def clamp_range(d1: date, d2: date, max_days=732):
    if d2 < d1:
        d1, d2 = d2, d1
    if (d2 - d1).days > max_days:
        d1 = d2 - timedelta(days=max_days)
    return d1, d2


def resolve_params(key: str, args: dict, tz) -> dict:
    """Turn raw query-string values into validated parameters (never raises)."""
    today = local_today(tz)
    p = {}
    spec = REPORT_INDEX[key]["params"]
    if "date" in spec:
        p["date"] = parse_date(args.get("date", ""), today)
    if "month" in spec:
        p["year"], p["month"] = parse_month(args.get("month", ""), today)
    if "range" in spec:
        default_range = "this_month" if key in ("tax", "sales", "repairs", "customers", "messaging", "staff", "exceptions") else "this_month"
        preset = args.get("preset", "")
        if preset in dict(RANGE_PRESETS) and not (args.get("from") and args.get("to")):
            d1, d2 = preset_range(preset, today)
        elif args.get("from") or args.get("to"):
            d1 = parse_date(args.get("from", ""), today.replace(day=1))
            d2 = parse_date(args.get("to", ""), today)
        else:
            d1, d2 = preset_range(default_range, today)
        p["d1"], p["d2"] = clamp_range(d1, d2)
    if "days" in spec:
        try:
            p["days"] = max(7, min(int(args.get("days", 60)), 365))
        except (TypeError, ValueError):
            p["days"] = 60
    if "threshold" in spec:
        try:
            p["threshold"] = max(1.0, min(float(args.get("threshold", 15)), 100.0))
        except (TypeError, ValueError):
            p["threshold"] = 15.0
    return p


def build_report(db, key: str, args: dict) -> tuple[Report, dict, object]:
    tz = get_shop_tz(db)
    p = resolve_params(key, args, tz)
    if key == "daily":
        rep = daily_report(db, tz, p["date"])
    elif key == "monthly":
        rep = monthly_report(db, tz, p["year"], p["month"])
    elif key == "cashup":
        rep = cashup_report(db, tz, p["year"], p["month"])
    elif key == "tax":
        rep = tax_report(db, tz, p["d1"], p["d2"])
    elif key == "sales":
        rep = sales_report(db, tz, p["d1"], p["d2"])
    elif key == "inventory":
        rep = inventory_report(db, tz, p["days"])
    elif key == "repairs":
        rep = repairs_report(db, tz, p["d1"], p["d2"])
    elif key == "customers":
        rep = customers_report(db, tz, p["d1"], p["d2"])
    elif key == "messaging":
        rep = messaging_report(db, tz, p["d1"], p["d2"])
    elif key == "staff":
        rep = staff_report(db, tz, p["d1"], p["d2"])
    elif key == "stockloss":
        rep = stock_loss_report(db, tz, p["d1"], p["d2"])
    elif key == "expenses":
        rep = expenses_report(db, tz, p["d1"], p["d2"])
    elif key == "exceptions":
        rep = exceptions_report(db, tz, p["d1"], p["d2"], p["threshold"])
    else:
        raise KeyError(key)
    return rep, p, tz
