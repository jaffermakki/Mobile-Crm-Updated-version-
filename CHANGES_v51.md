# v51 — The missing pieces: IMEI scanning, model lists, receiving, loss reasons, first-run setup, staff guide

Builds on v50 (camera barcode scanning and stock counts). This release closes the gaps found in the v50 review and adds
what is needed to run a **second shop (Red Deer, AB)** or hand a copy to a buyer.

Testing: the full suite passed on PostgreSQL 16 (1,209 tests) and on SQLite (1,208 pass; the 1 skipped is the
PostgreSQL-only foreign-key check). That is the v50 suite plus 155 new tests (`tests/test_v51_*.py`) for first-run setup,
the phone lists, IMEI cleaning and checking, scan-receiving, count reasons and the loss report, the Help page, permissions
and the database upgrade. An upgrade from a v50 database was checked on SQLite and PostgreSQL (nothing lost, the old
owner PIN still works, the shop is not sent to setup, both new columns appear, and the new features work on the old
data). The new screens were driven in a real browser (phone-size 390x844 screen, CSRF on, real security headers, a
brand-new database) with a **fake camera**: setup, Help, the pickers, IMEI scanning on the ticket / trade-in / register,
scan-receiving a short delivery, count reasons and the loss report, with no sideways page scrolling, no
content-security-policy violations and no console errors.

## How to upgrade

1. **Download a backup first** (Settings → Data → Download Full Backup).
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`).
3. Start the app. Two columns are added automatically (`stock_count_lines.reason`, `stock_count_lines.unit_cost`).
   Nothing else in your data changes. **Your existing shop is never sent to the new setup page** (it already has staff).
4. No new packages. One optional new variable, `SETUP_CODE`, only matters for a brand-new database (see 6).

## What changed

### 1. Scan the IMEI instead of typing it
* A **scan button** beside the IMEI box on the **new repair ticket**, the **ticket page**, the **trade-in form** and on
  each **line of the register cart** (a phone sold with its IMEI).
* The number is cleaned (spaces and dashes removed). A 15-digit IMEI with a bad check digit is **saved but flagged**
  ("check the number was read correctly"), the same way bad barcodes are.

### 2. Pick the phone from a list (iPhone, Samsung and more)
* On the repair ticket and the trade-in form: **Brand → Model** drop-downs. **Apple**: every iPhone from the iPhone 4
  up to the 2026 models (SE, mini, Plus, Pro, Pro Max, Air, "e" models), in families, newest first. **Samsung**: Galaxy S
  (S7 to S26 incl. Ultra, Plus, Edge, FE), Z Fold and Z Flip, Note, A series (current and older) and XCover. **Google
  Pixel** and **Motorola** use the shorter lists the app already had; anything else is typed.
* Pick a brand, then a model; **Not in the list — type it** (or brand "Other") lets you type anything, so an odd phone
  is never blocked. Storage and colour can be added after a picked model ("iPhone 15 Pro, 256GB, Blue").
* Models you add on the Bulk-Create Variants page also appear in the list ("Added by you").
* The Galaxy A list is best-effort for the oldest models; if one is missing, type it (or add it once on Bulk-Create
  Variants). Tablets and the Galaxy M series are not in the list.

### 3. Scan items as they arrive on a purchase order
* On a **sent** PO: **Scan items as they arrive**. Every line starts at 0 and counts up with each scan; an unknown
  barcode can be linked to a product from inside the scanner. Short deliveries are normal: the counted quantities are
  what is added to stock, nothing is added until you press **Confirm Receipt — Add to Stock**.
* Phone layout fixed so the received-quantity box is always visible.

### 4. Why was stock lost? (count reasons and a report)
* On the count review screen each product that is off gets a **Why?** choice. Losses offer: damaged or defective, lost or
  stolen, sold or used but not rung in, supplier delivered less than invoiced. Gains offer: delivery not entered,
  customer return not entered. Both offer "counting or entry mistake" and "other". **Save reasons for later** keeps them
  without updating stock. After the count is applied the reason is shown under each product.
* The cost per unit **at the time of the count** is stored, so the loss is valued correctly later, even if the cost
  changes.
* New report **Stock Counts & Losses** (Report Center): value lost or gained by reason, by product and by count, with
  CSV, Excel and print.

### 5. Barcodes in the product list
* The Products page shows each product's barcode (and a scan-to-find button), so you can see at a glance which products
  still have none.

### 6. First-run setup (for Red Deer or a buyer)
* A **brand-new** database no longer starts with PIN 1234. It is locked on `/setup` until the owner enters a one-time
  **setup code** (printed in the server log, or your own `SETUP_CODE` variable), the shop name, the province (sets the
  tax: Alberta is 5% GST) and **their own 6–8 digit PIN** (obvious PINs refused). Sample data is removed by default.
* The setup code is never printed when you choose your own `SETUP_CODE`, and it is deleted once setup is done.
* Existing shops are never affected; restoring a backup onto a new server brings that shop's own staff with it.

### 7. A staff guide inside the app
* **Help** link at the top of every page (`/help`): how to sell, take in a repair, do a trade-in, scan, count stock,
  receive a delivery (managers only), close the till and stay safe. Cashiers do not see manager-only sections.

## Things to know

* **Phone camera needs HTTPS** (Render/Railway provide it). On a shop PC at `http://192.168…` the scanner offers a box to
  type the number instead.
* The loss report's tables swipe sideways on a phone (the same as every report).
* Real-phone camera behaviour (iPhone Safari, Android Chrome) has been tested with a fake camera in a real browser, not
  on physical phones. Try the scanner on your own phone first.
* **Not in this release** (offered earlier as optional): expenses / till paid-outs, partial returns, quote approval by
  text message.
