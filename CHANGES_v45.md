# v45 — Reporting pack, shop-local time, refund & cost accuracy

Everything here is additive. Existing pages, routes and the original 254 tests are unchanged; the classic
`/reports` and `/reports/eod` pages still work. 300 tests pass on SQLite and on PostgreSQL 16, and an
upgrade from the original v44 database was verified on both.

## How to upgrade

1. Stop the app, **back up your database** (Settings → Backup, or your Postgres dump).
2. Replace the app files with this folder (keep your own `crm.db` / `DATABASE_URL`).
3. `pip install -r requirements.txt` — this installs `tzdata`, which the new time handling needs on Windows shop PCs.
4. Start the app. New columns are added automatically on first start, and old invoice lines get a cost snapshot once
   (so historical margin is as accurate as the data allows).
5. **Settings → Time zone**: leave on "Automatic (from province)" for Vernon (BC → America/Vancouver).
   The Red Deer instance should be set to America/Edmonton (AB does this automatically from its province).
6. Rotate the session secret: set a new `SESSION_SECRET` environment variable (the old zip contained a `.session_secret` file; it is not in this package).

## What's new

### Report Center (Business → Report Center)
Owner and manager only. Every report has an on-screen view, a print layout, CSV and a multi-sheet Excel export
(exports are written to the Audit Log).

| Report | Use it for |
|---|---|
| Daily Report (Z-Report) | End-of-day close: net sales, tax, tenders, expected drawer vs counted, card terminal check, hourly sales, categories, top items, staff, repair activity, every transaction. Print has sign-off lines. |
| Cash-Up History | Month view of every close: over/short, card-terminal differences, who closed, days nobody closed. |
| Monthly Business Review | Gross P&L, vs last month and last year, day-by-day, categories, payment methods, staff, busiest days/hours, top items, repairs, cash-up discipline, tax to remit, store-credit / points / layaway / inventory balances. |
| Tax / Remittance | GST/PST collected, refunded and net to remit for any period, day by day, with reconciliation checks. |
| Sales & Profit | Any date range: margin by category, tender, staff, weekday, hour, best sellers. |
| Products & Inventory | Reorder list, fast sellers about to run out, slow movers, stock value, products with missing/zero cost or price below cost. |
| Repairs | Volume, turnaround, revenue and margin (including parts) by repair type / brand / technician; open-ticket aging; uncollected-phone chase list; warranty comebacks. |
| Customers | New vs returning, top customers, win-back list (respects marketing consent), store credit owed, duplicate-customer clean-up. |
| Staff Performance | Sales, margin, discounts, refunds and Cash Up variance per person; repairs finished per technician. |
| Exceptions & Loss Prevention | Refunds, heavy discounts, overridden POS subtotals, below-cost sales, cash differences, days with no Cash Up, card sales with no terminal reference, sensitive audit events. |

Also: the dashboard, notifications, daily digest email (now includes gross profit, refunds and a warning when yesterday's Cash Up was not closed),
and the classic reports page now use shop-local days.

### Accuracy fixes behind the reports
- **Shop-local time.** Timestamps are still stored in UTC; they are converted at the edges. A sale at 6 pm in Vernon in winter
  used to land on the next day's report. All day/month boundaries, the Cash Up, the dashboard and every screen that prints a time
  now use the shop's time zone. (BC and Alberta stopped changing clocks on 1 Nov 2026; `tzdata>=2026.3` is required, and the app warns if a PC has an older database.)
- **Refunds are events with a date, method and reason.** The refund form asks for Cash / Card / Store Credit / E-Transfer and a reason.
  A refund reduces the day it was processed, not the day of the original sale. "Store Credit" credits the customer's balance (a customer is required).
  Old refunds with no date are kept and shown as "net zero" in their own period.
- **Cost snapshot.** Each new invoice line records the unit cost and category at the time of sale, so changing or deleting a product no longer rewrites past profit.
  Existing lines are back-filled once from current product costs.
- **Cash Up drawer rules** follow refunds correctly: a cash refund paid today for an earlier day's sale comes out of today's drawer; a same-day refund returned as store credit or e-transfer leaves the cash in the drawer.
- Money is shown as `-$12.99` (never `$-12.99` or `$-0.00`), and CSV/Excel keep amounts numeric (only text cells are protected against spreadsheet formula injection).

## Other changes
- `app/database.py`: forces the `psycopg2` driver for `postgresql://` URLs (SQLAlchemy 2.1 defaults to psycopg v3, which is not installed — this broke fresh deploys).
- `requirements.txt` pinned to the versions that were tested. Upgrade deliberately and re-run `pytest`.
- Legacy `/reports` page no longer loads every invoice and queries per line (it was an N+1).

## Known items deliberately NOT changed (see the review document)
- Store credit currently reduces the taxable amount, which may under-collect tax. Confirm with your accountant before changing.
- Backup/restore does not yet cover invoices, repair parts, trade-ins or layaways, and restore can fail on PostgreSQL foreign keys.
- Stock edits are not logged (no stock-movement ledger), so shrinkage cannot be reported yet.
- Many POST routes lack role checks; PIN lockout is global.

## Files
New: `app/localtime.py`, `app/report_types.py`, `app/reporting.py`, `app/report_routes.py`,
`templates/report_center.html`, `templates/report_view.html`, `templates/report_print.html`, `tests/test_reporting_pack.py`.
Edited: `app/main.py`, `app/models.py`, `app/seed.py`, `app/database.py`, `requirements.txt`, `tests/conftest.py`,
and small edits to `base.html`, `settings.html`, `reports.html`, `invoice_detail.html` and 17 date displays in other templates.
