// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sales Commission Scheme", {
	refresh(frm) {
		frm.set_query("salary_component", () => {
			return {
				filters: {
					type: "Earning"
				}
			};
		});
	}
});
