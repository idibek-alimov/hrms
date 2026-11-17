"""Native event provenance and bounded day-close checkout.

P3 will add PIN-authorized arrival/departure commands. No public arrival endpoint
exists in P2. All managed native writes require an exact private event context.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json

import frappe
from frappe import _
from frappe.utils import get_datetime

from hrms.hr.retail_attendance import access, schedules

_event = ContextVar("retail_attendance_native_event", default=None)


@contextmanager
def native_event(presence, log_type, timestamp):
    context = dict(presence=presence.name, employee=presence.employee, day=presence.day,
        log_type=log_type, time=get_datetime(timestamp), schedule=json.loads(presence.schedule))
    token = _event.set(context)
    try:
        yield
    finally:
        _event.reset(token)


def guard_native(doc, method=None):
    """Managed events stay immutable even through native CRUD/import APIs."""
    managed = doc.get("retail_attendance_presence")
    if not doc.is_new():
        old = frappe.db.get_value("Employee Checkin", doc.name, ["retail_attendance_presence", "employee", "time"], as_dict=True)
        if old and old.retail_attendance_presence:
            frappe.throw(_("Use attendance correction to change a managed event."), frappe.PermissionError)
        if old and schedules.is_managed(old.employee, get_datetime(old.time).date()):
            frappe.throw(_("Review this event through Attendance before changing it."), frappe.PermissionError)
    ctx = _event.get()
    timestamp = get_datetime(doc.time)
    if managed:
        if not ctx or any((managed != ctx["presence"], doc.employee != ctx["employee"],
            doc.log_type != ctx["log_type"], timestamp != ctx["time"], doc.get("retail_attendance_day") != ctx["day"])):
            frappe.throw(_("Managed check-ins require an attendance operation."), frappe.PermissionError)
        if doc.log_type == "IN":
            day = frappe.get_doc("Retail Attendance Day", ctx["day"])
            if day.status != "Open":
                frappe.throw(_("The attendance day is closing or closed."))
        doc.skip_auto_attendance = 1
    elif schedules.is_managed(doc.employee, timestamp.date()):
        frappe.throw(_("Use the employee attendance screen for this employee."), frappe.PermissionError)


def managed_shift(doc):
    """Called only after the guard; retain frozen association beyond shift buffers."""
    ctx = _event.get()
    if not doc.get("retail_attendance_presence") or not ctx:
        return False
    planned = ctx["schedule"]
    doc.shift = planned.get("shift")
    doc.shift_start = planned.get("starts_at")
    doc.shift_end = planned.get("ends_at")
    doc.shift_actual_start = doc.shift_start
    doc.shift_actual_end = doc.shift_end
    doc.offshift = 0 if doc.shift else 1
    return True


def automatic_checkout(site, day, presence, receipt):
    access.employee(presence.employee, site.company, write=True)
    if get_datetime(presence.in_time) > get_datetime(day.closed_at):
        frappe.throw(_("An arrival is later than closing. Correct the attendance first."))
    key = "attendance-out-" + hashlib.sha256((receipt.name + ":" + presence.name).encode()).hexdigest()[:32]
    # Presence + native OUT + receipt update share a transaction. The native name
    # is stable as extra protection; a conflicting event is review, never overwrite.
    if frappe.db.exists("Employee Checkin", key):
        frappe.throw(_("A checkout source conflicts with this presence. Review attendance."))
    out = frappe.get_doc(dict(doctype="Employee Checkin", employee=presence.employee,
        log_type="OUT", time=day.closed_at, skip_auto_attendance=1,
        retail_attendance_presence=presence.name, retail_attendance_day=day.name,
        retail_attendance_automatic=1, retail_attendance_operation=receipt.name))
    with native_event(presence, "OUT", day.closed_at):
        out.insert(set_name=key)
    presence.update(dict(check_out=out.name, out_time=day.closed_at, automatic_checkout=1,
        close_operation=receipt.name, version=presence.version + 1))
    access.save(presence)
