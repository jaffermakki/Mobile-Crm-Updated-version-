# v50 — Camera barcode scanning and stock-count mode

Builds on v49 (repair check-in and photos). Two things that belong together: the phone's camera becomes a **barcode
scanner** for the register, the product pages and stock-taking, and there is a new **Stock Count** screen for walking a
shelf, scanning what is really there, and fixing the system numbers once you have reviewed the differences.

Testing: the full suite passed on PostgreSQL 16 (1,054 tests) and on SQLite (1,053 pass; the 1 skipped is the
PostgreSQL-only foreign-key check). That is the v49 suite plus 90 new tests (`tests/test_v50_scan.py`) for barcodes,
the register, product pages, CSV, linking, the count arithmetic (including sales rung up mid-count, two phones counting at
once, deleted products, a 205-product catalogue), permissions, CSV safety, backup/restore and the database upgrade.
An upgrade from a v49 database was checked on SQLite and PostgreSQL (nothing lost, old products get an empty barcode and
an index, the register, scanning, counting and backup/restore all work on the old data). The new screens were driven in a
real browser (phone-size screen, CSRF on, real security headers) with a **fake camera playing a video of three printed-style
barcodes**: the bundled reader read them, a barcode spelled differently by the scanner still found its product, an
unknown barcode was linked to a product from inside the scanner, the camera was released on close, and there were no
console or security-policy errors.

## How to upgrade

1. **Download a backup first** (Settings → Data → Download Full Backup).
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`).
3. Start the app. One new column (`products.barcode`, indexed) and two new tables (`stock_counts`, `stock_count_lines`)
   are added automatically. Existing products simply start with an empty barcode; nothing else changes.
4. No new packages and no new environment variables. **The camera only works over HTTPS** (Railway is; a shop PC at
   `http://192.168…` is not, see "Things to know").

## What changed

### 1. Manufacturer barcodes on products
* A product can now carry the **UPC/EAN printed on the maker's box** (Products → Add, or the product's Edit page).
  Your own shelf labels still print the **SKU**, as before; the two work side by side.
* One number, any spelling: a 12-digit UPC-A, the same number as a 13-digit EAN-13 (leading 0) and as a 14-digit GTIN
  are treated as the same barcode, because different phones and scanners report it differently.
