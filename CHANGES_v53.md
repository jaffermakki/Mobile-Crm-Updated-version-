# v53 — Mobile Polish (+ repair parts picker)

Builds on v52.2. **This release is presentation only.** No database changes, no new packages, no new settings, no
changes to prices, tax, payments, permissions or any route's behaviour. It changes how the app *looks and feels on a
phone*, which is where you and your staff use it.

**Read this first: what I could and could not check.** Every screen below was driven in a real browser at phone size
(390 × 844) with the app's real security headers and CSRF checks, in both dark and light themes. I have *not* seen it on a
physical iPhone or Android. Two things only a real device can confirm: (1) the buttons that float above the bottom tab bar
sit correctly with your phone's own "home bar" / gesture area, and (2) the small vibration on "item added" (iPhones do not
vibrate from a web page; Androids do).

## How to upgrade

1. Download a backup first (Settings → Data → Download Full Backup) as you always should.
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`).
3. Start the app. Nothing in your data changes. If a phone still shows the old look, close and reopen the app once (the
   stylesheet is versioned `?v=53`, so it refreshes by itself).

## What changed

### 1. Sale screen (POS)
* **Cart bar.** Once something is in the cart, a bar floats above the bottom tabs showing **item count and total** with a
  **Checkout** button. Tapping it scrolls to the payment section; it hides itself while you are already looking at it.
  It is only a shortcut: the real **Charge** button, and what it charges, are unchanged (a test checks the bar's total
  always equals the server's total).
* **One-line category chips.** The three filter rows (category, brand, etc.) are single swipeable lines instead of
  wrapping into a wall of buttons, so the product grid is visible without scrolling past them.
* **"Added: iPhone 15 Screen"** toast and a light buzz each time something goes into the cart.

### 2. Repair ticket
* New **ticket header card**: ticket number, device, issue, status, the customer (initial avatar, **tap-to-call and
  tap-to-text**), and four fact tiles: **Estimate / Final, Promised (with "· late" in red when overdue), Technician,
  Warranty**.
* **Status stepper**: all seven stages (Received → … → Collected) in one row with ticks for what is done and the current
  stage highlighted. Fits a 390 px phone with no sideways scrolling.
* **Action bar pinned above the tab bar** on phones: the next step is always one tap away however far you have scrolled.
  On a Ready ticket it holds **Charge & Collect**, **Move to next status**, a **Note** button and **Notify**
  (the Ready-for-Pickup text). On a computer it stays in the page exactly as before.

### 3. Customer page
* Rebuilt as a compact profile: initial avatar, name, phone/email, badges (**Marketing OK / Do not text, open repairs**),
  **Call / Text / New repair / New sale** buttons (New repair opens the repair form with the name and phone already
  filled in; New sale starts a sale with this customer attached), and a **Spent / Points / Credit** strip.
* **Tabs** instead of one very long page: Repairs, Purchases, Layaways and Trade-ins (only if the customer has any),
  Messages (if there is a phone number) and Details (the edit form). Opens on Repairs, else Purchases. Links such as
  `/customers/ID#messages` open the right tab. Without JavaScript the page still shows every section.
* Incoming text messages now have a visible bubble (they were see-through before because of a mistyped colour name).

### 4. Cards instead of clipped tables on phones
Wide tables (Invoices, Customers, Repairs table view, the dashboard's Recent invoices and Low stock, and the history lists
on the customer page) are shown as **one card per row** on phones: main fact top-left, amount top-right, labelled details
underneath, action buttons at the bottom. Desktop and tablet keep the normal table, unchanged. Long or hostile names wrap
instead of pushing the layout wide.

### 5. Feedback
* **Toasts** for quick confirmations, and the old green/amber/red flash boxes now have an icon and a **dismiss ✕**.
  They stay until dismissed, so a message is not missed while you are looking at the screen.
* **Busy buttons.** Any normal form button shows a spinner and ignores a second tap while it is working, so a double tap
  cannot post twice. It frees itself after 8 seconds, or when you come back with the Back button.

### 6. Dashboard
* **"Needs attention" strip** at the top: ready for pickup, past the promised date, customer quote answers waiting, and
  layaways past due, each with a count and a link to the right list. When nothing needs attention it shows an
  "all clear" box instead of an empty gap.
* **Readable activity feed**: "**Mohammed** charged ticket #1003" with an icon, instead of internal codes
  (`REPAIR_CHARGE`). Unknown codes fall back to a tidied version of the code, so nothing is ever blank.

### 7. Repair ticket: Add Part shows repair parts only (added after the first v53 build)
* The **Parts Used → Product** dropdown no longer lists the whole catalogue. It shows only **Screens, Batteries and Parts**
  (the three repair categories), grouped under those headings. Cases, chargers, cables, screen protectors and other
  accessories are hidden.
* **Parts that fit the phone on the ticket come first**, under "Fits this phone — iPhone 15". The match is strict, so a part
  for an iPhone 15 is *not* offered as a fit for an iPhone 15 Pro, and a Galaxy S24 part is not offered for a Galaxy S24
  Ultra. If nothing matches, the normal groups are shown.
* A line under the form says how many products are hidden and has a **Show all products** link for the rare job that needs
  one (a charger or case sold with the repair). **Show repair parts only** takes you back.
* If no product has the category Screen, Battery or Part yet, the page says so and tells you how to fix it.
* To appear in this list a product must have the category **Screen, Battery or Part** (Products → edit). A part saved with
  no category, or as "Accessory", is under "Show all products".
* Only the dropdown changed. The button that adds a part still accepts any product and still deducts stock exactly as before.

## Quality checks

| Check | Result |
|---|---|
| Automated tests, SQLite (whole suite) | **1601 passed**, 1 skipped (1555 existing + 22 for the mobile polish + 24 for the parts picker) |
| Automated tests, PostgreSQL | whole suite **1578 passed** on the mobile-polish build; after the parts picker was added, the new tests plus the inventory and repair-parts suites were re-run on PostgreSQL: all pass |
| Real-browser end-to-end (phone viewport, real CSP/CSRF, 11 sections) | **1780 passed, 0 failed** (v52.2 had 1779) |
| Screenshots reviewed by eye (dark and light) | POS, ticket (Ready), customer (tabs), dashboard, invoices, repairs table, customers |

One honest note: when the browser run shared a 2-CPU machine with both full test suites, one sign-in took longer than the
harness's 8-second wait and that section stopped. The same run on its own passed everything, so it was a slow machine,
not a bug in the app.

Two layout bugs were found by those screenshots and fixed before release: the Ready-ticket action bar ran past the right
edge of the screen, and the stepper broke "Diagnosed" and "Completed" mid-word.

## Notes for testers / developers

* New tests: `tests/test_v53_mobile_polish.py` (22) and `tests/test_v53_repair_parts.py` (24). They check, among other things, that each ticket state shows exactly
  one Charge, Move and Notify form, that hostile customer names are escaped everywhere new, that the page renders for
  every role, and that the login page does not load the logged-in scripts.
* Browser harness (`e2e/`): on phones the ticket's "Optional note" box is now behind the **Note** button, so the helper
  `fill_ticket_note()` taps it first; the customer edit form is under the **Details** tab.
* Every flash message now carries a `data-flash` attribute (25 templates) which the new dismiss/icon script hooks into.
* The 150 px spacer at the bottom of a ticket on phones is what stops the pinned action bar covering the last card.

## Still on the list (not in v53)

Push notifications for "customer answered your quote", a branded customer status page, device-model icons and
swipe-to-act on list rows, and a full accessibility pass (screen-reader labels exist on the new controls; the whole app
has not been audited).
