"""Permission-filtered planning and actual presence. Reads never write."""
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, getdate, now_datetime

from hrms.hr.retail_attendance import commands, schedules


def get_board(site, start_date):
    doc = commands.site_doc(site)
    start = getdate(start_date)
    end = start + timedelta(days=6)
    rosters = frappe.get_list("Retail Attendance Roster", filters={"site": site, "company": doc.company},
        fields=["name", "company", "site", "employee", "enrolled_from", "version"], limit_page_length=0)
    ctx = schedules.load_context(rosters, start, end)
    days = frappe.get_list("Retail Attendance Day", filters={"site": site, "business_date": ["between", [start, end]]},
        fields=["name", "business_date", "status"], limit_page_length=0)
    day_dates = {r.name: str(r.business_date) for r in days}
    day_states = {str(r.business_date): r.status for r in days}
    current = frappe.get_doc("Retail Attendance Day", doc.current_day) if doc.current_day else None
    if current:
        day_dates[current.name] = str(current.business_date)
        day_states[str(current.business_date)] = current.status
    employees = list(ctx["employees"])
    presences = frappe.get_list("Retail Attendance Presence", filters={"site": site, "employee": ["in", employees], "day": ["in", list(day_dates)]},
        fields=["name", "employee", "day", "check_in", "check_out", "in_time", "out_time", "automatic_checkout", "schedule"],
        limit_page_length=0) if employees and day_dates else []
    by_employee_date = {}
    for p in presences:
        by_employee_date.setdefault((p.employee, day_dates[p.day]), []).append(p)
    rows = []
    now = now_datetime()
    for roster in rosters:
        emp = ctx["employees"][roster.employee]
        cells = []
        for offset in range(7):
            date = start + timedelta(days=offset)
            planned = schedules.resolve(roster, date, ctx)
            actual = sorted(by_employee_date.get((roster.employee, str(date)), []), key=lambda p: get_datetime(p.in_time))
            cells.append(dict(planned, actual=[dict(in_time=str(p.in_time), out_time=str(p.out_time) if p.out_time else None,
                automatic=bool(p.automatic_checkout)) for p in actual], status=schedules.display_status(planned, actual, now, day_states.get(str(date)))))
        rows.append(dict(employee=roster.employee, employee_name=emp.employee_name, roster=roster.name,
            version=roster.version, enrolled_from=str(roster.enrolled_from), cells=cells))
    rows.sort(key=lambda r: r["employee_name"].casefold())
    history = frappe.get_list("Retail Attendance Day", filters={"site": site}, fields=["name", "business_date", "status",
        "opened_at", "closed_at", "close_recorded_at", "closed_by", "difference_minutes"], order_by="business_date desc", limit_page_length=14)
    return dict(site=dict(name=doc.name, site_name=doc.site_name, company=doc.company, version=doc.version,
        opens_at=str(doc.opens_at), closes_at=str(doc.closes_at)), start_date=str(start), today=str(getdate()),
        server_time=str(now), current_day=_day(current), rows=rows, history=history, kiosk_available=False)


def _day(doc):
    if not doc:
        return None
    remaining = frappe.db.count("Retail Attendance Presence", {"day": doc.name, "check_out": ["is", "not set"]})
    return dict(name=doc.name, business_date=str(doc.business_date), status=doc.status, version=doc.version,
        opened_at=str(doc.opened_at), expected_close=str(doc.expected_close), remaining=remaining,
        closed_at=str(doc.closed_at) if doc.closed_at else None, close_operation=doc.close_operation,
        close_error=doc.close_error, previous_day=getdate(doc.business_date) < getdate())
