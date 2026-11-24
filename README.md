# TechPro CRM — Python (FastAPI) Edition

A from-scratch Python rewrite of the single-file HTML/JS CRM, with real
server-side enforcement instead of client-side checks that could be
bypassed from the browser console. Feature-complete against the
original — see `FEATURE_PARITY.md` for the full checklist and the
handful of deliberate differences.

## What's different from the JS version (and why)

- **PIN check, lockout, and role permissions run on the server.** In the
  JS version these were all client-side JavaScript — anyone with devtools
  could bypass them. Here, the browser is just rendering HTML the server
  decided to send; there's no client-side logic to bypass.
- **No Firebase, no secret to leak.** Data lives in a local SQLite file
  (`crm.db`). Nothing about the database is ever exposed to the browser.
- **PINs are bcrypt-hashed**, never stored or transmitted in plaintext.
- **XSS is handled for you.** Jinja2 escapes all variables by default —
  no manual `escH()`/`escJS()` helpers needed anywhere.
- **Email/SMS send directly from the server.** The original needed a
  Cloudflare Worker proxy for Twilio because browsers can't safely hold
  that secret. Since this has a real backend, SMTP/Twilio credentials
  just live in Settings and the server calls those APIs directly.

## Setup

```bash
cd crm_python
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` (or `http://<shop-pc-ip>:8000` from
another device on the same WiFi).

First start: a brand-new database is locked on the **/setup** page. The server prints a one-time
**SETUP CODE** in its console/log (or use the code you put in the `SETUP_CODE` variable); enter it, then
your shop name, province and your own 6-8 digit PIN. There is **no default PIN**. (Tests and old
databases that already have staff are not affected.)

**What v54 added:** an owner-editable **repair price list** (phone + repair → price, usual part, minutes; tap a price on a new ticket to fill the estimate, one-tap "Add this part" on the ticket), **branded customer pages** (your logo, colour, hours, Call / Text the shop, a five-step progress bar on the status and quote pages; drop-off photos optional and off by default), and an **About & health** page (version, database structure check, backup age, texting / email / photo storage, Back up now, Copy support info). Two new tables, no new packages. See `CHANGES_v54.md`.

**v54.1 (scanner fix):** the camera scanner now reads with ZXing-C++ in WebAssembly (served from this site), asks the camera for a sharper picture, has Zoom and a "Take a photo instead" route, and prefers a valid IMEI on IMEI scan buttons; the older JavaScript reader remains as a fallback. See the v54.1 section of `CHANGES_v54.md`.

**What v53 added (presentation only, no data changes):** a phone-first polish pass: a floating **cart bar** on the Sale screen,
a **header card + status stepper + pinned action bar** on repair tickets, a compact **customer profile with tabs and quick
actions**, **card lists instead of clipped tables** on phones, **toasts / dismissible messages / busy buttons**, and a
**"Needs attention" strip and readable activity feed** on the dashboard, and the repair ticket's **Add Part** list now shows
repair parts only (screens, batteries, parts; the ones that fit the phone first). See `CHANGES_v53.md`.

**What v52 added:** cash taken out of the till (**Expenses**, which Cash Up now subtracts), part-returns on an invoice
(**Return items**), repair quote approval by text link, the newer phone and tablet lists (Pixel 10/11, iPad, Galaxy Tab,
Galaxy M), and a **/scanner-test** page for trying the camera on your own phone. See `CHANGES_v52.md`.

**Camera barcode scanning (v50) needs HTTPS.** A phone opening
`http://<shop-pc-ip>:8000` will refuse to start its camera (a browser rule, not
an app setting); the scanner then offers a box to type the number. On Railway
(HTTPS) it works as is, and `http://localhost` works for testing.

## Setting up Email & SMS (optional)

Both are optional — invoices/repairs work fine without them, the
Email/SMS buttons just show "not configured" until you add credentials
in **Settings**.

**Email (SMTP):** works with Gmail (use an [App Password](https://myaccount.google.com/apppasswords),
not your real password), or any SMTP provider (SendGrid, Mailgun, your
domain host, etc.). Host/port/username/password/from-address.

**SMS (Twilio):** sign up at twilio.com, get a free trial number, and
grab your Account SID + Auth Token from the console. Paste those plus
the Twilio phone number into Settings.

**Daily Owner Digest:** once SMTP is set up, enable it in Settings →
Daily Owner Digest, set who it goes to and what hour (server-local
time) it sends. There's a "Send Test Digest Now" button so you don't
have to wait until that hour to check it works. Runs via APScheduler,
which starts automatically with the app — no separate cron job needed.

## Project layout

```
app/
  database.py      — SQLAlchemy engine/session setup
  models.py         — ORM models (Staff, Product, Customer, Invoice, Repair, ...)
  auth.py           — PIN hashing, lockout state, role checks
  tax.py            — Canadian sales tax calculator (by province)
  repairs_const.py  — repair status pipeline definitions
  notifications.py  — SMTP email + Twilio SMS sending
  seed.py           — creates tables + seeds an Owner account and sample products
  main.py           — all routes
  returns.py / returns_routes.py   — part-returns (v52)
  expenses.py / expenses_routes.py — cash out of the till and expenses (v52)
  quotes.py / quote_routes.py      — repair quote approval by text link (v52)
  devices.py        — phone / tablet model lists for the pickers
templates/          — Jinja2 HTML templates (autoescaped by default)
static/style.css    — dark theme matching the original CRM's look
```

## Feature coverage

See `FEATURE_PARITY.md` for the full section-by-section checklist
against the original HTML/JS CRM, including a few small, real gaps
(CSV *import* for inventory, invoice-specific SMS, customer notes
editing) and a short list of deliberate differences worth knowing about
(e.g. what "wipe all data" does and doesn't touch).

## Migrating your existing data

Once you're happy with this, export your current CRM's Firebase data as
JSON and I can write a one-off script that loops through it and inserts
matching rows via these same SQLAlchemy models.

## Notes

- The session secret is generated once and saved to `.session_secret`
  (gitignored) so restarting the server doesn't log everyone out. Set
  the `SESSION_SECRET` environment variable instead if you deploy this
  somewhere with an ephemeral filesystem.
- `crm.db` is your actual data — back it up the same way you'd back up
  any file, or use Settings → Data Export → Download Full Backup.
