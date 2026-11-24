"""
v49 — repair check-in: the condition checklist, and the photos taken at drop-off.

The checklist is a small JSON document kept on the repair (`Repair.intake_json`), so it travels with the ticket
and with every backup. Photos live in their own table (`repair_photos`); see `sniff_image` for what is accepted.

Nothing in here touches the database or the web layer: it only turns form fields into a clean document and
checks image bytes, which keeps it easy to test.
"""
from __future__ import annotations

import json
import struct
from datetime import datetime
from typing import Any, Optional

# ── the checklist ─────────────────────────────────────────────────────

# (key, label, text for "fine", text for "problem")
CHECKS = (
    ("powers_on", "Powers on", "Yes", "No / won't start"),
    ("screen", "Screen and touch", "Fine", "Cracked / damaged"),
    ("body", "Back, frame and buttons", "Fine", "Dented / cracked / stuck"),
    ("charging", "Charging port", "Works", "Loose / not charging"),
    ("camera", "Cameras", "Work", "Blurry / dead"),
    ("audio", "Speaker, mic, earpiece", "Work", "Faulty"),
    ("battery", "Battery", "Fine", "Swollen / drains fast"),
    ("network", "SIM, Wi-Fi and signal", "Work", "Problem"),
    ("liquid", "Liquid damage", "None seen", "Signs of liquid"),
)
CHECK_KEYS = tuple(c[0] for c in CHECKS)
STATES = ("ok", "issue", "na")                      # fine / a problem was found / not tested
STATE_LABEL = {"ok": "Fine", "issue": "Problem", "na": "Not tested"}

ACCESSORIES = (
    ("case", "Case"), ("charger", "Charger / cable"), ("sim", "SIM card"), ("sd", "SD card"), ("other", "Something else"),
)
ACCESSORY_KEYS = tuple(a[0] for a in ACCESSORIES)

PASSCODE_STATES = (("given", "Customer gave it"), ("none", "No passcode"), ("not_given", "Not given"))
FIND_MY_STATES = (("on", "ON"), ("off", "Off"), ("unknown", "Don't know"))
NOTES_MAX = 500

DEFAULT_TERMS = (
    "Please back up your data before a repair; repairs can erase it and we cannot be responsible for lost data. "
    "We are not responsible for faults that were already present or that cannot be tested until the device is repaired. "
    "Devices not collected within 90 days of being ready may be disposed of."
)


def _clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())          # one line: no stray line breaks in a printed sheet
    return text[:limit]


def empty() -> dict:
    return {"v": 1, "checks": {}, "left": [], "passcode": "", "find_my": "", "data_warning": False, "notes": "", "by": "", "at": ""}


def normalise(raw: dict, by_name: str = "", now: Optional[datetime] = None) -> Optional[dict]:
    """Turns submitted form fields into a clean checklist, or None when nothing at all was filled in.

    `raw` is a plain dict of the form's fields: `chk_<key>` = ok | issue | na, `acc_<key>` present when ticked,
    `lock_passcode`, `lock_find_my`, `intake_data_warning`, `intake_notes`. Anything unknown or invalid is ignored,
    so a hand-made request cannot put odd data into the ticket."""
    out = empty()
    for key in CHECK_KEYS:
        value = str(raw.get(f"chk_{key}", "") or "")
        if value in STATES:
            out["checks"][key] = value
    out["left"] = [k for k in ACCESSORY_KEYS if str(raw.get(f"acc_{k}", "") or "") not in ("", "0", "false", "off")]
    passcode = str(raw.get("lock_passcode", "") or "")
    out["passcode"] = passcode if passcode in {k for k, _ in PASSCODE_STATES} else ""
    find_my = str(raw.get("lock_find_my", "") or "")
    out["find_my"] = find_my if find_my in {k for k, _ in FIND_MY_STATES} else ""
    out["data_warning"] = str(raw.get("intake_data_warning", "") or "") not in ("", "0", "false", "off")
    out["notes"] = _clean_text(raw.get("intake_notes", ""), NOTES_MAX)
    if out["notes"]:
        from .money import scrub_card_numbers
        out["notes"] = scrub_card_numbers(out["notes"])[0]       # a card number pasted into a note is cut to its last four
    nothing = not (out["checks"] or out["left"] or out["passcode"] or out["find_my"] or out["data_warning"] or out["notes"])
    if nothing:
        return None
    out["by"] = _clean_text(by_name, 80)
    out["at"] = (now or datetime.utcnow()).isoformat(timespec="seconds")
    return out


