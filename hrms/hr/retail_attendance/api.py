"""Narrow manager API. All financial and kiosk actions belong to later phases."""
import frappe

from hrms.hr.retail_attendance import access, commands, schedules


@frappe.whitelist(methods=["POST"])
def configure_site(company, site_name, opens_at, closes_at, request_id, site=None, version=None):
    with access.staff().atomic():
        return commands.configure_site(company, site_name, opens_at, closes_at, request_id, site, version)


@frappe.whitelist(methods=["POST"])
def enroll(site, employee, enrolled_from, request_id):
    with access.staff().atomic():
        return commands.enroll(site, employee, enrolled_from, request_id)


@frappe.whitelist(methods=["POST"])
def save_schedule(site, roster, kind, effective_date, mode, version, request_id, weekdays="", starts_at=None, ends_at=None, reason=""):
    with access.staff().atomic():
        return commands.save_schedule(site, roster, kind, effective_date, mode, weekdays, starts_at, ends_at, reason, version, request_id)


@frappe.whitelist(methods=["POST"])
def open_day(site, business_date, version, request_id):
    with access.staff().atomic():
        return commands.open_day(site, business_date, version, request_id)


@frappe.whitelist(methods=["POST"])
def begin_close(site, day, effective_close, version, request_id, note=""):
    with access.staff().atomic():
        return commands.begin_close(site, day, effective_close, note, version, request_id)


@frappe.whitelist(methods=["POST"])
def continue_close(site, operation):
    with access.staff().atomic():
        return commands.continue_close(site, operation)


@frappe.whitelist()
def operation_status(site, request_id):
    """Lost response lookup; never re-execute a different or unknown operation."""
    doc = commands.site_doc(site)
    name = frappe.db.get_value("Retail Attendance Operation", {"site": site, "request_id": request_id, "owner": frappe.session.user})
    if not name:
        return dict(found=False)
    import json
    receipt = frappe.get_doc("Retail Attendance Operation", name)
    return dict(found=True, operation=receipt.name, status=receipt.status, result=json.loads(receipt.result))


@frappe.whitelist()
def get_context(company=None):
    access.manager()
    companies = access.staff().rows("Company", ["name"])
    if not company:
        return dict(companies=[r.name for r in companies], sites=[])
    access.manager(company)
    sites = frappe.get_list("Retail Attendance Site", filters={"company": company}, fields=["name", "site_name", "version", "opens_at", "closes_at"], limit_page_length=0)
    return dict(companies=[r.name for r in companies], sites=sites)


@frappe.whitelist()
def get_board(site, start_date):
    from hrms.hr.retail_attendance.board import get_board as load
    return load(site, start_date)
