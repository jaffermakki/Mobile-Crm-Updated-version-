# v47 — Moneris matching, required terminal reference, card fees and 5¢ cash rounding

Builds on v46 (automatic texts). Your workflow is: ring the sale on the Moneris terminal, then ring the same sale in the CRM.
v47 makes sure the two agree, and that the profit numbers include what Moneris charges you.

Testing: 706 tests pass on SQLite (the 540 existing + 166 new). On PostgreSQL 16, 704 pass and 2 are skipped (explained under "Known limits").
An upgrade from a v46 database was verified on SQLite and PostgreSQL, and the new pages and the checkout were driven in a real browser
with the CSRF protection switched on (phone-size screen).

## How to upgrade

1. Back up your database (Settings → Backup, or a Postgres dump).
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`). No new packages.
3. Start the app. One invoice column (`cash_rounding`) and two tables (`card_batches`, `card_statements`) are added automatically.
   Every old invoice keeps its numbers exactly as they were (rounding is never applied to past sales).
4. **Two switches are ON after the upgrade:** *round cash to 5¢* and *require the Moneris reference on card sales*.
   Both are on **Card Fees → Payment settings** if you want either off.

## What changed

### 1. Moneris Check (new page, `/reconcile`; owner and manager)
Pick a day and see the CRM's card transactions next to the terminal.

* **Enter the terminal's batch total** for the day (from the terminal's end-of-day report). The page shows Moneris vs CRM and the difference, the same figure Cash Up uses.
  If they differ you can tick "I found the reason", so it stops being flagged.
* **Tick-off list:** every card sale and card refund in the CRM that day with its reference; tick them against the terminal's printout and a running total builds up.
* **Compare with the Moneris list:** paste, or upload, the day's transactions from the Moneris portal. Any CSV/spreadsheet layout works: the app finds the header row, the delimiter and the amount / type / date columns itself (and lets you pick the columns by hand if it guesses wrong). Nothing is stored.
  It shows three things: **charged on the terminal but not in the CRM** (a sale you forgot to ring), **in the CRM but not on the terminal** (a sale rung that was never charged, or a typo in the amount), and **matched by amount only / reference differs** (listed for a quick glance). Voids and declines are ignored. A sale refunded the same day is not reported as missing.
* **Cash Up** now links here, and if you type the terminal batch while closing Cash Up it is saved to Moneris Check too (a blank field saves nothing).

### 2. Day-end checks (dashboard bell + top of Moneris Check)
Short reminders for things that normally slip:

* a day with card sales or card refunds but **no terminal batch entered**;
* a day with sales but **Cash Up never closed**;
* a day where the **batch and the CRM disagree** and nobody marked it explained;
* a **repair marked Collected with money on it but no sale** (the final cost, or the estimate if there is no final cost).

Only days **from the first start of v47 onwards** are judged, so your history does not light up red on day one. Today is never judged.

### 3. Moneris reference is required on card sales
A Card or Split sale cannot be saved without the reference / approval code from the terminal receipt. The phone checks it before sending, and the server refuses it too, so it cannot be skipped. Cash, E-Transfer, UPI and Store Credit never need one.
This reference is what lets Moneris Check pair a CRM sale with the terminal's row.

**Card numbers are never stored.** If someone pastes a real-looking card number into the reference box, only the last four digits are kept (`************1111`) and the cashier is told. The sale is never blocked by this. The check looks for a real card network's leading digits, length and checksum, so the long numeric Moneris reference numbers (about 18 digits) are left alone.

### 4. Canadian 5¢ cash rounding
* Only the **cash** part of a payment is rounded, to the nearest 5¢ (1–2¢ round down, 3–4¢ round up). Card, E-Transfer, UPI and store credit stay exact.
* **Sales, tax and profit stay on the true total.** Only the drawer money changes. The rounding is stored on each invoice and shown on the receipt (A4, thermal and emailed): "Cash rounding (to 5¢)" and "Cash due".
* The sale screen shows "Cash due (rounded to 5¢)", the change is worked out from it, and "Exact" fills it in.
* A **cash refund pays back the rounded amount**, and tells the cashier exactly how much to hand back.
* **Cash Up and the Daily Report** count the rounded cash, and the Daily Report shows "of which cash rounding to 5¢ (net)" so the small difference is visible instead of a mystery.

### 5. Card fees in the profit numbers
* **Card Fees** page (owner/manager): enter each month's **total fees** (and optionally the net card volume) from the Moneris statement.
  A month's statement is used exactly; part of a month is pro-rated by days.
* Optional **estimated fee rate (%)** for months you have not entered yet. Reports say whether the fee figure is *actual*, *estimated* or *mixed*.
* **Sales report:** new "Card fees" and "Profit after card fees" figures. If no fees are recorded the report says so, instead of silently overstating profit.
* **Monthly report:** profit and loss gets "Less: card processing fees" and "Profit after card fees", plus a **Moneris statement check** (statement volume vs the CRM's card total for that month).
* The Card Fees page has a 12-month table of CRM card sales next to each statement.

### Smaller things
* Backups now include the Moneris batches and statements. Restoring only **adds** missing days/months; it never overwrites or removes what is already entered.
* **Moneris Check** and **Card Fees** are in the menu for owner/manager.

## Fixed along the way
* A very long number in a pasted statement (hundreds of digits) crashed the compare page. It is now treated as "not an amount".
* The new tables fit a 390px phone screen (the customer column is hidden on narrow screens and long references wrap).

## Known limits (please read)

* **The Moneris export format is untested.** I do not have a real Moneris Merchant Direct export. The reader was tested on exports in the common shapes (title lines above the table, `$1,102.50`, brackets or minus for refunds, tab/semicolon files, Void/Declined rows, no header), and has a manual column picker, but **please try it with a real file the first week** and tell me if a column is mis-read.
* **Estimated fee rate** is a rate on the CRM's card total. It is not your real Moneris pricing (interchange, per-transaction fees, monthly fees); the statement figure is always the better one.
* **Matching depends on the reference.** A sale with no reference can still be paired by amount, but is only listed "for a glance". Two sales with the same amount on the same day are paired one-to-one, so which is which may be swapped.
* **Terminal tips and surcharges:** if your terminal adds a tip or surcharge that is not in the CRM total, the terminal amount will be higher than the CRM and shows as an amount difference. There is no field for it yet.
* **Refunds are still whole-invoice** (unchanged), so a partial card refund on the terminal will show as a difference.
* **PostgreSQL restore (still open from v46):** the existing "Restore backup" button deletes customers and products before re-adding them, and PostgreSQL refuses when other records (layaways, invoice lines) point at them. SQLite is fine. **Until v48, do not rely on Restore on Railway**; keep a database dump from Railway as your real backup. This is why two tests (one from v46, one new) run on SQLite only.
* **Rounding is not retroactive.** Invoices created before the upgrade are never changed.
* **Not legal or accounting advice.** The rounding rule is the Canadian one (cash only, nearest 5¢). Check with your accountant that you are happy with how rounding is shown in your books.

## Next (v48, owner-only security and backup pack)
Per-IP login throttling, 6-digit PIN, authenticator 2FA, security headers, private status link, login history, encrypted backups with a restore that works on PostgreSQL, idle session timeout. Say the word and I will start.
