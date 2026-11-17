"""One effective schedule resolver. Reads never enroll staff or change native shifts."""
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from hrms.hr.retail_attendance import access, rules


def is_managed(employee, work_date):
    """Internal native-writer guard, independent of who runs the scheduler."""
    if not frappe.db.table_exists("Retail Attendance Roster"):
        return False
    return bool(frappe.db.exists("Retail Attendance Roster", {
        "employee": employee, "enrolled_from": ["<=", getdate(work_date)],
    }))


def load_context(rosters, start, end):
    employees = [r.employee for r in rosters]
    result = dict(revisions={}, assignments={}, leave={}, employees={}, holidays={}, shifts={}, shop_schedules={})
    if not employees:
        return result
    ctx = access.service_scope()
    service = bool(ctx and len(rosters) == 1 and rosters[0].employee == ctx["employee"]
        and rosters[0].site == ctx["site"] and rosters[0].company == ctx["company"])
    read = frappe.get_all if service else frappe.get_list
    for row in read("Retail Attendance Shop Schedule", filters={"site": ["in", list({r.site for r in rosters})],
        "effective_date": ["<=", max(end, getdate())]}, fields=["name", "site", "effective_date", "weekdays", "starts_at", "ends_at", "revision"], limit_page_length=0):
        result["shop_schedules"].setdefault(row.site, []).append(row)
    for roster in rosters:
        if not service:
            access.staff().fields("Employee", ["name", "company", "employee_name", "status", "date_of_joining", "relieving_date", "default_shift", "holiday_list"])
        result["employees"][roster.employee] = frappe.get_doc("Employee", roster.employee) if service else access.employee(roster.employee, roster.company)
    for dt, fields in (
        ("Retail Attendance Schedule", ["name", "employee", "roster", "kind", "effective_date", "mode", "weekdays", "starts_at", "ends_at", "revision"]),
        ("Shift Assignment", ["name", "employee", "shift_type", "start_date", "end_date", "status", "docstatus"]),
        ("Leave Application", ["name", "employee", "from_date", "to_date", "status", "docstatus", "half_day", "half_day_date"]),
        ("Shift Type", ["name", "start_time", "end_time", "enable_auto_attendance", "holiday_list"]),
    ):
        if not service:
            access.staff().fields(dt, fields)
    revisions = read("Retail Attendance Schedule", filters={"employee": ["in", employees], "effective_date": ["<=", max(end, getdate())]},
        fields=["name", "employee", "roster", "kind", "effective_date", "mode", "weekdays", "starts_at", "ends_at", "revision"], limit_page_length=0)
    assignments = read("Shift Assignment", filters={"employee": ["in", employees], "docstatus": 1, "status": "Active", "start_date": ["<=", end]},
        fields=["name", "employee", "shift_type", "start_date", "end_date"], limit_page_length=0)
    leaves = read("Leave Application", filters={"employee": ["in", employees], "docstatus": 1, "status": "Approved", "from_date": ["<=", end], "to_date": [">=", start]},
        fields=["name", "employee", "from_date", "to_date", "half_day", "half_day_date"], limit_page_length=0)
    for key, rows in (("revisions", revisions), ("assignments", assignments), ("leave", leaves)):
        for row in rows:
            result[key].setdefault(row.employee, []).append(row)
    shift_names = {r.shift_type for r in assignments} | {e.get("default_shift") for e in result["employees"].values()}
    for name in shift_names - {None, ""}:
        doc = frappe.get_doc("Shift Type", name)
        if not service:
            doc.check_permission("read")
        result["shifts"][name] = doc
    return result


