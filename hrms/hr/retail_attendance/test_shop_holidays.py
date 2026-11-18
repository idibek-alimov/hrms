"""Shop scope, native HR/payroll reads and retry/permission invariants."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate

from hrms.hr.retail_attendance import board, commands, holidays, test_p4


class TestShopHolidays(test_p4.TestRetailAttendanceP4):
    def test_holidays_seed_cleanup_preserves_named_and_later_manual_holidays(self):
        from hrms.payroll.setup_retail_workspace import remove_generated_sundays, ensure_default_holiday_list
        ensure_default_holiday_list()
        doc = frappe.get_doc("Holiday List", self.company + " Standard Holiday List")
        doc.weekly_off = "Sunday"
        doc.get_weekly_off_dates()
        doc.holidays[0].description = "Named holiday on Sunday"
        preserved = getdate(doc.holidays[0].holiday_date)
        doc.save()
        frappe.db.set_default("retail_generated_sundays_removed_v1", None)
        remove_generated_sundays()
        doc.reload()
        self.assertEqual(len(doc.holidays), 1)
        self.assertEqual(getdate(doc.holidays[0].holiday_date), preserved)
        self.assertFalse(doc.weekly_off)
        doc.weekly_off = "Sunday"
        doc.get_weekly_off_dates()
        doc.save()
        remove_generated_sundays()
        self.assertGreater(len(doc.reload().holidays), 1)

    def plan(self, **changes):
        values = dict(site=self.site, mode="Dates", version=commands.site_doc(self.site).version,
            request_id=str(uuid4()), dates=[str(getdate() + timedelta(days=1))], description="Shop holiday")
        values.update(changes)
        return holidays.save_holidays(**values)

    def enroll_with_empty_native_list(self, employee):
        self.regular(self.enroll(employee))
        doc = frappe.get_doc(dict(doctype="Holiday List", holiday_list_name="Shop holiday test " + uuid4().hex,
            from_date=str(getdate().replace(month=1, day=1)), to_date=str(getdate().replace(year=getdate().year+1, month=12, day=31))))
        doc.insert()
        frappe.db.set_value("Employee", employee.name, "holiday_list", doc.name)
        frappe.clear_document_cache("Employee", employee.name)

    def test_holidays_expand_dates_validation(self):
        first = getdate() + timedelta(days=1)
        dates = holidays.expand_dates("Weekdays", from_date=str(first), to_date=str(first+timedelta(days=13)), weekdays="6")
        self.assertEqual(len(dates), 2)
        self.assertTrue(all(d.weekday() == 6 for d in dates))
        self.assertEqual(len(holidays.expand_dates("Range", from_date=str(first), to_date=str(first+timedelta(days=2)))), 3)
        for values in (dict(mode="Dates", dates=[]), dict(mode="Weekdays", from_date=str(first), to_date=str(first), weekdays=""),
                dict(mode="Range", from_date=str(first), to_date=str(first+timedelta(days=366)))):
            with self.assertRaises(frappe.ValidationError): holidays.expand_dates(**values)

    def test_holidays_sunday_is_scheduled_without_explicit_holiday(self):
        self.enroll_with_empty_native_list(self.employees[0])
        sunday = getdate() + timedelta(days=(6-getdate().weekday()) % 7)
        self.assertEqual(board.get_board(self.site, str(sunday), "day")["rows"][0]["cells"][0]["state"], "Scheduled")

    def test_holidays_individual_schedule_shop_scope_and_native_reads(self):
        a, b = self.employees
        self.enroll_with_empty_native_list(a)
        other = commands.configure_site(self.company, "Other holiday shop", "09:00", "18:00", str(uuid4()))["site"]
        enrolled = commands.enroll(other, b.name, str(getdate()), str(uuid4()))
        commands.save_schedule(other, enrolled["roster"], "Regular", str(getdate()), "Hours", "0,1,2,3,4,5,6", "10:00", "19:00", "", 0, str(uuid4()))
        target = getdate() + timedelta(days=1)
        self.plan(dates=[str(target)])
        self.assertEqual(board.get_board(self.site, str(target), "day")["rows"][0]["cells"][0]["state"], "Holiday")
        self.assertEqual(board.get_board(other, str(target), "day")["rows"][0]["cells"][0]["state"], "Scheduled")
        from erpnext.setup.doctype.employee.employee import is_holiday
        from hrms.hr.utils import get_holidays_for_employee
        from hrms.payroll.doctype.salary_slip.salary_slip import SalarySlip
        self.assertTrue(is_holiday(a.name, str(target)))
        self.assertFalse(holidays.employee_holidays(b.name, target, target))
        self.assertIn(target, [getdate(r.holiday_date) for r in get_holidays_for_employee(a.name, target, target)])
        slip = SalarySlip(dict(doctype="Salary Slip", employee=a.name, company=self.company))
        self.assertIn(target, slip.get_holidays_for_employee(target, target))

    def test_holidays_retry_stale_cancel_and_weekly_semantics(self):
        self.enroll_with_empty_native_list(self.employees[0])
        target = getdate() + timedelta(days=1)
        version = commands.site_doc(self.site).version
        request = str(uuid4())
        saved = self.plan(mode="Weekdays", from_date=str(target), to_date=str(target), weekdays=str(target.weekday()), request_id=request, version=version)
        self.assertEqual(self.plan(mode="Weekdays", from_date=str(target), to_date=str(target), weekdays=str(target.weekday()), request_id=request, version=version), saved)
        self.assertEqual(frappe.db.count(holidays.DOCTYPE, {"site": self.site}), 1)
        self.assertFalse(holidays.employee_holidays(self.employees[0].name, target, target, only_non_weekly=True))
        with self.assertRaises(frappe.TimestampMismatchError): self.plan(version=version)
        self.plan(active=0, description="")
        row = frappe.get_doc(holidays.DOCTYPE, {"site": self.site, "holiday_date": target})
        self.assertFalse(row.active)
        self.assertEqual(row.version, 2)
        self.assertFalse(holidays.employee_holidays(self.employees[0].name, target, target))
        self.plan(description="Named holiday")
        self.plan(mode="Weekdays", from_date=str(target), to_date=str(target), weekdays=str(target.weekday()))
        native = holidays.employee_holidays(self.employees[0].name, target, target, only_non_weekly=True)
        self.assertEqual(native[0].description, "Named holiday")

    def test_holidays_cashier_and_raw_write_denied(self):
        user = self.user("Retail Cashier")
        frappe.set_user(user)
        with self.assertRaises(frappe.PermissionError): holidays.get_holidays(self.site)
        with self.assertRaises(frappe.PermissionError): self.plan()
        frappe.set_user("Administrator")
        doc = frappe.get_doc(dict(doctype=holidays.DOCTYPE, site=self.site, company=self.company,
            holiday_date=str(getdate()), active=1, description="Bypass"))
        with self.assertRaises(frappe.PermissionError): doc.insert()

    def test_holidays_today_reconciles_existing_fine(self):
        self.prepare_with_fines()
        self.punch("IN", at=self.clock+timedelta(minutes=40))
        decision = frappe.get_doc("Retail Attendance Fine Decision", {"employee": self.employee_id, "business_date": getdate()})
        self.assertEqual(decision.status, "Applied")
        self.plan(dates=[str(getdate())])
        decision.reload()
        self.assertNotEqual(decision.status, "Applied")
        self.assertEqual(frappe.db.count("Additional Salary", {"employee": self.employee_id, "docstatus": 1}), 0)
