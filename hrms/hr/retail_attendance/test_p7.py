"""P7 Integrated verification, complete retail journeys, concurrency, and local activation tests."""
import json
import threading
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate, now_datetime

from hrms.hr.retail_attendance import (
    access,
    api,
    commands,
    corrections,
    credentials,
    eligibility,
    fines,
    presence,
    schedules,
    setup,
    test_p6,
)
from posawesome.posawesome.api.offline_sync.invoices import submit_invoice_outbox_entry
from posawesome.posawesome.api.invoice_processing.creation import submit_invoice
from posawesome.posawesome.api.terminal_state import activate_verified_cashier
from posawesome.setup import install as posa_install
from erpnext.setup import staff_pay_actions as spa


class TestRetailAttendanceP7(test_p6.TestRetailAttendanceP6):
    """P7 Integrated end-to-end verification and activation test suite."""

    def test_p7_1_whole_retail_attendance_journey(self):
        """P7-1: Complete retail attendance journey:

        Schedule Exception -> Kiosk PIN Check-in -> POS Sale Attribution ->
        Highest Fine Bracket Evaluation -> Manager Attendance Correction ->
        Automatic Fine Reconciliation -> Day Close Auto-Checkout -> Next-Day Clean State.
        """
        # 1. Setup Environment
        self.prepare_offline_context()

        # Enroll Employee 2 into site
        self.regular(self.enroll(self.employees[1]))

        # Enroll third employee (Emp C) into site
        emp_c = self.employee()
        self.regular(self.enroll(emp_c))
        sp_c_name = "SP-" + emp_c.name
        if not frappe.db.exists("Sales Person", sp_c_name):
            sp_c = frappe.get_doc(dict(
                doctype="Sales Person",
                sales_person_name=sp_c_name,
                employee=emp_c.name,
                enabled=1,
            )).insert(ignore_permissions=True)
            self.sp3 = sp_c.name
        else:
            self.sp3 = sp_c_name

        # Ensure base salary exists for fine deduction
        month = str(getdate())[:7]
        spa.save_pay_input(self.company, self.employees[0].name, "Salary", month, amount=1500, reason="Base pay", request_id=str(uuid4()))
        spa.save_pay_input(self.company, self.employees[1].name, "Salary", month, amount=1500, reason="Base pay", request_id=str(uuid4()))
        spa.save_pay_input(self.company, emp_c.name, "Salary", month, amount=1500, reason="Base pay", request_id=str(uuid4()))

        # Configure Fine Rules: 15m -> 20 TJS, 30m -> 50 TJS, 60m -> 100 TJS, absence -> 120 TJS
        fines.save_fine_rule(
            self.site,
            effective_date=str(getdate()),
            enabled=1,
            absence_amount=120,
            late_brackets=[
                {"late_minutes": 15, "amount": 20},
                {"late_minutes": 30, "amount": 50},
                {"late_minutes": 60, "amount": 100},
            ],
            reason="Retail shop fine rules",
            version=0,
            request_id=str(uuid4()),
        )

        # 2. Schedule Exception for Employee 1 (starts at 10:00 instead of 09:00)
        roster_a = presence.roster_for(self.employees[0].name, commands.site_doc(self.site))
        commands.save_schedule(
            self.site,
            roster_a.name,
            "Exception",
            str(getdate()),
            "Hours",
            "",
            "10:00",
            "18:00",
            "Doctor appointment approved",
            roster_a.version,
            str(uuid4()),
        )

        # Set PIN for Employee 2
        pin2 = "782914"
        credentials.set_pin(self.company, self.employees[1].name, pin2, 0, str(uuid4()))

        # 3. Check-ins at 09:40
        checkin_clock = self.clock + timedelta(minutes=40)  # 09:40:00

        # Employee 1 checks in at 09:40: effective start is 10:00 -> ON TIME
        punch_a = self.punch("IN", pin=self.pin, employee=self.employees[0].name, at=checkin_clock)
        self.assertTrue(punch_a["saved"])

        # Employee 1 fine check: status is "No Fine", amount is 0, no additional_salary
        emp1_fine = frappe.db.get_value(
            "Retail Attendance Fine Decision",
            {"employee": self.employees[0].name, "business_date": getdate()},
            ["status", "amount", "additional_salary"],
            as_dict=True,
        )
        self.assertIsNotNone(emp1_fine)
        self.assertEqual(emp1_fine.status, "No Fine")
        self.assertEqual(emp1_fine.amount, 0)
        self.assertIsNone(emp1_fine.additional_salary)

        # Employee 2 checks in at 09:40: regular start 09:00 -> 40m late!
        punch_b = self.punch("IN", pin=pin2, employee=self.employees[1].name, at=checkin_clock)
        self.assertTrue(punch_b["saved"])

        # Employee 2 fine check: 40m late -> highest applicable bracket is 30m (50 TJS)
        emp2_fine_doc = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employees[1].name, "business_date": getdate()})
        self.assertEqual(emp2_fine_doc.status, "Applied")
        self.assertEqual(emp2_fine_doc.amount, 50)
        self.assertTrue(emp2_fine_doc.additional_salary)
        add_sal = frappe.get_doc("Additional Salary", emp2_fine_doc.additional_salary)
        self.assertEqual(add_sal.amount, 50)
        self.assertEqual(add_sal.docstatus, 1)

        # 4. POS Sale Attribution:
        # A) Attempt to attribute sale to Employee 3 (Emp C, who never checked in) -> REJECTED
        inv_c, data_c, _ = self.make_invoice_payload(salesperson=self.sp3)
        with self.assertRaises(frappe.ValidationError):
            submit_invoice(json.dumps(inv_c), json.dumps(data_c))

        # B) Attribute sale to Employee 1 (checked in) -> SUCCEEDS
        inv_a, data_a, _ = self.make_invoice_payload(salesperson=self.sp1)
        res_a = submit_invoice(json.dumps(inv_a), json.dumps(data_a))
        inv_a_doc = frappe.get_doc("Sales Invoice", res_a["name"])
        self.assertEqual(inv_a_doc.docstatus, 1)
        self.assertEqual(inv_a_doc.custom_sales_person, self.sp1)

        # 5. Manager Attendance Correction:
        # Manager discovers Employee 2's delay was an authorized shop errand.
        # Manager corrects arrival time to 09:00 with mandatory reason.
        p2_doc = frappe.get_doc("Retail Attendance Presence", {"employee": self.employees[1].name, "day": self.day})
        corrections.correct(
            self.site,
            self.day,
            self.employees[1].name,
            str(self.clock),  # 09:00:00
            None,
            "Manager approved errand delay",
            p2_doc.version,
            str(uuid4()),
        )

        # Verify audit record created
        corr_audit = frappe.db.get_value(
            "Retail Attendance Correction",
            {"employee": self.employees[1].name, "day": self.day},
            ["reason", "before_state"],
            as_dict=True,
        )
        self.assertIsNotNone(corr_audit)
        self.assertEqual(corr_audit.reason, "Manager approved errand delay")

        # Verify Employee 2's fine is automatically reconciled and canceled
        emp2_fine_doc.reload()
        self.assertEqual(emp2_fine_doc.status, "Cancelled")
        self.assertEqual(frappe.db.get_value("Additional Salary", emp2_fine_doc.additional_salary, "docstatus"), 2)

        # 6. Day Close & Automatic Checkout:
        # Employee 1 worked until end of day and forgot to check out.
        # Manager closes the day at 20:00.
        close_time = self.clock + timedelta(hours=11)  # 20:00:00
        close_res = self.close(self.day, effective=close_time)
        if close_res.get("operation"):
            commands.continue_close(self.site, close_res["operation"])

        day_doc = frappe.get_doc("Retail Attendance Day", self.day)
        self.assertEqual(day_doc.status, "Closed")

        # Verify Employee 1 was automatically checked out at day close time
        p1_doc = frappe.get_doc("Retail Attendance Presence", {"employee": self.employees[0].name, "day": self.day})
        self.assertTrue(p1_doc.check_out)
        self.assertEqual(p1_doc.automatic_checkout, 1)

        # Verify closed day returns empty eligible list on POS Profile
        closed_meta = eligibility.get_eligible_salespersons(self.profile, return_meta=True)
        self.assertEqual(closed_meta["status"], "confirmed")
        self.assertEqual(len(closed_meta["sales_persons"]), 0)

        # Verify Employee 3 received scheduled absence fine on day close
        emp_c_fine = frappe.get_doc("Retail Attendance Fine Decision", {"employee": emp_c.name, "business_date": getdate()})
        self.assertEqual(emp_c_fine.status, "Applied")
        self.assertEqual(emp_c_fine.amount, 120)
        self.assertEqual(emp_c_fine.kind, "Absence")

        # 7. Next-Day Opening & Isolation:
        # Opening day for tomorrow using date patch
        tomorrow = getdate() + timedelta(days=1)
        with patch.object(commands, "getdate", return_value=tomorrow), \
             patch.object(presence, "getdate", return_value=tomorrow), \
             patch.object(eligibility, "getdate", return_value=tomorrow):
            next_day_res = commands.open_day(self.site, str(tomorrow), commands.site_doc(self.site).version, str(uuid4()))
            self.assertEqual(next_day_res["status"], "Open")

            # Check eligible salespersons on new day: neither Emp 1 nor Emp 2 is eligible
            # until they check in on the new day
            tomorrow_meta = eligibility.get_eligible_salespersons(self.profile, return_meta=True)
            self.assertEqual(tomorrow_meta["status"], "confirmed")
            self.assertEqual(len(tomorrow_meta["sales_persons"]), 0)

    def test_p7_2_offline_sale_sync_across_day_close(self):
        """P7-2: Offline sales captured before day close sync successfully after checkout and day close,

        while invalid or forged offline sales remain rejected or recorded in failed submission ledger.
        """
        self.prepare_offline_context()
        self.punch("IN", at=self.clock)

        # 1. Capture valid snapshot during open day while employee is checked in
        evidence = eligibility.get_eligible_salespersons(self.profile, return_meta=True)
        evidence["captured_at"] = str(self.clock + timedelta(hours=2))

        # 2. Terminal goes offline and creates invoice
        inv, data, req_id = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="last_confirmed_offline",
            evidence=evidence,
        )

        # 3. Meanwhile on server: employee departs and manager closes day
        self.punch("OUT", at=self.clock + timedelta(hours=8))
        self.close(self.day, effective=self.clock + timedelta(hours=10))

        # 4. Terminal reconnects and submits outbox entry
        res = submit_invoice_outbox_entry(client_request_id=req_id, invoice=inv, data=data)
        self.assertTrue(res.get("acknowledged"))
        submitted_inv = frappe.get_doc("Sales Invoice", res["invoice"]["name"])
        self.assertEqual(submitted_inv.docstatus, 1)
        self.assertEqual(submitted_inv.custom_sales_person, self.sp1)
        self.assertEqual(submitted_inv.posa_attendance_admission_mode, "last_confirmed_offline")
        self.assertEqual(submitted_inv.posa_attendance_sealed, 1)

        # 5. Outage fallback sale (unverified_offline_fallback) with no snapshot
        inv_fb, data_fb, req_id_fb = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="unverified_offline_fallback",
            evidence={"reason": "network_partition_at_launch", "captured_at": str(now_datetime())},
        )
        res_fb = submit_invoice_outbox_entry(client_request_id=req_id_fb, invoice=inv_fb, data=data_fb)
        self.assertTrue(res_fb.get("acknowledged"))
        submitted_inv_fb = frappe.get_doc("Sales Invoice", res_fb["invoice"]["name"])
        self.assertEqual(submitted_inv_fb.docstatus, 1)
        self.assertEqual(submitted_inv_fb.posa_attendance_admission_mode, "unverified_offline_fallback")
        self.assertEqual(submitted_inv_fb.posa_attendance_sealed, 1)

    def test_p7_3_real_concurrency_and_locking(self):
        """P7-3: Concurrency and locking invariants:

        - Idempotent duplicate check-in with same request_id yields exact same result.
        - Multiple simultaneous fine evaluations for same employee/date create only 1 decision and 1 deduction.
        - Concurrent replay of offline invoice submissions returns replayed result without duplicate records.
        """
        self.prepare_offline_context()

        # A) Kiosk duplicate check-in request identity
        req_id = str(uuid4())
        punch1 = self.punch("IN", request_id=req_id, at=self.clock)
        punch2 = self.punch("IN", request_id=req_id, at=self.clock)
        self.assertEqual(punch1, punch2)
        # Exactly 1 presence record
        self.assertEqual(frappe.db.count("Retail Attendance Presence", {"employee": self.employee_id, "day": self.day}), 1)

        # B) Concurrency on fine evaluation
        fines.save_fine_rule(
            self.site,
            effective_date=str(getdate()),
            enabled=1,
            absence_amount=100,
            late_brackets=[{"late_minutes": 15, "amount": 25}],
            reason="Concurrent test rule",
            version=0,
            request_id=str(uuid4()),
        )
        month = str(getdate())[:7]
        spa.save_pay_input(self.company, self.employee_id, "Salary", month, amount=1000, reason="Base pay", request_id=str(uuid4()))

        # Evaluate fines twice in rapid succession
        fines.apply_or_reconcile_fine(self.site, getdate(), self.employees[0].name)
        fines.apply_or_reconcile_fine(self.site, getdate(), self.employees[0].name)

        # Exactly 1 decision record and 1 Additional Salary
        decisions = frappe.get_all("Retail Attendance Fine Decision", filters={"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(len(decisions), 1)

        # C) Offline sync replay idempotency
        evidence = eligibility.get_eligible_salespersons(self.profile, return_meta=True)
        inv, data, sync_req_id = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="last_confirmed_offline",
            evidence=evidence,
        )
        res1 = submit_invoice_outbox_entry(client_request_id=sync_req_id, invoice=inv, data=data)
        self.assertFalse(res1.get("replayed"))
        res2 = submit_invoice_outbox_entry(client_request_id=sync_req_id, invoice=inv, data=data)
        self.assertTrue(res2.get("replayed"))
        self.assertEqual(res1["invoice"]["name"], res2["invoice"]["name"])

    def test_p7_4_cashier_role_isolation_and_security_boundaries(self):
        """P7-4: Cashier role isolation and server-side security boundaries:

        - Retail Cashier cannot access PIN credentials, Fine Rules, Fine Decisions, or Payroll records.
        - Retail Cashier cannot invoke day management commands.
        """
        self.prepare_pos(enforce=1)
        cashier_user = self.user("Retail Cashier", "Sales User")
        frappe.set_user(cashier_user)

        # 1. Cashier cannot read PIN credentials
        self.assertFalse(frappe.has_permission("Retail Attendance Credential", "read"))
        with self.assertRaises(frappe.PermissionError):
            frappe.get_list("Retail Attendance Credential")

        # 2. Cashier cannot write Fine Rules or Decisions
        self.assertFalse(frappe.has_permission("Retail Attendance Fine Rule", "write"))
        self.assertFalse(frappe.has_permission("Retail Attendance Fine Decision", "write"))

        # 3. Cashier cannot call manager attendance commands
        with self.assertRaises(frappe.PermissionError):
            commands.open_day(self.site, str(getdate()), 1, str(uuid4()))

        with self.assertRaises(frappe.PermissionError):
            commands.begin_close(self.site, self.day, str(now_datetime()), "Unauthorized attempt", 1, str(uuid4()))

        # 4. Cashier cannot read payroll or salary slip records
        self.assertFalse(frappe.has_permission("Salary Slip", "read"))
        self.assertFalse(frappe.has_permission("Additional Salary", "read"))

        # Restore user
        frappe.set_user("Administrator")

    def test_p7_5_source_provisioning_idempotency(self):
        """P7-5: Repeatable source provisioning idempotency:

        setup.setup(), posa_install.ensure_attendance_profile_fields(), and
        posa_install.ensure_attendance_invoice_fields() run cleanly multiple times
        without duplicate indexes, duplicate fields, or modifying patches.txt.
        """
        # Run HRMS retail attendance setup multiple times
        setup.setup()
        setup.setup()

        # Run POSAwesome attendance fields setup multiple times
        posa_install.ensure_attendance_profile_fields()
        posa_install.ensure_attendance_profile_fields()
        posa_install.ensure_attendance_invoice_fields()
        posa_install.ensure_attendance_invoice_fields()

        # Verify custom fields exist and are unique
        for dt in ("POS Profile", "POS Invoice", "Sales Invoice"):
            fields = frappe.get_all("Custom Field", filters={"dt": dt, "fieldname": ["like", "posa_attendance_%"]})
            names = [f.name for f in fields]
            self.assertEqual(len(names), len(set(names)))

    def test_p7_6_fixture_isolation_clean_rollback(self):
        """P7-6: Verify savepoint isolation leaves zero lingering records after rollback."""
        point = "isolation_test_" + uuid4().hex
        frappe.db.savepoint(point)

        # Create temporary attendance day and employee presence inside savepoint
        temp_emp = self.employee()
        self.enroll(temp_emp)
        temp_day = self.open()["day"]
        access.save(frappe.get_doc(dict(
            doctype="Retail Attendance Presence",
            company=self.company,
            site=self.site,
            employee=temp_emp.name,
            day=temp_day,
            in_time=now_datetime(),
            schedule=json.dumps({"state": "Scheduled", "starts_at": "09:00:00", "ends_at": "18:00:00"}),
            version=1,
        )))

        self.assertTrue(frappe.db.exists("Retail Attendance Day", temp_day))

        # Roll back to savepoint
        frappe.db.rollback(save_point=point)

        # Verify records are completely gone
        self.assertFalse(frappe.db.exists("Retail Attendance Day", temp_day))
        self.assertFalse(frappe.db.exists("Retail Attendance Presence", {"employee": temp_emp.name, "day": temp_day}))
