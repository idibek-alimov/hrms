"""Automatic lateness and absence fines integrated with native staff pay."""
import hashlib
import json
from uuid import UUID, uuid4

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, getdate, now_datetime

from hrms.hr.retail_attendance import access, commands, rules, schedules


def parse_brackets(value):
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except Exception:
            frappe.throw(_("Invalid fine brackets format."))
    if not isinstance(value, (list, tuple)):
        frappe.throw(_("Fine brackets must be a list."))
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            frappe.throw(_("Each bracket must specify late minutes and amount."))
        mins = int(item.get("late_minutes", 0))
        amt = flt(item.get("amount", 0))
        if mins <= 0 or amt <= 0:
            frappe.throw(_("Late minutes and amount must be positive numbers."))
        if mins in seen:
            frappe.throw(_("Duplicate late minutes in fine brackets."))
        seen.add(mins)
        result.append(dict(late_minutes=mins, amount=amt))
    return sorted(result, key=lambda b: b["late_minutes"])


def format_brackets(brackets):
    return json.dumps(brackets, separators=(",", ":"))


def get_effective_rule(site, work_date=None):
    work_date = getdate(work_date)
    rows = frappe.get_all(
        "Retail Attendance Fine Rule",
        filters={"site": site, "effective_date": ["<=", work_date]},
        fields=["name", "site", "company", "version", "revision", "effective_date", "enabled", "absence_amount", "late_brackets", "reason"],
        order_by="effective_date desc, revision desc",
        limit=1,
    )
    if not rows:
        return None
    rule = rows[0]
    rule["brackets"] = parse_brackets(rule.get("late_brackets") or "[]")
    rule["enabled"] = bool(rule.get("enabled"))
    rule["absence_amount"] = flt(rule.get("absence_amount", 0))
    return rule


def save_fine_rule(site, effective_date, enabled, absence_amount, late_brackets, reason, version, request_id):
    doc = commands.site_doc(site, lock=True)
    access.manager(doc.company)
    day = getdate(effective_date)
    brackets = parse_brackets(late_brackets)
    absence = flt(absence_amount)
    if absence < 0:
        frappe.throw(_("Absence fine amount cannot be negative."))
    note = commands._text(reason or "")
    payload = dict(
        site=site,
        effective_date=str(day),
        enabled=int(bool(enabled)),
        absence_amount=absence,
        late_brackets=brackets,
        reason=note,
        version=version,
    )
    digest, receipt, result = commands._request(doc, request_id, "FineRule", payload)
    if receipt:
        return result

    # Check optimistic concurrency against latest revision
    existing = get_effective_rule(site, getdate())
    latest_version = existing["revision"] if existing else 0
    commands._version(version, latest_version)

    rule = access.save(
        frappe.get_doc(
            dict(
                doctype="Retail Attendance Fine Rule",
                company=doc.company,
                site=site,
                version=latest_version + 1,
                revision=latest_version + 1,
                effective_date=day,
                enabled=int(bool(enabled)),
                absence_amount=absence,
                late_brackets=format_brackets(brackets),
                reason=note,
            )
        )
    )
    result = dict(rule=rule.name, revision=rule.revision, enabled=bool(rule.enabled))
    commands._receipt(doc, request_id, digest, "FineRule", result)
    return result


