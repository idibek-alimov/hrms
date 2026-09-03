# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

class SalesCommissionRun(Document):
	def validate(self):
		self.validate_dates()
		self.update_totals()

	def validate_dates(self):
		if getdate(self.from_date) > getdate(self.to_date):
			frappe.throw(_("From Date cannot be greater than To Date"))

	def update_totals(self):
		total_gross = 0.0
		total_ret = 0.0
		total_net = 0.0
		total_comm = 0.0

		for item in self.get("items", []):
			total_gross += flt(item.gross_sales)
			total_ret += flt(item.returns)
			total_net += flt(item.net_sales)
			total_comm += flt(item.commission_amount)

		self.total_gross_sales = flt(total_gross, 2)
		self.total_returns = flt(total_ret, 2)
		self.total_net_sales = flt(total_net, 2)
		self.total_commission_amount = flt(total_comm, 2)

	@frappe.whitelist()
	def calculate_commissions(self):
		self.validate_dates()
		self.set("items", [])

		# 1. Fetch active Sales Persons linked to active Employees with optional filters
		conditions = ["sp.enabled = 1", "sp.is_group = 0", "e.status = 'Active'"]
		values = []

		if self.sales_person:
			conditions.append("sp.name = %s")
			values.append(self.sales_person)
		if self.employee:
			conditions.append("e.name = %s")
			values.append(self.employee)
		if self.department:
			conditions.append("e.department = %s")
			values.append(self.department)

		where_clause = " AND ".join(conditions)

		sp_records = frappe.db.sql(
			f"""
			SELECT 
				sp.name as sales_person,
				sp.employee,
				e.employee_name,
				e.department,
				sp.sales_commission_scheme
			FROM `tabSales Person` sp
			INNER JOIN `tabEmployee` e ON e.name = sp.employee
			WHERE {where_clause}
			ORDER BY e.employee_name ASC
			""",
			tuple(values),
			as_dict=1
		)

		if not sp_records:
			frappe.msgprint(_("No matching active Sales Persons linked to active Employees found."))
			self.status = "Draft"
			return

		# Pre-load company default scheme
		default_scheme_name = self.commission_scheme or frappe.db.get_value(
			"Sales Commission Scheme",
			{"company": self.company, "is_default": 1, "disabled": 0},
			"name"
		)

		schemes_cache = {}

		def get_scheme_doc(scheme_name):
			if not scheme_name:
				return None
			if scheme_name not in schemes_cache:
				try:
					schemes_cache[scheme_name] = frappe.get_doc("Sales Commission Scheme", scheme_name)
				except Exception:
					schemes_cache[scheme_name] = None
			return schemes_cache[scheme_name]

		# 2. Fetch sales per Sales Person in date range for this company
		sales_data = frappe.db.sql(
			"""
			SELECT 
				st.sales_person,
				SUM(CASE 
					WHEN si.is_return = 0 AND (st.allocated_amount IS NOT NULL AND st.allocated_amount > 0) THEN st.allocated_amount 
					WHEN si.is_return = 0 THEN (si.base_net_total * IFNULL(st.allocated_percentage, 100) / 100)
					ELSE 0 
				END) as gross_sales,
				SUM(CASE 
					WHEN si.is_return = 1 AND st.allocated_amount < 0 THEN ABS(st.allocated_amount)
					WHEN si.is_return = 1 AND st.allocated_amount > 0 THEN st.allocated_amount
					WHEN si.is_return = 1 THEN ABS(si.base_net_total * IFNULL(st.allocated_percentage, 100) / 100)
					ELSE 0 
				END) as return_sales
			FROM `tabSales Team` st
			INNER JOIN `tabSales Invoice` si ON si.name = st.parent
			WHERE si.docstatus = 1
				AND si.company = %s
				AND si.posting_date BETWEEN %s AND %s
			GROUP BY st.sales_person
			""",
			(self.company, self.from_date, self.to_date),
			as_dict=1
		)

		sales_by_sp = {d.sales_person: d for d in sales_data}

		# 3. Calculate commissions
		for sp in sp_records:
			sp_name = sp.sales_person
			sp_sales = sales_by_sp.get(sp_name, {})
			gross = flt(sp_sales.get("gross_sales", 0), 2)
			returns = flt(sp_sales.get("return_sales", 0), 2)
			net = flt(gross - returns, 2)

			# Determine scheme to use
			scheme_name = sp.get("sales_commission_scheme") or default_scheme_name
			scheme_doc = get_scheme_doc(scheme_name)

			rate = 0.0
			commission_amt = 0.0

			if scheme_doc and net > 0:
				rate, commission_amt = scheme_doc.calculate_commission(net)

			# Include in items table if there are sales or commission
			if gross > 0 or returns > 0 or commission_amt > 0:
				self.append("items", {
					"sales_person": sp_name,
					"employee": sp.employee,
					"employee_name": sp.employee_name,
					"department": sp.department,
					"gross_sales": gross,
					"returns": returns,
					"net_sales": net,
					"commission_rate": rate,
					"commission_amount": commission_amt
				})

		self.update_totals()
		self.status = "Calculated" if self.items else "Draft"

	def on_submit(self):
		if not self.items:
			frappe.throw(_("No commission items to submit. Please click 'Calculate Commission' first."))

		# Validate salary structure assignment exists for all commission earners
		for item in self.items:
			if flt(item.commission_amount) > 0:
				if not frappe.db.exists("Salary Structure Assignment", {"employee": item.employee, "docstatus": 1}):
					frappe.throw(
						_("Employee {0} ({1}) has no active Salary Structure Assignment. Please assign a Salary Structure before submitting commission payroll.").format(
							item.employee, item.employee_name
						)
					)

		created_count = 0
		for item in self.items:
			amt = flt(item.commission_amount)
			if amt > 0:
				add_sal = frappe.new_doc("Additional Salary")
				add_sal.employee = item.employee
				add_sal.salary_component = self.salary_component
				add_sal.amount = amt
				add_sal.payroll_date = self.payroll_date
				add_sal.company = self.company
				add_sal.overwrite_salary_structure_amount = 0
				add_sal.ref_doctype = self.doctype
				add_sal.ref_docname = self.name
				add_sal.submit()

				item.db_set("additional_salary", add_sal.name)
				created_count += 1

		self.db_set("status", "Submitted")
		frappe.msgprint(_("Successfully created and submitted {0} Additional Salary record(s).").format(created_count))

	def on_cancel(self):
		# Check and cancel linked Additional Salaries
		linked_add_salaries = frappe.get_all(
			"Additional Salary",
			filters={"ref_doctype": self.doctype, "ref_docname": self.name, "docstatus": 1},
			pluck="name"
		)

		for sal_name in linked_add_salaries:
			# Check if used in submitted salary slip
			slip_item = frappe.db.sql(
				"""
				SELECT ss.name 
				FROM `tabSalary Slip` ss
				INNER JOIN `tabSalary Detail` sd ON sd.parent = ss.name
				WHERE sd.additional_salary = %s AND ss.docstatus = 1
				LIMIT 1
				""",
				(sal_name,)
			)
			if slip_item:
				frappe.throw(
					_("Cannot cancel this run because Additional Salary {0} is linked to submitted Salary Slip {1}. Please cancel the Salary Slip first.").format(
						sal_name, slip_item[0][0]
					)
				)

			sal_doc = frappe.get_doc("Additional Salary", sal_name)
			sal_doc.cancel()

		for item in self.items:
			item.db_set("additional_salary", None)

		self.db_set("status", "Cancelled")
