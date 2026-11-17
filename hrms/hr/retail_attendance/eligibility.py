# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate, now_datetime


def get_eligible_employees(site_name: str, company: str | None = None) -> list[str]:
    """Return employee IDs currently checked in on an open attendance day at the site."""
    if not site_name or not frappe.db.exists("Retail Attendance Site", site_name):
        return []

    site = frappe.get_doc("Retail Attendance Site", site_name)
    if company and site.company != company:
        return []

    if not site.current_day:
        return []

    day = frappe.get_doc("Retail Attendance Day", site.current_day)
    if day.status != "Open":
        return []

    today = getdate(day.business_date)
    rows = frappe.db.sql(
        """
        select e.name
        from `tabEmployee` e
        join `tabRetail Attendance Roster` r on r.employee = e.name and r.site = %s and r.company = %s
        join `tabRetail Attendance Presence` p on p.employee = e.name and p.day = %s and p.site = %s
        where e.company = %s and e.status = 'Active'
          and r.enrolled_from <= %s
          and (e.date_of_joining is null or e.date_of_joining <= %s)
          and (e.relieving_date is null or e.relieving_date >= %s)
          and p.in_time is not null
          and p.check_out is null
          and p.out_time is null
        order by e.employee_name asc
        """,
        (site.name, site.company, day.name, site.name, site.company, today, today, today),
        as_dict=True,
    )
    return [r.name for r in rows]


def get_eligible_salespersons(pos_profile_doc, return_meta: bool = False):
    """Return eligible sales persons according to POS Profile attendance enforcement."""
    if isinstance(pos_profile_doc, str):
        pos_profile_doc = frappe.get_cached_doc("POS Profile", pos_profile_doc)
    elif isinstance(pos_profile_doc, dict):
        # ensure dict has access to helper methods or convert
        pass

    if not pos_profile_doc:
        if return_meta:
            return {"status": "unavailable", "enforced": False, "sales_persons": []}
        return []

    enforce = cint(pos_profile_doc.get("posa_enforce_attendance"))
    allowed_configured = [
        d.get("sales_person")
        for d in pos_profile_doc.get("posa_sales_persons", [])
        if d.get("sales_person")
    ]

    if not enforce:
        filters = {"enabled": 1}
        if allowed_configured:
            filters["name"] = ["in", allowed_configured]
        sales_persons = frappe.get_all(
            "Sales Person",
            filters=filters,
            fields=["name", "sales_person_name", "employee"],
            order_by="sales_person_name asc, name asc",
            limit_page_length=100000,
        )
        if return_meta:
            return {"status": "disabled", "enforced": False, "sales_persons": sales_persons}
        return sales_persons

    site_name = pos_profile_doc.get("posa_attendance_site")
    if not site_name or not frappe.db.exists("Retail Attendance Site", site_name):
        if return_meta:
            return {
                "status": "unavailable",
                "enforced": True,
                "reason": _("Attendance site not configured."),
                "sales_persons": [],
            }
        return []

    site = frappe.get_doc("Retail Attendance Site", site_name)
    if site.company != pos_profile_doc.get("company"):
        if return_meta:
            return {
                "status": "unavailable",
                "enforced": True,
                "reason": _("Attendance site company mismatch."),
                "sales_persons": [],
            }
        return []

    day = frappe.get_doc("Retail Attendance Day", site.current_day) if site.current_day else None
    if not day or day.status != "Open":
        if return_meta:
            return {
                "status": "confirmed",
                "enforced": True,
                "pos_profile": pos_profile_doc.get("name"),
                "company": pos_profile_doc.get("company"),
                "site": site.name,
                "day": day.name if day else None,
                "day_status": day.status if day else "Closed",
                "day_version": day.version if day else 0,
                "business_date": str(day.business_date) if day and day.business_date else None,
                "captured_at": str(now_datetime()),
                "sales_persons": [],
            }
        return []

    eligible_employees = get_eligible_employees(site.name, site.company)
    if not eligible_employees:
        if return_meta:
            return {
                "status": "confirmed",
                "enforced": True,
                "pos_profile": pos_profile_doc.get("name"),
                "company": pos_profile_doc.get("company"),
                "site": site.name,
                "day": day.name,
                "day_status": day.status,
                "day_version": day.version,
                "business_date": str(day.business_date) if day and day.business_date else None,
                "captured_at": str(now_datetime()),
                "sales_persons": [],
            }
        return []

    filters = {
        "enabled": 1,
        "employee": ["in", eligible_employees],
    }
    if allowed_configured:
        filters["name"] = ["in", allowed_configured]

    sales_persons = frappe.get_all(
        "Sales Person",
        filters=filters,
        fields=["name", "sales_person_name", "employee"],
        order_by="sales_person_name asc, name asc",
        limit_page_length=100000,
    )

    if return_meta:
        return {
            "status": "confirmed",
            "enforced": True,
            "pos_profile": pos_profile_doc.get("name"),
            "company": pos_profile_doc.get("company"),
            "site": site.name,
            "day": day.name,
            "day_status": day.status,
            "day_version": day.version,
            "business_date": str(day.business_date) if day and day.business_date else None,
            "captured_at": str(now_datetime()),
            "sales_persons": sales_persons,
        }
    return sales_persons


