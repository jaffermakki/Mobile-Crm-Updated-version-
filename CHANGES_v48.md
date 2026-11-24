# v48 — Security and backup pack

Builds on v47 (Moneris matching). Two things in this release: **harder to break into**, and **backups you can trust**
(complete, optionally password-locked, and restorable on PostgreSQL, which is what the live shop runs on).

Testing: 895 tests pass on PostgreSQL 16, and 894 pass on SQLite (the one other test is the PostgreSQL-only foreign-key check, which SQLite cannot run). That is the 706 from v47 plus 189 new ones for sign-in, 2-step and backups.
An upgrade from a v47 database was checked on SQLite and PostgreSQL, and the new screens were driven in a real browser
(phone-size screen, CSRF on, the real security headers) to make sure the new headers do not break any page.

## How to upgrade

1. **Download a backup first** (Settings → Data → Download Full Backup). Keep it somewhere safe.
2. Replace the app files with this folder (keep your `crm.db` / `DATABASE_URL`).
3. Install the new package: `pip install -r requirements.txt` (adds `segno`, which draws the 2-step QR code; Railway does this on deploy).
4. Start the app. New columns and one new table (`login_attempts`) are added automatically. Nobody's PIN changes and nobody is signed out except by the rules below.
5. **Do these three things once** (about five minutes): turn on 2-step for the Owner account (Security page), set a backup password (Settings → Data), and download a fresh backup.

## What changed

### 1. Wrong-PIN protection (every device counts separately)
* After **5 wrong PINs** from one device the sign-in pauses for **60 seconds**; each further block of 5 doubles the wait, up to **30 minutes**. A correct sign-in resets it. Tries made while it is paused do not make it longer.
* The pause shows a countdown on the sign-in screen and the keypad is switched off while it runs.
* A shop-wide backstop: if **40** wrong tries arrive from all devices within 10 minutes (someone spraying PINs from many places), devices the shop has not signed in from before are paused for 2 minutes. Your own phones and the shop computer carry on normally.
* Forgot-PIN, the 2-step code and recovery codes are covered by the same wait.
* Every wrong try is recorded (when, which device, which account if known) in a new log, trimmed to 6 months.

### 2. Two-step sign-in (authenticator app) — optional, per person
* Security page → *Set up 2-step sign-in*: scan the QR code (or type the key) into Google Authenticator, Microsoft Authenticator, 1Password, Authy or similar, then type the 6-digit code once to switch it on.
* From then on: PIN, then the 6-digit code. A code can be used **once** (a code typed in twice is refused), and a phone clock that is up to 30 seconds off is tolerated.
* You are given **8 one-time recovery codes** (shown once; only a fingerprint of each is kept). Each works once if the phone is lost or broken. You can make a new set (asks for your PIN; the old set stops working) or turn 2-step off (asks for your PIN **and** a code).
* The Owner can reset someone else's 2-step from Staff (for a lost phone).
* **Locked out of the owner account entirely** (lost phone and codes)? Set `RESET_2FA=1` in the server's variables and restart: 2-step is switched off for every account. **Remove the variable afterwards**, or it switches it off again at every restart. It is written to the audit log.
* The dashboard bell reminds the Owner to turn it on until they do.

### 3. Sessions
* A sign-in lasts at most **24 hours**, however busy; the default idle timeout is **60 minutes** (Security → Shop rules: 15 minutes to 4 hours, or never). With "never", a sign-in still ends after 12 hours unused.
* The background refresh that updates the bell does not count as activity, so a phone left open on a counter signs itself out.
* **Changing a PIN signs out that person's other devices.** Switching an account off signs it out immediately. A new **"Sign every other device out"** button (Security) does it for everyone except the device you are on. A restore (below) signs everyone out.
* Signing out says why ("You were signed out after 60 minutes without activity").

### 4. PINs
* PINs can now be **4 to 8 digits** (digits only). The keypad on the sign-in screen adapts to each person's length. The Owner sets the **shortest PIN allowed** for the shop (Security → Shop rules).
* Easy-to-guess PINs (1234, 0000, 1111, repeated or counting up) are **not forbidden**, because that could lock someone out of their own shop, but the person gets a yellow reminder to change it until they do.
* Existing 4-digit PINs keep working unchanged.

### 5. Customer repair-status page
* The public `/status` lookup (ticket number + last 4 digits of the phone) is now limited: **10 wrong lookups per device per 15 minutes**, then it waits. A wrong ticket and wrong digits give the **same** answer, so it cannot be used to find out which tickets exist.
* Each repair can have a **private link** (shown on the repair page, with a copy box) that opens that repair's status with no typing. The link is an unguessable code; it shows only the status, never the customer's name, phone, address or the price.

