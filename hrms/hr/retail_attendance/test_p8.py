"""P8 shared schedules and the manager-session employee screen, without devices."""
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate
from werkzeug.exceptions import HTTPException
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from hrms.hr.retail_attendance import api, board, commands, credentials, screen, shop_schedule, test_p2


class TestRetailAttendanceP8(test_p2.TestRetailAttendanceP2):
    def shared(self, version=0, date=None, **changes):
        values = dict(site=self.site, effective_date=str(date or getdate()), weekdays="0,1,2,3,4,5,6",
            starts_at="09:00", ends_at="18:00", version=version, request_id=str(uuid4()))
        values.update(changes)
        return shop_schedule.save_schedule(**values)

    def prepare_screen(self):
        frappe.db.set_single_value("HR Settings", "allow_geolocation_tracking", 0)
        for employee in self.employees:
            self.enroll(employee)
        self.shared()
        self.day = self.open()["day"]
        self.manager = self.user("Retail Manager")
        frappe.set_user(self.manager)
        self.sid = "screen-test-" + uuid4().hex
        frappe.session.sid = self.sid
        credentials.set_pin(self.company, self.employees[0].name, "634829", 0, str(uuid4()))
        self.assertEqual(screen.start(self.site)["url"], "/attendance-kiosk")

    def request(self, path, method="POST", data=None):
        req = Request(EnvironBuilder(base_url="http://radolfa.localhost", path=path, method=method,
            headers={"Origin":"http://radolfa.localhost", "X-Retail-Attendance":"1"}).get_environ())
        return patch.object(frappe.local, "request", req, create=True)

    def test_p8_shared_schedule_defaults_overrides_and_history(self):
        a, b = [self.enroll(e) for e in self.employees]
        self.shared()
        tomorrow = getdate() + timedelta(days=1)
        rows = board.get_board(self.site, str(getdate()))["rows"]
        self.assertTrue(all(r["cells"][0]["starts_at"].endswith("09:00:00") for r in rows))
        commands.save_schedule(self.site, a["roster"], "Exception", str(tomorrow), "Hours", "", "11:00", "18:00", "Approved later start", 0, str(uuid4()))
        self.shared(version=1, date=tomorrow, starts_at="10:00")
        rows = {r["employee"]: r for r in board.get_board(self.site, str(tomorrow))["rows"]}
        self.assertTrue(rows[self.employees[0].name]["cells"][0]["starts_at"].endswith("11:00:00"))
        self.assertTrue(rows[self.employees[1].name]["cells"][0]["starts_at"].endswith("10:00:00"))
        self.assertTrue(board.get_board(self.site, str(getdate()))["rows"][0]["cells"][0]["starts_at"].endswith("09:00:00"))
        # Explicit individual schedules remain independent, and can return to Shop.
        custom = self.regular(b, starts_at="12:00")
        commands.save_schedule(self.site, b["roster"], "Regular", str(getdate()), "Shop", "", None, None, "Return to shop schedule", custom["version"], str(uuid4()))
        rows = {r["employee"]: r for r in board.get_board(self.site, str(getdate()))["rows"]}
        self.assertTrue(rows[self.employees[1].name]["cells"][0]["starts_at"].endswith("09:00:00"))

    def test_p8_shared_schedule_retry_and_invalid_input(self):
        request = str(uuid4())
        saved = self.shared(request_id=request)
        self.assertEqual(self.shared(request_id=request), saved)
        with self.assertRaises(frappe.PermissionError):
            self.shared(request_id=request, starts_at="10:00")
        with self.assertRaises(frappe.TimestampMismatchError):
            self.shared()
        with self.assertRaises(frappe.ValidationError):
            self.shared(version=1, weekdays="")
        self.assertEqual(frappe.db.count("Retail Attendance Shop Schedule", {"site":self.site}),1)

    def test_p8_no_device_required_pin_retry_and_minimal_public_board(self):
        self.prepare_screen()
        self.assertFalse(frappe.db.exists("Retail Attendance Device", {"site":self.site}))
        request_id = str(uuid4())
        with self.request("/attendance-kiosk", "GET"), patch.object(screen, "now_datetime", return_value=self.clock + timedelta(minutes=10)):
            screen.guard_request()
            data = screen.context()
            self.assertEqual(len(data["employees"]),2)
            self.assertNotIn("salary", json.dumps(data).lower())
            with self.assertRaises(frappe.ValidationError):
                screen.punch(self.employees[0].name,"0000","IN",str(uuid4()),self.day)
            saved = screen.punch(self.employees[0].name,"634829","IN",request_id,self.day)
            self.assertTrue(saved["saved"])
            self.assertEqual(screen.punch(self.employees[0].name,"634829","IN",request_id,self.day),saved)
            self.assertEqual(screen.result(request_id)["result"],saved)
            data = screen.context()
            emp = next(e for e in data["employees"] if e["name"] == self.employees[0].name)
            self.assertEqual(set(emp["actual"][0]), {"in_time","out_time","automatic"})
            with self.assertRaises(frappe.PermissionError):
                screen.punch(self.employees[0].name,"634829","OUT",request_id,self.day)
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day":self.day}),1)

    def test_p8_session_guard_blocks_raw_apis_files_and_cmd_tunnelling(self):
        self.prepare_screen()
        for path in ("/api/resource/Employee", "/api/method/frappe.client.get_list", "/api/v2/document/Employee", "/private/files/payroll.pdf"):
            with self.request(path):
                with self.assertRaises(frappe.PermissionError): screen.guard_request()
        with self.request("/app/staff-hub", "GET"):
            with self.assertRaises(HTTPException) as caught: screen.guard_request()
            self.assertEqual(caught.exception.response.location,"/attendance-kiosk")
        with self.request("/api/method/" + screen.PREFIX + "context"):
            screen.guard_request()
            frappe.form_dict.cmd="frappe.client.get_list"
            try:
                with self.assertRaises(frappe.PermissionError): screen.guard_request()
            finally: frappe.form_dict.pop("cmd",None)
        # A new SID has no inherited lock or right to read another screen.
        frappe.session.sid = "other-" + self.sid
        self.assertIsNone(screen.current())
        with self.request("/attendance-kiosk", "GET"):
            with self.assertRaises(frappe.PermissionError): screen.context()

    def test_p8_password_unlock_failure_keeps_lock_success_does_not_close_day(self):
        self.prepare_screen()
        with self.request("/api/method/" + screen.PREFIX + "unlock"):
            with patch.object(screen,"check_password",side_effect=frappe.AuthenticationError):
                with self.assertRaises(frappe.ValidationError): screen.unlock("bad")
            self.assertTrue(screen.current().locked)
            # Cache invalidation must never discard the durable session lock.
            frappe.clear_cache(user=self.manager)
            self.assertTrue(screen.current().locked)
            with patch.object(screen,"check_password",return_value=self.manager):
                self.assertEqual(screen.unlock("correct")["url"],"/app/staff-attendance")
            self.assertIsNone(screen.current())
            screen.guard_request()
        self.assertEqual(frappe.db.get_value("Retail Attendance Day",self.day,"status"),"Open")

    def test_p8_guest_and_cashier_cannot_start_screen(self):
        for user in ("Guest",self.user("Retail Cashier")):
            frappe.set_user(user)
            with self.assertRaises(frappe.PermissionError): screen.start(self.site)

    def test_p8_pin_action_posts_one_fine_and_shows_it_on_public_board(self):
        self.prepare_screen()
        from erpnext.setup import staff_pay_actions
        staff_pay_actions.save_pay_input(self.company,self.employees[0].name,"Salary",str(getdate())[:7],
            amount=1000,reason="Test salary",request_id=str(uuid4()))
        api.save_fine_rule(self.site,str(getdate()),1,100,'[{"late_minutes":15,"amount":20}]',0,str(uuid4()))
        with self.request("/attendance-kiosk","GET"), patch.object(screen,"now_datetime",return_value=self.clock + timedelta(minutes=40)):
            screen.punch(self.employees[0].name,"634829","IN",str(uuid4()),self.day)
            row = next(e for e in screen.context()["employees"] if e["name"] == self.employees[0].name)
            self.assertEqual(row["fine"]["amount"],20)
            self.assertEqual(row["fine"]["status"],"Applied")
            self.assertNotIn("additional_salary",row["fine"])
        self.assertEqual(frappe.db.count("Additional Salary",{"employee":self.employees[0].name,"docstatus":1}),1)

    def test_p8_fine_editor_revision_includes_future_rules(self):
        tomorrow = str(getdate() + timedelta(days=1))
        api.save_fine_rule(self.site,tomorrow,1,100,'[{"late_minutes":15,"amount":20}]',0,str(uuid4()))
        config = api.get_fine_rule(self.site)
        self.assertEqual(config["latest_revision"],1)
        api.save_fine_rule(self.site,tomorrow,0,0,"[]",1,str(uuid4()))
        self.assertFalse(api.get_fine_rule(self.site)["enabled"])