def validate_salesperson_eligibility(sales_person: str, pos_profile_doc) -> bool:
    """Validate a salesperson's active attendance eligibility against POS Profile enforcement."""
    if isinstance(pos_profile_doc, str):
        pos_profile_doc = frappe.get_cached_doc("POS Profile", pos_profile_doc)

    if not pos_profile_doc or not cint(pos_profile_doc.get("posa_enforce_attendance")):
        return True

    site_name = pos_profile_doc.get("posa_attendance_site")
    if not site_name or not frappe.db.exists("Retail Attendance Site", site_name):
        frappe.throw(_("Attendance Site is required when attendance enforcement is enabled."))

    site = frappe.get_doc("Retail Attendance Site", site_name)
    if site.company != pos_profile_doc.get("company"):
        frappe.throw(
            _("The selected Attendance Site belongs to {0}, not {1}.").format(
                site.company, pos_profile_doc.get("company")
            )
        )

    allowed = [
        d.get("sales_person")
        for d in pos_profile_doc.get("posa_sales_persons", [])
        if d.get("sales_person")
    ]
    if allowed and sales_person not in allowed:
        frappe.throw(
            _("Sales Person {0} is not permitted for this POS profile.").format(sales_person)
        )

    sp = frappe.db.get_value(
        "Sales Person", sales_person, ["name", "enabled", "employee", "sales_person_name"], as_dict=True
    )
    if not sp:
        frappe.throw(_("Sales Person {0} not found.").format(sales_person))
    if not cint(sp.enabled):
        frappe.throw(_("Sales Person {0} is disabled.").format(sp.sales_person_name or sales_person))
    if not sp.employee:
        frappe.throw(
            _("Sales Person {0} is not linked to an Employee.").format(
                sp.sales_person_name or sales_person
            )
        )

    emp = frappe.db.get_value(
        "Employee",
        sp.employee,
        ["name", "employee_name", "status", "company", "date_of_joining", "relieving_date"],
        as_dict=True,
    )
    if not emp or emp.status != "Active":
        frappe.throw(
            _("Employee {0} is not active in company {1}.").format(
                emp.employee_name if emp else sp.employee, pos_profile_doc.get("company")
            )
        )
    if emp.company != pos_profile_doc.get("company"):
        frappe.throw(
            _("Employee {0} is not active in company {1}.").format(
                emp.employee_name, pos_profile_doc.get("company")
            )
        )

    today = getdate()
    if emp.date_of_joining and getdate(emp.date_of_joining) > today:
        frappe.throw(
            _("Employee {0} is not active in company {1}.").format(
                emp.employee_name, pos_profile_doc.get("company")
            )
        )
    if emp.relieving_date and getdate(emp.relieving_date) < today:
        frappe.throw(
            _("Employee {0} is not active in company {1}.").format(
                emp.employee_name, pos_profile_doc.get("company")
            )
        )

    roster = frappe.db.get_value(
        "Retail Attendance Roster",
        {"employee": sp.employee, "site": site.name, "company": pos_profile_doc.get("company")},
        ["name", "enrolled_from"],
        as_dict=True,
    )
    if not roster or (roster.enrolled_from and getdate(roster.enrolled_from) > today):
        frappe.throw(
            _("Employee {0} is not enrolled in the attendance roster for this shop.").format(
                emp.employee_name
            )
        )

    if not site.current_day:
        frappe.throw(_("The attendance day for this shop is not open."))

    day = frappe.db.get_value(
        "Retail Attendance Day", site.current_day, ["name", "status", "business_date"], as_dict=True
    )
    if not day or day.status != "Open":
        frappe.throw(_("The attendance day for this shop is not open."))

    presence = frappe.db.get_value(
        "Retail Attendance Presence",
        {"employee": sp.employee, "day": day.name, "site": site.name},
        ["name", "in_time", "check_out", "out_time"],
        as_dict=True,
    )
    if not presence or not presence.in_time:
        frappe.throw(_("Employee {0} has not checked in today.").format(emp.employee_name))
    if presence.check_out or presence.out_time:
        frappe.throw(_("Employee {0} has already checked out.").format(emp.employee_name))

    return True


