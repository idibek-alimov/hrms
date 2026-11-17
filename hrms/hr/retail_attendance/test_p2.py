"""Native attendance invariants on the active development site.

No production/site enrollment, financial fixtures, PINs or POS transactions.
"""
import json
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate, now_datetime

from hrms.hr.retail_attendance import access, api, board, commands, events, rules, schedules


class TestRetailAttendanceP2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if frappe.local.site != "radolfa.localhost":
            raise unittest.SkipTest("Requires the radolfa.localhost development site")
        frappe.set_user("Administrator")
        if not frappe.db.exists("Gender", "Male"):
            frappe.get_doc(dict(doctype="Gender", gender="Male")).insert()
        cls.company = "_Attendance P2 Test"
        if not frappe.db.exists("Company", cls.company):
            frappe.get_doc(dict(doctype="Company", company_name=cls.company, abbr="ATP2", country="Tajikistan",
                default_currency="TJS", chart_of_accounts="Standard", create_chart_of_accounts_based_on="Standard Template",
                enable_perpetual_inventory=0)).insert()
        cls.other_company = "_Attendance P2 Other"
        if not frappe.db.exists("Company", cls.other_company):
            frappe.get_doc(dict(doctype="Company", company_name=cls.other_company, abbr="ATPO", country="Tajikistan",
                default_currency="TJS", chart_of_accounts="Standard", create_chart_of_accounts_based_on="Standard Template",
                enable_perpetual_inventory=0)).insert()
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")
        self.point = "attendance_test_" + uuid4().hex
        frappe.db.savepoint(self.point)
        self.clock = now_datetime().replace(hour=9, minute=0, second=0, microsecond=0)
        self.site = commands.configure_site(self.company, "Fixture shop", "09:00", "18:00", str(uuid4()))["site"]
        self.employees = [self.employee(), self.employee()]

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.clear_messages()

    def employee(self):
        return frappe.get_doc(dict(doctype="Employee", first_name="Attendance fixture " + uuid4().hex[:8],
            company=self.company, status="Active", gender="Male", date_of_birth="1990-01-01", date_of_joining="2020-01-01")).insert()

    def enroll(self, emp=None):
        emp = emp or self.employees[0]
        return commands.enroll(self.site, emp.name, str(getdate()), str(uuid4()))

    def regular(self, roster, **changes):
        kwargs = dict(site=self.site, roster=roster["roster"], kind="Regular", effective_date=str(getdate()), mode="Hours",
            weekdays="0,1,2,3,4,5,6", starts_at="09:00", ends_at="18:00", reason="", version=roster["version"], request_id=str(uuid4()))
        kwargs.update(changes)
        return commands.save_schedule(**kwargs)

    def open(self):
        with patch.object(commands, "now_datetime", return_value=self.clock):
            return commands.open_day(self.site, str(getdate()), commands.site_doc(self.site).version, str(uuid4()))

    def presence(self, emp, day, stamp=None):
        """Private fixture for P3's future atomic Employee -> site/day writer."""
        stamp = stamp or self.clock + timedelta(minutes=10)
        commands.lock_employee(emp.name)
        commands.site_doc(self.site, lock=True)
        current = frappe.get_doc("Retail Attendance Day", day, for_update=True)
        if current.status != "Open":
            frappe.throw("Attendance day is not open")
        planned = dict(starts_at=str(self.clock), ends_at=str(self.clock + timedelta(hours=9)), shift=None)
        p = access.save(frappe.get_doc(dict(doctype="Retail Attendance Presence", company=self.company, site=self.site,
            employee=emp.name, day=day, in_time=stamp, schedule=json.dumps(planned), version=1)))
        with events.native_event(p, "IN", stamp):
            log = frappe.get_doc(dict(doctype="Employee Checkin", employee=emp.name, time=stamp, log_type="IN",
                retail_attendance_presence=p.name, retail_attendance_day=day)).insert()
        p.check_in = log.name
        access.save(p)
        return p

    def close(self, day, effective=None, request_id=None):
        effective = effective or self.clock + timedelta(hours=10)
        # Retries must reuse the original request's version; first use reads the live one.
        versions = self.__dict__.setdefault("close_versions", {})
        versions.setdefault(day, frappe.db.get_value("Retail Attendance Day", day, "version"))
        with patch.object(commands, "now_datetime", return_value=self.clock + timedelta(hours=20)):
            return commands.begin_close(self.site, day, str(effective), "Fixture close",
                versions[day], request_id or str(uuid4()))

    def test_individual_exception_does_not_change_colleagues(self):
        a, b = [self.enroll(e) for e in self.employees]
        a, b = self.regular(a), self.regular(b)
        tomorrow = getdate() + timedelta(days=1)
        commands.save_schedule(self.site, a["roster"], "Exception", str(tomorrow), "Hours", "", "11:00", "18:00", "Agreed late start", a["version"], str(uuid4()))
        data = board.get_board(self.site, str(tomorrow))
        cells = {r["employee"]: r["cells"][0] for r in data["rows"]}
        self.assertTrue(cells[self.employees[0].name]["starts_at"].endswith("11:00:00"))
        self.assertTrue(cells[self.employees[1].name]["starts_at"].endswith("09:00:00"))
        self.assertEqual(frappe.db.count("Retail Attendance Schedule", {"employee": self.employees[0].name}), 2)

    def test_off_and_restore_usual_keep_audit(self):
        row = self.regular(self.enroll())
        date = str(getdate() + timedelta(days=1))
        off = commands.save_schedule(self.site, row["roster"], "Exception", date, "Off", "", None, None, "Approved day off", 1, str(uuid4()))
        self.assertEqual(board.get_board(self.site, date)["rows"][0]["cells"][0]["state"], "Day off")
        commands.save_schedule(self.site, row["roster"], "Exception", date, "Usual", "", None, None, "Canceled day off", off["version"], str(uuid4()))
        self.assertEqual(board.get_board(self.site, date)["rows"][0]["cells"][0]["state"], "Scheduled")
        self.assertEqual(frappe.db.count("Retail Attendance Schedule", {"roster": row["roster"]}), 3)

    def test_stale_schedule_and_retry_payload_conflict(self):
        row = self.enroll()
        request = str(uuid4())
        first = self.regular(row, request_id=request)
        self.assertEqual(self.regular(row, request_id=request), first)
        with self.assertRaises(frappe.PermissionError):
            self.regular(row, request_id=request, starts_at="10:00")
        with self.assertRaises(frappe.TimestampMismatchError):
            self.regular(row, starts_at="10:00")

    def test_manual_midnight_and_previous_day_recovery(self):
        result = self.open()
        next_day = getdate() + timedelta(days=1)
        with patch.object(commands, "getdate", return_value=next_day):
            with self.assertRaises(frappe.ValidationError):
                commands.open_day(self.site, str(next_day), commands.site_doc(self.site).version, str(uuid4()))
        self.assertEqual(frappe.get_doc("Retail Attendance Day", result["day"]).status, "Open")
        close = self.close(result["day"], effective=self.clock + timedelta(hours=18))
        done = commands.continue_close(self.site, close["operation"])
        self.assertEqual(done["status"], "Closed")

    def test_close_retry_is_one_native_out_with_provenance(self):
        self.regular(self.enroll())
        day = self.open()["day"]
        p = self.presence(self.employees[0], day)
        request = str(uuid4())
        closing = self.close(day, request_id=request)
        self.assertEqual(self.close(day, request_id=request)["operation"], closing["operation"])
        self.assertEqual(commands.continue_close(self.site, closing["operation"])["status"], "Closed")
        self.assertEqual(commands.continue_close(self.site, closing["operation"])["status"], "Closed")
        p.reload()
        out = frappe.get_doc("Employee Checkin", p.check_out)
        self.assertEqual(out.log_type, "OUT")
        self.assertEqual(out.retail_attendance_operation, closing["operation"])
        self.assertTrue(p.automatic_checkout)
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_presence": p.name, "log_type": "OUT"}), 1)
        with self.assertRaises(frappe.PermissionError):
            out.time += timedelta(minutes=1)
            out.save()

    def test_interrupted_close_can_resume_without_repeating_saved_employee(self):
        for e in self.employees:
            self.regular(self.enroll(e))
        day = self.open()["day"]
        presences = [self.presence(e, day) for e in self.employees]
        closing = self.close(day)
        original = events.automatic_checkout
        count = [0]
        def fail_second(*args):
            count[0] += 1
            if count[0] == 2:
                frappe.throw("Fixture native conflict")
            return original(*args)
        with patch.object(events, "automatic_checkout", side_effect=fail_second):
            pending = commands.continue_close(self.site, closing["operation"])
        self.assertEqual(pending["status"], "Closing")
        self.assertEqual(pending["remaining"], 1)
        self.assertEqual(pending["error"], "Native conflict")
        done = commands.continue_close(self.site, closing["operation"])
        self.assertEqual(done["status"], "Closed")
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day": day, "log_type": "OUT"}), 2)

    def test_backdated_close_cannot_precede_existing_arrival(self):
        self.regular(self.enroll())
        day = self.open()["day"]
        self.presence(self.employees[0], day, self.clock + timedelta(hours=2))
        with self.assertRaises(frappe.ValidationError):
            self.close(day, effective=self.clock + timedelta(hours=1))
        self.assertEqual(frappe.get_doc("Retail Attendance Day", day).status, "Open")

    def test_closing_blocks_new_presence(self):
        self.regular(self.enroll())
        day = self.open()["day"]
        self.close(day)
        with self.assertRaises(frappe.ValidationError):
            self.presence(self.employees[0], day)
        self.assertEqual(frappe.db.count("Retail Attendance Presence", {"day": day}), 0)

    def test_native_raw_write_and_history_delete_blocked(self):
        self.regular(self.enroll())
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(dict(doctype="Employee Checkin", employee=self.employees[0].name, log_type="IN", time=self.clock)).insert()
        doc = commands.site_doc(self.site)
        doc.site_name = "Raw rewrite"
        with self.assertRaises(frappe.PermissionError):
            doc.save()
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc(doc.doctype, doc.name)

    def test_reads_do_not_create_operations_or_days(self):
        self.regular(self.enroll())
        before = [frappe.db.count("Retail Attendance " + s) for s in ("Day", "Operation", "Presence")]
        board.get_board(self.site, str(getdate()))
        self.assertEqual(before, [frappe.db.count("Retail Attendance " + s) for s in ("Day", "Operation", "Presence")])

    def test_native_absence_paths_skip_enrolled_employee_only(self):
        self.enroll()
        from hrms.hr.doctype.shift_type.shift_type import ShiftType
        shift = ShiftType(dict(doctype="Shift Type", name="Fixture shift", start_time="09:00", last_sync_of_checkin=str(self.clock)))
        start, end = getdate(), getdate() + timedelta(days=1)
        with patch.object(shift, "get_start_and_end_dates", return_value=(start, end)), patch.object(shift, "get_holiday_list", return_value=None), \
            patch("hrms.hr.doctype.shift_type.shift_type.get_holiday_dates_between", return_value=[]), \
            patch.object(shift, "get_marked_attendance_dates_between", return_value=[]):
            self.assertEqual(shift.get_dates_for_attendance(self.employees[0].name), [])
            self.assertEqual(len(shift.get_dates_for_attendance(self.employees[1].name)), 2)

    def test_cashier_cannot_read_manager_attendance(self):
        user = self.user("Retail Cashier", "Sales User")
        frappe.set_user(user)
        with self.assertRaises(frappe.PermissionError):
            api.get_context(self.company)
        with self.assertRaises(frappe.PermissionError):
            commands.site_doc(self.site)
        self.assertEqual(access.query_conditions(user, "Retail Attendance Day"), "1=0")

    def test_manager_company_restriction_applies_to_reads_and_commands(self):
        user = self.user("Retail Manager", "HR User", "Accounts User")
        frappe.get_doc(dict(doctype="User Permission", user=user, allow="Company", for_value=self.other_company,
            apply_to_all_doctypes=1)).insert()
        frappe.set_user(user)
        with self.assertRaises(frappe.PermissionError):
            commands.site_doc(self.site)
        with self.assertRaises(frappe.PermissionError):
            api.get_context(self.company)
        self.assertEqual(frappe.get_list("Retail Attendance Site", filters={"company": self.company}, pluck="name"), [])

    def user(self, *roles):
        return frappe.get_doc(dict(doctype="User", email="attendance-" + uuid4().hex + "@example.invalid",
            first_name="Attendance fixture", enabled=1, user_type="System User", send_welcome_email=0,
            roles=[dict(role=r) for r in roles])).insert().name

    def test_ordinary_manager_can_configure_enroll_and_schedule(self):
        user = self.user("Retail Manager", "HR User", "Accounts User")
        frappe.set_user(user)
        result = commands.configure_site(self.company, "Manager fixture shop", "09:00", "18:00", str(uuid4()))
        self.site = result["site"]
        row = self.regular(self.enroll())
        self.assertEqual(row["version"], 1)
        self.assertTrue(board.get_board(self.site, str(getdate()))["rows"])
        day = self.open()["day"]
        self.presence(self.employees[0], day)
        closing = self.close(day)
        self.assertEqual(commands.continue_close(self.site, closing["operation"])["status"], "Closed")
        self.assertTrue(api.get_schedule_history(self.site, row["roster"]))

    def test_geolocation_validation_leaves_recoverable_closing_state(self):
        self.regular(self.enroll())
        day = self.open()["day"]
        self.presence(self.employees[0], day)
        closing = self.close(day)
        frappe.db.set_single_value("HR Settings", "allow_geolocation_tracking", 1)
        result = commands.continue_close(self.site, closing["operation"])
        self.assertEqual(result["status"], "Closing")
        self.assertEqual(result["remaining"], 1)
        self.assertFalse(frappe.get_doc("Retail Attendance Presence", frappe.db.get_value("Retail Attendance Presence", {"day": day})).check_out)

    def parallel(self, jobs):
        # Only disposable test-site fixtures are committed for separate real DB
        # sessions. No threads share a Frappe connection or fake an SQL lock.
        frappe.db.commit()
        current_site = frappe.local.site
        gate = threading.Barrier(len(jobs))
        results, errors = [], []
        def run(job):
            frappe.init(site=current_site)
            frappe.connect()
            frappe.set_user("Administrator")
            frappe.flags.in_test = True
            frappe.flags.mute_emails = True
            try:
                gate.wait(timeout=10)
                result = job()
                frappe.db.commit()
                results.append(result)
            except Exception as exc:
                frappe.db.rollback()
                errors.append(exc)
            finally:
                frappe.destroy()
        threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "Concurrent attendance command did not finish")
        return results, errors

    def test_concurrent_same_open_request_creates_one_day(self):
        request_id, version = str(uuid4()), commands.site_doc(self.site).version
        def job():
            return commands.open_day(self.site, str(getdate()), version, request_id)
        results, errors = self.parallel([job, job])
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["day"], results[1]["day"])
        self.assertEqual(frappe.db.count("Retail Attendance Day", {"site": self.site}), 1)

    def test_parallel_close_workers_create_one_out_per_presence(self):
        for emp in self.employees:
            self.regular(self.enroll(emp))
        day = self.open()["day"]
        for emp in self.employees:
            self.presence(emp, day)
        closing = self.close(day)
        results, errors = self.parallel([lambda: commands.continue_close(self.site, closing["operation"])] * 2)
        self.assertEqual(errors, [])
        self.assertEqual([r["status"] for r in results], ["Closed", "Closed"])
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day": day, "log_type": "OUT"}), 2)

    def test_arrival_racing_close_is_checked_out_or_rejected(self):
        self.regular(self.enroll())
        day = self.open()["day"]
        def arrive():
            return self.presence(self.employees[0], day).name
        results, errors = self.parallel([arrive, lambda: self.close(day)])
        self.assertTrue(all(isinstance(e, frappe.ValidationError) for e in errors), errors)
        operation = frappe.get_doc("Retail Attendance Day", day).close_operation
        self.assertTrue(operation)
        commands.continue_close(self.site, operation)
        self.assertEqual(frappe.db.count("Retail Attendance Presence", {"day": day, "check_out": ["is", "not set"]}), 0)
        self.assertEqual(frappe.get_doc("Retail Attendance Day", day).status, "Closed")

    def test_replay_enrollment_after_midnight_recovers_original_result(self):
        request = str(uuid4())
        first = commands.enroll(self.site, self.employees[0].name, str(getdate()), request)
        original = commands.getdate
        with patch.object(commands, "getdate", side_effect=lambda value=None: original(value) if value else original() + timedelta(days=1)):
            replay = commands.enroll(self.site, self.employees[0].name, str(getdate()), request)
        self.assertEqual(first, replay)

    def test_native_holiday_and_approved_leave_are_not_scheduled_absence(self):
        self.regular(self.enroll())
        target = getdate() + timedelta(days=1)
        holidays = frappe.get_doc(dict(doctype="Holiday List", holiday_list_name="Attendance holiday " + uuid4().hex,
            from_date=str(target.replace(month=1, day=1)), to_date=str(target.replace(month=12, day=31)),
            holidays=[dict(holiday_date=target, description="Fixture approved holiday")])).insert()
        emp = self.employees[0]
        emp.holiday_list = holidays.name
        emp.save()
        self.assertEqual(board.get_board(self.site, str(target))["rows"][0]["cells"][0]["state"], "Holiday")
        leave_type = frappe.get_doc(dict(doctype="Leave Type", leave_type_name="Attendance LWP " + uuid4().hex,
            is_lwp=1, include_holiday=1)).insert()
        leave = frappe.get_doc(dict(doctype="Leave Application", employee=emp.name, company=self.company,
            leave_type=leave_type.name, from_date=target, to_date=target, status="Approved",
            posting_date=getdate())).insert().submit()
        self.assertEqual(leave.docstatus, 1)
        self.assertEqual(board.get_board(self.site, str(target))["rows"][0]["cells"][0]["state"], "Approved leave")

    def test_native_shift_template_and_automatic_attendance_conflict(self):
        shift = frappe.get_doc(dict(doctype="Shift Type", name="Attendance shift " + uuid4().hex, __islocal=1,
            start_time="22:00", end_time="06:00", enable_auto_attendance=0)).insert()
        emp = self.employees[0]
        emp.default_shift = shift.name
        emp.save()
        row = self.enroll(emp)
        self.regular(row, mode="Native", starts_at=None, ends_at=None)
        planned = board.get_board(self.site, str(getdate()))["rows"][0]["cells"][0]
        self.assertTrue(planned["starts_at"].endswith("22:00:00"))
        self.assertNotEqual(planned["starts_at"][:10], planned["ends_at"][:10])
        # A later native setup change is visible as review, never a false absence.
        shift.enable_auto_attendance = 1
        shift.save()
        self.assertEqual(board.get_board(self.site, str(getdate()))["rows"][0]["cells"][0]["state"], "Review")

    def test_today_keeps_open_business_day_after_midnight(self):
        self.regular(self.enroll())
        day = self.open()["day"]
        self.presence(self.employees[0], day)
        tomorrow = getdate() + timedelta(days=1)
        data = board.get_board(self.site, str(tomorrow), view="today")
        self.assertEqual(data["start_date"], str(getdate()))
        self.assertEqual(len(data["rows"][0]["cells"][0]["actual"]), 1)

    def test_committed_close_batch_survives_next_request_rollback(self):
        for employee in self.employees:
            self.regular(self.enroll(employee))
        day = self.open()["day"]
        for employee in self.employees:
            self.presence(employee, day)
        closing = self.close(day)
        frappe.db.commit()
        with patch.object(commands, "CLOSE_BATCH_SIZE", 1):
            first = api.continue_close(self.site, closing["operation"])
        self.assertEqual(first["status"], "Closing")
        self.assertEqual(first["remaining"], 1)
        frappe.db.commit()
        # A later interrupted request cannot erase the earlier committed OUT.
        frappe.db.rollback()
        done = api.continue_close(self.site, closing["operation"])
        self.assertEqual(done["status"], "Closed")
        self.assertEqual(frappe.db.count("Employee Checkin", {"retail_attendance_day": day, "log_type": "OUT"}), 2)


class TestAttendanceDateRules(unittest.TestCase):
    def test_overnight_shift_and_negative_closing_difference(self):
        first, last = rules.span("2026-11-17", "22:00", "06:00")
        self.assertEqual((last - first).total_seconds(), 8 * 3600)
        self.assertEqual(last.day, 18)
        self.assertEqual(rules.hours_difference(last, last - timedelta(minutes=20)), -20)

    def test_higher_revision_does_not_override_a_later_effective_regular(self):
        rows = [dict(kind="Regular", effective_date="2026-11-17", revision=9, mode="Hours"),
            dict(kind="Regular", effective_date="2026-11-20", revision=2, mode="Hours")]
        self.assertEqual(rules.choose_revision(rows, "2026-11-21")[0]["revision"], 2)

    def test_reject_equal_times_and_invalid_weekdays(self):
        with self.assertRaises(ValueError):
            rules.span("2026-11-17", "09:00", "09:00")
        for value in ("", "0,0", "7", "-1"):
            with self.assertRaises(ValueError):
                rules.weekdays(value)
