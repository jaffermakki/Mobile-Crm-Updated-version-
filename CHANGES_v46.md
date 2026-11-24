# v46 — Automatic customer texts

Builds on v45 (reporting pack). Everything is additive and **off by default**: until the owner switches it on in
**Automatic texts → Settings**, the app behaves exactly like v45 and no customer is ever texted automatically.
The full suite (540 tests) passes on SQLite; on PostgreSQL 16, 539 pass and 1 is skipped (explained under "Known limits").
An upgrade from a v45 database was verified on both.

## How to upgrade

1. Back up your database (Settings → Backup, or a Postgres dump).
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`). No new packages.
3. Start the app. A new table (`outbound_messages`) and two customer columns (`sms_opt_out`, `sms_opt_out_at`) are added automatically.
4. To use it: **Settings → Notifications** must already have your Twilio details (the same ones the manual SMS buttons use).
5. **Two-way webhook (needed for STOP):** in the Twilio console, set your number's "A message comes in" webhook to the
   URL shown under Settings → Notifications ("Two-Way SMS Webhook"). Without it, Twilio still blocks texts to anyone who replies STOP, but the CRM only
   learns about it when a later text is rejected (Twilio error 21610), so staff may see avoidable failures and the manual SMS buttons stay enabled.
6. **Automatic texts → Settings**: read the four wordings, send yourself a test of each, then tick the master switch.

## What it does

| Text | When it is sent | Needs marketing consent? |
|---|---|---|
| Ready for pickup | 5 minutes after a ticket becomes Ready (the delay lets you undo a mis-click) | No (service message) |
| Pickup reminder | 3, 7 and 14 days after Ready (editable) until the device is collected | No, always ends "Reply STOP to opt out." |
| Review request | A few days after Collected, if you pasted a Google review link | **Yes** |
| Win-back (optional) | Good customers not seen for 120+ days; a few a day, once per 180 days each | **Yes** |

Safeguards (each one is covered by tests):

* **Re-checked at send time.** If the device was collected, the customer said STOP, the phone number was fixed or removed, or consent was withdrawn since the text was queued, it is skipped (with the reason shown).
* **Never twice.** A unique key per text plus an atomic claim means the same text cannot go out twice, even with two servers or a double-click. A text sent by hand with the existing "Send Ready-for-Pickup SMS" button stops the automatic one. A reminder is also skipped if the customer was texted about the same ticket in the last 36 hours.
* **Quiet hours** (default 9:00–20:00 shop time) and a **daily limit** (default 150). Hand-sent texts do not count against the limit.
* **Stale texts are dropped, not sent late** (ready text after 48 h, reminder after 72 h, review after 14 days, win-back after 7 days).
* **Retries:** temporary Twilio/network problems retry after 15 min, 1 h, 4 h, then show as Failed with a Retry button. Permanent errors (bad number) never retry. A wrong Twilio login or missing setup stops the run and leaves everything waiting, so nothing is lost.
* **Crash safety:** if the app stops mid-send, that text is marked Failed ("unknown whether it went out") instead of being re-sent.
* **STOP / START:** a reply of STOP, UNSUBSCRIBE, CANCEL, END, QUIT (or a short message starting with one) switches the customer to "do not text", cancels their queued texts and blocks the manual SMS and receipt-SMS buttons. START/YES/UNSTOP turns it back on. Staff can also toggle "do not text" on the customer page. Matching works across number formats ("+12505550101" finds "(250) 555-0101") and across duplicate customer records.
* **Turning it on is gentle.** Only tickets that become Ready after you switch on are texted. Phones already on the shelf get nothing unless the owner presses "Send one reminder to each".
* **A messaging problem can never block the workshop.** The status change is saved first; if queueing fails it is logged and ignored.

### Pages
* **Automatic texts** (owner/manager): upcoming, sent, failed/skipped, filter by type, cancel or retry a single text, "Send what is due now", status banners (off / Twilio not set up / outside hours / phones already waiting).
* **Messaging settings** (owner): master switch, the four wordings with live preview and text-part count, review link, win-back rules, hours, daily limit, test-send per wording. The form validates everything first and saves nothing if anything is wrong.
* **Repair ticket**: "Automatic texts" panel with what was sent / is queued; warning when the customer opted out.
* **Customer page**: "Do not text" badge and toggle.
* **Report Center → Customer Messaging**: sent / failed / skipped / cancelled, by type, day by day, why texts did not go out, failures to fix, new STOP replies, and how many reminded phones were collected. Print, CSV and Excel like the other reports.
* **Daily digest email** gains one line (texts sent today; failures in the last 7 days).

## Fixes made along the way

* **Phone numbers are converted to E.164 before sending** (customers are stored as typed: "(250) 555-0101", "250.555.0101", "1-250-…"). Numbers that cannot receive a text ("N/A", 7 digits, "ext 5") are refused with a clear message instead of being sent to Twilio. Applies to the existing manual SMS buttons too.
* **STOP replies were never obeyed before.** The inbound webhook only logged replies and matched customers by exact text.
* **Receipt, customer and ready-for-pickup SMS buttons now refuse customers who replied STOP.**
* **Backup restore** now converts `consent_date` / `sms_opt_out_at` back to dates and clears the text queue first.
* **PostgreSQL upgrade bug:** the automatic column migration wrote `BOOLEAN DEFAULT 0`, which PostgreSQL rejects, so any new true/false column would have crashed start-up on Railway. It now writes FALSE/TRUE there.

## Known limits (please read)

* **"Sent" means Twilio accepted the text.** Delivery receipts are not tracked, so a text to a phone that is off, or a landline, still counts as sent. The pages and the report say so.
* **Texting laws.** Ready-for-pickup texts are service messages. Reminders always carry an opt-out line. Review requests and win-back texts are promotional: they are only sent to customers ticked "Consents to marketing emails/SMS" and always carry "Reply STOP to opt out." That matches how CASL (Canada) is normally applied, but it is not legal advice; have the sender identification wording (your shop name, and your phone number via `{shop_phone}`) checked if you resell this. Customers' consent dates are stored but proof of *how* consent was obtained is up to your intake process. US numbers (TCPA) have stricter rules still.
* **Twilio account side:** Canadian carriers require a registered sender for business texting; unregistered numbers may be filtered regardless of what this app does.
* **Restore on PostgreSQL (existing, not changed):** `/import/backup` deletes all products and customers and re-inserts them. PostgreSQL refuses that once repair parts or invoices point at them, so a restore on Railway will fail with an error and change nothing. SQLite works. A safe fix is a merge-by-id restore; it needs a decision about what "restore" should mean, so it is left for a later version. The test for this is skipped on PostgreSQL for that reason.
* The background timer runs inside the web process (every 5 minutes; win-back queueing daily at 10:07 shop time). If Railway puts the app to sleep, texts go out when it wakes. The unique key makes it safe if more than one copy runs.

## Tests

`tests/test_messaging.py` (239 tests): phone normalising, wording safety, settings validation (all-or-nothing), queueing rules,
sending, quiet hours, daily limit, retries/backoff, crash recovery, concurrent claim, Twilio error handling (HTTP faked),
STOP/START, role gating on every route, pages and forms, report and exports, digest, scheduler jobs, backup round-trip and
the v45→v46 schema upgrade. `tests/test_reporting_pack.py` now also runs the new report through its page/print/CSV/Excel checks.
