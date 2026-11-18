"""Transactional manager commands. No check-in kiosk, fines or payroll writes.

Opening/configuration/schedule commands are atomic. Closing intentionally spans
POST transactions: freeze Closing first, then bounded employee-first batches.
No commits occur inside these services; the request transaction owns each batch.
"""
import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from hrms.hr.retail_attendance import access, rules, schedules

PREFIX = "Retail Attendance "
CLOSE_BATCH_SIZE = 20


def _text(value, maximum=500, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        frappe.throw(_("Enter a short description."))
    return value.strip()


def _version(value, expected):
    if str(value) != str(expected):
        frappe.throw(_("This record changed. Refresh before saving."), frappe.TimestampMismatchError)


def lock_employee(employee, company=None):
    rows = frappe.db.sql("select name, company, status from `tabEmployee` where name=%s for update", employee, as_dict=True)
    if not rows or (company and rows[0].company != company):
        access.staff().deny()
    return rows[0]


def site_doc(site, lock=False):
    doc = frappe.get_doc(PREFIX + "Site", site, for_update=lock)
    ctx = access.service_scope()
    if ctx and ctx["site"] == doc.name and ctx["company"] == doc.company:
        return doc
    access.manager(doc.company)
    doc.check_permission("read")
    return doc


def _request(site, request_id, action, payload):
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        frappe.throw(_("Invalid request identifier."))
    digest = hashlib.sha256(json.dumps(dict(action=action, payload=payload), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    # Caller holds the site lock (or Company for creating a site). Unique request
    # key also rejects cross-site simultaneous re-use without duplicate effects.
    rows = frappe.db.sql("select name from `tabRetail Attendance Operation` where request_id=%s for update", request_id)
    if rows:
        receipt = frappe.get_doc(PREFIX + "Operation", rows[0][0], for_update=True)
        if receipt.owner != frappe.session.user or receipt.site != site.name or receipt.intent_hash != digest:
            access.staff().deny()
        return digest, receipt, json.loads(receipt.result)
    return digest, None, None


def _receipt(site, request_id, digest, action, result, pending=False):
    doc = frappe.get_doc(dict(doctype=PREFIX + "Operation", company=site.company, site=site.name,
        request_id=request_id, intent_hash=digest, action=action, status="Pending" if pending else "Complete",
        result=json.dumps(result, sort_keys=True, default=str)))
    access.save(doc)
    return doc


def configure_site(company, site_name, opens_at, closes_at, request_id, site=None, version=None):
    access.manager(company)
    # This writes a scoped attendance configuration, never the Company itself.
    # Native Site create/write permission supplements manager + Company read.
    name = _text(site_name, 140, True)
    try:
        starts, ends = (str(rules.parse_time(t)) for t in (opens_at, closes_at))
        rules.span(getdate(), starts, ends)
    except (TypeError, ValueError):
        frappe.throw(_("Enter different opening and closing times."))
    if site:
        doc = site_doc(site, lock=True)
        if doc.company != company:
            access.staff().deny()
    else:
        frappe.db.sql("select name from `tabCompany` where name=%s for update", company)
        # Stable site identity permits retry of the creation operation itself.
        try:
            identity = UUID(request_id).hex
        except (ValueError, TypeError, AttributeError):
            frappe.throw(_("Invalid request identifier."))
        target = "attendance-" + identity
        doc = site_doc(target, lock=True) if frappe.db.exists(PREFIX + "Site", target) else frappe.get_doc(dict(
            doctype=PREFIX + "Site", name=target, company=company, version=1, __islocal=1))
    payload = dict(company=company, site_name=name, opens_at=starts, closes_at=ends, site=site, version=version)
    digest, existing_receipt, result = _request(doc, request_id, "Site", payload)
    if result:
        return result
    if not doc.is_new():
        _version(version, doc.version)
        doc.version += 1
    doc.update(dict(site_name=name, opens_at=starts, closes_at=ends))
    access.save(doc, set_name=doc.name if not site else None)
    result = dict(site=doc.name, version=doc.version)
    _receipt(doc, request_id, digest, "Site", result)
    return result


def enroll(site, employee, enrolled_from, request_id):
    doc = site_doc(site)
    emp = access.employee(employee, doc.company, write=True)
    start = getdate(enrolled_from)
    current_employee = lock_employee(employee, doc.company)
    doc = site_doc(site, lock=True)
    digest, existing_receipt, result = _request(doc, request_id, "Enroll", dict(employee=employee, enrolled_from=str(start)))
    if result:
        return result
    if current_employee.status != "Active":
        frappe.throw(_("Select an active employee."))
    if start < getdate():
        frappe.throw(_("Enroll from today or a future date. Historical records are preserved."))
    existing = frappe.db.sql("select name, site from `tabRetail Attendance Roster` where employee=%s for update", employee)
    if existing:
        frappe.throw(_("This employee already has an attendance shop. Open their existing schedule."))
    roster = access.save(frappe.get_doc(dict(doctype=PREFIX + "Roster", company=doc.company, site=site,
        employee=employee, enrolled_from=start, version=0)))
    result = dict(roster=roster.name, version=0)
    _receipt(doc, request_id, digest, "Enroll", result)
    return result


def save_schedule(site, roster, kind, effective_date, mode, weekdays, starts_at, ends_at, reason, version, request_id):
    doc = site_doc(site)
    record = frappe.get_doc(PREFIX + "Roster", roster)
    if record.site != site or record.company != doc.company:
        access.staff().deny()
    access.employee(record.employee, doc.company, write=True)
    lock_employee(record.employee, doc.company)
    doc = site_doc(site, lock=True)
    record = frappe.get_doc(PREFIX + "Roster", roster, for_update=True)
    day = getdate(effective_date)
    if kind not in ("Regular", "Exception") or mode not in (("Shop", "Hours", "Native", "Off") if kind == "Regular" else ("Hours", "Off", "Usual")):
        frappe.throw(_("Choose a supported schedule change."))
    note = _text(reason or "", required=kind == "Exception")
    try:
        week = rules.weekdays(weekdays) if kind == "Regular" and mode not in ("Off", "Shop") else ""
        starts, ends = (str(rules.parse_time(t)) for t in (starts_at, ends_at)) if mode == "Hours" else (None, None)
        if mode == "Hours":
            rules.span(day, starts, ends)
    except (TypeError, ValueError):
        frappe.throw(_("Choose working days and different start and end times."))
    payload = dict(roster=roster, kind=kind, effective_date=str(day), mode=mode, weekdays=week,
        starts_at=starts, ends_at=ends, reason=note, version=version)
    digest, existing_receipt, result = _request(doc, request_id, "Schedule", payload)
    if result:
        return result
    _version(version, record.version)
    if day < getdate(record.enrolled_from):
        frappe.throw(_("The schedule starts before attendance enrollment."))
    # Retroactive schedule changes reconcile attendance fines in P4.
    if mode == "Native":
        ctx = schedules.load_context([record], day, day)
        candidates = [r.shift_type for r in ctx["assignments"].get(record.employee, []) if not r.end_date or getdate(r.end_date) >= day]
        if not candidates and ctx["employees"][record.employee].get("default_shift"):
            candidates = [ctx["employees"][record.employee].default_shift]
        if len(candidates) != 1 or not ctx["shifts"].get(candidates[0]) or ctx["shifts"][candidates[0]].enable_auto_attendance:
            frappe.throw(_("Use one native shift with automatic attendance off, or enter working hours here."))
    row = access.save(frappe.get_doc(dict(doctype=PREFIX + "Schedule", company=doc.company, site=site,
        employee=record.employee, revision=record.version + 1, **{k: v for k, v in payload.items() if k != "version"})))
    record.version += 1
    access.save(record)
    if day <= getdate():
        day_name = frappe.db.get_value(PREFIX + "Day", {"site": site, "business_date": day}, "name")
        has_presence = frappe.db.exists(PREFIX + "Presence", {"employee": record.employee, "day": day_name}) if day_name else False
        has_decision = frappe.db.exists("Retail Attendance Fine Decision", {"employee": record.employee, "business_date": day})
        if has_presence or has_decision:
            from hrms.hr.retail_attendance import fines
            fines.apply_or_reconcile_fine(site, day, record.employee)
    result = dict(schedule=row.name, roster=roster, version=record.version)
    _receipt(doc, request_id, digest, "Schedule", result)
    return result


def open_day(site, business_date, version, request_id):
    doc = site_doc(site, lock=True)
    day = getdate(business_date)
    digest, existing_receipt, result = _request(doc, request_id, "Open", dict(business_date=str(day), version=version))
    if result:
        return result
    _version(version, doc.version)
    if doc.current_day:
        frappe.throw(_("Close the previous attendance day before opening another."))
    if day != getdate():
        frappe.throw(_("Open today's attendance day. A previous open day can be closed using its actual closing time."))
    if frappe.db.exists(PREFIX + "Day", {"site": site, "business_date": day}):
        frappe.throw(_("An attendance day already exists for this date."))
    first, last = rules.span(day, doc.opens_at, doc.closes_at)
    record = access.save(frappe.get_doc(dict(doctype=PREFIX + "Day", company=doc.company, site=site,
        business_date=day, status="Open", opened_at=now_datetime().replace(microsecond=0),
        expected_open=first, expected_close=last, site_version=doc.version, version=1)))
    doc.current_day = record.name
    doc.version += 1
    access.save(doc)
    result = dict(day=record.name, status="Open", version=record.version)
    _receipt(doc, request_id, digest, "Open", result)
    return result


def begin_close(site, day, effective_close, note, version, request_id):
    doc = site_doc(site, lock=True)
    record = frappe.get_doc(PREFIX + "Day", day, for_update=True)
    if record.site != site or record.company != doc.company:
        access.staff().deny()
    note = _text(note or "")
    digest, receipt, result = _request(doc, request_id, "Close", dict(day=day, effective_close=effective_close, note=note, version=version))
    if receipt:
        return dict(result, operation=receipt.name, status=record.status)
    _version(version, record.version)
    if doc.current_day != day or record.status != "Open":
        frappe.throw(_("This attendance day is already closing or closed. Refresh its status."))
    rows = frappe.db.sql("""select employee, in_time, out_time from `tabRetail Attendance Presence`
        where day=%s for update""", day, as_dict=True)
    for row in rows:
        access.employee(row.employee, doc.company, write=True)
    if rows:
        if not frappe.has_permission("Employee Checkin", "create"):
            access.staff().deny()
        access.staff().fields("Employee Checkin", ["employee", "time", "log_type", "skip_auto_attendance"], "write")
    latest = max([get_datetime(r.out_time or r.in_time) for r in rows], default=None)
    try:
        effective = rules.close_time(effective_close, get_datetime(record.opened_at), latest, now_datetime())
    except (TypeError, ValueError):
        frappe.throw(_("Closing time must be between opening and now, and after all recorded arrivals and departures. Correct conflicting attendance first."))
    result = dict(day=day, status="Closing")
    receipt = _receipt(doc, request_id, digest, "Close", result, pending=True)
    record.update(dict(status="Closing", closed_at=effective, close_recorded_at=now_datetime(),
        closed_by=frappe.session.user, close_operation=receipt.name, close_note=note, version=record.version + 1,
        difference_minutes=rules.hours_difference(get_datetime(record.expected_close), effective)))
    access.save(record)
    return dict(result, operation=receipt.name, version=record.version)


def continue_close(site, operation):
    """Retrying the same durable close is safe, including after a lost response.

    No employee locks are acquired while holding the site lock. Lock at most 20
    employee IDs first, in sorted order; then re-read site/day/presences. A blocked
    native OUT leaves Closing visible with the original operation for recovery.
    """
    doc = site_doc(site)
    receipt = frappe.get_doc(PREFIX + "Operation", operation)
    if receipt.site != site or receipt.action != "Close":
        access.staff().deny()
    day_name = json.loads(receipt.result)["day"]
    # A different authorized manager may resume a close, but must have authority
    # over the whole day's employee scope before receiving aggregate progress.
    for name in frappe.get_all(PREFIX + "Presence", filters={"day": day_name}, pluck="employee"):
        access.employee(name, doc.company, write=True)
    remaining = frappe.get_all(PREFIX + "Presence", filters={"day": day_name, "check_out": ["is", "not set"]},
        fields=["name", "employee"], order_by="employee asc, name asc", limit_page_length=CLOSE_BATCH_SIZE)
    for name in sorted({p.employee for p in remaining}):
        access.employee(name, doc.company, write=True)
        lock_employee(name, doc.company)
    doc = site_doc(site, lock=True)
    day = frappe.get_doc(PREFIX + "Day", day_name, for_update=True)
    receipt = frappe.get_doc(PREFIX + "Operation", operation, for_update=True)
    if day.site != site or day.close_operation != operation:
        access.staff().deny()
    if day.status == "Closed":
        return json.loads(receipt.result)
    if day.status != "Closing" or doc.current_day != day.name:
        frappe.throw(_("Refresh the attendance day before continuing closure."))
    day.close_error = None
    from hrms.hr.retail_attendance.events import automatic_checkout
    for candidate in remaining:
        try:
            with access.staff().atomic():
                presence = frappe.get_doc(PREFIX + "Presence", candidate.name, for_update=True)
                if presence.day != day.name or presence.site != site or presence.company != doc.company:
                    access.staff().deny()
                if not presence.check_out:
                    try:
                        automatic_checkout(doc, day, presence, receipt)
                    except (frappe.ValidationError, frappe.PermissionError) as exc:
                        raise CloseBlocked("Permission" if isinstance(exc, frappe.PermissionError) else "Native conflict") from exc
        except CloseBlocked as exc:
            day.close_error = str(exc)
            frappe.clear_messages()
            break
    return _finish_close(doc, day, receipt)


class CloseBlocked(Exception):
    pass


def _finish_close(site, day, receipt):
    # Current locking read, not a repeatable-read snapshot taken before waiting
    # for another closer. Site lock prevents new attendance writes in this day.
    count = len(frappe.db.sql("""select name from `tabRetail Attendance Presence`
        where day=%s and (check_out is null or check_out='') for update""", day.name))
    if count == 0:
        day.status = "Closed"
        day.version += 1
        site.current_day = None
        site.version += 1
        access.save(site)
        receipt.status = "Complete"
        from hrms.hr.retail_attendance import fines
        fines.process_scheduled_absence_fines(site.name, day.business_date)
    result = dict(day=day.name, status=day.status, remaining=count, operation=receipt.name, error=day.close_error)
    access.save(day)
    receipt.result = json.dumps(result)
    access.save(receipt)
    return result
