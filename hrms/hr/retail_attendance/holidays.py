"""Explicit shop dates, shared by attendance and native HR/payroll holiday reads."""
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import getdate

from hrms.hr.retail_attendance import access, commands, fines, rules

DOCTYPE = "Retail Attendance Shop Holiday"


def expand_dates(mode, dates=None, from_date=None, to_date=None, weekdays=""):
    try:
        if mode == "Dates":
            values = frappe.parse_json(dates) if isinstance(dates, str) else dates
            if not isinstance(values, list) or not values or len(values) > 366:
                raise ValueError
            result = sorted({rules.parse_date(d) for d in values})
        elif mode in ("Range", "Weekdays"):
            first, last = rules.parse_date(from_date), rules.parse_date(to_date)
            if not 0 <= (last - first).days < 366:
                raise ValueError
            week = set(int(d) for d in rules.weekdays(weekdays).split(",")) if mode == "Weekdays" else set(range(7))
            result = [first + timedelta(days=i) for i in range((last - first).days + 1)
                if (first + timedelta(days=i)).weekday() in week]
        else:
            raise ValueError
        if not result:
            raise ValueError
        return result
    except (TypeError, ValueError):
        frappe.throw(_("Select dates, or a date range of up to one year and the repeating weekdays."))


def employee_holidays(employee, start_date, end_date, only_non_weekly=False):
    """Internal native HR read. Callers retain their existing employee permissions."""
    if not frappe.db.table_exists(DOCTYPE):
        return []
    roster = frappe.db.get_value("Retail Attendance Roster", {"employee": employee},
        ["site", "company", "enrolled_from"], as_dict=True)
    if not roster:
        return []
    filters = {"site": roster.site, "company": roster.company,
        "holiday_date": ["between", [max(getdate(start_date), getdate(roster.enrolled_from)), getdate(end_date)]],
        "active": 1}
    if only_non_weekly:
        filters["weekly_off"] = 0
    return frappe.get_all(DOCTYPE, filters=filters, fields=["holiday_date", "description"], order_by="holiday_date", limit_page_length=0)


@frappe.whitelist()
def get_holidays(site):
    shop = commands.site_doc(site)
    rows = frappe.get_list(DOCTYPE, filters={"site": site, "company": shop.company,
        "holiday_date": [">=", getdate()]}, fields=["name", "holiday_date", "description", "active", "version"],
        order_by="holiday_date", limit_page_length=0)
    return dict(rows=rows, version=shop.version)


@frappe.whitelist(methods=["POST"])
def save_holidays(site, mode, version, request_id, dates=None, from_date=None, to_date=None,
        weekdays="", description="", active=1):
    selected = expand_dates(mode, dates, from_date, to_date, weekdays)
    if selected[0] < getdate():
        frappe.throw(_("Plan holidays from today onward. Historical attendance is preserved."))
    if str(active) not in ("0", "1"):
        access.staff().deny()
    enabled = int(active)
    label = commands._text(description or "", maximum=140, required=bool(enabled))
    with access.staff().atomic():
        shop = commands.site_doc(site)
        employees = frappe.get_all("Retail Attendance Roster", filters={"site": site, "company": shop.company},
            pluck="employee", limit_page_length=0)
        for employee in sorted(employees):
            access.employee(employee, shop.company, write=True)
            commands.lock_employee(employee, shop.company)
        shop = commands.site_doc(site, lock=True)
        payload = dict(dates=[str(d) for d in selected], description=label, active=enabled, version=version, weekly_off=mode == "Weekdays")
        digest, receipt, saved = commands._request(shop, request_id, "ShopHoliday", payload)
        if receipt:
            return saved
        commands._version(version, shop.version)
        for date in selected:
            name = frappe.db.get_value(DOCTYPE, {"site": site, "holiday_date": date})
            doc = frappe.get_doc(DOCTYPE, name, for_update=True) if name else frappe.get_doc(dict(
                doctype=DOCTYPE, site=site, company=shop.company, holiday_date=date, version=0))
            # Like native Holiday List weekly-off generation, do not turn an
            # explicitly named holiday into a weekly off on an overlapping date.
            if name and doc.active and not doc.weekly_off and enabled and mode == "Weekdays":
                continue
            doc.update(dict(active=enabled, weekly_off=int(mode == "Weekdays") if enabled else doc.weekly_off, description=label if enabled else doc.description,
                version=doc.version + 1))
            access.save(doc)
        shop.version += 1
        access.save(shop)
        # Only today can have attendance among these prospective dates. Reuse the
        # established reversal/compensation path; never change recorded punches.
        if getdate() in selected:
            for employee in employees:
                fines.apply_or_reconcile_fine(site, getdate(), employee)
        frappe.cache().delete_value("holidays_between_dates")
        saved = dict(count=len(selected), version=shop.version)
        commands._receipt(shop, request_id, digest, "ShopHoliday", saved)
        return saved
