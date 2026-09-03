# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, round_based_on_smallest_currency_fraction

class SalesCommissionScheme(Document):
	def validate(self):
		self.validate_slabs()
		self.validate_default()

	def validate_slabs(self):
		if not self.slabs:
			frappe.throw(_("Please add at least one commission slab in the table."))

		# Sort slabs by from_amount
		self.slabs = sorted(self.slabs, key=lambda x: flt(x.from_amount))
		for i, slab in enumerate(self.slabs):
			slab.idx = i + 1
			from_amt = flt(slab.from_amount)
			to_amt = flt(slab.to_amount)
			rate = flt(slab.commission_rate)

			if from_amt < 0:
				frappe.throw(_("Row #{0}: From Amount cannot be negative").format(slab.idx))
			if to_amt < 0:
				frappe.throw(_("Row #{0}: To Amount cannot be negative").format(slab.idx))
			if to_amt > 0 and from_amt >= to_amt:
				frappe.throw(
					_("Row #{0}: From Amount ({1}) must be less than To Amount ({2})").format(
						slab.idx, from_amt, to_amt
					)
				)
			if rate < 0:
				frappe.throw(_("Row #{0}: Commission Rate cannot be negative").format(slab.idx))

	def validate_default(self):
		if self.is_default and not self.disabled:
			# Unset default on other schemes for the same company
			frappe.db.sql(
				"""
				UPDATE `tabSales Commission Scheme`
				SET is_default = 0
				WHERE company = %s AND name != %s AND is_default = 1
				""",
				(self.company, self.name),
			)

	def calculate_commission(self, net_sales):
		"""
		Calculates commission based on net sales amount.
		Returns (applicable_rate, commission_amount)
		"""
		net_sales = flt(net_sales)
		if net_sales <= 0 or not self.slabs:
			return 0.0, 0.0

		if self.calculation_type == "Flat Tier Rate":
			matched_rate = 0.0
			for slab in sorted(self.slabs, key=lambda x: flt(x.from_amount)):
				from_amt = flt(slab.from_amount)
				to_amt = flt(slab.to_amount)
				if net_sales >= from_amt:
					if to_amt == 0 or net_sales <= to_amt:
						matched_rate = flt(slab.commission_rate)
						break
					elif net_sales > to_amt:
						matched_rate = flt(slab.commission_rate)

			commission_amt = flt(net_sales * (matched_rate / 100.0), 2)
			return matched_rate, commission_amt

		elif self.calculation_type == "Progressive Marginal Slabs":
			total_commission = 0.0
			for slab in sorted(self.slabs, key=lambda x: flt(x.from_amount)):
				from_amt = flt(slab.from_amount)
				to_amt = flt(slab.to_amount)
				rate = flt(slab.commission_rate)

				if net_sales > from_amt:
					if to_amt > 0:
						subject_amt = min(net_sales, to_amt) - from_amt
					else:
						subject_amt = net_sales - from_amt
					total_commission += subject_amt * (rate / 100.0)

			total_commission = flt(total_commission, 2)
			effective_rate = flt((total_commission / net_sales) * 100.0, 2) if net_sales > 0 else 0.0
			return effective_rate, total_commission

		return 0.0, 0.0
