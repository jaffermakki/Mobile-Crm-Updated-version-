"""
Shop-local time handling.

Every timestamp in the database is stored as naive UTC (datetime.utcnow()),
which is the right way to store them. The bug this module fixes is *reading*
them: "today", "this month", "which day did this sale belong to" were all
being answered in UTC. For a shop in Vernon, BC (UTC-8 in winter, UTC-7 in
summer) that means anything rung up after ~4-5pm local time landed on
*tomorrow's* Cash Up, dashboard and daily digest, and a sale at 5:30pm on
the last day of the month landed in next month's tax report.

The fix is to keep storing UTC and convert at the edges:

  - "what is today / this month in the shop's own timezone?"  -> local_today()
  - "which local calendar day does this stored UTC timestamp fall on?" -> to_local()
  - "give me the UTC range that covers local day(s) X..Y, so the database
     can filter on an indexed column instead of loading every row" -> day_bounds_utc()

The timezone comes from the `timezone` setting. If that has never been set
(every existing install), it is derived from the Province setting, so an
existing BC shop gets America/Vancouver with no action needed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Province -> the IANA zone most of that province's population lives in.
# (A few provinces straddle two zones — e.g. BC's far east, SK's Lloydminster,
# Nunavut-adjacent areas — which is why the Settings page lets you override.)
PROVINCE_TZ = {
    "BC": "America/Vancouver",
    "AB": "America/Edmonton",
    "SK": "America/Regina",
    "MB": "America/Winnipeg",
    "ON": "America/Toronto",
    "QC": "America/Toronto",
    "NB": "America/Moncton",
    "NS": "America/Halifax",
    "PE": "America/Halifax",
    "NL": "America/St_Johns",
}

# What the Settings dropdown offers. Canadian zones first, UTC last.
TIMEZONE_CHOICES = [
    ("America/Vancouver", "Pacific — BC (Vancouver, Vernon, Kelowna)"),
    ("America/Edmonton", "Mountain — AB (Edmonton, Red Deer, Calgary)"),
    ("America/Regina", "Central, no DST — SK (Regina, Saskatoon)"),
    ("America/Winnipeg", "Central — MB (Winnipeg)"),
    ("America/Toronto", "Eastern — ON / QC (Toronto, Ottawa, Montréal)"),
    ("America/Moncton", "Atlantic — NB (Moncton, Fredericton)"),
    ("America/Halifax", "Atlantic — NS / PE (Halifax, Charlottetown)"),
    ("America/St_Johns", "Newfoundland (St. John's)"),
    ("UTC", "UTC (no conversion)"),
]

_UTC = timezone.utc  # stdlib UTC; never needs the tzdata package


def load_zone(name: str) -> "ZoneInfo | timezone":
    """ZoneInfo for `name`, or plain UTC if it can't be loaded (e.g. a
    Windows machine without the `tzdata` package installed). Falling back
    to UTC reproduces the old behaviour rather than crashing the page."""
    if not name or name.upper() == "UTC":
        return _UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return _UTC


def zone_name_for(setting_value: str, province: str) -> str:
    """Resolve the effective zone name: explicit setting wins, otherwise
    derive from the province, otherwise UTC."""
    if setting_value:
        return setting_value
    return PROVINCE_TZ.get((province or "").upper(), "UTC")


# The zone templates use to show stored UTC times as shop-local ones (the
# `L()` Jinja global below). Refreshed every time get_shop_tz() runs — which
# every report, Cash Up and the dashboard do — and when Settings are saved.
_active = {"tz": _UTC}


def get_shop_tz(db) -> "ZoneInfo | timezone":
    """The shop's timezone, read from Settings (with province fallback)."""
    from .models import Setting  # local import: models imports nothing from here, but keep this module import-light

    tz_row = db.get(Setting, "timezone")
    prov_row = db.get(Setting, "province")
    name = zone_name_for(tz_row.value if tz_row else "", prov_row.value if prov_row else "")
    tz = load_zone(name)
    _active["tz"] = tz
    return tz


def active_zone():
    return _active["tz"]


def L(dt_utc_naive):
    """Template helper: `{{ L(inv.date).strftime('%H:%M') }}` shows the
    stored UTC timestamp as shop-local wall-clock time."""
    return to_local(dt_utc_naive, _active["tz"]) if dt_utc_naive else dt_utc_naive


# Zones whose government stopped changing clocks on Nov 1 2026 (BC "Pacific
# time" = UTC-7 all year; Alberta "ABT" = UTC-6 all year). Older timezone
# databases still switch to standard time in winter, which would put every
# winter evening sale on the wrong side of midnight by an hour.
_NO_CLOCK_CHANGE_2026 = {"America/Vancouver": "tzdata 2026.2", "America/Edmonton": "tzdata 2026.3"}


def tz_staleness_warning(tz):
    """A one-line warning if this machine's timezone database predates the
    2026 BC/Alberta end of clock changes; None when everything is current."""
    key = getattr(tz, "key", None)
    if key not in _NO_CLOCK_CHANGE_2026:
        return None
    jan = datetime(2027, 1, 15, 12, tzinfo=tz).utcoffset()
    jul = datetime(2027, 7, 15, 12, tzinfo=tz).utcoffset()
    if jan == jul:
        return None
    return (f"This computer's timezone data is out of date: it still switches to winter time, but "
            f"{'BC' if key == 'America/Vancouver' else 'Alberta'} stopped changing clocks on Nov 1 2026, so "
            f"winter sales near midnight can land on the wrong day. Update the 'tzdata' package "
            f"(pip install --upgrade tzdata, version {_NO_CLOCK_CHANGE_2026[key]} or newer) and restart the app.")


def tz_label(tz) -> str:
    return getattr(tz, "key", None) or "UTC"


def utc_now_naive() -> datetime:
    """Same value as the (deprecated) datetime.utcnow(): naive, UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc_naive: datetime | None, tz) -> datetime | None:
    """Convert a stored naive-UTC timestamp to a naive local wall-clock one."""
    if dt_utc_naive is None:
        return None
    aware = dt_utc_naive.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz).replace(tzinfo=None)


def to_local_date(dt_utc_naive: datetime | None, tz) -> date | None:
    local = to_local(dt_utc_naive, tz)
    return local.date() if local else None


def local_now(tz) -> datetime:
    return to_local(utc_now_naive(), tz)


def local_today(tz) -> date:
    return local_now(tz).date()


def day_start_utc(d: date, tz) -> datetime:
    """Naive-UTC instant at which local calendar day `d` begins."""
    local_midnight = datetime(d.year, d.month, d.day, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def day_bounds_utc(d1: date, d2: date | None, tz) -> tuple[datetime, datetime]:
    """[start, end) in naive UTC covering local days d1..d2 inclusive.
    DST-safe: the end is the start of the *next* local day, not start+24h."""
    d2 = d2 or d1
    return day_start_utc(d1, tz), day_start_utc(d2 + timedelta(days=1), tz)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last calendar date of a month."""
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, nxt - timedelta(days=1)


def parse_month(value: str, default: date) -> tuple[int, int]:
    """'YYYY-MM' -> (year, month); anything malformed -> default's month."""
    try:
        y, m = int(value[:4]), int(value[5:7])
        if 1 <= m <= 12 and 2000 <= y <= 2100:
            return y, m
    except (ValueError, TypeError, IndexError):
        pass
    return default.year, default.month


def parse_date(value: str, default: date) -> date:
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return default
