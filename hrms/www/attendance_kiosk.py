import frappe
from frappe.sessions import get_csrf_token

no_cache = 1


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/app/staff-attendance"
        raise frappe.Redirect
    from hrms.hr.retail_attendance import access
    access.manager()
    context.no_cache = 1
    context.csrf_token = get_csrf_token()
    context.lang = frappe.form_dict.get("lang") if frappe.form_dict.get("lang") in ("ru", "tg") else "ru"
