"""Employee credentials and revocable shop capabilities. Secrets live only in __Auth."""
import re
import secrets
from datetime import timedelta
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime
from frappe.utils.password import check_password, update_password, delete_all_passwords_for

from hrms.hr.retail_attendance import access, commands

COOKIE = "retail_attendance_device"


def pin_context(company, employee):
    access.manager(company)
    emp = access.employee(employee, company, write=True)
    rows = frappe.get_all("Retail Attendance Credential", filters={"employee": employee}, fields=["enabled", "version"])
    roster = frappe.db.get_value("Retail Attendance Roster", {"employee": employee, "company": company}, ["name", "site"], as_dict=True)
    sites = frappe.get_all("Retail Attendance Site", filters={"company": company}, fields=["name", "site_name"], order_by="creation asc")
    site_name = None
    if roster and roster.site:
        site_name = frappe.db.get_value("Retail Attendance Site", roster.site, "site_name")
    return dict(company=company, employee=employee, available=bool(sites), active=emp.status == "Active",
        enabled=bool(rows and rows[0].enabled), version=rows[0].version if rows else 0,
        enrolled=bool(roster), site_name=site_name,
        sites=[dict(name=s.name, label=s.site_name) for s in sites])


def set_pin(company, employee, pin, version, request_id, revoke=False, site=None):
    access.manager(company)
    emp = access.employee(employee, company, write=True)
    commands.lock_employee(employee, company)
    roster = frappe.db.get_value("Retail Attendance Roster", {"employee": employee}, ["site", "company"], as_dict=True)
    if not roster or roster.company != company:
        if revoke:
            return dict(company=company, employee=employee, version=version, enabled=False)
        site_name = site
        if not site_name:
            sites = frappe.get_all("Retail Attendance Site", filters={"company": company}, fields=["name"], order_by="creation asc")
            if not sites:
                frappe.throw(_("Add the employee to an attendance shop first."))
            site_name = sites[0].name
        target_site = commands.site_doc(site_name)
        if target_site.company != company:
            access.staff().deny()
        enroll_id = str(uuid4())
        commands.enroll(site_name, employee, frappe.utils.getdate(), enroll_id)
        roster = frappe.db.get_value("Retail Attendance Roster", {"employee": employee}, ["site", "company"], as_dict=True)
    site = commands.site_doc(roster.site, lock=True)
    digest, receipt, result = commands._request(site, request_id, "PIN", dict(employee=employee, version=version, revoke=bool(revoke)))
    if receipt:
        return result  # Deliberately never hash or log low-entropy PIN intent.
    name = frappe.db.get_value("Retail Attendance Credential", {"employee": employee})
    doc = frappe.get_doc("Retail Attendance Credential", name, for_update=True) if name else frappe.get_doc(dict(
        doctype="Retail Attendance Credential", company=company, site=site.name, employee=employee, version=0))
    commands._version(version, doc.version)
    if not revoke and (emp.status != "Active" or not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,8}", pin)):
        frappe.throw(_("Enter an attendance PIN of 4 to 8 digits for an active employee."))
    doc.version += 1
    doc.enabled = int(not revoke)
    access.save(doc)
    if revoke:
        delete_all_passwords_for(doc.doctype, doc.name)
    else:
        update_password(doc.name, pin, doctype=doc.doctype, fieldname="pin")
    result = dict(company=company, employee=employee, version=doc.version, enabled=bool(doc.enabled))
    commands._receipt(site, request_id, digest, "PIN", result)
    return result


def provision(site, device_name, request_id):
    site = commands.site_doc(site, lock=True)
    if frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"):
        frappe.throw(_("Attendance kiosks need a setup without required GPS coordinates. Ask the administrator to review HR settings."))
    label = commands._text(device_name, 100, True)
    digest, receipt, result = commands._request(site, request_id, "Device", dict(device_name=label))
    if receipt:
        return dict(result, pairing_code=None)  # Lost one-time code: revoke and create another device.
    doc = access.save(frappe.get_doc(dict(doctype="Retail Attendance Device", company=site.company,
        site=site.name, device_name=label, enabled=1, version=1, paired=0,
        expires_at=now_datetime() + timedelta(minutes=10))))
    secret = secrets.token_urlsafe(32)
    update_password(doc.name, secret, doctype=doc.doctype, fieldname="token")
    result = dict(device=doc.name, site=site.name, version=doc.version)
    commands._receipt(site, request_id, digest, "Device", result)
    return dict(result, pairing_code=doc.name + "." + secret)


