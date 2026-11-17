"""P4 automatic fines, salary integration, reconciliation, and native proration tests."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate, now_datetime

from hrms.hr.retail_attendance import access, api, commands, corrections, fines, presence, schedules, test_p3


class TestRetailAttendanceP4(test_p3.TestRetailAttendanceP3):
    def prepare_with_fines(self, absence=120, brackets=None):
        self.prepare()
        from erpnext.setup import staff_pay_actions as spa
        month = str(getdate())[:7]
        spa.save_pay_input(self.company, self.employee_id, "Salary", month, amount=1000, reason="Initial salary", request_id=str(uuid4()))
        if len(self.employees) > 1 and self.employees[1].name != self.employee_id:
            try:
                spa.save_pay_input(self.company, self.employees[1].name, "Salary", month, amount=1000, reason="Initial salary", request_id=str(uuid4()))
            except Exception:
                pass
        if brackets is None:
            brackets = [
                {"late_minutes": 15, "amount": 20},
                {"late_minutes": 30, "amount": 50},
                {"late_minutes": 60, "amount": 100},
            ]
        self.fine_rule_res = fines.save_fine_rule(
            self.site,
            effective_date=str(getdate()),
            enabled=1,
            absence_amount=absence,
            late_brackets=brackets,
            reason="Test fine rules",
            version=0,
            request_id=str(uuid4()),
        )
        self.fine_rule = fines.get_effective_rule(self.site, getdate())

    def test_fine_rule_configuration_validation_and_idempotency(self):
        self.prepare()
        req_id = str(uuid4())
        # Bad bracket format
        with self.assertRaises(frappe.ValidationError):
            fines.save_fine_rule(self.site, str(getdate()), 1, 50, [{"late_minutes": -5, "amount": 20}], "", 0, str(uuid4()))
        with self.assertRaises(frappe.ValidationError):
            fines.save_fine_rule(self.site, str(getdate()), 1, -10, [], "", 0, str(uuid4()))

        # Valid save
        res1 = fines.save_fine_rule(
            self.site, str(getdate()), 1, 100, [{"late_minutes": 15, "amount": 25}], "Initial", 0, req_id
        )
        self.assertEqual(res1["revision"], 1)

        # Idempotent replay with same request_id
        res2 = fines.save_fine_rule(
            self.site, str(getdate()), 1, 100, [{"late_minutes": 15, "amount": 25}], "Initial", 0, req_id
        )
        self.assertEqual(res1["rule"], res2["rule"])

        # Stale version check
        with self.assertRaises(frappe.TimestampMismatchError):
            fines.save_fine_rule(self.site, str(getdate()), 1, 100, [{"late_minutes": 15, "amount": 25}], "Stale", 0, str(uuid4()))

    def test_on_time_arrival_creates_no_fine_decision(self):
        self.prepare_with_fines()
        # Arrival at scheduled time 09:00
        res = self.punch("IN", at=self.clock)
        self.assertTrue(res["saved"])

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "No Fine")
        self.assertEqual(decision.amount, 0)
        self.assertEqual(decision.kind, "None")
        self.assertFalse(decision.pay_change)
        self.assertEqual(frappe.db.count("Additional Salary", {"employee": self.employee_id}), 0)

    def test_sub_threshold_lateness_does_not_fine(self):
        self.prepare_with_fines()
        # Arrival at 09:10 (10 mins late; threshold is 15 mins)
        res = self.punch("IN", at=self.clock + timedelta(minutes=10))
        self.assertTrue(res["saved"])

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "No Fine")
        self.assertEqual(decision.amount, 0)
        self.assertEqual(decision.late_minutes, 10)
        self.assertEqual(frappe.db.count("Additional Salary", {"employee": self.employee_id}), 0)

    def test_lateness_selects_highest_applicable_bracket_and_creates_native_fine(self):
        self.prepare_with_fines()
        # Arrival at 09:35 (35 mins late -> matches 15m (20) and 30m (50); highest is 50)
        res = self.punch("IN", at=self.clock + timedelta(minutes=35))
        self.assertTrue(res["saved"])

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "Applied")
        self.assertEqual(decision.kind, "Lateness")
        self.assertEqual(decision.amount, 50)
        self.assertEqual(decision.late_minutes, 35)
        self.assertTrue(decision.pay_change)
        self.assertTrue(decision.additional_salary)

        # Check native Additional Salary
        salary = frappe.get_doc("Additional Salary", decision.additional_salary)
        self.assertEqual(salary.type, "Deduction")
        self.assertEqual(salary.amount, 50)
        self.assertEqual(salary.docstatus, 1)

        # Check Staff Pay Change audit
        pay_change = frappe.get_doc("Staff Pay Change", decision.pay_change)
        self.assertEqual(pay_change.kind, "Fine")
        self.assertEqual(pay_change.amount, 50)

    def test_missed_shift_evaluates_absence_fine(self):
        self.prepare_with_fines(absence=120)
        # Employee does not check in at all. Day is closed.
        closing = self.close(self.day)
        commands.continue_close(self.site, closing["operation"])

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "Applied")
        self.assertEqual(decision.kind, "Absence")
        self.assertEqual(decision.amount, 120)

        salary = frappe.get_doc("Additional Salary", decision.additional_salary)
        self.assertEqual(salary.type, "Deduction")
        self.assertEqual(salary.amount, 120)

    def test_approved_leave_and_day_off_are_exempt_from_absence_fine(self):
        self.prepare_with_fines(absence=120)
        # Change employee schedule to Day Off
        roster_name = frappe.db.get_value("Retail Attendance Roster", {"employee": self.employee_id, "site": self.site})
        roster_doc = frappe.get_doc("Retail Attendance Roster", roster_name)
        commands.save_schedule(
            self.site,
            roster_name,
            kind="Exception",
            effective_date=str(getdate()),
            mode="Off",
            weekdays="",
            starts_at=None,
            ends_at=None,
            reason="Approved day off",
            version=roster_doc.version,
            request_id=str(uuid4()),
        )

        closing = self.close(self.day)
        commands.continue_close(self.site, closing["operation"])

        # Employee should NOT have an applied fine
        decisions = frappe.get_all(
            "Retail Attendance Fine Decision",
            filters={"employee": self.employee_id, "business_date": getdate(), "status": "Applied"},
        )
        self.assertEqual(len(decisions), 0)

    def test_attendance_correction_reconciles_fine_in_open_period(self):
        self.prepare_with_fines()
        # Arrive 35 min late (fined 50 TJS)
        self.punch("IN", at=self.clock + timedelta(minutes=35))

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "Applied")
        original_salary = decision.additional_salary

        # Manager corrects arrival to on-time (09:00)
        starts = str(self.clock)
        corrections.correct(self.site, self.day, self.employee_id, starts, None, "Traffic jam excused", 1, str(uuid4()))

        decision.reload()
        self.assertEqual(decision.status, "Cancelled")
        # Native Additional Salary should be cancelled
        self.assertEqual(frappe.db.get_value("Additional Salary", original_salary, "docstatus"), 2)

        # Audit should record Fine cancellation
        cancellation = frappe.get_doc("Staff Pay Change", {"replaces": decision.pay_change})
        self.assertEqual(cancellation.kind, "Fine cancellation")

    def test_attendance_correction_replaces_fine_with_new_bracket(self):
        self.prepare_with_fines()
        # Arrive 65 min late (fined 100 TJS)
        self.punch("IN", at=self.clock + timedelta(minutes=65))

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.amount, 100)
        old_salary = decision.additional_salary

        # Correct arrival to 09:20 (20 min late -> bracket is 20 TJS)
        corrections.correct(self.site, self.day, self.employee_id, str(self.clock + timedelta(minutes=20)), None, "Actual arrival was 09:20", 1, str(uuid4()))

        decision.reload()
        self.assertEqual(decision.status, "Applied")
        self.assertEqual(decision.amount, 20)
        self.assertNotEqual(decision.additional_salary, old_salary)
        # Old salary cancelled, new salary active for 20
        self.assertEqual(frappe.db.get_value("Additional Salary", old_salary, "docstatus"), 2)
        new_sal = frappe.get_doc("Additional Salary", decision.additional_salary)
        self.assertEqual(new_sal.docstatus, 1)
        self.assertEqual(new_sal.amount, 20)

    def test_correction_after_payroll_payment_creates_compensation(self):
        self.prepare_with_fines()
        # Arrive 35 min late (50 TJS fine in current month)
        self.punch("IN", at=self.clock + timedelta(minutes=35))

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "Applied")

        # Create and submit a Salary Slip covering this month
        month = str(getdate())[:7]
        from erpnext.setup.staff_monthly_view import month_scope
        period = month_scope(month)
        slip = frappe.get_doc(dict(
            doctype="Salary Slip",
            employee=self.employee_id,
            company=self.company,
            start_date=period["start_date"],
            end_date=period["end_date"],
            posting_date=period["end_date"],
        )).insert()
        slip.submit()

        # Now correct attendance when month is closed
        corrections.correct(self.site, self.day, self.employee_id, str(self.clock), None, "Pardoned after payroll", 1, str(uuid4()))

        decision.reload()
        self.assertEqual(decision.status, "Compensated")
        self.assertTrue(decision.compensation_pay_change)

        # Compensation is created as Earning in next open month
        comp_change = frappe.get_doc("Staff Pay Change", decision.compensation_pay_change)
        self.assertEqual(comp_change.kind, "Fine compensation")
        self.assertEqual(comp_change.amount, 50)
        comp_sal = frappe.get_doc("Additional Salary", comp_change.native_name)
        self.assertEqual(comp_sal.type, "Earning")
        self.assertEqual(comp_sal.amount, 50)

    def test_manager_cancellation_suppresses_regeneration_on_retry(self):
        self.prepare_with_fines()
        self.punch("IN", at=self.clock + timedelta(minutes=35))

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "Applied")

        # Manager cancels fine via Staff Pay API
        from erpnext.setup.staff_pay_actions import cancel_pay_input
        month = str(getdate())[:7]
        cancel_pay_input(self.company, self.employee_id, month, decision.pay_change, "Manager waived fine", str(uuid4()))

        decision.reload()
        self.assertEqual(decision.status, "Cancelled")

        # Retry background fine processor
        fines.process_scheduled_absence_fines(self.site, getdate())
        decision.reload()
        # Status MUST remain Cancelled, not re-applied!
        self.assertEqual(decision.status, "Cancelled")

    def test_no_salary_double_deduction_on_absence(self):
        self.prepare_with_fines(absence=120)
        # Shift missed -> fine applied (120 TJS)
        closing = self.close(self.day)
        commands.continue_close(self.site, closing["operation"])

        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.amount, 120)

        # Also create a native Attendance record marked "Absent" on that date
        # to test that salary proration does NOT double-deduct
        frappe.get_doc(dict(
            doctype="Attendance",
            employee=self.employee_id,
            company=self.company,
            attendance_date=getdate(),
            status="Absent",
        )).insert()

        month = str(getdate())[:7]
        from erpnext.setup.staff_monthly_view import month_scope
        period = month_scope(month)

        frappe.db.set_single_value("Payroll Settings", "payroll_based_on", "Attendance")
        slip = frappe.get_doc(dict(
            doctype="Salary Slip",
            employee=self.employee_id,
            company=self.company,
            start_date=period["start_date"],
            end_date=period["end_date"],
            posting_date=period["end_date"],
        )).insert()

        # Gross pay must be full base (1000), total deductions must be exactly fine (120), net pay = 880
        self.assertEqual(slip.gross_pay, 1000)
        self.assertEqual(slip.total_deduction, 120)
        self.assertEqual(slip.net_pay, 880)

    def test_concurrent_fine_evaluation_is_idempotent(self):
        self.prepare_with_fines()
        # Punch IN 35m late
        self.punch("IN", at=self.clock + timedelta(minutes=35))

        # Run parallel evaluations of the fine
        p = frappe.get_doc("Retail Attendance Presence", {"day": self.day, "employee": self.employee_id})
        def job():
            frappe.set_user("Administrator")
            return fines.apply_or_reconcile_fine(self.site, getdate(), self.employee_id, presence=p, day=self.day)

        results, errors = self.parallel([job, job])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        # Both must return the same decision and there must be only 1 decision and 1 active Additional Salary
        self.assertEqual(results[0].name, results[1].name)
        self.assertEqual(
            frappe.db.count("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()}),
            1,
        )
        self.assertEqual(
            frappe.db.count("Additional Salary", {"employee": self.employee_id, "docstatus": 1}),
            1,
        )