* **A barcode can only be on one product.** Saving a barcode that another product already has (or that equals another
  product's SKU) is refused with the name of the product that has it, so a scan never has to guess.
* A barcode with a wrong check digit is saved but flagged ("check the number was read correctly").
* Search on the Products page also finds by barcode. The CSV export has a **Barcode** column; the CSV import reads it
  (optional last column). Imported barcodes that are already used, or that a spreadsheet ruined (`4.00638E+12`), are left
  blank and counted in the message, while the product itself is still added.

### 2. Camera scanner (register, Products, Stock Count)
* **Register:** a camera button next to the scan box. One read fills the box and goes through exactly the same lookup as
  a typed or USB-scanned code (price check first, as before). It now finds products by SKU *or* barcode.
* **Unknown barcode?** The owner/manager is offered **Link this barcode to a product…**: search by name, tap the product,
  done; the next scan finds it. Cashiers and technicians see a message that a manager must link it.
* **Products page / Edit page:** scan buttons that fill the barcode field (or the search box).
* The scanner uses the phone's **built-in reader** where there is one (Chrome on Android) and a bundled open-source
  reader (ZXing, Apache 2.0) everywhere else, including iPhone. **Everything is served from your own app; nothing is
  loaded from another website, and no picture leaves the phone.**
* Reads only the formats with a built-in check digit (EAN-13, EAN-8, UPC-A, UPC-E, Code 128). That is what retail
  packaging uses, and it avoids the false reads Code 39 and similar produce.
* Same code held in front of the camera counts **once** (a short cooldown); beep and vibration on a read; a light
  button where the phone allows it; **a box to type the number** is always there as a fallback.
* If the camera is blocked, missing or busy, the scanner says so in plain words and the typing box still works. The
  camera stops when you close the scanner, switch apps or lock the phone.

### 3. Stock Count (sidebar → Inventory → Stock Count)
* **Start a count** (optionally limited to one category), then scan each unit: **one scan = one unit**. Or tap a
  product found by name when it has no barcode. + / − buttons and a typed quantity fix mistakes; Remove takes a product out.
* **It saves as you go.** Every scan is stored immediately; closing the browser or losing signal does not lose the count,
  and two phones can count the same shelf at once without losing scans.
* **Blind counting:** the counting screen never shows what the system expects, so people count what is there.
* **Nothing changes until it is approved.** *Review & finish* shows each product's counted number against the system's,
  the net units and (for managers) the value at cost, and lists the products that were **not** counted.
  Only the **Owner or a manager** can apply or cancel a count. Staff can count, and see "waiting for approval".
* **A sale rung up while you were counting is not undone.** Applying sets stock to the counted number **plus or minus
  whatever was sold or delivered since the product was counted** (new stock = current stock + (counted − what the system
  said at the moment of counting)). Stock never goes below zero.
* **Partial counts are fine:** products you did not scan are left exactly as they are. An optional tick on the review
  screen ("set the N uncounted products to 0") exists for when you really counted the whole category.
* Applied counts are kept as a record (who, when, products changed, net units, net value), included in the Full Backup,
  exportable as CSV (owner/manager), and written to the audit log. A count can be applied only once.

### 4. Register on a phone
* Below tablet width the register used to squeeze the product side to a sliver beside a 340 px cart, leaving no room for
  the scan box. On a phone it now stacks: scan box and camera button, categories, products, then the cart and payment.
  Tablets and desktops look exactly as before.

## Things to know (limits)
* **HTTPS is required for the camera** (a browser rule). Railway is HTTPS. A phone opening the app at
  `http://<shop-pc-ip>:8000` cannot start its camera; the scanner says so and offers the typing box. A USB or Bluetooth
  scanner that "types" into the box works on every screen as before.
* **Code 39, ITF, QR and DataMatrix are not read.** Most retail packaging (UPC/EAN) and your own SKU labels (Code 128)
  are fine. Labels printed with other formats can be typed or found by name.
* **Reading is a little slower on iPhone** than on Android Chrome (software reader versus the phone's own). Hold the
  barcode flat, well lit, about 15–25 cm away; use the light button in the dark.
* Counting adds to a product's baseline the **first** time it is scanned. If you change a product's stock by hand during
  a count, that change is treated like a sale or delivery (kept).
* A product deleted while a count is open is skipped when the count is applied (the count still shows it).
* A manufacturer barcode can be on only one product. Cases that come in many colours under **one package barcode** need
  the barcode on a single product (or counted by name for the others); the app refuses duplicates by design.
* Stock counts do not create purchase orders, returns or write-offs; they only correct the stock number.
* Camera scanning has not been tried on real phones in the shop; it was tested with a simulated camera in Chrome. Try a
  few real packages on the phones you will use before relying on it.

## Fixed along the way
* A product's barcode can be cleared from the Edit page (the first version of the field silently ignored an empty box).
* Old tests no longer pin the backup's version label to v49.

## Under the hood
* New: `app/barcodes.py` (code cleaning, UPC/EAN equivalence, check digit, lookup, duplicate check), `app/stockcount.py`
  (counting arithmetic, apply/cancel), `app/scan_routes.py` (pages and JSON), `static/scan.js` (scanner overlay),
  `static/stockcount.js`, `static/vendor/zxing-library-0.23.0.min.js` (+ licence and README),
  `templates/stock_count*.html`, `tests/test_v50_scan.py`.
* Database: `products.barcode` (text, indexed, `''` default); tables `stock_counts` and `stock_count_lines`
  (`stock_count_lines.product_id` is deliberately **not** a foreign key so deleting a product is never blocked by an old
  count; one line per product per count is enforced by a unique constraint).
* `migrate_schema()` now also creates the index for any newly added indexed column (older versions added the column only).
* `backup.py`: version label v50 and names for the two new tables; old backups without them restore unchanged.
* The existing security headers already allowed the camera for this site only (`camera=(self)`) and no `unsafe-eval`;
  nothing in the policy was loosened.
