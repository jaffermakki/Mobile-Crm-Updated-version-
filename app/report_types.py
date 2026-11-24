"""
Report data structures + CSV/XLSX exporters.

Every report in the Report Center is built as a `Report` — a title, a row of
headline numbers (KPIs) and a list of table `Section`s. One generic template
renders any of them on screen, one print template prints any of them, and
the exporters below turn any of them into CSV or a multi-sheet Excel
workbook. So adding a new report means writing one builder function that
returns a `Report`; the page, print view, CSV and XLSX come for free.

Cell values are plain Python values, with three float subclasses that carry
*how* a number should be shown (and which Excel number format to use):

    Money(12.5)  -> $12.50     Pct(12.5) -> 12.5%     Dec(2.34) -> 2.3
    int          -> 1,234      str       -> as is     None      -> —
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Optional


class _Num(float):
    """Float that never carries a negative zero (it would print as "$-0.00")."""
    def __new__(cls, v=0.0):
        return super().__new__(cls, float(v) + 0.0)


class Money(_Num):
    """Dollar amount."""


class Pct(_Num):
    """Percentage as a number (12.5 means 12.5%)."""


class Dec(_Num):
    """A one-decimal figure (averages, days, ratios)."""


def fmt_cell(v: Any) -> str:
    """Display string for a cell — used by the HTML templates and CSV."""
    if v is None or v == "":
        return "—"
    if isinstance(v, Money):
        return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
    if isinstance(v, Pct):
        return f"{v:.1f}%"
    if isinstance(v, Dec):
        return f"{v:,.1f}"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def is_numeric_cell(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass
class Kpi:
    label: str
    value: Any
    sub: str = ""
    tone: str = ""  # "good" | "warn" | "bad" | ""


@dataclass
class Section:
    title: str
    headers: list
    rows: list
    foot: Optional[list] = None      # optional totals row
    note: str = ""                   # small explanatory text under the title
    bars: Optional[list] = None      # [(label, value)] -> simple bar chart above the table
    empty: str = "Nothing to show for this period."


@dataclass
class Report:
    key: str
    title: str
    subtitle: str = ""
    kpis: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# ── exporters ─────────────────────────────────────────────────────────

def _csv_value(v: Any):
    if v is None:
        return ""
    if isinstance(v, Money):
        return f"{v:.2f}"
    if isinstance(v, (Pct, Dec)):
        return f"{v:.1f}"
    return v


def _safe_cell(v: Any):
    """CSV/Excel formula-injection guard: a customer name or device typed as
    '=HYPERLINK(...)' must not become a live formula when the file is opened."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") and len(v) > 1:
        return "'" + v
    return v


def _csv_cell(v: Any):
    """Numbers are formatted first and never guarded (a negative amount like
    -5.00 must stay a number); only text can carry a formula payload."""
    return _safe_cell(v) if isinstance(v, str) else _csv_value(v)


def report_to_csv(report: Report) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([report.title])
    if report.subtitle:
        w.writerow([report.subtitle])
    w.writerow([])
    if report.kpis:
        w.writerow(["Summary"])
        for k in report.kpis:
            w.writerow([k.label, _csv_cell(k.value), k.sub])
        w.writerow([])
    for s in report.sections:
        w.writerow([s.title])
        w.writerow(s.headers)
        for row in s.rows:
            w.writerow([_csv_cell(c) for c in row])
        if s.foot:
            w.writerow([_csv_cell(c) for c in s.foot])
        w.writerow([])
    for n in report.notes:
        w.writerow([n])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 (accents, em-dashes) correctly


_SHEET_BAD = set('[]:*?/\\')


def _sheet_title(name: str, used: set) -> str:
    clean = "".join(ch for ch in name if ch not in _SHEET_BAD).strip() or "Sheet"
    clean = clean[:31]
    base, n = clean, 2
    while clean.lower() in used:
        suffix = f" ({n})"
        clean = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(clean.lower())
    return clean


def report_to_xlsx(report: Report) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="E8EAF0")
    used: set = set()

    def style_value(cell, v):
        if isinstance(v, Money):
            cell.number_format = '"$"#,##0.00;[Red]-"$"#,##0.00'
        elif isinstance(v, Pct):
            cell.number_format = '0.0"%"'
        elif isinstance(v, Dec):
            cell.number_format = "#,##0.0"
        elif isinstance(v, int) and not isinstance(v, bool):
            cell.number_format = "#,##0"

    def autosize(ws, ncols):
        for ci in range(1, ncols + 1):
            letter = get_column_letter(ci)
            longest = max((len(str(c.value)) for c in ws[letter] if c.value is not None), default=8)
            ws.column_dimensions[letter].width = min(max(longest + 2, 10), 48)

    # Summary sheet
    ws = wb.active
    ws.title = _sheet_title("Summary", used)
    ws.append([report.title]); ws["A1"].font = Font(bold=True, size=14)
    if report.subtitle:
        ws.append([report.subtitle])
    ws.append([])
    for k in report.kpis:
        ws.append([k.label, _safe_cell(k.value), k.sub])
        style_value(ws.cell(row=ws.max_row, column=2), k.value)
        ws.cell(row=ws.max_row, column=1).font = bold
    for n in report.notes:
        ws.append([])
        ws.append([n])
    autosize(ws, 3)

    for s in report.sections:
        sh = wb.create_sheet(_sheet_title(s.title, used))
        sh.append(s.headers)
        for c in sh[1]:
            c.font = bold; c.fill = head_fill; c.alignment = Alignment(wrap_text=True, vertical="top")
        for row in s.rows:
            sh.append([_safe_cell(c) for c in row])
            for ci, v in enumerate(row, start=1):
                style_value(sh.cell(row=sh.max_row, column=ci), v)
        if s.foot:
            sh.append([_safe_cell(c) for c in s.foot])
            for ci, v in enumerate(s.foot, start=1):
                cell = sh.cell(row=sh.max_row, column=ci)
                cell.font = bold
                style_value(cell, v)
        sh.freeze_panes = "A2"
        autosize(sh, len(s.headers))

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
