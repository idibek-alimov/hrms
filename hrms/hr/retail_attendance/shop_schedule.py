"""Dated shop default. Individual overrides and history remain authoritative."""
import frappe
from frappe import _
from frappe.utils import getdate

from hrms.hr.retail_attendance import access, commands, fines, rules


def current(site, work_date=None):
    rows = frappe.get_all("Retail Attendance Shop Schedule", filters={"site": site,
        "effective_date": ["<=", getdate(work_date)]}, fields=["name", "effective_date", "weekdays", "starts_at", "ends_at", "revision"],
        order_by="effective_date desc, revision desc", limit_page_length=1)
    if not rows:
        return None
    row = rows[0]
    row.starts_at, row.ends_at = str(rules.parse_time(row.starts_at)), str(rules.parse_time(row.ends_at))
    return row


@frappe.whitelist()
def get_schedule(site):
    commands.site_doc(site)
    latest = frappe.db.get_value("Retail Attendance Shop Schedule", {"site": site}, "max(revision)") or 0
    return dict(current=current(site), version=latest)


@frappe.whitelist(methods=["POST"])
def save_schedule(site, effective_date, weekdays, starts_at, ends_at, version, request_id, reason=""):
    with access.staff().atomic():
        # Match the established Employee -> Site lock ordering for reconciliation.
        shop = commands.site_doc(site)
        employees = frappe.get_all("Retail Attendance Roster", filters={"site": site, "company": shop.company}, pluck="employee", limit_page_length=0)
        for employee in sorted(employees):
            access.employee(employee, shop.company, write=True)
            commands.lock_employee(employee, shop.company)
        shop = commands.site_doc(site, lock=True)
        try:
            week = rules.weekdays(weekdays)
            date = getdate(effective_date)
            rules.span(date, starts_at, ends_at)
            first, last = str(rules.parse_time(starts_at)), str(rules.parse_time(ends_at))
        except (TypeError, ValueError):
            frappe.throw(_("Choose working days and different start and end times."))
        note = commands._text(reason or "")
        payload = dict(effective_date=str(date), weekdays=week, starts_at=first, ends_at=last, version=version, reason=note)
        digest, receipt, saved = commands._request(shop, request_id, "ShopSchedule", payload)
        if receipt:
            return saved
        latest = frappe.db.get_value("Retail Attendance Shop Schedule", {"site": site}, "max(revision)") or 0
        commands._version(version, latest)
        row = access.save(frappe.get_doc(dict(doctype="Retail Attendance Shop Schedule", site=site, company=shop.company,
            effective_date=date, weekdays=week, starts_at=first, ends_at=last, revision=latest + 1, reason=note)))
        # Reconcile existing affected days through the original fine/payroll service.
        days = frappe.get_all("Retail Attendance Day", filters={"site": site, "business_date": ["between", [date, getdate()]]},
            fields=["business_date"], limit_page_length=0) if date <= getdate() else []
        for day in days:
            for employee in employees:
                fines.apply_or_reconcile_fine(site, day.business_date, employee)
        saved = dict(schedule=row.name, revision=row.revision)
        commands._receipt(shop, request_id, digest, "ShopSchedule", saved)
        return saved
