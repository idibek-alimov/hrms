# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
import frappe
from frappe.utils import flt

@frappe.whitelist()
def get_cash_in_hand():
	"""Custom calculation for Cash in Hand Number Card across all Cash accounts in company."""
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		comp_list = frappe.get_all("Company", limit=1, pluck="name")
		company = comp_list[0] if comp_list else None

	if not company:
		return {"value": 0.0, "fieldtype": "Currency"}

	cash_accounts = frappe.get_all("Account", filters={"account_type": "Cash", "company": company, "is_group": 0}, pluck="name")
	if not cash_accounts:
		return {"value": 0.0, "fieldtype": "Currency"}

	balance = frappe.db.sql("""
		SELECT SUM(debit - credit)
		FROM `tabGL Entry`
		WHERE is_cancelled = 0 AND account IN %(accounts)s AND company = %(company)s
	""", {"accounts": cash_accounts, "company": company})[0][0] or 0.0

	return {"value": flt(balance, 2), "fieldtype": "Currency"}


def upsert_number_cards():
	"""Idempotently creates or updates retail number cards."""
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		comp_list = frappe.get_all("Company", limit=1, pluck="name")
		company = comp_list[0] if comp_list else ""

	cash_accounts = frappe.get_all("Account", filters={"account_type": "Cash", "is_group": 0}, pluck="name") if company else []

	cards = [
		{
			"name": "Today's Sales",
			"label": "Today's Sales",
			"type": "Document Type",
			"document_type": "Sales Invoice",
			"function": "Sum",
			"aggregate_function_based_on": "grand_total",
			"filters_json": json.dumps([
				["Sales Invoice", "docstatus", "=", "1", False],
				["Sales Invoice", "posting_date", "=", "Today", False]
			]),
			"is_public": 1,
			"module": "Accounts"
		},
		{
			"name": "Cash in Hand",
			"label": "Cash in Hand",
			"type": "Document Type",
			"document_type": "GL Entry",
			"function": "Sum",
			"aggregate_function_based_on": "debit",
			"filters_json": json.dumps([
				["GL Entry", "account", "in", cash_accounts, False],
				["GL Entry", "is_cancelled", "=", 0, False]
			]) if cash_accounts else "[]",
			"is_public": 1,
			"module": "Accounts"
		},
		{
			"name": "Low Stock Items",
			"label": "Low Stock Items",
			"type": "Document Type",
			"document_type": "Bin",
			"function": "Count",
			"filters_json": json.dumps([
				["Bin", "projected_qty", "<=", 0, False]
			]),
			"is_public": 1,
			"module": "Stock"
		},
		{
			"name": "Active Employees",
			"label": "Active Employees",
			"type": "Document Type",
			"document_type": "Employee",
			"function": "Count",
			"filters_json": json.dumps([
				["Employee", "status", "=", "Active", False]
			]),
			"is_public": 1,
			"module": "HR"
		}
	]

	for c in cards:
		c_name = c["name"]
		if frappe.db.exists("Number Card", c_name):
			doc = frappe.get_doc("Number Card", c_name)
			doc.update(c)
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc({"doctype": "Number Card", **c})
			doc.insert(ignore_permissions=True)


def upsert_dashboard_charts():
	"""Idempotently creates or updates retail dashboard charts."""
	charts = [
		{
			"chart_name": "Monthly Profit & Loss",
			"chart_type": "Sum",
			"document_type": "Sales Invoice",
			"based_on": "posting_date",
			"value_based_on": "base_net_total",
			"timeseries": 1,
			"timespan": "Last Year",
			"time_interval": "Monthly",
			"type": "Bar",
			"filters_json": json.dumps([
				["Sales Invoice", "docstatus", "=", "1", False]
			]),
			"is_public": 1,
			"module": "Accounts"
		},
		{
			"chart_name": "Sales Performance by Sales Person",
			"chart_type": "Group By",
			"document_type": "Sales Team",
			"parent_document_type": "Sales Invoice",
			"group_by_based_on": "sales_person",
			"group_by_type": "Sum",
			"aggregate_function_based_on": "allocated_amount",
			"type": "Bar",
			"filters_json": json.dumps([
				["Sales Team", "parenttype", "=", "Sales Invoice", False]
			]),
			"is_public": 1,
			"module": "Selling"
		},
		{
			"chart_name": "Best-Selling Categories",
			"chart_type": "Group By",
			"document_type": "Sales Invoice Item",
			"parent_document_type": "Sales Invoice",
			"group_by_based_on": "item_group",
			"group_by_type": "Sum",
			"aggregate_function_based_on": "base_net_amount",
			"type": "Donut",
			"filters_json": json.dumps([
				["Sales Invoice Item", "docstatus", "=", "1", False]
			]),
			"is_public": 1,
			"module": "Stock"
		},
		{
			"chart_name": "Top 10 Fast-Moving Items",
			"chart_type": "Group By",
			"document_type": "Sales Invoice Item",
			"parent_document_type": "Sales Invoice",
			"group_by_based_on": "item_name",
			"group_by_type": "Sum",
			"aggregate_function_based_on": "qty",
			"number_of_groups": 10,
			"type": "Bar",
			"filters_json": json.dumps([
				["Sales Invoice Item", "docstatus", "=", "1", False]
			]),
			"is_public": 1,
			"module": "Stock"
		},
		{
			"chart_name": "Payment Method Breakdown",
			"chart_type": "Group By",
			"document_type": "Sales Invoice Payment",
			"parent_document_type": "Sales Invoice",
			"group_by_based_on": "mode_of_payment",
			"group_by_type": "Sum",
			"aggregate_function_based_on": "amount",
			"type": "Donut",
			"filters_json": json.dumps([
				["Sales Invoice Payment", "docstatus", "=", "1", False]
			]),
			"is_public": 1,
			"module": "Accounts"
		}
	]

	for c in charts:
		c_name = c["chart_name"]
		if frappe.db.exists("Dashboard Chart", c_name):
			doc = frappe.get_doc("Dashboard Chart", c_name)
			doc.update(c)
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc({"doctype": "Dashboard Chart", **c})
			doc.insert(ignore_permissions=True)


