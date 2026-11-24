# v52 — Cash out of the till, part-returns, quote approval by text, newer phone models, scanner self-test

Builds on v51. This release closes the five items that were listed as "not yet" at the end of v51.

**Read this first: the one thing I could not test.** The camera scanner has only ever been run against a *fake* camera
(a video file standing in for a lens). A fake camera proves the page's code works, not that your iPhone's or Android's
real lens focuses, reads a curved sticker or copes with the shop lighting. I cannot do that from here. v52 therefore adds
a **scanner self-test page** (Help → "Test the scanner on this phone", or `/scanner-test`) that you open on your own
phone: it checks camera permission, shows which reader your phone uses (built-in or the bundled one), and reads a barcode
you hold up (or one shown on the page's own screen). **Please run it on your iPhone and on an Android before you rely on
the scanner at the counter.**

## How to upgrade

1. **Download a backup first** (Settings → Data → Download Full Backup).
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`).
3. Start the app. New tables (`sale_returns`, `sale_return_lines`, `expenses`, `quote_approvals`) and one new column
   (`cash_sessions.paid_out`) are created automatically. **Nothing in your existing data changes**, old Cash Ups keep
   the figures they closed with, and the shop is not sent to setup.
4. No new packages and no new required variables.

## What changed

### 1. Cash out of the till: Expenses and paid-outs
* New **Expenses** page (side menu). **Record cash out / expense**: total paid (tax included), of which tax, category,
  where the money came from (**Cash from the till** or **Paid another way**), what it was for, paid to, receipt number
  and date. Any signed-in staff can record one; **voiding needs a manager or the owner** and asks for a reason.
* **Cash Up now expects the right amount.** Expected cash in the drawer = opening float + cash sales − cash taken out. The
  Cash Up screen shows a *Took cash out of the drawer today?* card with a link to record it, and lists what was taken.
* The figure is **frozen when you close the day**. If someone records a till expense for a day that is already closed,
  you are told straight away that the saved expected figure does not include it (count and close again).
* *Bank deposit* and *Owner draw* are recorded as till paid-outs but are **not counted as business expenses** (they
  are not a cost of doing business) and must come from the till.
* New report **Expenses & Till Paid-Outs** (Report Center, "Every month") by category, by day, every entry and voided
  entries, with CSV/Excel/print. Expenses also appear in the daily owner digest ("taken out of the till today"), the monthly
  Excel export (an Expenses sheet) and the Full Backup (and restore).

### 2. Partial returns
* On an invoice: **Return items**. Choose how many of each line comes back, whether each goes **Back on the shelf**
  (damaged or defective stock does not), and how the money goes back (**cash from the drawer, card back on the
  terminal, store credit (needs a customer), or e-transfer**), with an optional reason.
* **Review the refund** shows every number before anything is saved; **Confirm return & refund** saves it. A page that is
  reloaded or double-tapped cannot refund twice.
* The customer gets back what they really paid for those items, **including their share of any discount and of each
  tax (GST/PST)**, to the cent. Returns can be made in several pieces; together they can never exceed what was sold.
  A part-return prints its own receipt.
* Returns are **manager or owner only**. Once part of a sale has been returned, the whole-invoice *Process Refund* and
  exchanges are switched off for it (to stop double refunds); use *Return items* for what is left.
* Cash Up, the Sales & Profit report, the card-fee report, the Moneris check and the daily digest all count a return on
  the day it happened. The Full Backup includes returns.

### 3. Repair quote approval by text
* On a repair ticket, **Quote approval**: enter the price (before tax), what the work is, an optional message and how
  long the quote is good for (3, 7, 14 or 30 days). **Text the quote to** the customer sends a text with a private link;
  **Just make the link** gives you the link to copy (for a customer with no phone number or who asked not to be texted).
* The customer opens the link and sees only your shop name, the ticket number, the device, the work and the price
  before tax. They tap **Approve this quote** or **Decline**. **Opening the link changes nothing**; only the deliberate
  button press does, and **the first answer wins** (a second tap or a later tap on an old text cannot flip it).
* The link is a long random code (24 characters, not guessable). Too many wrong codes from one phone are slowed down
  like wrong PINs. A new quote replaces the old one (the old link stops working). A quote that runs out says so. **Withdraw**
  cancels an unanswered quote.
* When the customer approves, the ticket's price is set to the quote and a line is added to the ticket's history. It
  does **not** move the repair to the next stage; you decide when work starts. If the customer tells you in person or by
  phone, tap **They approved** / **They declined** to record it.
* You get a bell notification when a customer answers by link, and the repairs list shows a quote badge
  (waiting / approved / declined / expired). Everything is audited and included in the Full Backup.
* The text uses your Twilio settings (Settings → SMS). If texting is not set up, or the customer has no valid number or
  has opted out, the page says so and offers the link instead. **The link uses the address you opened the CRM from**,
  so open it on your real (https) address, not a local one, when you send quotes.

### 4. Newer phone models
* **Google Pixel**: Pixel 9 Pro Fold, Pixel 10 (10, 10 Pro, 10 Pro XL, 10 Pro Fold, 10a) and Pixel 11 (11, 11 Pro, 11 Pro XL,
  11 Pro Fold) added; **Pixel Tablet** added.
* **Apple iPad** is a new family (iPad, iPad mini, iPad Air, iPad Pro, in generations). **Samsung Galaxy Tab** (S incl. FE,
  A and Active) and **Galaxy M** are new families. Pickers on the repair ticket, the trade-in form and Bulk-Create
  Variants all use the same lists.
* The model lists were checked against manufacturer and press sources in November 2026. **Models I could not confirm
  (for example Galaxy M37 / M57, and a 12th-generation iPad) are deliberately not in the list.** Use **Not in the list —
  type it**, or add the model on Bulk-Create Variants.

### 5. Scanner self-test
* **Help → Test the scanner on this phone** (or `/scanner-test`, signed in). Checks that the page is on https, that the
  phone allows the camera, which reader it uses, whether the camera starts, and then reads a barcode: hold up any
  product, or use the sample barcode shown on the page itself. It reports how long each read took and whether the barcode's check digit is correct
  (a wrong one means a misread or a damaged label), and has a **Copy results** button so you can send me the outcome.
  A sample barcode can be printed from the page. Nothing is saved.

### 6. Help
* The Help page has new sections: **Taking back some of the items on a sale** (managers), **Asking the customer to approve
  a repair price**, and a cash-out step in **End of the day**. Tests check that every button name it mentions exists.

## v52.2 — the whole build checked end to end, and what that found

Everything was driven in a phone-size browser (390x844) on a brand-new database with CSRF on: sign-in, people and roles, the
catalog, sales (every payment type), invoices, refunds, partial returns, exchanges, layaways, repairs (every status, quote
approval by text, photos, collecting), trade-ins, suppliers and purchase orders, stock counts, expenses and Cash Up, reports,
settings (including the move to Alberta tax), backups and a full restore, automatic texts, and a sweep that sends hand-made
bad numbers to every box. The numbers were worked out separately from the app, and a physical till was tracked and compared
with Cash Up after every step. What it found, all fixed and covered by tests in `tests/test_v52_*.py`:

* **Store credit paid the wrong tax.** Credit spent on a sale reduced the price before tax, so a $24.99 case paid with $20
  credit collected 60 cents of tax instead of $3.00. Credit is now a way of paying: tax is on the full price. Only the credit
  a sale needs is used (asking for more no longer burns the extra), and a layaway can't be started with credit or points on it.
* **The "Store Credit" payment button took nothing off anyone's balance** and worked with nobody attached. It now needs the
  customer, checks they have enough, and takes the sale off their balance.
* **A layaway payment larger than the balance** was recorded in full, so Cash Up expected the change you handed back. Now only
  the balance is recorded and the screen says how much change to give; a card or e-transfer can't be over the balance.
* **A "Split" payment with only one side filled in** (or nothing) was saved with no cash and no card, so it counted in neither
  column of Cash Up. One side only becomes a Cash or Card sale; nothing is refused.
* **Refunds and loyalty points.** Refunding a whole invoice now takes back the points that sale earned and gives back the points
  and store credit the customer spent on it; a partial return takes back the earned points in proportion.
* **Two people could have the same PIN**, so sign-in picked whoever came first. Refused now (and the refusals are throttled so
  they can't be used to guess someone's PIN).
* **Bad numbers.** "nan", "inf", a minus sign, a huge number, or one past the database limit was accepted as a price, cost,
  stock level, deposit, store credit, payment, repair estimate, warranty, supplier lead time, points rate or digest hour, and
  then broke the pages that list them. Every typed number is now checked on the server (limits in `app/inputs.py`).
* **Tax rounding** was a cent short on about 2% of tax lines; it is now exact half-up cents.
* **Names and roles.** A staff member or customer with a blank name, a made-up role, and a ticket with no name or phone are
  refused. The only Owner can't be switched to Manager. A duplicate SKU gives a message instead of an error page.
* Smaller: phone numbers typed differently reuse the same customer; a collected repair's final price no longer includes tax;
  trade-in and Cash Up inputs are checked; a purchase order can be cancelled; CSV/Excel exports can't run a formula typed into a
  name; reports agree about a refund day; the Products page script error for cashiers and technicians; the API pages
  (`/docs`, `/redoc`, `/openapi.json`) are switched off.

**One thing it found that was deliberately not changed:** an *exchange* that changes the price (the screen says "collect
$33.60") is not a till entry, so Cash Up and the card check are off by that amount that day. The screen now says so and tells
staff to use **Return items** and ring the new item up as a separate sale instead, which the till does see. An exchange at the
same price is unaffected.

## Known limits (honest list)

* **Real-phone camera**: see the box at the top. Use the self-test on your own phones.
* A quote can only be answered once. If a customer approves by mistake, record the correction on the ticket (and send a new
  quote if the price changed) rather than trying to reuse the link.
* The quote text is plain SMS (no tracking of whether it was delivered beyond Twilio's own answer).
* A part-return whose refund method is *card* only records that the money should go back on the terminal; **you still
  press the refund on the card machine**, exactly as with full refunds.
* An expense cannot be edited, only voided and re-entered (the audit trail stays clean).
* Receipts for expenses are a receipt *number* only; photos of receipts are not stored.

## Testing

* **Automated suite (after the v52.2 fixes):** 1,556 tests pass on PostgreSQL; 1,555 pass on SQLite with 1 skipped (a
  PostgreSQL-only test). The tests for v52 and v52.2 are in `tests/test_v52_*.py`.
* **Whole-build run in a real browser at phone size (390x844), CSRF on, a brand-new database:** 1,779 checks, all passing, in
  eleven sections (set-up and people, sales, invoices and money, repairs, trade-ins / purchase orders / stock counts, expenses
  and Cash Up, reports, roles and security, settings and backups, a bad-numbers sweep, automatic texts and the remaining
  screens). It also tracks a physical till and compares it with Cash Up after each step.
* **Upgrade from v51 (tested on both databases, earlier in v52):** a v51 database with real-looking data opens with v52 with
  nothing lost, the old PIN works, and a v51-style backup restores.
* **Not tested, because it cannot be from here: a real lens on a real phone.** The scanner test page (Help -> Test the scanner
  on this phone) checks your own phone's camera. Texts were tested up to the point of sending; no real text service was
  connected, so a real text delivery was not tried.