def load(repair_or_text: Any) -> dict:
    """The stored checklist as a dict (an empty one for a ticket that never had one, or for damaged data)."""
    text = repair_or_text if isinstance(repair_or_text, str) else getattr(repair_or_text, "intake_json", "")
    if not text:
        return empty()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    base = empty()
    base.update({k: data.get(k, base[k]) for k in base})
    if not isinstance(base["checks"], dict):
        base["checks"] = {}
    if not isinstance(base["left"], list):
        base["left"] = []
    return base


def has_checklist(intake: dict) -> bool:
    return bool(intake.get("at"))


def rows(intake: dict) -> list[dict]:
    """The checklist as display rows, in a fixed order: key, label, state, and the words to show for that state."""
    out = []
    for key, label, ok_text, issue_text in CHECKS:
        state = intake.get("checks", {}).get(key, "na")
        if state not in STATES:
            state = "na"
        words = {"ok": ok_text, "issue": issue_text, "na": "Not tested"}[state]
        out.append({"key": key, "label": label, "state": state, "words": words, "ok_text": ok_text, "issue_text": issue_text})
    return out


def problems(intake: dict) -> list[str]:
    """Labels of everything marked as a problem — what a technician should read first."""
    return [r["label"] for r in rows(intake) if r["state"] == "issue"]


def accessory_labels(intake: dict) -> list[str]:
    names = dict(ACCESSORIES)
    return [names[k] for k in intake.get("left", []) if k in names]


def passcode_label(intake: dict) -> str:
    return dict(PASSCODE_STATES).get(intake.get("passcode", ""), "")


def find_my_label(intake: dict) -> str:
    return dict(FIND_MY_STATES).get(intake.get("find_my", ""), "")


# ── photos ────────────────────────────────────────────────────────────

MAX_PHOTOS = 12                      # per repair
MAX_PHOTO_BYTES = 6 * 1024 * 1024    # what the server accepts (the phone shrinks them to ~200 KB first)
MAX_THUMB_BYTES = 400 * 1024
MAX_SIDE = 8000                      # pixels; bigger is refused so nobody can ship a decompression bomb to a viewer
PHOTO_LABELS = (("front", "Front"), ("back", "Back"), ("damage", "Damage"), ("label", "IMEI / serial label"),
                ("after", "After repair"), ("other", "Other"))
PHOTO_LABEL_KEYS = tuple(k for k, _ in PHOTO_LABELS)


def sniff_image(data: bytes) -> Optional[tuple[str, int, int]]:
    """(mime type, width, height) when `data` really is a JPEG, PNG or WebP picture of a sane size, else None.
    The file name and the type the browser claims are never trusted; the bytes decide."""
    if not data or len(data) < 24:
        return None
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            if data[12:16] != b"IHDR":
                return None
            w, h = struct.unpack(">II", data[16:24])
            mime = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            size = _jpeg_size(data)
            if not size:
                return None
            w, h = size
            mime = "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            size = _webp_size(data)
            if not size:
                return None
            w, h = size
            mime = "image/webp"
        else:
            return None
    except (struct.error, IndexError):
        return None
    if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE):
        return None
    return mime, w, h


def _jpeg_size(data: bytes) -> Optional[tuple[int, int]]:
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:                       # padding
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if length < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def _webp_size(data: bytes) -> Optional[tuple[int, int]]:
    kind = data[12:16]
    if kind == b"VP8 " and len(data) >= 30:
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L" and len(data) >= 25:
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X" and len(data) >= 30:
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return w, h
    return None


def photo_file_name(ticket_no: Any, label: str, photo_id: str, mime: str) -> str:
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "bin")
    safe_label = "".join(c for c in (label or "photo") if c.isalnum() or c in "-_")[:20] or "photo"
    return f"ticket-{ticket_no}/{safe_label}-{str(photo_id)[:8]}.{ext}"
