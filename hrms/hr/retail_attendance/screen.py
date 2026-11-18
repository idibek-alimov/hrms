"""Manager-opened employee screen. Session-scoped; no device registration.

The durable SQL lock survives cache clearing/restarts. It restricts the whole SID,
including other tabs and direct CRUD/RPC requests, until password verification.
"""
import hashlib
import json
from uuid import UUID

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime
from frappe.utils.password import check_password

from hrms.hr.retail_attendance import access, board, commands, credentials, presence

PREFIX = "hrms.hr.retail_attendance.screen."
ENDPOINTS = {PREFIX + name for name in ("context", "punch", "result", "unlock", "workday", "close_batch", "sign_out")}


def session_key():
    sid = frappe.session.sid
    if not sid or sid == "Guest" or frappe.session.user == "Guest":
        access.staff().deny()
    return hashlib.sha256(sid.encode()).hexdigest()


def current(lock=False):
    if frappe.session.user == "Guest":
        return None
    name = session_key()
    if not frappe.db.exists("Retail Attendance Screen", name):
        return None
    doc = frappe.get_doc("Retail Attendance Screen", name, for_update=lock)
    return doc if doc.locked and doc.manager == frappe.session.user else None


def guard_request():
    """Run after cookie authentication and before any request dispatch."""
    if frappe.session.user == "Guest" or not frappe.db.table_exists("Retail Attendance Screen"):
        return
    doc = current()
    if not doc:
        return
    request = frappe.local.request
    # No cmd tunnelling, alternate API versions, uploads, reports or file access.
    method = request.path.removeprefix("/api/method/") if request.path.startswith("/api/method/") else None
    if method in ENDPOINTS and request.method == "POST" and not frappe.form_dict.get("cmd"):
        return
    if request.path.rstrip("/") == "/attendance-kiosk" and request.method in ("GET", "HEAD") and not frappe.form_dict.get("cmd"):
        return
    if request.method in ("GET", "HEAD") and not request.path.startswith(("/api/", "/private/", "/backups")):
        from werkzeug.exceptions import HTTPException
        from werkzeug.utils import redirect
        raise HTTPException(response=redirect("/attendance-kiosk", code=303))
    frappe.throw(_("Return to management with the manager password first."), frappe.PermissionError)


def require_screen(lock=False):
    credentials.transport()
    doc = current(lock=lock)
    if not doc:
        frappe.throw(_("Open the employee screen from Attendance."), frappe.PermissionError)
    # Recheck current manager/company authority on every command/read.
    access.manager(doc.company)
    return doc