def validate_salesperson_profile_membership(sales_person: str, pos_profile_doc) -> bool:
    """Validate that a salesperson is permitted for this profile and belongs to the company."""
    if isinstance(pos_profile_doc, str):
        pos_profile_doc = frappe.get_cached_doc("POS Profile", pos_profile_doc)

    allowed = [
        d.get("sales_person")
        for d in pos_profile_doc.get("posa_sales_persons", [])
        if d.get("sales_person")
    ]
    if allowed and sales_person not in allowed:
        frappe.throw(
            _("Sales Person {0} is not permitted for this POS profile.").format(sales_person)
        )

    sp = frappe.db.get_value(
        "Sales Person", sales_person, ["name", "enabled", "employee", "sales_person_name"], as_dict=True
    )
    if not sp:
        frappe.throw(_("Sales Person {0} not found.").format(sales_person))
    if not cint(sp.enabled):
        frappe.throw(_("Sales Person {0} is disabled.").format(sp.sales_person_name or sales_person))
    if sp.employee:
        emp_company = frappe.db.get_value("Employee", sp.employee, "company")
        if emp_company and emp_company != pos_profile_doc.get("company"):
            frappe.throw(
                _("Employee {0} is not active in company {1}.").format(
                    sp.employee, pos_profile_doc.get("company")
                )
            )
    return True


def is_attendance_snapshot_usable(snapshot: dict, pos_profile_doc, current_date=None) -> bool:
    """Check if an attendance snapshot is valid and fresh for offline admission."""
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("status") != "confirmed" or not snapshot.get("enforced"):
        return False
    if isinstance(pos_profile_doc, str):
        pos_profile_doc = frappe.get_cached_doc("POS Profile", pos_profile_doc)
    if snapshot.get("pos_profile") and snapshot.get("pos_profile") != pos_profile_doc.get("name"):
        return False
    if snapshot.get("site") != pos_profile_doc.get("posa_attendance_site"):
        return False
    if snapshot.get("company") != pos_profile_doc.get("company"):
        return False
    if snapshot.get("day_status") != "Open":
        return False
    if current_date and snapshot.get("business_date"):
        if getdate(snapshot.get("business_date")) != getdate(current_date):
            return False
    return True


def verify_offline_admission_evidence(evidence, pos_profile_doc, salespersons: list) -> bool:
    """Verify cryptographic and historical integrity of offline admission evidence against the DB."""
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except Exception:
            frappe.throw(_("Invalid or unverified offline attendance evidence."))

    if not isinstance(evidence, dict):
        frappe.throw(_("Invalid or unverified offline attendance evidence."))

    if isinstance(pos_profile_doc, str):
        pos_profile_doc = frappe.get_cached_doc("POS Profile", pos_profile_doc)

    site_name = pos_profile_doc.get("posa_attendance_site")
    if not site_name or evidence.get("site") != site_name:
        frappe.throw(_("Invalid or unverified offline attendance evidence."))

    if evidence.get("company") != pos_profile_doc.get("company"):
        frappe.throw(_("Invalid or unverified offline attendance evidence."))

    day_name = evidence.get("day")
    if not day_name:
        frappe.throw(_("Invalid or unverified offline attendance evidence."))

    day = frappe.db.get_value(
        "Retail Attendance Day", day_name, ["name", "site", "status", "business_date"], as_dict=True
    )
    if not day or day.site != site_name:
        frappe.throw(_("Invalid or unverified offline attendance evidence."))

    captured_at = evidence.get("captured_at")
    captured_dt = get_datetime(captured_at) if captured_at else None

    for sp_name in salespersons:
        validate_salesperson_profile_membership(sp_name, pos_profile_doc)

        sp = frappe.db.get_value(
            "Sales Person", sp_name, ["name", "employee", "sales_person_name"], as_dict=True
        )
        if not sp or not sp.employee:
            frappe.throw(_("Sales Person {0} is not linked to an Employee.").format(sp_name))

        emp = frappe.db.get_value(
            "Employee", sp.employee, ["name", "status", "company"], as_dict=True
        )
        if not emp or emp.status != "Active" or emp.company != pos_profile_doc.get("company"):
            frappe.throw(
                _("Employee {0} is not active in company {1}.").format(
                    sp.employee, pos_profile_doc.get("company")
                )
            )

        roster = frappe.db.exists(
            "Retail Attendance Roster",
            {"employee": sp.employee, "site": site_name, "company": pos_profile_doc.get("company")},
        )
        if not roster:
            frappe.throw(
                _("Employee {0} is not enrolled in the attendance roster for this shop.").format(
                    sp.employee
                )
            )

        presence = frappe.db.get_value(
            "Retail Attendance Presence",
            {"employee": sp.employee, "day": day_name, "site": site_name},
            ["name", "in_time"],
            as_dict=True,
        )
        if not presence or not presence.in_time:
            frappe.throw(
                _("Employee {0} has no check-in record for the stated attendance day.").format(
                    sp.employee
                )
            )

        if captured_dt and presence.in_time:
            presence_in_dt = get_datetime(presence.in_time)
            if presence_in_dt > captured_dt + timedelta(minutes=1):
                frappe.throw(_("Employee check-in time is later than evidence capture time."))

    return True


