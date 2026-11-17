"""P5 POS Profile switch and online eligibility tests."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate

from hrms.hr.retail_attendance import commands, eligibility, test_p4


class TestRetailAttendanceP5(test_p4.TestRetailAttendanceP4):
    def get_pos_profile_fixtures(self, company=None):
        comp = company or self.company
        currency = frappe.db.get_value("Company", comp, "default_currency") or "USD"
        warehouse = frappe.db.get_value("Warehouse", {"company": comp, "is_group": 0}, "name")
        if not warehouse:
            warehouse = frappe.get_doc(dict(
                doctype="Warehouse", warehouse_name="Test WH " + str(uuid4())[:8], company=comp
            )).insert(ignore_permissions=True).name

        cost_center = frappe.db.get_value("Company", comp, "cost_center")
        if not cost_center:
            cost_center = frappe.db.get_value("Cost Center", {"company": comp, "is_group": 0}, "name")
        if not cost_center:
            cost_center = frappe.get_doc(dict(
                doctype="Cost Center", cost_center_name="Main " + str(uuid4())[:8], company=comp
            )).insert(ignore_permissions=True).name

        write_off_account = frappe.db.get_value("Company", comp, "write_off_account")
        if not write_off_account:
            write_off_account = frappe.db.get_value("Account", {"company": comp, "account_type": "Expense Account", "is_group": 0}, "name")
        if not write_off_account:
            write_off_account = frappe.db.get_value("Account", {"company": comp, "root_type": "Expense", "is_group": 0}, "name")
        if not write_off_account:
            parent = frappe.db.get_value("Account", {"company": comp, "is_group": 1, "root_type": "Expense"}, "name")
            write_off_account = frappe.get_doc(dict(
                doctype="Account", account_name="Write Off " + str(uuid4())[:8], company=comp, parent_account=parent, root_type="Expense"
            )).insert(ignore_permissions=True).name

        mode_of_payment = frappe.db.get_value("Mode of Payment", {"type": "Cash"}, "name")
        if not mode_of_payment:
            mode_of_payment = frappe.db.get_value("Mode of Payment", {}, "name")
        if not mode_of_payment:
            mode_of_payment = frappe.get_doc(dict(doctype="Mode of Payment", mode_of_payment="Cash", type="Cash")).insert(ignore_permissions=True).name

        return {
            "warehouse": warehouse,
            "cost_center": cost_center,
            "write_off_account": write_off_account,
            "currency": currency,
            "mode_of_payment": mode_of_payment,
        }

    def prepare_pos(self, enforce=1):
        self.prepare()
        self.sp1_name = "SP-" + self.employees[0].name
        if not frappe.db.exists("Sales Person", self.sp1_name):
            sp1 = frappe.get_doc(dict(
                doctype="Sales Person",
                sales_person_name=self.sp1_name,
                employee=self.employees[0].name,
                enabled=1,
            )).insert(ignore_permissions=True)
            self.sp1 = sp1.name
        else:
            self.sp1 = self.sp1_name

        self.sp2_name = "SP-" + self.employees[1].name
        if not frappe.db.exists("Sales Person", self.sp2_name):
            sp2 = frappe.get_doc(dict(
                doctype="Sales Person",
                sales_person_name=self.sp2_name,
                employee=self.employees[1].name,
                enabled=1,
            )).insert(ignore_permissions=True)
            self.sp2 = sp2.name
        else:
            self.sp2 = self.sp2_name

        profile_name = "Test Profile " + self.site
        fix = self.get_pos_profile_fixtures()

        if frappe.db.exists("POS Profile", profile_name):
            self.profile = frappe.get_doc("POS Profile", profile_name)
        else:
            self.profile = frappe.get_doc(dict(
                doctype="POS Profile",
                name=profile_name,
                company=self.company,
                currency=fix["currency"],
                warehouse=fix["warehouse"],
                write_off_account=fix["write_off_account"],
                write_off_cost_center=fix["cost_center"],
                write_off_limit=100,
                posa_enforce_attendance=enforce,
                posa_attendance_site=self.site,
                payments=[dict(mode_of_payment=fix["mode_of_payment"], default=1)],
            )).insert(ignore_permissions=True)
        if not self.profile.get("payments"):
            self.profile.append("payments", dict(mode_of_payment=fix["mode_of_payment"], default=1))
        self.profile.posa_enforce_attendance = enforce
        self.profile.posa_attendance_site = self.site
        self.profile.save(ignore_permissions=True)

    def ensure_customer(self):
        c = frappe.db.get_value("Customer", {}, "name")
        if not c:
            c = frappe.get_doc(dict(
                doctype="Customer", customer_name="Test Customer " + self.site, customer_type="Individual"
            )).insert(ignore_permissions=True).name
        return c

    def ensure_item(self):
        i = frappe.db.get_value("Item", {"is_sales_item": 1}, "name")
        if not i:
            i = frappe.get_doc(dict(
                doctype="Item", item_code="Test Item " + self.site, item_name="Test Item", is_sales_item=1, item_group="All Item Groups"
            )).insert(ignore_permissions=True).name
        return i

    def make_invoice(self, sales_person=None, sales_team=None, is_return=0, pos_profile=None):
        customer = self.ensure_customer()
        item = self.ensure_item()
        profile_name = pos_profile or self.profile.name
        team = sales_team
        if team is None and sales_person:
            team = [{"sales_person": sales_person, "allocated_percentage": 100}]
        return frappe.get_doc(dict(
            doctype="Sales Invoice",
            company=self.company,
            customer=customer,
            pos_profile=profile_name,
            is_pos=1,
            is_return=is_return,
            custom_sales_person=sales_person,
            sales_team=team or [],
            items=[dict(item_code=item, qty=1, rate=100)],
        ))

    def test_profile_switch_disabled_allows_any_salesperson(self):
        self.prepare_pos(enforce=0)
        # Employee 1 is not punched in
        choices = eligibility.get_eligible_salespersons(self.profile)
        choice_names = [c["name"] for c in choices]
        self.assertIn(self.sp1, choice_names)
        self.assertIn(self.sp2, choice_names)

        inv = self.make_invoice(sales_person=self.sp1)
        # validate should succeed without error
        eligibility.validate_invoice_attendance(inv)

    def test_profile_switch_enabled_requires_valid_attendance_site(self):
        self.prepare_pos(enforce=1)
        from posawesome.posawesome.api import pos_profile as pp_api

        # Missing attendance site
        self.profile.posa_attendance_site = ""
        with self.assertRaises(frappe.ValidationError):
            pp_api.validate(self.profile)

        # Mismatched company site
        other_company = frappe.get_doc(dict(doctype="Company", company_name="Other Co " + str(uuid4())[:8], default_currency="USD")).insert(ignore_permissions=True)
        other_site = commands.configure_site(other_company.name, "Other Site", "08:00", "20:00", str(uuid4()))["site"]

        self.profile.posa_attendance_site = other_site
        with self.assertRaises(frappe.ValidationError):
            pp_api.validate(self.profile)

        # Correct site passes
        self.profile.posa_attendance_site = self.site
        pp_api.validate(self.profile)

    def test_checked_in_salesperson_is_eligible_and_attributed(self):
        self.prepare_pos(enforce=1)
        # Employee 1 punches IN
        res = self.punch("IN", at=self.clock)
        self.assertTrue(res["saved"])

        choices = eligibility.get_eligible_salespersons(self.profile)
        choice_names = [c["name"] for c in choices]
        self.assertIn(self.sp1, choice_names)
        self.assertNotIn(self.sp2, choice_names)

        inv = self.make_invoice(sales_person=self.sp1)
        eligibility.validate_invoice_attendance(inv)

    def test_not_checked_in_salesperson_is_ineligible(self):
        self.prepare_pos(enforce=1)
        # Neither punched in
        choices = eligibility.get_eligible_salespersons(self.profile)
        self.assertEqual(len(choices), 0)

        inv = self.make_invoice(sales_person=self.sp1)
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv)

    def test_checked_out_salesperson_becomes_ineligible(self):
        self.prepare_pos(enforce=1)
        self.punch("IN", at=self.clock)
        # Punches OUT 4 hours later
        self.punch("OUT", at=self.clock + timedelta(hours=4))

        choices = eligibility.get_eligible_salespersons(self.profile)
        choice_names = [c["name"] for c in choices]
        self.assertNotIn(self.sp1, choice_names)

        inv = self.make_invoice(sales_person=self.sp1)
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv)

    def test_closed_day_makes_all_salespersons_ineligible(self):
        self.prepare_pos(enforce=1)
        self.punch("IN", at=self.clock)

        # Day close
        self.close(self.day, effective=self.clock + timedelta(hours=10))

        choices = eligibility.get_eligible_salespersons(self.profile)
        self.assertEqual(len(choices), 0)

        inv = self.make_invoice(sales_person=self.sp1)
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv)

    def test_overtime_salesperson_remains_eligible_until_checkout_or_close(self):
        self.prepare_pos(enforce=1)
        self.punch("IN", at=self.clock)

        # 3 hours past shift end, but not checked out, day still open
        choices = eligibility.get_eligible_salespersons(self.profile)
        choice_names = [c["name"] for c in choices]
        self.assertIn(self.sp1, choice_names)

        inv = self.make_invoice(sales_person=self.sp1)
        eligibility.validate_invoice_attendance(inv)

    def test_return_invoice_does_not_require_original_salesperson_present(self):
        self.prepare_pos(enforce=1)
        # Employee 1 is not checked in
        inv = self.make_invoice(sales_person=self.sp1, is_return=1)
        # Should not raise
        eligibility.validate_invoice_attendance(inv)

    def test_contradictory_attribution_rejected(self):
        self.prepare_pos(enforce=1)
        self.punch("IN", at=self.clock)

        inv = self.make_invoice(
            sales_person=self.sp1,
            sales_team=[{"sales_person": self.sp2, "allocated_percentage": 100}]
        )
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv)

    def test_wrong_shop_salesperson_rejected(self):
        self.prepare_pos(enforce=1)
        self.punch("IN", at=self.clock)

        # Create another site in the same company
        other_site = commands.configure_site(self.company, "Site 2 " + str(uuid4())[:8], "08:00", "20:00", str(uuid4()))["site"]

        fix = self.get_pos_profile_fixtures()
        other_profile = frappe.get_doc(dict(
            doctype="POS Profile",
            name="Profile Site 2 " + str(uuid4())[:8],
            company=self.company,
            currency=fix["currency"],
            warehouse=fix["warehouse"],
            write_off_account=fix["write_off_account"],
            write_off_cost_center=fix["cost_center"],
            write_off_limit=100,
            posa_enforce_attendance=1,
            posa_attendance_site=other_site,
            payments=[dict(mode_of_payment=fix["mode_of_payment"], default=1)],
        )).insert(ignore_permissions=True)

        # Employee 1 is enrolled in self.site, not other_site
        inv = self.make_invoice(sales_person=self.sp1, pos_profile=other_profile.name)
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv)

    def test_crafted_request_without_checked_in_salesperson_blocked(self):
        self.prepare_pos(enforce=1)
        # Create unlinked salesperson (no employee)
        orphan_sp = frappe.get_doc(dict(
            doctype="Sales Person",
            sales_person_name="Orphan SP " + str(uuid4())[:6],
            enabled=1,
        )).insert(ignore_permissions=True)

        inv = self.make_invoice(sales_person=orphan_sp.name)
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv)

        # Disabled salesperson
        temp_emp = self.employee()
        disabled_sp = frappe.get_doc(dict(
            doctype="Sales Person",
            sales_person_name="Disabled SP " + str(uuid4())[:6],
            employee=temp_emp.name,
            enabled=0,
        )).insert(ignore_permissions=True)

        inv2 = self.make_invoice(sales_person=disabled_sp.name)
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv2)

    def test_saved_cart_revalidation_detects_departure(self):
        self.prepare_pos(enforce=1)
        self.punch("IN", at=self.clock)

        # Saved cart while checked in
        inv = self.make_invoice(sales_person=self.sp1)
        eligibility.validate_invoice_attendance(inv)

        # Employee checks out
        self.punch("OUT", at=self.clock + timedelta(hours=5))

        # Revalidation now fails
        with self.assertRaises(frappe.ValidationError):
            eligibility.validate_invoice_attendance(inv)