def configure_home_workspace():
	"""Configures the Home Workspace desk layout with cards, charts, shortcuts, and link blocks."""
	workspace_content = [
		{"id": "nc_hdr", "type": "header", "data": {"text": "<span class=\"h4\"><b>Retail Key Metrics</b></span>", "col": 12}},
		{"id": "nc_1", "type": "number_card", "data": {"number_card_name": "Today's Sales", "col": 3}},
		{"id": "nc_2", "type": "number_card", "data": {"number_card_name": "Cash in Hand", "col": 3}},
		{"id": "nc_3", "type": "number_card", "data": {"number_card_name": "Low Stock Items", "col": 3}},
		{"id": "nc_4", "type": "number_card", "data": {"number_card_name": "Active Employees", "col": 3}},
		{"id": "sp_1", "type": "spacer", "data": {"col": 12}},

		{"id": "sc_hdr", "type": "header", "data": {"text": "<span class=\"h4\"><b>Quick Shortcuts</b></span>", "col": 12}},
		{"id": "sc_1", "type": "shortcut", "data": {"shortcut_name": "Point of Sale", "col": 3}},
		{"id": "sc_2", "type": "shortcut", "data": {"shortcut_name": "Item", "col": 3}},
		{"id": "sc_3", "type": "shortcut", "data": {"shortcut_name": "Stock Entry", "col": 3}},
		{"id": "sc_4", "type": "shortcut", "data": {"shortcut_name": "Purchase Receipt", "col": 3}},
		{"id": "sc_5", "type": "shortcut", "data": {"shortcut_name": "Payment Entry", "col": 3}},
		{"id": "sc_6", "type": "shortcut", "data": {"shortcut_name": "Additional Salary", "col": 3}},
		{"id": "sc_7", "type": "shortcut", "data": {"shortcut_name": "Stock Balance", "col": 3}},
		{"id": "sp_2", "type": "spacer", "data": {"col": 12}},

		{"id": "dc_hdr", "type": "header", "data": {"text": "<span class=\"h4\"><b>Retail & Performance Analytics</b></span>", "col": 12}},
		{"id": "dc_1", "type": "chart", "data": {"chart_name": "Monthly Profit & Loss", "col": 6}},
		{"id": "dc_2", "type": "chart", "data": {"chart_name": "Sales Performance by Sales Person", "col": 6}},
		{"id": "dc_3", "type": "chart", "data": {"chart_name": "Best-Selling Categories", "col": 6}},
		{"id": "dc_4", "type": "chart", "data": {"chart_name": "Payment Method Breakdown", "col": 6}},
		{"id": "dc_5", "type": "chart", "data": {"chart_name": "Top 10 Fast-Moving Items", "col": 12}},
		{"id": "sp_3", "type": "spacer", "data": {"col": 12}},

		{"id": "cd_hdr", "type": "header", "data": {"text": "<span class=\"h4\"><b>Operations & Reports</b></span>", "col": 12}},
		{"id": "cd_1", "type": "card", "data": {"card_name": "Sales & Register", "col": 4}},
		{"id": "cd_2", "type": "card", "data": {"card_name": "Inventory Management", "col": 4}},
		{"id": "cd_3", "type": "card", "data": {"card_name": "Staff & Payroll", "col": 4}},
		{"id": "cd_4", "type": "card", "data": {"card_name": "Financial Reports", "col": 4}}
	]

	number_cards = [
		{"number_card_name": "Today's Sales", "label": "Today's Sales"},
		{"number_card_name": "Cash in Hand", "label": "Cash in Hand"},
		{"number_card_name": "Low Stock Items", "label": "Low Stock Items"},
		{"number_card_name": "Active Employees", "label": "Active Employees"}
	]

	charts = [
		{"chart_name": "Monthly Profit & Loss", "label": "Monthly Profit & Loss"},
		{"chart_name": "Sales Performance by Sales Person", "label": "Sales Performance by Sales Person"},
		{"chart_name": "Best-Selling Categories", "label": "Best-Selling Categories"},
		{"chart_name": "Payment Method Breakdown", "label": "Payment Method Breakdown"},
		{"chart_name": "Top 10 Fast-Moving Items", "label": "Top 10 Fast-Moving Items"}
	]

	shortcuts = [
		{"label": "Point of Sale", "link_to": "point-of-sale", "type": "Page", "color": "Blue"},
		{"label": "Item", "link_to": "Item", "type": "DocType", "color": "Grey"},
		{"label": "Stock Entry", "link_to": "Stock Entry", "type": "DocType", "color": "Grey"},
		{"label": "Purchase Receipt", "link_to": "Purchase Receipt", "type": "DocType", "color": "Grey"},
		{"label": "Payment Entry", "link_to": "Payment Entry", "type": "DocType", "color": "Grey"},
		{"label": "Additional Salary", "link_to": "Additional Salary", "type": "DocType", "color": "Grey"},
		{"label": "Stock Balance", "link_to": "Stock Balance", "type": "Report", "color": "Green"}
	]

	links = [
		{"label": "Sales & Register", "type": "Card Break"},
		{"label": "POS Opening Entry", "link_type": "DocType", "link_to": "POS Opening Entry", "type": "Link"},
		{"label": "POS Closing Entry", "link_type": "DocType", "link_to": "POS Closing Entry", "type": "Link"},
		{"label": "Sales Invoice", "link_type": "DocType", "link_to": "Sales Invoice", "type": "Link"},
		{"label": "Customer", "link_type": "DocType", "link_to": "Customer", "type": "Link"},
		{"label": "POS Register", "link_type": "Report", "link_to": "POS Register", "type": "Link", "is_query_report": 1},

		{"label": "Inventory Management", "type": "Card Break"},
		{"label": "Item", "link_type": "DocType", "link_to": "Item", "type": "Link"},
		{"label": "Stock Ledger", "link_type": "Report", "link_to": "Stock Ledger", "type": "Link", "is_query_report": 1},
		{"label": "Item Price", "link_type": "DocType", "link_to": "Item Price", "type": "Link"},
		{"label": "Purchase Order", "link_type": "DocType", "link_to": "Purchase Order", "type": "Link"},
		{"label": "Purchase Receipt", "link_type": "DocType", "link_to": "Purchase Receipt", "type": "Link"},

		{"label": "Staff & Payroll", "type": "Card Break"},
		{"label": "Employee", "link_type": "DocType", "link_to": "Employee", "type": "Link"},
		{"label": "Employee Checkin", "link_type": "DocType", "link_to": "Employee Checkin", "type": "Link"},
		{"label": "Additional Salary", "link_type": "DocType", "link_to": "Additional Salary", "type": "Link"},
		{"label": "Payroll Entry", "link_type": "DocType", "link_to": "Payroll Entry", "type": "Link"},
		{"label": "Salary Slip", "link_type": "DocType", "link_to": "Salary Slip", "type": "Link"},

		{"label": "Financial Reports", "type": "Card Break"},
		{"label": "General Ledger", "link_type": "Report", "link_to": "General Ledger", "type": "Link", "is_query_report": 1},
		{"label": "Accounts Receivable Summary", "link_type": "Report", "link_to": "Accounts Receivable Summary", "type": "Link", "is_query_report": 1},
		{"label": "Cash Flow", "link_type": "Report", "link_to": "Cash Flow", "type": "Link", "is_query_report": 1},
		{"label": "Profit and Loss Statement", "link_type": "Report", "link_to": "Profit and Loss Statement", "type": "Link", "is_query_report": 1}
	]

	# Upsert Home Workspace
	ws_name = "Home"
	if frappe.db.exists("Workspace", ws_name):
		ws = frappe.get_doc("Workspace", ws_name)
	else:
		ws = frappe.new_doc("Workspace")
		ws.name = ws_name
		ws.label = ws_name
		ws.title = ws_name
		ws.sequence_id = 1.0
		ws.icon = "getting-started"
		ws.public = 1
		ws.module = "Setup"

	ws.content = json.dumps(workspace_content)
	ws.set("number_cards", number_cards)
	ws.set("charts", charts)
	ws.set("shortcuts", shortcuts)
	ws.set("links", links)
	ws.public = 1
	ws.is_hidden = 0
	ws.save(ignore_permissions=True)


def setup_home_workspace():
	"""Master entry point for after_migrate hook."""
	orig_in_migrate = getattr(frappe.flags, "in_migrate", False)
	frappe.flags.in_migrate = True
	try:
		upsert_number_cards()
		upsert_dashboard_charts()
		configure_home_workspace()
		frappe.db.commit()
		print("Successfully initialized Retail Home Workspace, Number Cards, and Dashboard Charts.")
	except Exception as e:
		frappe.log_error(f"Error setting up retail home workspace: {e}", "Setup Retail Workspace")
		print(f"Warning: could not setup retail home workspace: {e}")
	finally:
		frappe.flags.in_migrate = orig_in_migrate
