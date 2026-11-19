"""Permission-filtered planning and actual presence. Reads never write."""
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from hrms.hr.retail_attendance import access, commands, rules, schedules


def get_board(site, start_date, view="week"):
    doc = commands.site_doc(site)
    if view not in ("today", "week", "day"):
        frappe.throw(_("Choose Today or Week."))
    current = frappe.get_doc("Retail Attendance Day", doc.current_day) if doc.current_day else None
    # Today's work belongs to the still-open business day across midnight.
    # Do not move an overtime employee into an empty new calendar-day row.
    start = getdate(current.business_date) if view == "today" and current else getdate(start_date)
    end = start + timedelta(days=6)
    rosters = frappe.get_list("Retail Attendance Roster", filters={"site": site, "company": doc.company},
        fields=["name", "company", "site", "employee", "enrolled_from", "version"], limit_page_length=0)
    ctx = schedules.load_context(rosters, start, end)
    days = frappe.get_list("Retail Attendance Day", filters={"site": site, "business_date": ["between", [start, end]]},
        fields=["name", "business_date", "status"], limit_page_length=0)
    day_dates = {r.name: str(r.business_date) for r in days}
    day_states = {str(r.business_date): r.status for r in days}
    if current:
        day_dates[current.name] = str(current.business_date)
        day_states[str(current.business_date)] = current.status
    employees = list(ctx["employees"])
    presences = frappe.get_list("Retail Attendance Presence", filters={"site": site, "employee": ["in", employees], "day": ["in", list(day_dates)]},
        fields=["name", "employee", "day", "check_in", "check_out", "in_time", "out_time", "automatic_checkout", "schedule", "version"],
        limit_page_length=0) if employees and day_dates else []
    by_employee_date = {}
    for p in presences:
        by_employee_date.setdefault((p.employee, day_dates[p.day]), []).append(p)
    decisions = frappe.get_list("Retail Attendance Fine Decision", filters={"site": site,
        "employee": ["in", employees], "business_date": ["between", [start, end]]},
        fields=["name", "employee", "business_date", "kind", "amount", "late_minutes", "status", "cancellation_reason"], limit_page_length=0) if employees else []
    fine_by_date = {(r.employee, str(r.business_date)): dict(name=r.name, kind=r.kind, amount=r.amount,
        late_minutes=r.late_minutes, status=r.status, cancellation_reason=r.cancellation_reason) for r in decisions}
    pin_employees = set(frappe.get_all("Retail Attendance Credential", filters={"employee": ["in", employees], "enabled": 1}, pluck="employee")) if employees else set()
    rows = []
    now = now_datetime()
    for roster in rosters:
        emp = ctx["employees"][roster.employee]
        regular, unused_exception = rules.choose_revision([r for r in ctx["revisions"].get(roster.employee, []) if r.kind == "Regular"], max(start, getdate()))
        cells = []
        for offset in range(7):
            date = start + timedelta(days=offset)
            planned = schedules.resolve(roster, date, ctx)
            actual = sorted(by_employee_date.get((roster.employee, str(date)), []), key=lambda p: get_datetime(p.in_time))
            cells.append(dict(planned, day=next((n for n, value in day_dates.items() if value == str(date)), None), actual=[dict(name=p.name, version=p.version, in_time=str(p.in_time), out_time=str(p.out_time) if p.out_time else None,
                automatic=bool(p.automatic_checkout)) for p in actual], fine=fine_by_date.get((roster.employee, str(date))),
                status=schedules.display_status(planned, actual, now, day_states.get(str(date)))))
        rows.append(dict(employee=roster.employee, employee_name=emp.employee_name, designation=emp.get("designation"),
            has_pin=roster.employee in pin_employees, active=emp.status == "Active", roster=roster.name,
            version=roster.version, enrolled_from=str(roster.enrolled_from), cells=cells,
            regular=dict(mode=regular.mode, weekdays=regular.weekdays,
                starts_at=str(rules.parse_time(regular.starts_at)) if regular.starts_at else None,
                ends_at=str(rules.parse_time(regular.ends_at)) if regular.ends_at else None) if regular else None))
    rows.sort(key=lambda r: r["employee_name"].casefold())
    history = frappe.get_list("Retail Attendance Day", filters={"site": site}, fields=["name", "business_date", "status",
        "opened_at", "owner", "closed_at", "close_recorded_at", "closed_by", "difference_minutes"], order_by="business_date desc", limit_page_length=100)
    history_presence = frappe.get_list("Retail Attendance Presence", filters={"site": site,
        "day": ["in", [h.name for h in history]], "employee": ["in", employees]},
        fields=["day", "employee"], limit_page_length=0) if history and employees else []
    history_fines = frappe.get_list("Retail Attendance Fine Decision", filters={"site": site,
        "business_date": ["in", [h.business_date for h in history]], "employee": ["in", employees], "status": "Applied"},
        fields=["business_date", "amount"], limit_page_length=0) if history and employees else []
    for h in history:
        h.employee_count = len({p.employee for p in history_presence if p.day == h.name})
        h.fine_total = sum(float(f.amount) for f in history_fines if str(f.business_date) == str(h.business_date))
    active_employees = access.staff().rows("Employee", ["name"], filters={"company": doc.company, "status": "Active"})
    enrolled_all = set(frappe.get_all("Retail Attendance Roster", filters={"company": doc.company}, pluck="employee"))
    unenrolled_count = len([r for r in active_employees if r.name not in enrolled_all])
    return dict(site=dict(name=doc.name, site_name=doc.site_name, company=doc.company, version=doc.version,
        opens_at=str(rules.parse_time(doc.opens_at)), closes_at=str(rules.parse_time(doc.closes_at))), start_date=str(start), today=str(getdate()),
        currency=frappe.db.get_value("Company", doc.company, "default_currency"),
        server_time=str(now), current_day=_day(current, employees), rows=rows, history=history,
        unenrolled_count=unenrolled_count, kiosk_available=True)


def _day(doc, employees):
    if not doc:
        return None
    remaining = frappe.db.count("Retail Attendance Presence", {"day": doc.name, "employee": ["in", employees],
        "check_out": ["is", "not set"]}) if employees else 0
    return dict(name=doc.name, business_date=str(doc.business_date), status=doc.status, version=doc.version,
        opened_at=str(doc.opened_at), expected_close=str(doc.expected_close), remaining=remaining,
        closed_at=str(doc.closed_at) if doc.closed_at else None, close_operation=doc.close_operation,
        close_error=doc.close_error, previous_day=getdate(doc.business_date) < getdate()
            and now_datetime() > get_datetime(doc.expected_close))
