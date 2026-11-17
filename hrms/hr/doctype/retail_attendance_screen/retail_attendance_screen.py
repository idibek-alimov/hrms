import frappe
from frappe.model.document import Document


class RetailAttendanceScreen(Document):
    def validate(self):
        # Internal session state. Never writable through generic CRUD, even by a
        # manager; only the start/unlock commands carry this narrow capability.
        if not self.flags.ignore_permissions:
            frappe.throw("Use the Attendance page.", frappe.PermissionError)
