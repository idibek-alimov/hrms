"""Scoped manager authority; custom documents are writable only by commands."""
from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _
from frappe.model.document import Document

_writing = ContextVar("retail_attendance_writer", default=False)


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
    def run_before_save_methods(self):
        if not _writing.get():
            frappe.throw(_("Use the Attendance page to change this record."), frappe.PermissionError)
        manager(self.company)
        if self.get("employee"):
            employee(self.employee, self.company, write=True)
        if not self.is_new():
            old = frappe.get_doc(self.doctype, self.name)
            if self.doctype in ("Retail Attendance Schedule",) or any(
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
