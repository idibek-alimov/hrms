"""Scoped manager authority; custom documents are writable only by commands."""
from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _
from frappe.model.document import Document

_writing = ContextVar("retail_attendance_writer", default=False)
_service = ContextVar("retail_attendance_service", default=None)
_document = ContextVar("retail_attendance_exact_document", default=None)


@contextmanager
def kiosk_scope(device, employee, day):
    """Private capability after current device + PIN checks, never a Desk role."""
    token = _service.set(dict(site=device.site, company=device.company, device=device.name,
        employee=employee, day=day))
    try:
        yield
    finally:
        _service.reset(token)


def service_scope():
    return _service.get()


def exact_permission(doc, ptype):
    return ptype in ("create", "write") and _document.get() is doc


def service_save(doc):
    ctx = _service.get()
    if not ctx or doc.doctype not in ("Retail Attendance Presence", "Retail Attendance Operation", "Employee Checkin", "Retail Attendance Fine Decision"):
        staff().deny()
    if doc.doctype == "Employee Checkin":
        from hrms.hr.retail_attendance.events import event_matches
        if not event_matches(doc):
            staff().deny()
        # Native `time` is permlevel 1, so Frappe would otherwise reset it to Now
        # for the PIN-authenticated service. This exact new document was matched to
        # the locked presence/day/time context above; no other record is affected.
        doc.flags.ignore_permissions = True
    elif doc.site != ctx["site"] or doc.company != ctx["company"] or doc.employee != ctx["employee"]:
        staff().deny()
    token = _document.set(doc)
    try:
        with writing():
            return doc.insert() if doc.is_new() else doc.save()
    finally:
        _document.reset(token)


def staff():
    from erpnext.setup import staff_access
    return staff_access


def manager(company=None):
    staff().require_manager()
    if company:
        return staff().company_doc(company)


def employee(name, company, write=False):
    return staff().employee_doc(name, company, ptype="write" if write else "read", sensitive=False)


@contextmanager
def writing():
    token = _writing.set(True)
    try:
        yield
    finally:
        _writing.reset(token)


def save(doc, set_name=None):
    """Use ordinary native create/write rights plus controller capability guards."""
    with writing():
        return doc.insert(set_name=set_name) if doc.is_new() else doc.save()


class ManagedRecord(Document):
    def check_permission(self, permtype="read", permlevel=None):
        if exact_permission(self, permtype):
            return
        return super().check_permission(permtype, permlevel)

    def run_before_save_methods(self):
        if not _writing.get():
            frappe.throw(_("Use the Attendance page to change this record."), frappe.PermissionError)
        if not exact_permission(self, "write"):
            manager(self.company)
            if self.get("employee"):
                employee(self.employee, self.company, write=True)
        if not self.is_new():
            old = frappe.get_doc(self.doctype, self.name)
            if self.doctype in ("Retail Attendance Schedule", "Retail Attendance Correction") or any(
                old.get(f) != self.get(f) for f in ("company", "employee", "site", "roster")
            ):
                frappe.throw(_("Attendance history and its scope cannot be reassigned."))
        return super().run_before_save_methods()

    def on_trash(self):
        frappe.throw(_("Attendance history cannot be deleted."), frappe.PermissionError)

    def before_rename(self, *args):
        frappe.throw(_("Attendance history cannot be renamed."), frappe.PermissionError)


def has_permission(doc, user=None, ptype=None):
    user = user or frappe.session.user
    if user != "Administrator" and not set(frappe.get_roles(user)) & {"Retail Manager", "Retail Owner", "System Manager"}:
        return False
    if ptype in ("delete", "cancel", "amend", "share", "submit"):
        return False
    if ptype in ("write", "create") and not _writing.get():
        return False
    if not doc.get("company"):
        return None if ptype == "create" else False
    if not frappe.has_permission("Company", "read", doc=frappe.get_doc("Company", doc.company), user=user):
        return False
    if doc.get("employee"):
        emp = frappe.get_doc("Employee", doc.employee)
        if emp.company != doc.company or not frappe.has_permission("Employee", "read", doc=emp, user=user):
            return False
    return None


def query_conditions(user=None, doctype=None):
    user = user or frappe.session.user
    if user != "Administrator" and not set(frappe.get_roles(user)) & {"Retail Manager", "Retail Owner", "System Manager"}:
        return "1=0"
    companies = frappe.get_list("Company", pluck="name", user=user, limit_page_length=0)
    if not companies:
        return "1=0"
    table = "`tab" + doctype + "`"
    result = table + ".company in (" + ",".join(frappe.db.escape(c) for c in companies) + ")"
    if doctype in ("Retail Attendance Roster", "Retail Attendance Schedule", "Retail Attendance Presence"):
        employees = frappe.get_list("Employee", filters={"company": ["in", companies]}, pluck="name", user=user, limit_page_length=0)
        if not employees:
            return "1=0"
        result += " and " + table + ".employee in (" + ",".join(frappe.db.escape(e) for e in employees) + ")"
    return result
