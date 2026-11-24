# v54 — Repair price list, branded customer pages, About & health

Builds on v53.1. Three additions, all owner-controlled. **Nothing is switched on that changes money or stock by itself.**

**Read this first: what I could and could not check.** Every new screen was driven in a real browser at phone size
(390 × 844) with the app's real security headers and CSRF checks, in both dark and light themes, against a real v53-era
database file and against PostgreSQL. I have *not* seen it on a physical iPhone or Android. The prices, hours, address and
phone number in my test shop are sample data, not yours. Customer photos are **off** until you switch them on.

## How to upgrade

1. Download a backup first (Settings → Data → Download Full Backup).
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`).
3. Start the app. Two new tables are created automatically (`repair_prices`, `shop_logo`); no existing data is touched and
   no new packages are needed. If a phone shows the old look, close and reopen the app once (stylesheet is `?v=54`).
4. Open **More → About & health**. It should say *"All 32 tables and their columns are in place"*.

## What changed

### 1. Repair price list (owner edits it, everyone benefits)
* New screen **Repair prices** (button on the Repairs page; also `/repair-prices`). One line = **phone + repair → price,
  usual part (optional), minutes (optional)**. Prices are *before tax*, like every other price in the app.
* **Only the owner can add, change, remove or import** prices. Managers can look. Every change is in the audit log
  (before and after price).
* **Same price on every phone** line for repairs that do not depend on the model (for example a free diagnostic).
  A price for a specific phone always beats the every-phone line; "iPhone 15 Pro" never falls back to "iPhone 15", and
  "S24+" is not "S24".
* **New ticket:** pick or type the phone and your price list appears as buttons. Tap one and the **Issue** and **Estimate**
  fill in. Both stay editable; the ticket keeps what you type, not the list price.
* **Ticket page:** shows the price-list line and the usual part with **+ Add this part** (one tap, takes one from stock,
  exactly like the existing Add Part). Making a ticket never touches stock; only that tap does.
* **CSV export / import** (Phone model, Repair, Price, Part SKU, Minutes). Bad rows are skipped and listed by row number;
  good rows still go in. Exports are safe to open in Excel (formula characters are neutralised).
* Technicians and cashiers still get the price buttons on a new ticket, but cannot open or edit the list.

### 2. Branded customer pages
* New screen **Settings → Customer pages** (`/branding`): upload your **logo** (PNG, JPEG or WebP, up to 300 KB), choose a
  **colour**, type your **opening hours**. The shop name, address and phone come from your existing Settings.
* The **status page** (`/s/<private link>` and the ticket-number + last-4 lookup) and the **quote page** now show your logo,
  colour, hours, **Call** and **Text the shop** buttons, and a **five-step progress bar**
  (Received → Checked → Repairing → Ready → Picked up).
* **Preview** buttons open every view (received, in progress, waiting for a part, ready, lookup, quote, approved quote) with
  made-up details so no real customer is ever shown.
* **Drop-off photos on the customer's page are OFF by default.** If you switch them on, a customer sees the front, back,
  damage and after-repair photos **only on their own private link**: never on the ticket-number lookup, never the IMEI-sticker
  photo, and the link stops working the moment you switch it off.
* Logo safety: checked by file signature (no SVG, no scripts), size and pixel limits, served with a locked-down security
  policy. A hand-edited bad colour falls back to the default yellow. The logo travels inside your backups.
* Still the old look: the **staff** screens and the login page keep the TechPro+ logo (a separate change if you want it).

### 3. About & health (owner and manager)
* **More → About & health**: version, database type and structure check (every table and column present), what is stored,
  last backup (and how old), weekly emailed backup, backup password, texting, email, photo storage, customer pages,
  HTTPS, and whether the encryption key and sign-in secret are set. Each line says OK, "look at this" or fix-this, in plain words.
* **Back up now** (owner) downloads a backup file and the page then shows "today".
* **Copy support info** puts a plain-text report on the clipboard: counts and yes/no answers only, no customer names, phone
  numbers, passwords or keys.

## Small fixes found while testing this release
* The Brand / Model picker (new ticket, trade-ins, and the new price screen) could be wider than its card on a 390 px phone
  when a model name was long; it now stays inside the card.
* The **Remove** button in a ticket's *Parts Used* list was cut off on a phone; the table drops its *Unit cost* column on
  narrow screens so Remove is fully visible (desktop is unchanged).
* The shop phone number on the **Call** button no longer breaks across two lines.
* The full-size photo page for customers no longer makes Chrome log a blocked-style warning.

## Behaviour you may notice
* The backup file label now follows the version file (`TechPro+ CRM v54`). Backups from older versions still restore.
* One existing test line was updated for that label (it compared against a fixed "v52" text).

## Testing done
* **Automated, final code:** the whole suite on **SQLite: 1,732 passed, 1 skipped**, and on **PostgreSQL 16: 1,733 passed**.
  131 of those are new for this release (price list 51, customer pages 51, About / upgrade 29).
* **Real browser, phone size (390 × 844), real CSRF and CSP, whole app end to end: 1,909 checks, 0 failed**, including a new
  section for this release (130 checks): price list add / edit / search / remove / CSV; price buttons on a new ticket; typed
  phone names; stock untouched until "+ Add this part"; logo / colour / hours / photos switch; a customer's phone that is *not
  signed in* opening the status, lookup and quote pages in dark and light; the About page and its clipboard copy; role checks.
  I also looked at the phone screenshots in dark and light; that is how the four small layout fixes above were found.
* **Two real bugs were found by the browser run and fixed:** the price screen's Brand / Model picker was missing its script,
  and the customer photo page's style policy made Chrome log a warning.
* **Upgrade probe:** a database file made by an older version, opened by v54, gets its two new tables and passes the structure
  check; a v53-style backup (without the new tables) restores; a v54 backup restores prices and logo.
* **Two existing tests were adjusted, nothing else:** one compared the backup label with a fixed "v52" text, another looked for
  the stylesheet number "?v=53"; and the old front-desk browser step now clicks the redesigned customer page's button.


---

# v54.1 — the camera scanner fix (barcode and IMEI)

**What was wrong.** The camera scanner could not read phone IMEI labels and was weak on ordinary barcodes in real conditions.
I reproduced it with recorded camera video of realistic labels: the old reader (ZXing, JavaScript) read **0 of 8** IMEI labels
whenever the label sat on anything darker than a white sheet (a phone box, a table, a hand) or was small in the picture. It
also only asked the camera for a modest picture size, and on Android phones that *say* they have a built-in reader but cannot
use it, the failure was silent, so nothing was ever read and nothing was ever said.

**What changed**
* **A much stronger reader.** The scan screen now reads the camera with **ZXing-C++ compiled to WebAssembly** (about 4 ms per
  picture). On the same recorded labels it reads **8 of 8** at 1080p. It is served from this site (`static/vendor/zxing-wasm/`,
  MIT / Apache-2.0, licence file included); nothing comes from another website and no picture leaves the phone.
* **Asks the camera for a sharper picture** (up to 1920 × 1080), switches on continuous autofocus where the phone allows it,
  looks at the part of the picture inside the on-screen frame first (then the whole picture every few looks), and shows a
  **Zoom 2×** button where the phone supports zoom, next to **Light**.
* **"Take a photo instead"** on the scan screen: the phone's own camera app takes a full-resolution, autofocused photo of the
  label and it is read from that. This is the most reliable route for a tiny IMEI label. A photo with no barcode says so and
  keeps the screen open; "Use the live camera" goes back.
* **IMEI mode.** The IMEI scan buttons (new ticket, repair page, trade-in, register line) now prefer a valid 15-digit IMEI
  when a phone box shows several barcodes (IMEI, serial, model), instead of taking whichever it saw first.
* **Phones that cannot run it still work.** If a browser cannot run WebAssembly (an iPhone older than iOS 16.4, for example),
  the older JavaScript reader is used as before, and the Scanner test page says so in plain words. A built-in reader that
  keeps failing is given up on after a few tries instead of silently blocking everything.
* **Security policy:** the content-security policy gained `'wasm-unsafe-eval'` (needed for any WebAssembly) and nothing else.
  `'unsafe-eval'` is still not allowed, and nothing from other sites is allowed. A test locks that.
* The Scanner test page checks the new reader; Help has a short tip on scanning small IMEI labels.

**Still only formats with a check digit** (EAN-13, EAN-8, UPC-A, UPC-E, Code 128), as before, so a misread cannot slip in.

## Testing done for v54.1
* **Automated, final code:** the whole suite on **SQLite: 1,750 passed, 1 skipped**, and on **PostgreSQL 16: 1,751 passed**.
  18 of those are new (`test_v54_1_scanner.py`); three existing assertions were updated (the security-policy check, the
  reader names, the script version number).
* **Real browser, phone size, real CSP:** a new section (58 checks) plays recorded camera video through the real scan screen:
  large shop barcodes on the register and Scanner test page; IMEI labels (dark and light backgrounds, large and small) on a
  new ticket, the repair page and a trade-in; **8 of 8** labels read; the photo route (read, no-barcode photo, back to live);
  typing the number; WebAssembly blocked (falls back to the old reader and still reads a large barcode); camera refused. No
  security-policy violations in any of it. Whole app end to end (sections 1 to 12, 1,909 checks) still passes unchanged.

## What I could not test (honest limits)
* **A real phone.** A recorded video is not a lens: real autofocus, glare, hand shake, the torch and zoom buttons, the Android
  built-in reader, and iPhone Safari's WebAssembly support are not exercised here. Please try it on your phone and, if
  anything still fails, open **Help → Test the scanner on this phone** and send me what it shows.
* The first scan on a phone downloads about 950 KB (the reader); after that it is cached.
