"""Scoped manager commands and separate capability-checked kiosk transport."""
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
def open_day(site, business_date, version, request_id, password=None):
    _workday_password(password)
    with access.staff().atomic():
        return commands.open_day(site, business_date, version, request_id)


@frappe.whitelist(methods=["POST"])
def begin_close(site, day, effective_close, version, request_id, note="", password=None):
    _workday_password(password)
    with access.staff().atomic():
        return commands.begin_close(site, day, effective_close, note, version, request_id)


@frappe.whitelist(methods=["POST"])
def continue_close(site, operation, password=None):
    _workday_password(password)
    with access.staff().atomic():
        return commands.continue_close(site, operation)


def _workday_password(password):
    from hrms.hr.retail_attendance import screen
    from frappe.utils.password import check_password
    access.manager()
    frappe.form_dict.pop("password", None)
    screen.rate("screen-unlock", frappe.session.user, 5)
    try:
        check_password(frappe.session.user, password or "", delete_tracker_cache=False)
    except frappe.AuthenticationError:
        frappe.throw("Manager password is incorrect.", frappe.ValidationError)


@frappe.whitelist()
def operation_status(site, request_id):
    """Lost response lookup; never re-execute a different or unknown operation."""
    access.manager()
    if not frappe.db.exists("Retail Attendance Site", site):
        return dict(found=False)
    commands.site_doc(site)
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
def get_board(site, start_date, view="week"):
    from hrms.hr.retail_attendance.board import get_board as load
    return load(site, start_date, view)


@frappe.whitelist()
def get_day_history(site, day):
    from hrms.hr.retail_attendance import board
    shop = commands.site_doc(site)
    record = frappe.get_doc("Retail Attendance Day", day)
    record.check_permission("read")
    if record.site != site or record.company != shop.company:
        access.staff().deny()
    data = board.get_board(site, str(record.business_date), "day")
    return dict(rows=data["rows"], currency=data["currency"])


@frappe.whitelist()
def get_schedule_history(site, roster):
    doc = commands.site_doc(site)
    record = frappe.get_doc("Retail Attendance Roster", roster)
    if record.site != site or record.company != doc.company:
        access.staff().deny()
    access.employee(record.employee, doc.company)
    rows = frappe.get_list("Retail Attendance Schedule", filters={"site": site, "roster": roster},
        fields=["revision", "kind", "effective_date", "mode", "weekdays", "starts_at", "ends_at", "reason", "owner", "creation"],
        order_by="revision desc", limit_page_length=100)
    from hrms.hr.retail_attendance.rules import parse_time
    for row in rows:
        row.starts_at = str(parse_time(row.starts_at)) if row.starts_at is not None else None
        row.ends_at = str(parse_time(row.ends_at)) if row.ends_at is not None else None
    return rows


@frappe.whitelist(methods=["POST"])
def provision_device(site, device_name, request_id):
    from hrms.hr.retail_attendance.credentials import provision
    with access.staff().atomic():
        return provision(site, device_name, request_id)


@frappe.whitelist()
def get_devices(site):
    commands.site_doc(site)
    return frappe.get_list("Retail Attendance Device", filters={"site": site},
        fields=["name", "device_name", "enabled", "paired", "expires_at", "version"], limit_page_length=100)


@frappe.whitelist(methods=["POST"])
def revoke_device(site, device, version, request_id):
    from hrms.hr.retail_attendance.credentials import revoke_device as revoke
    with access.staff().atomic():
        return revoke(site, device, version, request_id)


@frappe.whitelist(methods=["POST"])
def correct_presence(site, day, employee, in_time, reason, version, request_id, out_time=None):
    from hrms.hr.retail_attendance.corrections import correct
    with access.staff().atomic():
        return correct(site, day, employee, in_time, out_time, reason, version, request_id)


@frappe.whitelist()
def get_presence_history(site, day, employee):
    doc = commands.site_doc(site)
    access.employee(employee, doc.company)
    record = frappe.get_doc("Retail Attendance Day", day)
    if record.site != site:
        access.staff().deny()
    return frappe.get_list("Retail Attendance Correction", filters={"site": site, "day": day, "employee": employee},
        fields=["reason", "before_state", "after_state", "owner", "creation"], order_by="creation desc", limit_page_length=100)


@frappe.whitelist(methods=["POST"])
def save_fine_rule(site, effective_date, enabled, absence_amount, late_brackets, version, request_id, reason=""):
    from hrms.hr.retail_attendance.fines import save_fine_rule as save_rule
    with access.staff().atomic():
        return save_rule(site, effective_date, enabled, absence_amount, late_brackets, reason, version, request_id)


@frappe.whitelist()
def get_fine_rule(site, work_date=None):
    from hrms.hr.retail_attendance.fines import get_effective_rule
    commands.site_doc(site)
    if work_date:
        return get_effective_rule(site, work_date)
    # Include a scheduled revision in the editor so saving a future rule does not
    # leave the manager looking at an older rule or a stale revision counter.
    latest = frappe.get_all("Retail Attendance Fine Rule", filters={"site": site},
        fields=["effective_date", "revision"], order_by="revision desc", limit_page_length=1)
    if not latest:
        return None
    rule = get_effective_rule(site, latest[0].effective_date)
    rule["latest_revision"] = latest[0].revision
    return rule


@frappe.whitelist()
def get_fine_history(site, employee=None, month=None):
    doc = commands.site_doc(site)
    filters = {"site": site}
    allowed_employees = access.staff().rows("Employee", ["name"], filters={"company": doc.company})
    if not allowed_employees:
        return []
    filters["employee"] = ["in", [r.name for r in allowed_employees]]
    if employee:
        access.employee(employee, doc.company)
        filters["employee"] = employee
    if month:
        filters["business_date"] = ["between", [f"{month}-01", f"{month}-31"]]
    return frappe.get_list(
        "Retail Attendance Fine Decision",
        filters=filters,
        fields=["name", "employee", "business_date", "kind", "late_minutes", "amount", "status", "cancellation_reason", "creation"],
        order_by="business_date desc, creation desc",
        limit_page_length=100,
    )


def _kiosk_guard():
    from hrms.hr.retail_attendance.credentials import transport, guard_rate
    transport()
    guard_rate("transport", getattr(frappe.local, "request_ip", "unknown"), 300)
    frappe.local.response["headers"] = {"Cache-Control": "no-store"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def pair_device(pairing_code):
    from hrms.hr.retail_attendance.credentials import pair
    _kiosk_guard()
    frappe.form_dict.pop("pairing_code", None)
    with access.staff().atomic():
        return pair(pairing_code)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def kiosk_context():
    from hrms.hr.retail_attendance.presence import kiosk_context as context
    _kiosk_guard()
    return context()


@frappe.whitelist(allow_guest=True, methods=["POST"])
def kiosk_action(employee, pin, action, request_id, day):
    from hrms.hr.retail_attendance.presence import kiosk_action as action_command
    _kiosk_guard()
    frappe.form_dict.pop("pin", None)
    with access.staff().atomic():
        return action_command(employee, pin, action, request_id, day)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def kiosk_result(request_id):
    from hrms.hr.retail_attendance.presence import kiosk_result as result
    _kiosk_guard()
    return result(request_id)