def rate(kind, identity, limit):
    try:
        credentials.guard_rate(kind, identity, limit)
    except frappe.AuthenticationError:
        # AuthenticationError clears the manager's login cookies in Frappe's
        # HTTP handler. A failed employee PIN must never log this screen out.
        frappe.throw(_("Too many attempts. Wait five minutes and try again."), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def start(site=None, attendance_only=0):
    """An explicit manager action; never opens/closes an attendance day."""
    if not site:
        access.manager()
        sites = frappe.get_list("Retail Attendance Site", fields=["name"], limit_page_length=2)
        if len(sites) != 1:
            frappe.throw(_("Select the shop in Attendance first."))
        site = sites[0].name
    shop = commands.site_doc(site, lock=True)
    if frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"):
        frappe.throw(_("Attendance needs a setup without required GPS coordinates. Ask the administrator to review HR settings."))
    key = session_key()
    old = frappe.db.exists("Retail Attendance Screen", key)
    doc = frappe.get_doc("Retail Attendance Screen", key, for_update=True) if old else frappe.get_doc(dict(
        doctype="Retail Attendance Screen", name=key, company=shop.company, site=shop.name, manager=frappe.session.user))
    if old and (doc.manager != frappe.session.user or (doc.locked and doc.site != site)):
        access.staff().deny()
    doc.site, doc.company = shop.name, shop.company
    doc.locked = 1
    doc.attendance_only = int(bool(int(attendance_only))) or int(doc.attendance_only or 0)
    # Private session state is not a user-editable attendance record.
    doc.save(ignore_permissions=True) if old else doc.insert(ignore_permissions=True, set_name=key)
    return dict(url="/attendance-kiosk")


@frappe.whitelist(methods=["POST"])
def context():
    screen = require_screen()
    data = board.get_board(screen.site, str(getdate()), "today")
    day = data["current_day"]
    # Explicit projection: never ship employee documents, salaries, leave details,
    # PIN credentials, schedule history or internal manager operation payloads.
    return dict(shop=data["site"]["site_name"], scope=screen.name, currency=data["currency"],
        attendance_only=bool(screen.attendance_only), today=data["today"], site_version=data["site"]["version"],
        day_info=day,
        server_time=data["server_time"], day=day["name"] if day else None,
        business_date=day["business_date"] if day else None,
        open=bool(day and day["status"] == "Open"), employees=[dict(
            name=row["employee"], employee_name=row["employee_name"],
            state=row["cells"][0]["state"], status=row["cells"][0]["status"],
            starts_at=row["cells"][0]["starts_at"], ends_at=row["cells"][0]["ends_at"],
            actual=[{k: p[k] for k in ("in_time", "out_time", "automatic")} for p in row["cells"][0]["actual"]],
            fine=row["cells"][0].get("fine"),
        ) for row in data["rows"] if row["active"] and row["cells"][0]["state"] not in ("Not enrolled", "Not employed")])


@frappe.whitelist(methods=["POST"])
def punch(employee, pin, action, request_id, day):
    frappe.form_dict.pop("pin", None)
    screen = require_screen()
    rate("global", "all", 1000)
    access.employee(employee, screen.company)
    with access.staff().atomic():
        presence.active(employee, screen.company)
        # Employee -> credential -> site -> day: same order as existing PIN writes.
        try:
            cred = credentials.verify_pin(employee, pin, screen.name)
        except frappe.AuthenticationError:
            frappe.throw(_("PIN is incorrect, unavailable, or temporarily locked. After several attempts wait five minutes."), frappe.ValidationError)
        if cred.site != screen.site or cred.company != screen.company:
            access.staff().deny()
        shop = commands.site_doc(screen.site, lock=True)
        if not access.employee(employee, shop.company).status == "Active":
            access.staff().deny()
        if action not in ("IN", "OUT"):
            access.staff().deny()
        digest, receipt, saved = commands._request(shop, request_id, action,
            dict(employee=employee, screen=screen.name, day=day))
        if receipt:
            return saved
        record = frappe.get_doc("Retail Attendance Day", day, for_update=True)
        if record.site != shop.name or record.company != shop.company:
            access.staff().deny()
        receipt = access.save(frappe.get_doc(dict(doctype="Retail Attendance Operation", company=shop.company,
            site=shop.name, employee=employee, credential_version=cred.version, request_id=request_id,
            intent_hash=digest, action=action, status="Pending", result="{}")))
        # Restrict native permlevel-1 timestamp handling to this exact PIN action.
        with access.kiosk_scope(frappe._dict(site=shop.name, company=shop.company, name=screen.name), employee, day):
            saved = presence.write(shop, record, employee, action, now_datetime(), receipt.name)
        receipt.status, receipt.result = "Complete", json.dumps(saved)
        access.save(receipt)
        return saved


@frappe.whitelist(methods=["POST"])
def result(request_id):
    screen = require_screen()
    try:
        UUID(request_id)
    except (TypeError, ValueError, AttributeError):
        access.staff().deny()
    rows = frappe.get_all("Retail Attendance Operation", filters=dict(site=screen.site,
        owner=screen.manager, request_id=request_id, status="Complete", action=["in", ["IN", "OUT"]]),
        fields=["result", "intent_hash", "employee"])
    if not rows:
        return dict(found=False)
    receipt = rows[0]
    # Match the same session's intention, rather than another screen's operation.
    # The original day is retained by the immutable presence/event linkage.
    operation = frappe.db.get_value("Retail Attendance Operation", {"request_id": request_id}, "name")
    day = frappe.db.get_value("Employee Checkin", {"retail_attendance_operation": operation}, "retail_attendance_day")
    saved = json.loads(receipt.result)
    expected = hashlib.sha256(json.dumps(dict(action=saved["action"], payload=dict(
        employee=receipt.employee, screen=screen.name, day=day)), sort_keys=True, default=str,
        separators=(",", ":")).encode()).hexdigest()
    if expected != receipt.intent_hash:
        access.staff().deny()
    return dict(found=True, result=saved)


@frappe.whitelist(methods=["POST"])
def unlock(password):
    frappe.form_dict.pop("password", None)
    doc = require_screen(lock=True)
    if doc.attendance_only:
        access.staff().deny()
    rate("screen-unlock", doc.manager, 5)
    try:
        check_password(doc.manager, password, delete_tracker_cache=False)
    except frappe.AuthenticationError:
        # Keep the session and lock intact after a wrong password.
        frappe.throw(_("Manager password is incorrect."), frappe.ValidationError)
    doc.db_set("locked", 0)
    return dict(url="/app/staff-attendance")


def verify_manager(password):
    frappe.form_dict.pop("password", None)
    doc = require_screen()
    rate("screen-unlock", doc.manager, 5)
    try:
        check_password(doc.manager, password, delete_tracker_cache=False)
    except frappe.AuthenticationError:
        frappe.throw(_("Manager password is incorrect."), frappe.ValidationError)
    identity = hashlib.sha256(str(doc.manager).encode()).hexdigest()
    frappe.cache.delete_value("attendance-rate:screen-unlock:" + identity)
    return doc


@frappe.whitelist(methods=["POST"])
def workday(password, action, request_id, version, business_date=None, day=None, effective_close=None):
    doc = verify_manager(password)
    with access.staff().atomic():
        if action == "Open":
            return commands.open_day(doc.site, business_date, version, request_id)
        if action == "Close":
            return commands.begin_close(doc.site, day, effective_close, "", version, request_id)
        access.staff().deny()


@frappe.whitelist(methods=["POST"])
def close_batch(password, operation):
    doc = verify_manager(password)
    with access.staff().atomic():
        return commands.continue_close(doc.site, operation)


@frappe.whitelist(methods=["POST"])
def sign_out():
    doc = require_screen()
    url = "/login?redirect-to=" + ("%2Fattendance-kiosk%3Fattendance_only%3D1" if doc.attendance_only else "%2Fattendance-kiosk")
    frappe.local.login_manager.logout()
    return dict(url=url)
