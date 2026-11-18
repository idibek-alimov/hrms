import frappe
from frappe.sessions import get_csrf_token

no_cache = 1


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=" + ("%2Fattendance-kiosk%3Fattendance_only%3D1" if frappe.form_dict.get("attendance_only") == "1" else "%2Fattendance-kiosk")
        raise frappe.Redirect
    from hrms.hr.retail_attendance import access
    access.manager()
    from hrms.hr.retail_attendance import screen
    if not screen.current():
        screen.start(attendance_only=frappe.form_dict.get("attendance_only", 0))
        frappe.local.flags.commit = True
    context.no_cache = 1
    context.csrf_token = get_csrf_token()
    context.lang = frappe.form_dict.get("lang") if frappe.form_dict.get("lang") in ("ru", "tg") else "ru"
