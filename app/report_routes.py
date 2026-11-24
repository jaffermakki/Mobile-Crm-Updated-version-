"""
Report Center routes: hub, on-screen report, print view, CSV and Excel export.

Built with a factory (`build_router`) so it can borrow main.py's template
engine, settings reader and login check without importing main.py (which
would be a circular import).

Access: owner and manager only — these pages show cost, profit, discounts
given and who processed what.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .auth import add_audit, role_allowed
from .localtime import get_shop_tz, local_now, local_today, month_bounds, tz_label, tz_staleness_warning
from .report_types import fmt_cell, is_numeric_cell, report_to_csv, report_to_xlsx
from .reporting import RANGE_PRESETS, REPORT_INDEX, REPORTS, build_report, preset_range

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file_stem(key: str, p: dict, today_iso: str) -> str:
    if "date" in p:
        return f"{key}_{p['date'].isoformat()}"
    if "year" in p:
        return f"{key}_{p['year']:04d}-{p['month']:02d}"
    if "d1" in p:
        return f"{key}_{p['d1'].isoformat()}_to_{p['d2'].isoformat()}"
    return f"{key}_{today_iso}"


def build_router(templates, get_setting, require_login, get_db) -> APIRouter:
    router = APIRouter()
    templates.env.globals["fmt_cell"] = fmt_cell
    templates.env.globals["is_num"] = is_numeric_cell

    def _gate(request: Request, db: Session):
        staff = require_login(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        if not role_allowed(staff, "owner", "manager"):
            return None, HTMLResponse("Forbidden — reports require manager or owner role.", status_code=403)
        return staff, None

    @router.get("/reports/center", response_class=HTMLResponse)
    def report_center(request: Request, db: Session = Depends(get_db)):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        tz = get_shop_tz(db)
        today = local_today(tz)
        yesterday = today - timedelta(days=1)
        ly, lm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        q_first, q_last = preset_range("last_quarter", today)
        quick = [
            ("Today's Daily Report", f"/reports/center/daily?date={today.isoformat()}"),
            ("Yesterday's Daily Report", f"/reports/center/daily?date={yesterday.isoformat()}"),
            (f"{month_bounds(today.year, today.month)[0]:%B} so far", f"/reports/center/monthly?month={today.year:04d}-{today.month:02d}"),
            (f"{month_bounds(ly, lm)[0]:%B %Y} review", f"/reports/center/monthly?month={ly:04d}-{lm:02d}"),
            ("Tax for last quarter", f"/reports/center/tax?from={q_first.isoformat()}&to={q_last.isoformat()}"),
            ("Phones waiting for pickup", "/reports/center/repairs?preset=last_90"),
            ("What to reorder", "/reports/center/inventory"),
        ]
        groups = {}
        for r in REPORTS:
            groups.setdefault(r["group"], []).append(r)
        return templates.TemplateResponse(request, "report_center.html", {
            "staff": staff, "groups": groups, "quick": quick, "tz_label": tz_label(tz), "tz_warning": tz_staleness_warning(tz),
            "now_local": local_now(tz).strftime("%a %b %d, %Y %H:%M"),
        })

    def _render(request, db, key, template, extra=None):
        staff, deny = _gate(request, db)
        if deny:
            return None, deny
        if key not in REPORT_INDEX:
            return None, HTMLResponse("Unknown report.", status_code=404)
        report, params, tz = build_report(db, key, dict(request.query_params))
        ctx = {"staff": staff, "report": report, "spec": REPORT_INDEX[key], "p": params, "key": key,
               "presets": RANGE_PRESETS, "qs": request.url.query, "tz_label": tz_label(tz), "tz_warning": tz_staleness_warning(tz),
               "shop_name": get_setting(db, "shop_name", "TechPro+"),
               "generated": local_now(tz).strftime("%a %b %d, %Y %H:%M"), "args": dict(request.query_params)}
        if extra:
            ctx.update(extra)
        return templates.TemplateResponse(request, template, ctx), None

    @router.get("/reports/center/{key}", response_class=HTMLResponse)
    def report_view(request: Request, key: str, db: Session = Depends(get_db)):
        resp, deny = _render(request, db, key, "report_view.html")
        return deny or resp

    @router.get("/reports/center/{key}/print", response_class=HTMLResponse)
    def report_print(request: Request, key: str, db: Session = Depends(get_db)):
        resp, deny = _render(request, db, key, "report_print.html")
        return deny or resp

    def _export(request, key, db, kind):
        staff, deny = _gate(request, db)
        if deny:
            return deny
        if key not in REPORT_INDEX:
            return HTMLResponse("Unknown report.", status_code=404)
        report, params, tz = build_report(db, key, dict(request.query_params))
        stem = _file_stem(key, params, local_today(tz).isoformat())
        add_audit(db, staff, "EXPORT", f"{report.title} exported ({kind.upper()}) — {report.subtitle.split(' · ')[0]}")
        db.commit()
        if kind == "csv":
            body, mime = report_to_csv(report), "text/csv; charset=utf-8"
        else:
            body, mime = report_to_xlsx(report), XLSX_MIME
        return Response(content=body, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{stem}.{kind}"', "Cache-Control": "no-store"})

    @router.get("/reports/center/{key}/export.csv")
    def report_csv(request: Request, key: str, db: Session = Depends(get_db)):
        return _export(request, key, db, "csv")

    @router.get("/reports/center/{key}/export.xlsx")
    def report_xlsx(request: Request, key: str, db: Session = Depends(get_db)):
        return _export(request, key, db, "xlsx")

    return router