### 6. Browser security headers
Every page now sends headers that stop the CRM being embedded in another website (`frame-ancestors 'none'`), stop content-type sniffing, limit where scripts, images and form posts may go (the CRM itself only), keep the camera available only to the CRM (ready for the barcode scanner) and switch off microphone, location, payment and USB. Pages are marked *no-store* so a shared phone cannot show an old page after sign-out. On the hosted site `Strict-Transport-Security` is sent too.
The policy allows the pages' own inline scripts and styles, because the CRM is built that way; what it blocks is loading code from, or sending data to, anywhere else.

### 7. Card numbers cannot sit in free-text boxes
v47 already trimmed card numbers typed into the Moneris reference box. v48 applies the same check to the other free-text fields where a number could be pasted by mistake (customer notes, repair issue/description, refund reasons, exchange notes, cash-up notes, supplier / purchase order / trade-in / layaway notes). A real-looking card number is cut down to its last four digits (`************1111`) before it is saved, wherever it was typed. Long reference numbers, IMEIs and phone numbers are not touched.

### 8. Complete backups, optionally locked with a password
**What was wrong:** the old backup held only products, customers, repairs and the Moneris records. It did **not** hold invoices, sales lines, repair parts, layaways, purchase orders, suppliers, trade-ins, cash-ups, texts or the audit log, and restoring it **failed on PostgreSQL** because the database refused to delete rows that other rows still pointed to.

**Now:**
* The backup holds **every table** of the CRM (except the sign-in attempt log).
* **No backup password set:** a plain `.json` file. It leaves out PIN hashes, authenticator keys, recovery codes and the saved passwords / API keys (Brevo, SMTP, Twilio), so a copy that lands in the wrong inbox does not carry the keys to the shop.
* **Backup password set** (Settings → Data → Backup password, at least 8 characters): a locked `.tpbackup` file, encrypted (AES-256-GCM, key derived from your password with scrypt) and compressed. A locked backup **does** include PINs and the other secrets, so it can rebuild the shop on a new server from nothing. Without the password the file is unreadable, and any change to the file is detected.
* The same rule applies to **Download Full Backup** and the **weekly backup email** (the email says which kind it is). The email is not sent if the file is over **3.5 MB** (the email service refuses 4 MB); it records a visible failure instead of failing quietly, and you use Download instead.

### 9. Restore that is safe
Settings → Data → *Restore from Backup* is now two steps:
1. **Look at this backup** (choose the file, type its password if it is locked). Nothing changes. You see, for each kind of record, how many the file holds, how many the CRM holds now, and how many **would be deleted**.
2. **Type `RESTORE`** to confirm. The CRM is made to match the file exactly, **in one go: if anything is wrong, nothing changes at all** (the whole file is checked first, then applied in a single database transaction that is undone on any error).
* Afterwards **everyone is signed out** and signs in again, and queued automatic texts are cancelled so nobody gets texts about restored-over jobs.
* Without a locked file, current PINs and authenticator settings are **kept**; people who exist only in the file come back switched off with a random PIN until you set one. With a locked file, the PINs and authenticators in the file come back.
* A restore puts the invoice and ticket counters back to the file's values (they match the invoices that come back with it).
* Old (v1) backup files still work: they are **merged in** (add what is missing, update what matches) and **never delete** anything; their counters never wind a number back below what this CRM already used.
* Only the Owner can do any of this.

## Known limits (please read)
* **The backup password cannot be recovered.** Write it down. Without it a locked backup is only noise (that is the point).
* **Wrong-PIN counting is per internet address.** Phones on the shop's Wi-Fi usually share one address, so five wrong tries by one person pause sign-in for everyone **at the shop** for a minute. Phones on mobile data count separately. On Railway the address comes from the proxy header; on another host set `CLIENT_IP_HEADER` (for example `x-forwarded-for`) or all visitors look the same.
* **Rebuilding on a new server needs the same `SETTINGS_ENCRYPTION_KEY`** (DEPLOY.md already says never to lose it). Saved passwords and API keys, and authenticator secrets, are stored encrypted with that key, and a locked backup carries them in that form. With a different key they read as empty: re-enter the Twilio / Brevo details, and use a recovery code or `RESET_2FA=1` for 2-step. PINs do not depend on the key.
* **2-step is optional, not forced.** The Owner is reminded; staff can be added later. It protects against a stolen or guessed PIN, not against a phone that is already signed in.
* **The header policy keeps inline scripts allowed** (the pages need them), so it limits *where* things load from and post to, not injected inline script.
* **The restore preview waits in the server's memory for 15 minutes.** If the server restarts in between, choose the file again.
* **Restore replaces everything** with the file. Download a fresh backup first if you are unsure; the preview shows what would be lost.
* **Weekly backup by email** is capped at 3.5 MB. A shop with years of invoices will outgrow that; use Download, or tell me and I will add storage to a cloud drive.
* The wrong-PIN waits (60 seconds, doubling, 30-minute cap) and the other numbers above are fixed in the code, not settings. The idle timeout and shortest PIN are the only two the Owner can change.
