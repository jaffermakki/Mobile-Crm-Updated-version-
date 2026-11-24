# v49 — Repair check-in and device photos

Builds on v48 (security and backups). One feature, done properly: when a phone is handed over the counter, the shop now
records **what condition it arrived in** and keeps **photos of it**, so a later "you cracked my screen" has an answer.

Testing: the full suite passed on PostgreSQL 16 (963 tests) and on SQLite (962 pass; the 1 skipped is the PostgreSQL-only
foreign-key check). That is the 895 from v48 plus 68 new ones for check-in, photos, zip export, backup/restore and the
print sheet; a 69th test (printing) was added afterwards and run on both databases together with the backup and print tests.
An upgrade from a v48 database was checked on SQLite and PostgreSQL (nothing lost, old tickets open, photos and
check-ins work on old tickets, restore handles photos). The new screens were driven in a real browser (phone-size screen,
CSRF on, real security headers): a 9 MB JPEG with GPS data is shrunk to ~1600 px, uploaded, stored without its EXIF,
viewed, deleted, printed on one A4 page and downloaded in the .zip, with no console or security-policy errors.

## How to upgrade

1. **Download a backup first** (Settings → Data → Download Full Backup).
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`).
3. Start the app. One new column (`repairs.intake_json`) and one new table (`repair_photos`) are added automatically.
   Existing tickets are untouched; they simply show "No check-in was recorded for this ticket."
4. No new packages and no new environment variables.

## What changed

### 1. Condition checklist at drop-off
* **New Ticket** has a *Check-in* section (tap to open; recommended, never required): nine yes/no/not-tested rows —
  powers on, screen and touch, back/frame/buttons, charging port, cameras, speaker/mic/earpiece, battery, SIM/Wi-Fi/signal,
  liquid damage — plus *Mark everything fine* for the common case.
* **Left with us:** case, charger/cable, SIM card, SD card, something else.
* **Passcode:** *Customer gave it / No passcode / Not given.* **The passcode itself is never typed in or stored.**
* **Find My iPhone / Google lock:** ON / Off / Don't know. If it is **ON** the ticket shows a red warning (a locked device
  cannot be fully tested or restored).
* **Data warning:** a tick that the customer was told repairs can erase data.
* **Notes:** one line for scratches and marks. A card number pasted here is cut down to its last four digits, like every
  other free-text box since v48.
* The same form is on every ticket (*Check-in* card → *Edit check-in*), so it can be filled in or corrected afterwards.
  Anyone signed in can record it. Each save is written to the audit log.

### 2. Photos
* On a ticket: **Take photo** (opens the back camera on a phone) or **Choose photos** (several at once). Pick what the
  photos show first: front, back, damage, IMEI/serial label, after repair, other.
* **The phone shrinks each picture before sending** (about 1600 px wide, ~200 KB instead of 4–8 MB) and makes a small
  preview. Re-drawing the picture also **drops its hidden data** — location and camera details do not reach the server.
* Up to **12 photos per ticket**, one at a time with a progress message. Tap a thumbnail for a full-screen viewer
  (caption shows label, time and who took it; *Open full size*).
* After you create a ticket you now land **on the ticket, at its photo area** ("Take photos of the device now, while the
  customer is here") instead of the ticket list.
* **Deleting a photo is for the Owner and Managers only**, and asks first. Everything is in the audit log.
* Photos are only served to people who are signed in, are never cached by shared caches, and are checked by their real
  bytes (JPEG, PNG or WebP, at most 6 MB, at most 8000 px a side) — a file merely *named* `.jpg` is refused.

### 3. Printable check-in sheet
*Print check-in sheet* on the ticket (or `/repairs/<id>/intake-sheet`): shop header, ticket number, customer, device, IMEI,
problem, estimate, the condition table, what was left, passcode / Find My status, up to six drop-off photos, terms, and
signature lines for the customer and the staff member. Black on white so any printer is fine, and tightened for paper so that even the fullest possible ticket (every row ticked, long notes, six photos, 1,200 characters of terms) still fits one A4 or Letter page.
* The **terms wording** at the bottom can be changed by the Owner in **Settings → Data → Repair photos & check-in sheet**
  (blank = the standard wording, up to 1,200 characters). It is general wording, not legal advice — have it checked.

### 4. Photos and backups
* Photos are **not** inside the Full Backup file (they would make it huge and too big to email). The checklists **are**
  (they are part of the ticket).
* Settings → Data → **Download all repair photos (.zip)** (Owner only): every picture, in a folder per ticket, plus an
  `index.csv` listing ticket, device, customer, label, time and who took it. Names that start with `=`, `+`, `-` or `@`
  are neutralised so a spreadsheet cannot run them.
* **Restoring a backup keeps the photos of tickets that are in the file and deletes the photos of tickets that are not**
  (they could never be shown again). The restore screen tells you both numbers before you type RESTORE.

## Things to know (limits)
* **Photos live in the database**, about 200 KB each. 1,000 photos is roughly 200 MB. Watch your Postgres size on Railway
  and download the .zip now and then; it is your only copy besides the database itself.
* The photo .zip is **not** emailed automatically and is not part of the weekly backup email.
* Hidden-data stripping is done by the phone's browser. A hand-made upload straight to the server would keep whatever it
  contained; only signed-in staff can upload.
* Some Android browsers cannot read HEIC pictures. If one refuses a photo, take it with the camera button or choose a JPEG.
* Photos are not shown to the customer on the status page, and are not sent by text or email.
* The 12-photo and 6 MB limits are per ticket and per picture. Photos are removed only by the delete button or a restore
  as described above — closing or collecting a ticket does not remove them.
* Check-in and photos are optional. A ticket created without them works exactly as before.

## Fixed along the way
* **Printed pages no longer carry the "Your PIN is easy to guess" banner** (v48 added it to every page, so it printed on
  receipts, invoices and labels for anyone with a weak PIN), and a phone using the dark theme no longer prints a dark
  page margin.

## Under the hood
* New: `app/intake.py` (checklist rules, picture checking), `app/intake_routes.py` (pages and downloads),
  `static/intake.js` (shrinking, upload, viewer), `templates/partials/intake_fields.html`,
  `templates/repair_intake_sheet.html`.
* Database: `repairs.intake_json` (text) and table `repair_photos`. `repair_photos.repair_id` is deliberately **not** a
  foreign key, so a restore (which replaces the tickets table) is never refused by PostgreSQL because of a photo.
* `backup.py`: `repair_photos` is excluded from the file; `restore_full` removes orphaned photos inside the same
  transaction; the preview shows what happens to photos.
