"""One attendance writer shared by kiosk and manager entry; no payroll effects."""
import json
from uuid import UUID

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from hrms.hr.retail_attendance import access, commands, events, schedules


def active(employee, company):
    emp = commands.lock_employee(employee, company)
    if emp.status != "Active":
        frappe.throw(_("Select an active employee."))
    emp = frappe.get_doc("Employee", employee)
    if (emp.date_of_joining and getdate(emp.date_of_joining) > getdate()) or (emp.relieving_date and getdate(emp.relieving_date) < getdate()):
        frappe.throw(_("Select an active employee."))
    return emp


def roster_for(employee, site):
    name = frappe.db.get_value("Retail Attendance Roster", {"employee": employee, "site": site.name, "company": site.company})
    if not name:
        access.staff().deny()
    return frappe.get_doc("Retail Attendance Roster", name)


def planned(roster, day):
    date = getdate(day.business_date)
    result = schedules.resolve(roster, date, schedules.load_context([roster], date, date))
    if result["state"] != "Scheduled":
        frappe.throw(_("No confirmed working schedule. Ask the manager to review attendance."))
    return result


def native(presence, kind, timestamp, operation, correction=None):
    doc = frappe.get_doc(dict(doctype="Employee Checkin", employee=presence.employee,
        time=timestamp, log_type=kind, skip_auto_attendance=1,
        retail_attendance_presence=presence.name, retail_attendance_day=presence.day,
        retail_attendance_operation=operation, retail_attendance_correction=correction))
    with events.native_event(presence, kind, timestamp):
        return access.service_save(doc) if access.service_scope() else doc.insert()


def write(site, day, employee, kind, timestamp, operation):
    """Caller holds Employee -> device(if any) -> site -> day locks."""
    if day.status != "Open" or site.current_day != day.name:
        frappe.throw(_("The attendance day is closing or closed."))
    stamp = get_datetime(timestamp).replace(microsecond=0)
    if stamp < get_datetime(day.opened_at):
        frappe.throw(_("Attendance time cannot be before the day opened."))
    roster = roster_for(employee, site)
    if getdate(roster.enrolled_from) > getdate(day.business_date):
        access.staff().deny()
    rows = frappe.db.sql("select name, day, site, check_out from `tabRetail Attendance Presence` where employee=%s for update", employee, as_dict=True)
    save = access.service_save if access.service_scope() else access.save
    current = next((r for r in rows if not r.check_out), None)
    if kind == "IN":
        if current:
            frappe.throw(_("This employee is already checked in."))
        if any(r.day == day.name for r in rows):
            frappe.throw(_("A visit is already recorded for this day. Ask the manager to correct it."))
        plan = planned(roster, day)
        p = save(frappe.get_doc(dict(doctype="Retail Attendance Presence", company=site.company,
            site=site.name, employee=employee, day=day.name, in_time=stamp, schedule=json.dumps(plan), version=1)))
        p.check_in = native(p, "IN", stamp, operation).name
        from hrms.hr.retail_attendance import fines
        fines.apply_or_reconcile_fine(site.name, day.business_date, employee, presence=p, day=day)
    elif kind == "OUT":
        if not current or current.day != day.name or current.site != site.name:
            frappe.throw(_("No current check-in was found in this shop."))
        p = frappe.get_doc("Retail Attendance Presence", current.name, for_update=True)
        if stamp < get_datetime(p.in_time):
            frappe.throw(_("Departure must be after arrival."))
        p.check_out = native(p, "OUT", stamp, operation).name
        p.out_time = stamp
        p.version += 1
    else:
        access.staff().deny()
    save(p)
    day.version += 1
    # Private service can invalidate this exact locked day's eligibility revision
    # without granting general Day write rights to Guest.
    if access.service_scope():
        day.db_set("version", day.version)
    else:
        access.save(day)
    return dict(action=kind, saved=True, time=str(stamp))


def kiosk_action(employee, pin, action, request_id, day):
    from hrms.hr.retail_attendance import credentials
    credentials.guard_rate("global", "all", 1000)
    preview = credentials.device()
    active(employee, preview.company)
    dev = credentials.device(lock=True)
    if dev.name != preview.name:
        access.staff().deny()
    cred = credentials.verify_pin(employee, pin, dev.name)
    if cred.company != dev.company or cred.site != dev.site:
        access.staff().deny()
    with access.kiosk_scope(dev, employee, day):
        site = commands.site_doc(dev.site, lock=True)
        roster_for(employee, site)
        if action not in ("IN", "OUT"):
            access.staff().deny()
        digest, receipt, result = commands._request(site, request_id, action, dict(employee=employee, device=dev.name, day=day))
        if receipt:
            if receipt.device != dev.name or receipt.employee != employee:
                access.staff().deny()
            return result
        record = frappe.get_doc("Retail Attendance Day", day, for_update=True)
        if record.site != site.name or record.company != site.company:
            access.staff().deny()
        # Receipt and native log/presence share the HTTP request transaction.
        receipt = access.service_save(frappe.get_doc(dict(doctype="Retail Attendance Operation", company=dev.company,
            site=dev.site, employee=employee, device=dev.name, credential_version=cred.version,
            request_id=request_id, intent_hash=digest, action=action, status="Pending", result="{}")))
        result = write(site, record, employee, action, now_datetime(), receipt.name)
        receipt.status, receipt.result = "Complete", json.dumps(result)
        access.service_save(receipt)
        return result


def kiosk_result(request_id):
    """Device-scoped receipt, no names, credential data, or employee history."""
    from hrms.hr.retail_attendance import credentials
    dev = credentials.device(lock=True)
    try:
        UUID(request_id)
    except (ValueError, TypeError, AttributeError):
        access.staff().deny()
    rows = frappe.db.sql("select result from `tabRetail Attendance Operation` where device=%s and request_id=%s and status='Complete' for update", (dev.name, request_id))
    return dict(found=bool(rows), result=json.loads(rows[0][0]) if rows else None)


def kiosk_context():
    from hrms.hr.retail_attendance import credentials
    dev = credentials.device()
    site = frappe.get_doc("Retail Attendance Site", dev.site)
    day = frappe.get_doc("Retail Attendance Day", site.current_day) if site.current_day else None
    # Explicit minimal projection, scoped to enrolled active employees. Never
    # return images/contact details, schedules, salaries, leave, PIN state or hashes.
    rows = frappe.db.sql("""select e.name, e.employee_name from `tabEmployee` e
        join `tabRetail Attendance Roster` r on r.employee=e.name
        where r.site=%s and r.company=%s and e.company=%s and e.status='Active'
        and r.enrolled_from<=%s and (e.date_of_joining is null or e.date_of_joining<=%s)
        and (e.relieving_date is null or e.relieving_date>=%s) order by e.employee_name""",
        (site.name, site.company, site.company, getdate(), getdate(), getdate()), as_dict=True)
    return dict(shop=site.site_name, device=dev.name, day=day.name if day else None,
        business_date=str(day.business_date) if day else None, open=bool(day and day.status == "Open"), employees=rows)