def validate_invoice_attendance(doc) -> None:
    """Validate salesperson attendance eligibility for POS and Sales Invoices."""
    if cint(doc.get("is_return")):
        # Returns preserve original salesperson attribution and do not require current check-in.
        return

    pos_profile = doc.get("pos_profile")
    if not pos_profile:
        # Ordinary non-POS invoices are not constrained by POS attendance enforcement.
        return

    profile_doc = frappe.get_cached_doc("POS Profile", pos_profile)
    if not cint(profile_doc.get("posa_enforce_attendance")):
        if isinstance(doc, dict):
            doc["posa_attendance_admission_mode"] = "enforcement_disabled"
        else:
            setattr(doc, "posa_attendance_admission_mode", "enforcement_disabled")
        return

    if getattr(frappe.flags, "posa_attendance_sealed", False) or cint(doc.get("posa_attendance_sealed")):
        # Background submit or offline replay with sealed admission.
        return

    sales_person = doc.get("custom_sales_person")
    sales_team = doc.get("sales_team") or []
    team_salespersons = [
        st.get("sales_person")
        for st in sales_team
        if st.get("sales_person")
    ]

    # Reject contradictory attribution
    if sales_person and team_salespersons and sales_person not in team_salespersons:
        frappe.throw(
            _("Sales Person {0} conflicts with sales team: {1}.").format(
                sales_person, ", ".join(team_salespersons)
            )
        )

    all_sp = list(dict.fromkeys(([sales_person] if sales_person else []) + team_salespersons))
    if not all_sp:
        return

    admission_mode = doc.get("posa_attendance_admission_mode")
    evidence_raw = doc.get("posa_attendance_evidence")

    trusted_shift_audit = getattr(getattr(frappe, "flags", None), "posa_trusted_shift_audit", None)
    is_trusted_offline_sync = (
        isinstance(trusted_shift_audit, dict)
        and trusted_shift_audit.get("source") == "offline_sync"
    ) or getattr(frappe.flags, "in_offline_sync", False)

    if admission_mode == "last_confirmed_offline":
        if not is_trusted_offline_sync:
            frappe.throw(_("Offline admission is only permitted via authorized offline sync."))
        verify_offline_admission_evidence(evidence_raw, profile_doc, all_sp)
        if isinstance(doc, dict):
            doc["posa_attendance_sealed"] = 1
        else:
            setattr(doc, "posa_attendance_sealed", 1)
        return

    if admission_mode == "unverified_offline_fallback":
        if not is_trusted_offline_sync:
            frappe.throw(_("Offline fallback admission is only permitted via authorized offline sync."))
        for sp in all_sp:
            validate_salesperson_profile_membership(sp, profile_doc)
        if isinstance(doc, dict):
            doc["posa_attendance_sealed"] = 1
        else:
            setattr(doc, "posa_attendance_sealed", 1)
        return

    if admission_mode in {"last_confirmed_offline", "unverified_offline_fallback"}:
        frappe.throw(_("Offline admission modes are not allowed for online checkout."))

    # Online submission path
    for sp in all_sp:
        validate_salesperson_eligibility(sp, profile_doc)

    if isinstance(doc, dict):
        doc["posa_attendance_admission_mode"] = "confirmed_at_checkout"
    else:
        setattr(doc, "posa_attendance_admission_mode", "confirmed_at_checkout")