def revoke_device(site, device, version, request_id):
    # Device -> site is also used by employee commands and pairing.
    access.manager()
    doc = frappe.get_doc("Retail Attendance Device", device, for_update=True)
    current = commands.site_doc(site, lock=True)
    if doc.site != site or doc.company != current.company:
        access.staff().deny()
    digest, receipt, result = commands._request(current, request_id, "Revoke", dict(device=device, version=version))
    if receipt:
        return result
    commands._version(version, doc.version)
    doc.enabled = 0
    doc.version += 1
    access.save(doc)
    delete_all_passwords_for(doc.doctype, doc.name)
    result = dict(device=device, revoked=True, version=doc.version)
    commands._receipt(current, request_id, digest, "Revoke", result)
    return result


def guard_rate(kind, identity, limit, seconds=300):
    """Atomic Redis counters survive failed SQL transactions. Fail closed on outage."""
    import hashlib
    key = frappe.cache.make_key("attendance-rate:" + kind + ":" + hashlib.sha256(str(identity).encode()).hexdigest())
    try:
        count = frappe.cache.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n", 1, key, seconds)
    except Exception:
        frappe.throw(_("Attendance verification is unavailable. Try again later."))
    if count > limit:
        frappe.throw(_("Too many attempts. Wait five minutes and try again."), frappe.AuthenticationError)


def transport():
    request = getattr(frappe.local, "request", None)
    if not request:
        access.staff().deny()
    # Custom header is unavailable to cross-origin HTML forms; Origin must also
    # match. No CORS relaxation or general guest CRUD is introduced.
    from urllib.parse import urlsplit
    origin = request.headers.get("Origin")
    if request.headers.get("X-Retail-Attendance") != "1" or not origin or urlsplit(origin).netloc != request.host or urlsplit(origin).scheme != request.scheme:
        access.staff().deny()
    # HTTP is restricted to this local development hostname/loopback.
    if request.scheme != "https" and request.host.split(":")[0] not in ("radolfa.localhost", "localhost", "127.0.0.1"):
        access.staff().deny()


def cookie_token():
    return frappe.local.request.cookies.get(COOKIE, "")


def device(token=None, lock=False, pairing=False):
    raw = token if token is not None else cookie_token()
    if not isinstance(raw, str) or len(raw) > 160 or "." not in raw:
        access.staff().deny()
    name, secret = raw.split(".", 1)
    if not frappe.db.exists("Retail Attendance Device", name):
        access.staff().deny()
    doc = frappe.get_doc("Retail Attendance Device", name, for_update=lock)
    if not doc.enabled or bool(doc.paired) == bool(pairing) or get_datetime(doc.expires_at) <= now_datetime():
        access.staff().deny()
    try:
        check_password(name, secret, doctype=doc.doctype, fieldname="token", delete_tracker_cache=False)
    except frappe.AuthenticationError:
        access.staff().deny()
    site = frappe.get_doc("Retail Attendance Site", doc.site)
    if site.company != doc.company:
        access.staff().deny()
    return doc


def pair(pairing_code):
    guard_rate("pair", getattr(frappe.local, "request_ip", "unknown"), 20)
    doc = device(pairing_code, lock=True, pairing=True)
    secret = secrets.token_urlsafe(32)
    # Exact capability rotation, no manager login or generalized document write.
    doc.db_set(dict(paired=1, expires_at=now_datetime() + timedelta(days=90)))
    update_password(doc.name, secret, doctype=doc.doctype, fieldname="token")
    frappe.local.cookie_manager.set_cookie(COOKIE, doc.name + "." + secret,
        httponly=True, samesite="Strict", max_age=90 * 86400)
    return dict(paired=True)


def verify_pin(employee, pin, device_name):
    guard_rate("device", device_name, 60)
    guard_rate("employee", employee, 5)
    name = frappe.db.get_value("Retail Attendance Credential", {"employee": employee, "enabled": 1})
    if not name or not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,8}", pin):
        frappe.throw(_("Attendance PIN is incorrect or unavailable."), frappe.AuthenticationError)
    doc = frappe.get_doc("Retail Attendance Credential", name, for_update=True)
    try:
        check_password(name, pin, doctype=doc.doctype, fieldname="pin", delete_tracker_cache=False)
    except frappe.AuthenticationError:
        frappe.throw(_("Attendance PIN is incorrect or unavailable."), frappe.AuthenticationError)
    return doc