def evaluate_fine(rule, schedule, in_time=None, shift_ended=False):
    """Pure fine evaluation against scheduled shift and attendance times.

    Returns dict with state ('applicable', 'no_fine', 'pending', 'exempt', 'disabled'),
    kind ('Lateness', 'Absence', 'None'), amount, late_minutes, and reason.
    """
    if schedule.get("state") != "Scheduled":
        return dict(state="exempt", kind="None", amount=0, late_minutes=0, reason=_("Not scheduled to work."))

    if not rule or not rule.get("enabled"):
        return dict(state="disabled", kind="None", amount=0, late_minutes=0, reason=_("Fine rules are not enabled."))

    starts_at = get_datetime(schedule["starts_at"])
    ends_at = get_datetime(schedule["ends_at"])

    if in_time:
        arrival = get_datetime(in_time).replace(microsecond=0)
        late_sec = (arrival - starts_at).total_seconds()
        late_min = int(late_sec // 60) if late_sec > 0 else 0
        if late_min <= 0:
            return dict(
                state="no_fine",
                kind="None",
                amount=0,
                late_minutes=0,
                reason=_("Arrived on time (scheduled {0}, arrived {1})").format(
                    starts_at.strftime("%H:%M"), arrival.strftime("%H:%M")
                ),
            )
        brackets = rule.get("brackets") or []
        matching = [b for b in brackets if late_min >= b["late_minutes"]]
        if matching:
            highest = max(matching, key=lambda b: flt(b["amount"]))
            return dict(
                state="applicable",
                kind="Lateness",
                amount=flt(highest["amount"]),
                late_minutes=late_min,
                reason=_("Late by {0} min (scheduled {1}, arrived {2})").format(
                    late_min, starts_at.strftime("%H:%M"), arrival.strftime("%H:%M")
                ),
            )
        else:
            return dict(
                state="no_fine",
                kind="None",
                amount=0,
                late_minutes=late_min,
                reason=_("Late by {0} min (below fine threshold)").format(late_min),
            )
    else:
        now = now_datetime()
        if shift_ended or now >= ends_at:
            absence_amt = flt(rule.get("absence_amount", 0))
            if absence_amt > 0:
                return dict(
                    state="applicable",
                    kind="Absence",
                    amount=absence_amt,
                    late_minutes=0,
                    reason=_("Missed shift ({0} - {1})").format(
                        starts_at.strftime("%H:%M"), ends_at.strftime("%H:%M")
                    ),
                )
            else:
                return dict(
                    state="no_fine",
                    kind="None",
                    amount=0,
                    late_minutes=0,
                    reason=_("Missed shift (no absence fine configured)"),
                )
        else:
            return dict(state="pending", kind="None", amount=0, late_minutes=0, reason=_("Shift in progress."))


def apply_or_reconcile_fine(site_name, business_date, employee, presence=None, day=None, shift_ended=False):
    """Authoritative writer for attendance fines. Holds locks and updates ledger/decision."""
    from erpnext.setup.staff_pay_actions import (
        cancel_or_compensate_attendance_fine,
        create_attendance_fine,
    )

    business_date = getdate(business_date)
    site_doc = commands.site_doc(site_name, lock=True)
    company = site_doc.company
    commands.lock_employee(employee, company)

    # Check if a fine decision already exists for this employee and shift date
    existing_name = frappe.db.get_value(
        "Retail Attendance Fine Decision",
        {"employee": employee, "business_date": business_date},
        "name",
        for_update=True,
    )
    decision = frappe.get_doc("Retail Attendance Fine Decision", existing_name, for_update=True) if existing_name else None

    # Manager cancellation or explicit suppression is honored; retries never regenerate it
    if decision and decision.status in ("Cancelled", "Suppressed"):
        return decision

    # Resolve effective schedule for this employee on this date
    roster_name = frappe.db.get_value("Retail Attendance Roster", {"employee": employee, "site": site_name, "company": company})
    if not roster_name:
        return decision
    roster = frappe.get_doc("Retail Attendance Roster", roster_name)
    context = schedules.load_context([roster], business_date, business_date)
    schedule = schedules.resolve(roster, business_date, context)

    # Resolve effective rule
    rule = get_effective_rule(site_name, business_date)
    in_time = presence.in_time if presence else None
    evaluation = evaluate_fine(rule, schedule, in_time=in_time, shift_ended=shift_ended)

    save_fn = access.service_save if access.service_scope() else access.save

    if evaluation["state"] == "applicable":
        amt = flt(evaluation["amount"])
        kind = evaluation["kind"]
        reason = evaluation["reason"]

        if decision and decision.status == "Applied":
            if decision.kind == kind and flt(decision.amount) == amt:
                return decision  # Already posted with exact amount; idempotent

            # Amount or kind changed (e.g. corrected arrival): cancel/compensate old fine first
            cancel_req = str(uuid4())
            cancel_or_compensate_attendance_fine(
                company, employee, decision.pay_change, _("Attendance corrected: {0}").format(reason), cancel_req
            )
            decision.reload()

        # Post new native fine deduction
        post_req = str(uuid4())
        pay_res = create_attendance_fine(
            company=company,
            employee=employee,
            amount=amt,
            month=str(business_date)[:7],
            reason=reason,
            request_id=post_req,
        )

        d_hash = hashlib.sha256(f"{employee}:{business_date}:{site_name}".encode()).hexdigest()
        fields = dict(
            doctype="Retail Attendance Fine Decision",
            company=company,
            site=site_name,
            employee=employee,
            business_date=business_date,
            day=day.name if hasattr(day, "name") else (day or (presence.day if presence else None)),
            presence=presence.name if hasattr(presence, "name") else (presence or None),
            schedule_source=schedule.get("source"),
            rule_source=rule.get("name") if rule else None,
            rule_revision=rule.get("revision") if rule else 1,
            kind=kind,
            late_minutes=evaluation["late_minutes"],
            amount=amt,
            status="Applied",
            pay_change=pay_res["pay_change"],
            additional_salary=pay_res["additional_salary"],
            decision_hash=d_hash,
        )
        if decision:
            decision.update(fields)
            decision.version += 1
            return save_fn(decision)
        else:
            return save_fn(frappe.get_doc(dict(fields, version=1)))

    elif evaluation["state"] in ("no_fine", "exempt", "disabled"):
        if decision and decision.status == "Applied":
            # Attendance corrected to on-time or employee received leave/exemption: cancel old fine
            cancel_req = str(uuid4())
            res = cancel_or_compensate_attendance_fine(
                company, employee, decision.pay_change, _("Attendance corrected: {0}").format(evaluation["reason"]), cancel_req
            )
            decision.reload()
            return decision

        d_hash = hashlib.sha256(f"{employee}:{business_date}:{site_name}".encode()).hexdigest()
        fields = dict(
            doctype="Retail Attendance Fine Decision",
            company=company,
            site=site_name,
            employee=employee,
            business_date=business_date,
            day=day.name if hasattr(day, "name") else (day or (presence.day if presence else None)),
            presence=presence.name if hasattr(presence, "name") else (presence or None),
            schedule_source=schedule.get("source"),
            rule_source=rule.get("name") if rule else None,
            rule_revision=rule.get("revision") if rule else 0,
            kind="None",
            late_minutes=evaluation.get("late_minutes", 0),
            amount=0,
            status="No Fine",
            decision_hash=d_hash,
        )
        if decision:
            decision.update(fields)
            decision.version += 1
            return save_fn(decision)
        else:
            return save_fn(frappe.get_doc(dict(fields, version=1)))

    return decision


def reconcile_on_correction(site_name, day, employee, presence):
    """Reconciles fine decisions when a manager corrects attendance."""
    return apply_or_reconcile_fine(site_name, day.business_date, employee, presence=presence, day=day)


def process_scheduled_absence_fines(site_name, business_date=None, force_shift_ended=False):
    """Scans scheduled roster members for shifts that ended without a check-in."""
    site_doc = commands.site_doc(site_name)
    business_date = getdate(business_date or site_doc.get("business_date") or getdate())
    rule = get_effective_rule(site_name, business_date)
    if not rule or not rule.get("enabled") or flt(rule.get("absence_amount", 0)) <= 0:
        return []

    rosters = frappe.get_all(
        "Retail Attendance Roster",
        filters={"site": site_name, "company": site_doc.company, "enrolled_from": ["<=", business_date]},
        fields=["name", "employee", "company", "site", "version", "enrolled_from"],
    )
    if not rosters:
        return []

    context = schedules.load_context(rosters, business_date, business_date)
    applied = []
    now = now_datetime()

    day_name = frappe.db.get_value("Retail Attendance Day", {"site": site_name, "business_date": business_date}, "name")
    day_status = frappe.db.get_value("Retail Attendance Day", day_name, "status") if day_name else None

    for roster in rosters:
        sched = schedules.resolve(roster, business_date, context)
        if sched.get("state") != "Scheduled":
            continue
        ends_at = get_datetime(sched["ends_at"])
        if not (force_shift_ended or day_status == "Closed" or now >= ends_at):
            continue  # Shift has not yet ended

        # Check against active attendance day
        if day_name and frappe.db.exists("Retail Attendance Presence", {"employee": roster.employee, "day": day_name, "in_time": ["is", "set"]}):
            continue

        decision = apply_or_reconcile_fine(site_name, business_date, roster.employee, presence=None, day=day_name, shift_ended=True)
        if decision and decision.status == "Applied":
            applied.append(decision.name)

    return applied
