"""P3 native security/retry/correction regressions plus inherited day/schedule cases."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import now_datetime
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from hrms.hr.retail_attendance import access, api, commands, credentials, corrections, presence, test_p2


class TestRetailAttendanceP3(test_p2.TestRetailAttendanceP2):
    def prepare(self, manager=False):
        frappe.db.set_single_value("HR Settings", "allow_geolocation_tracking", 0)
        self.regular(self.enroll())
        if manager:
            self.manager = self.user("Retail Manager")
            frappe.set_user(self.manager)
        self.pin = "634829"
        self.employee_id = self.employees[0].name
        self.reset_id = str(uuid4())
        credentials.set_pin(self.company, self.employee_id, self.pin, 0, self.reset_id)
        result = credentials.provision(self.site, "Fixture kiosk", str(uuid4()))
        self.device_id = result["device"]
        cookies = {}
        frappe.local.cookie_manager = SimpleNamespace(set_cookie=lambda name, value, **kw: cookies.update({name:value}))
        credentials.pair(result["pairing_code"])
        self.token = cookies[credentials.COOKIE]
        self.day = self.open()["day"]

    def punch(self, action="IN", pin=None, request_id=None, employee=None):
        with patch.object(credentials, "cookie_token", return_value=self.token), \
            patch.object(presence, "now_datetime", return_value=self.clock + timedelta(minutes=30)):
            with access.staff().atomic():
                return presence.kiosk_action(employee or self.employee_id, pin or self.pin, action, request_id or str(uuid4()), self.day)

    def test_employee_without_login_can_arrive_and_leave_as_guest(self):
        self.prepare()
        self.assertFalse(self.employees[0].user_id)
        frappe.set_user("Guest")
        arrived = self.punch()
        self.assertTrue(arrived["saved"])
        self.assertTrue(self.punch("OUT")["saved"])
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day":self.day}), 2)
        self.assertFalse(frappe.has_permission("Employee", "read"))
        self.assertFalse(frappe.has_permission("Employee Checkin", "create"))
        with self.assertRaises(frappe.PermissionError):
            api.get_context(self.company)

    def test_duplicate_and_lost_response_have_one_result(self):
        self.prepare()
        frappe.set_user("Guest")
        request = str(uuid4())
        first = self.punch(request_id=request)
        self.assertEqual(self.punch(request_id=request), first)
        with patch.object(credentials, "cookie_token", return_value=self.token):
            self.assertEqual(presence.kiosk_result(request)["result"], first)
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day":self.day}), 1)
        with self.assertRaises(frappe.PermissionError):
            self.punch("OUT", request_id=request)

    def test_bad_pin_and_reset_revocation_are_immediate(self):
        self.prepare()
        frappe.set_user("Guest")
        with self.assertRaises(frappe.AuthenticationError):
            self.punch(pin="0000")
        frappe.set_user("Administrator")
        credentials.set_pin(self.company, self.employee_id, "123456", 1, str(uuid4()))
        frappe.set_user("Guest")
        with self.assertRaises(frappe.AuthenticationError):
            self.punch()
        self.assertTrue(self.punch(pin="123456")["saved"])
        frappe.set_user("Administrator")
        credentials.set_pin(self.company, self.employee_id, "", 2, str(uuid4()), revoke=True)
        frappe.set_user("Guest")
        with self.assertRaises(frappe.AuthenticationError):
            self.punch("OUT", pin="123456")

    def test_pin_reset_retry_contains_no_secret_and_does_not_reset_again(self):
        self.prepare()
        result = credentials.set_pin(self.company, self.employee_id, "1111", 0, self.reset_id)
        self.assertEqual(result["version"], 1)
        name = frappe.db.get_value("Retail Attendance Credential", {"employee":self.employee_id})
        credential = frappe.get_doc("Retail Attendance Credential", name)
        self.assertNotIn("pin", credential.as_dict())
        credentials.check_password(name, self.pin, doctype=credential.doctype, fieldname="pin", delete_tracker_cache=False)
        receipt = frappe.get_doc("Retail Attendance Operation", {"request_id":self.reset_id})
        self.assertNotIn(self.pin, json.dumps(receipt.as_dict(), default=str))
        self.assertNotIn("pin", json.loads(receipt.result))

    def test_device_revoke_invalidates_read_write_and_result_lookup(self):
        self.prepare()
        credentials.revoke_device(self.site, self.device_id, 1, str(uuid4()))
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            self.punch()
        with patch.object(credentials, "cookie_token", return_value=self.token):
            for fn in (presence.kiosk_context, lambda: presence.kiosk_result(str(uuid4()))):
                with self.assertRaises(frappe.PermissionError): fn()

    def test_device_and_employee_scope_and_minimal_roster(self):
        self.prepare()
        frappe.set_user("Guest")
        with patch.object(credentials, "cookie_token", return_value=self.token):
            result = presence.kiosk_context()
        self.assertEqual(set(result["employees"][0]), {"name", "employee_name"})
        self.assertEqual(len(result["employees"]), 1)
        # Not-enrolled/no-PIN employees receive the same non-enumerating failure.
        with self.assertRaises(frappe.AuthenticationError):
            self.punch(employee=self.employees[1].name)

    def test_inactive_employee_cannot_use_valid_pin(self):
        self.prepare()
        self.employees[0].status = "Inactive"
        self.employees[0].save()
        frappe.set_user("Guest")
        with self.assertRaises(frappe.ValidationError): self.punch()

    def test_failed_attempt_bound_survives_database_rollback(self):
        self.prepare()
        frappe.set_user("Guest")
        for _ in range(5):
            with self.assertRaises(frappe.AuthenticationError):
                self.punch(pin="0000")
        with self.assertRaises(frappe.AuthenticationError):
            self.punch()
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day":self.day}), 0)

    def test_attempt_guard_outage_fails_closed(self):
        self.prepare()
        frappe.set_user("Guest")
        with patch.object(frappe.cache, "eval", side_effect=ConnectionError):
            with self.assertRaises(frappe.ValidationError): self.punch()

    def test_guest_cannot_create_native_or_custom_records_outside_scope(self):
        self.prepare()
        frappe.set_user("Guest")
        for dt in ("Retail Attendance Device", "Retail Attendance Credential", "Retail Attendance Presence", "Employee Checkin"):
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc(dict(doctype=dt, company=self.company, site=self.site, employee=self.employee_id, time=self.clock, log_type="IN")).insert()

    def test_manager_without_login_editor_rights_can_manage_attendance_pin(self):
        self.prepare(manager=True)
        from erpnext.setup.staff_employee_management import get_attendance_pin
        self.assertTrue(get_attendance_pin(self.company, self.employee_id)["enabled"])
        frappe.set_user("Guest")
        self.assertTrue(self.punch()["saved"])

    def test_closed_day_and_unscheduled_arrival_cannot_create_presence(self):
        self.prepare()
        closing = self.close(self.day)
        commands.continue_close(self.site, closing["operation"])
        frappe.set_user("Guest")
        with self.assertRaises(frappe.ValidationError): self.punch()

    def test_manager_correction_keeps_original_and_revision_history(self):
        self.prepare(manager=True)
        self.punch()
        current = frappe.get_doc("Retail Attendance Presence", {"day":self.day})
        original = current.check_in
        starts = str(self.clock + timedelta(minutes=10))
        request = str(uuid4())
        result = corrections.correct(self.site, self.day, self.employee_id, starts, None, "Forgot arrival", 1, request)
        self.assertEqual(corrections.correct(self.site, self.day, self.employee_id, starts, None, "Forgot arrival", 1, request), result)
        current.reload()
        self.assertNotEqual(current.check_in, original)
        self.assertEqual(frappe.db.get_value("Employee Checkin", original, "retail_attendance_superseded_by"), current.check_in)
        self.assertEqual(frappe.db.get_value("Employee Checkin", original, "time"), self.clock + timedelta(minutes=30))
        history = api.get_presence_history(self.site, self.day, self.employee_id)
        self.assertEqual(history[0].reason, "Forgot arrival")
        with self.assertRaises(frappe.TimestampMismatchError):
            corrections.correct(self.site, self.day, self.employee_id, starts, None, "Stale", 1, str(uuid4()))

    def test_manager_missing_entry_and_closed_day_correction(self):
        self.prepare(manager=True)
        start, end = self.clock + timedelta(minutes=10), self.clock + timedelta(minutes=40)
        with patch.object(corrections, "now_datetime", return_value=self.clock + timedelta(hours=20)):
            result = corrections.correct(self.site, self.day, self.employee_id, str(start), str(end), "Forgot both", 0, str(uuid4()))
            closing = self.close(self.day)
            commands.continue_close(self.site, closing["operation"])
            corrections.correct(self.site, self.day, self.employee_id, str(start), str(end + timedelta(minutes=1)), "Actual departure", result["version"], str(uuid4()))
        self.assertEqual(frappe.db.count("Retail Attendance Correction", {"day":self.day}), 2)
        self.assertEqual(frappe.get_doc("Retail Attendance Day", self.day).status, "Closed")

    def test_correction_requires_reason_and_cannot_erase_departure(self):
        self.prepare()
        self.punch(); self.punch("OUT")
        p = frappe.get_doc("Retail Attendance Presence", {"day":self.day})
        with self.assertRaises(frappe.ValidationError):
            corrections.correct(self.site, self.day, self.employee_id, str(p.in_time), None, "", p.version, str(uuid4()))
        with self.assertRaises(frappe.ValidationError):
            corrections.correct(self.site, self.day, self.employee_id, str(p.in_time), None, "Erase", p.version, str(uuid4()))

    def test_transport_rejects_cross_origin_and_missing_custom_header(self):
        for origin, header in (("https://evil.invalid", "1"), ("http://radolfa.localhost", None)):
            request = Request(EnvironBuilder(base_url="http://radolfa.localhost", method="POST", headers={"Origin":origin, **({"X-Retail-Attendance":header} if header else {})}).get_environ())
            with patch.object(frappe.local, "request", request, create=True):
                with self.assertRaises(frappe.PermissionError): credentials.transport()

    def test_geolocation_setup_is_explicit_and_not_disabled(self):
        frappe.db.set_single_value("HR Settings", "allow_geolocation_tracking", 1)
        with self.assertRaises(frappe.ValidationError):
            credentials.provision(self.site, "GPS blocked", str(uuid4()))
        self.assertEqual(frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"), 1)

    def test_concurrent_pin_arrival_retry_uses_one_native_log(self):
        self.prepare()
        request = str(uuid4())
        with patch.object(credentials, "cookie_token", return_value=self.token):
            def job():
                frappe.set_user("Guest")
                return self.punch(request_id=request)
            results, errors = self.parallel([job, job])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day":self.day}), 1)
