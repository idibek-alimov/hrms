"""Reasoned, revision-checked correction; original native events are retained."""
import json

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from hrms.hr.retail_attendance import access, commands, events, presence


def snapshot(p):
    return {f: str(p.get(f)) if p.get(f) is not None else None for f in
        ("check_in", "in_time", "check_out", "out_time", "automatic_checkout", "version")}


def correct(site, day, employee, in_time, out_time, reason, version, request_id):
    preview = commands.site_doc(site)
    access.employee(employee, preview.company, write=True)
    presence.active(employee, preview.company)
    doc = commands.site_doc(site, lock=True)
    record = frappe.get_doc("Retail Attendance Day", day, for_update=True)
    if record.site != site or record.company != doc.company:
        access.staff().deny()
    note = commands._text(reason, required=True)
    starts = get_datetime(in_time).replace(microsecond=0)
    ends = get_datetime(out_time).replace(microsecond=0) if out_time else None
    digest, receipt, result = commands._request(doc, request_id, "Correction", dict(day=day, employee=employee,
        in_time=str(starts), out_time=str(ends) if ends else None, reason=note, version=version))
    if receipt:
        return result
    if record.status == "Closing":
        frappe.throw(_("Finish closing this attendance day before correcting it."))
    if starts < get_datetime(record.opened_at) or starts > now_datetime() or (ends and (ends < starts or ends > now_datetime())):
        frappe.throw(_("Use arrival and departure times between day opening and now, with departure after arrival."))
    if record.status == "Closed" and (not ends or ends > get_datetime(record.closed_at)):
        frappe.throw(_("A closed attendance day needs a departure no later than its closing time."))
    roster = presence.roster_for(employee, doc)
    if getdate(roster.enrolled_from) > getdate(record.business_date):
        access.staff().deny()
    # P4 owns summary/payroll reconciliation. Do not silently diverge from an
    # existing native summary while this phase has no such bridge.
    if frappe.db.exists("Attendance", {"employee": employee, "attendance_date": record.business_date, "docstatus": ["!=", 2]}):
        frappe.throw(_("A native attendance summary already exists. Review it before correcting this day."))
    rows = frappe.db.sql("select name, day, check_out from `tabRetail Attendance Presence` where employee=%s for update", employee, as_dict=True)
    if any(r.day != day and not r.check_out for r in rows):
        frappe.throw(_("Close the employee's other attendance visit first."))
    same = [r for r in rows if r.day == day]
    if len(same) > 1:
        frappe.throw(_("Multiple visits need review before correction."))
    p = frappe.get_doc("Retail Attendance Presence", same[0].name, for_update=True) if same else None
    commands._version(version, p.version if p else 0)
    if p and p.check_out and not ends:
        frappe.throw(_("Recorded departure cannot be removed. Enter its corrected time."))
    before = snapshot(p) if p else {}
    if not p:
        p = access.save(frappe.get_doc(dict(doctype="Retail Attendance Presence", company=doc.company,
            site=site, employee=employee, day=day, in_time=starts,
            schedule=json.dumps(presence.planned(roster, record)), version=1)))
    receipt = commands._receipt(doc, request_id, digest, "Correction", {}, pending=True)
    audit = access.save(frappe.get_doc(dict(doctype="Retail Attendance Correction", company=doc.company,
        site=site, employee=employee, day=day, presence=p.name, operation=receipt.name,
        reason=note, before_state=json.dumps(before))))
    with events.correction_context():
        for kind, stamp, link, time_field in (("IN", starts, "check_in", "in_time"), ("OUT", ends, "check_out", "out_time")):
            if not stamp:
                continue
            old = p.get(link)
            if old and get_datetime(p.get(time_field)) == stamp:
                continue
            new = presence.native(p, kind, stamp, receipt.name, audit.name)
            if old:
                # Only supersession metadata changes; original event/time/actor
                # stay intact. Native auto attendance skips all managed logs.
                frappe.db.set_value("Employee Checkin", old, "retail_attendance_superseded_by", new.name)
            p.set(link, new.name)
            p.set(time_field, stamp)
    if ends and (not before or before.get("out_time") != str(ends)):
        p.automatic_checkout = 0
    if same:
        p.version += 1
    access.save(p)
    # New audit is finalized in the same transaction, then immutable forever.
    audit.db_set("after_state", json.dumps(snapshot(p)))
    record.version += 1
    access.save(record)
    from hrms.hr.retail_attendance import fines
    fines.reconcile_on_correction(site, record, employee, p)
    result = dict(saved=True, presence=p.name, version=p.version, correction=audit.name)
    receipt.status, receipt.result = "Complete", json.dumps(result)
    access.save(receipt)
    return result
