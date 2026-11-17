"""Repeatable source provisioning. Never enroll staff or open/close days."""
import frappe


def setup():
    # DocType JSON owns fields, permissions and unique request/employee indexes.
    # These composite indexes bound due schedules and recovery queries.
    for dt, columns, name in (
        ("Retail Attendance Day", ["site", "business_date"], "retail_site_business_date"),
        ("Retail Attendance Schedule", ["roster", "revision"], "retail_roster_revision"),
        ("Retail Attendance Fine Rule", ["site", "revision"], "retail_site_fine_revision"),
        ("Retail Attendance Fine Decision", ["employee", "business_date"], "retail_emp_date_fine_decision"),
    ):
        if frappe.db.table_exists(dt):
            frappe.db.add_unique(dt, columns, constraint_name=name)
    if frappe.db.table_exists("Retail Attendance Presence"):
        frappe.db.add_index("Retail Attendance Presence", ["day", "check_out"], "retail_open_presence")
    if frappe.db.table_exists("Retail Attendance Fine Decision"):
        frappe.db.add_index("Retail Attendance Fine Decision", ["site", "business_date"], "retail_site_fine_date")
