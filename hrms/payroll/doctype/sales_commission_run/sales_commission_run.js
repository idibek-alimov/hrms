// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sales Commission Run", {
	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Calculate Commission"), () => {
				if (!frm.doc.company || !frm.doc.from_date || !frm.doc.to_date || !frm.doc.payroll_date) {
					frappe.msgprint(__("Please fill Company, From Date, To Date, and Payroll Date first."));
					return;
				}
				frappe.call({
					doc: frm.doc,
					method: "calculate_commissions",
					freeze: true,
					freeze_message: __("Calculating sales and commissions..."),
					callback: function(r) {
						frm.refresh_fields();
						frappe.show_alert({
							message: __("Commission calculation complete. Check the items below."),
							indicator: "green"
						});
					}
				});
			}).addClass("btn-primary");
		}

		frm.set_query("salary_component", () => {
			return {
				filters: {
					type: "Earning"
				}
			};
		});

		frm.set_query("commission_scheme", () => {
			return {
				filters: {
					company: frm.doc.company || "",
					disabled: 0
				}
			};
		});
	}
});