def resolve(roster, work_date, ctx):
    work_date = getdate(work_date)
    emp = ctx["employees"][roster.employee]
    output = dict(date=str(work_date), state="Unconfigured", starts_at=None, ends_at=None, source=None,
        source_revision=roster.version, shift=None, holiday_list=None)
    if work_date < getdate(roster.enrolled_from) or (emp.date_of_joining and work_date < getdate(emp.date_of_joining)):
        return dict(output, state="Not enrolled")
    if emp.relieving_date and work_date > getdate(emp.relieving_date):
        return dict(output, state="Not employed")
    if emp.status != "Active" and not emp.relieving_date:
        return dict(output, state="Review")
    rev, exception = rules.choose_revision(ctx["revisions"].get(roster.employee, []), work_date)
    if not rev or rev["mode"] == "Shop":
        shared = [r for r in ctx.get("shop_schedules", {}).get(roster.site, []) if getdate(r.effective_date) <= work_date]
        shared = max(shared, key=lambda r: (getdate(r.effective_date), int(r.revision)), default=None)
        if not shared:
            return output
        rev = dict(shared, kind="Regular", mode="Hours")
        output["source_revision"] = shared.revision
    output.update(source=rev["name"], exception=exception["name"] if exception else None)
    # Leave wins over ad-hoc work times. Half-day handling is explicit review until
    # partial-shift rules are specified; it must never become an automatic absence.
    for leave in ctx["leave"].get(roster.employee, []):
        if getdate(leave.from_date) <= work_date <= getdate(leave.to_date):
            partial = leave.half_day and (not leave.half_day_date or getdate(leave.half_day_date) == work_date)
            return dict(output, state="Review" if partial else "Approved leave")
    if rev["mode"] == "Off":
        return dict(output, state="Day off")
    if rev["kind"] == "Regular" and work_date.weekday() not in [int(x) for x in (rev["weekdays"] or "").split(",") if x]:
        return dict(output, state="Day off")
    starts, ends, holiday = rev["starts_at"], rev["ends_at"], None
    if rev["mode"] == "Native":
        matches = [r for r in ctx["assignments"].get(roster.employee, [])
            if getdate(r.start_date) <= work_date and (not r.end_date or getdate(r.end_date) >= work_date)]
        if len(matches) > 1:
            return dict(output, state="Review")
        shift_name = matches[0].shift_type if matches else emp.get("default_shift")
        shift = ctx["shifts"].get(shift_name)
        if not shift or shift.enable_auto_attendance:
            return dict(output, state="Review")
        output["shift"] = shift.name
        output["native_assignment"] = matches[0].name if matches else None
        starts, ends, holiday = shift.start_time, shift.end_time, shift.holiday_list
    # Match the native Shift Type -> employee -> company holiday hierarchy.
    if not holiday:
        from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
        holiday = get_holiday_list_for_employee(roster.employee, raise_exception=False)
    output["holiday_list"] = holiday
    if holiday:
        if holiday not in ctx["holidays"]:
            # This native child table is consulted only after Employee authority.
            ctx["holidays"][holiday] = set(frappe.get_all("Holiday", filters={"parent": holiday}, pluck="holiday_date"))
        if work_date in ctx["holidays"][holiday]:
            return dict(output, state="Holiday")
    try:
        first, last = rules.span(work_date, starts, ends)
    except (TypeError, ValueError):
        return dict(output, state="Review")
    return dict(output, state="Scheduled", starts_at=str(first), ends_at=str(last))


def display_status(schedule, presences, now, day_state=None):
    presence = next((p for p in reversed(presences) if not p.check_out), None) or (presences[-1] if presences else None)
    if presence:
        if presence.check_out:
            return "Checked out"
        end = get_datetime(frappe.parse_json(presence.schedule).get("ends_at")) if frappe.parse_json(presence.schedule).get("ends_at") else None
        return "Overtime" if end and now > end else "At work"
    if schedule["state"] != "Scheduled":
        return schedule["state"]
    if day_state == "Closed":
        return "Not recorded"
    if now > get_datetime(schedule["ends_at"]):
        return "Not arrived"
    if now > get_datetime(schedule["starts_at"]):
        return "Late"
    return "Expected"
