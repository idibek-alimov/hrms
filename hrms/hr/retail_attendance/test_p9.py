"""Password protected workday and attendance-only session regression checks."""
from unittest.mock import patch
from datetime import timedelta
from uuid import uuid4

import frappe

from hrms.hr.retail_attendance import api, screen, test_p8


class TestAttendanceP9(test_p8.TestRetailAttendanceP8):
    def test_p9_open_uses_password_and_existing_idempotent_command(self):
        self.prepare_screen()
        with self.request("/attendance-kiosk", "GET"), patch.object(screen, "check_password", return_value=self.manager), patch.object(screen.commands, "open_day", return_value={"status": "Open"}) as opening:
            request_id = str(uuid4())
            saved = screen.workday("valid", "Open", request_id, 2, business_date=str(frappe.utils.getdate()))
            self.assertEqual(saved["status"], "Open")
            opening.assert_called_once_with(self.site, str(frappe.utils.getdate()), 2, request_id)

    def test_p9_attendance_only_cannot_unlock(self):
        self.prepare_screen()
        doc = screen.current()
        doc.db_set("attendance_only", 1)
        with self.request("/attendance-kiosk", "GET"):
            self.assertTrue(screen.context()["attendance_only"])
            with self.assertRaises(frappe.PermissionError):
                screen.unlock("anything")
        self.assertTrue(screen.current().locked)

    def test_p9_close_password_and_history_scope(self):
        self.prepare_screen()
        with self.request("/attendance-kiosk", "GET"), patch.object(screen, "check_password", side_effect=frappe.AuthenticationError):
            with self.assertRaises(frappe.ValidationError):
                screen.workday("wrong", "Close", str(uuid4()), 1, day=self.day)
        self.assertEqual(frappe.db.get_value("Retail Attendance Day", self.day, "status"), "Open")
        data = api.get_day_history(self.site, self.day)
        self.assertEqual(len(data["rows"]), 2)
        self.assertTrue(all(r["cells"][0]["date"] == str(frappe.utils.getdate()) for r in data["rows"]))

    def test_p9_successful_close_retains_session_lock(self):
        self.prepare_screen()
        with self.request("/attendance-kiosk", "GET"), patch.object(screen, "check_password", return_value=self.manager), patch.object(screen.commands, "now_datetime", return_value=self.clock + timedelta(hours=10)):
            info = screen.context()["day_info"]
            saved = screen.workday("valid", "Close", str(uuid4()), info["version"], day=self.day,
                effective_close=str(self.clock + timedelta(hours=9)))
            result = screen.close_batch("valid", saved["operation"])
            self.assertEqual(result["status"], "Closed")
            self.assertFalse(screen.context()["open"])
        self.assertTrue(screen.current().locked)

    def test_waive_fine_and_enriched_board_context(self):
        self.prepare_screen()
        from erpnext.setup import staff_pay_actions
        emp = self.employees[0].name
        staff_pay_actions.save_pay_input(self.company, emp, "Salary", str(frappe.utils.getdate())[:7],
            amount=1000, reason="Test salary", request_id=str(uuid4()))
        api.save_fine_rule(self.site, str(frappe.utils.getdate()), 1, 100, '[{"late_minutes":15,"amount":20}]', 0, str(uuid4()))
        with self.request("/attendance-kiosk", "GET"), patch.object(screen, "now_datetime", return_value=self.clock + timedelta(minutes=40)):
            screen.punch(emp, "634829", "IN", str(uuid4()), self.day)
        board_data = api.get_board(self.site, str(frappe.utils.getdate()), "today")
        self.assertIn("unenrolled_count", board_data)
        emp_row = next(r for r in board_data["rows"] if r["employee"] == emp)
        self.assertTrue(emp_row["has_pin"])
        self.assertEqual(emp_row["cells"][0]["fine"]["amount"], 20)
        decision_name = emp_row["cells"][0]["fine"]["name"]
        self.assertTrue(decision_name)
        frappe.set_user("Administrator")
        res = api.waive_fine(self.site, decision_name, "Excused by store manager", str(uuid4()))
        self.assertEqual(res["status"], "Cancelled")
        history = api.get_fine_history(self.site, employee=emp, month="2026-11")
        if not history:
            history = api.get_fine_history(self.site, employee=emp, month=str(frappe.utils.getdate())[:7])
        self.assertEqual(history[0]["status"], "Cancelled")
        self.assertEqual(history[0]["cancellation_reason"], "Excused by store manager")
