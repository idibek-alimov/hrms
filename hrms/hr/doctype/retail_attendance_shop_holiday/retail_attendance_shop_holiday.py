from hrms.hr.retail_attendance.access import ManagedRecord


class RetailAttendanceShopHoliday(ManagedRecord):
    def validate(self):
        if not self.is_new() and self.has_value_changed("holiday_date"):
            import frappe
            frappe.throw("A saved shop holiday date cannot be reassigned.")
