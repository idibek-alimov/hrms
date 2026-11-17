"""P6 Offline admission, cache and synchronization tests."""
import json
from datetime import timedelta
from uuid import uuid4

import frappe
from frappe.utils import getdate, now_datetime

from hrms.hr.retail_attendance import commands, eligibility, test_p5
from posawesome.posawesome.api.offline_sync.invoices import submit_invoice_outbox_entry
from posawesome.posawesome.api.invoice_processing.creation import (
    trusted_invoice_shift_reassignment,
    submit_invoice,
)


class TestRetailAttendanceP6(test_p5.TestRetailAttendanceP5):
    def ensure_item(self):
        item_code = "P6-Item-" + self.site
        if not frappe.db.exists("Item", item_code):
            item_group = (
                "All Item Groups"
                if frappe.db.exists("Item Group", "All Item Groups")
                else frappe.db.get_value("Item Group", {"is_group": 0}, "name")
            )
            uom = frappe.db.get_value("UOM", {}, "name") or "Nos"
            frappe.get_doc(dict(
                doctype="Item",
                item_code=item_code,
                item_name="P6 Test Item " + self.site,
                is_sales_item=1,
                item_group=item_group,
                stock_uom=uom,
            )).insert(ignore_permissions=True)
        return item_code

    def prepare_offline_context(self):
        self.prepare_pos(enforce=1)
        self.pos_fixtures = self.get_pos_profile_fixtures()
        self.item_code = self.ensure_item()
        self.customer = self.ensure_customer()

        from posawesome.posawesome.api.terminal_state import activate_verified_cashier
        if not frappe.db.exists("POS Profile User", {"parent": self.profile.name, "user": frappe.session.user}):
            self.profile.append("applicable_for_users", {"user": frappe.session.user, "default": 0})
            self.profile.save(ignore_permissions=True)
        activate_verified_cashier(self.profile, frappe.session.user)

        # Create POS Opening Shift
        shift = frappe.get_doc(dict(
            doctype="POS Opening Shift",
            pos_profile=self.profile.name,
            company=self.company,
            user=frappe.session.user,
            period_start_date=now_datetime(),
            posting_date=getdate(),
            status="Open",
            balance_details=[dict(mode_of_payment=self.pos_fixtures["mode_of_payment"], opening_amount=100)],
        )).insert(ignore_permissions=True)
        shift.submit()
        self.shift = shift.name

    def make_invoice_payload(self, salesperson=None, admission_mode=None, evidence=None, request_id=None):
        req_id = request_id or ("req-" + str(uuid4()))
        inv = {
            "doctype": "Sales Invoice",
            "company": self.company,
            "pos_profile": self.profile.name,
            "customer": self.customer,
            "posa_pos_opening_shift": self.shift,
            "posa_client_request_id": req_id,
            "taxes": [],
            "taxes_and_charges": "",
            "items": [
                {
                    "item_code": self.item_code,
                    "qty": 1,
                    "rate": 100,
                    "amount": 100,
                }
            ],
            "payments": [
                {
                    "mode_of_payment": self.pos_fixtures["mode_of_payment"],
                    "amount": 100,
                }
            ],
        }
        if salesperson:
            inv["custom_sales_person"] = salesperson
            inv["sales_team"] = [{"sales_person": salesperson, "allocated_percentage": 100}]
        if admission_mode:
            inv["posa_attendance_admission_mode"] = admission_mode
        if evidence:
            inv["posa_attendance_evidence"] = json.dumps(evidence) if isinstance(evidence, dict) else evidence

        data = {
            "client_request_id": req_id,
            "idempotency_key": req_id,
            "company": self.company,
            "pos_profile": self.profile.name,
            "posa_pos_opening_shift": self.shift,
        }
        return inv, data, req_id

    def test_admission_evidence_generation(self):
        """P6-1: get_eligible_salespersons(return_meta=True) generates self-describing scoped evidence."""
        self.prepare_pos(enforce=1)
        self.punch("IN", at=self.clock)

        meta = eligibility.get_eligible_salespersons(self.profile, return_meta=True)
        self.assertEqual(meta["status"], "confirmed")
        self.assertTrue(meta["enforced"])
        self.assertEqual(meta["site"], self.site)
        self.assertEqual(meta["pos_profile"], self.profile.name)
        self.assertEqual(meta["company"], self.company)
        self.assertEqual(meta["day"], self.day)
        self.assertEqual(meta["day_status"], "Open")
        self.assertEqual(meta["business_date"], str(getdate()))
        self.assertTrue(meta.get("captured_at"))

        # Minimal evidence check: contains sales person name/employee, but NO salaries, fines or PINs
        sales_persons = meta["sales_persons"]
        self.assertEqual(len(sales_persons), 1)
        sp_record = sales_persons[0]
        self.assertEqual(sp_record["name"], self.sp1)
        self.assertNotIn("pin", sp_record)
        self.assertNotIn("salary", sp_record)
        self.assertNotIn("fine", sp_record)

    def test_is_attendance_snapshot_usable(self):
        """P6-2: Snapshot usability rejects cross-day, mismatched site/profile, or closed day state."""
        self.prepare_pos(enforce=1)
        base_meta = {
            "status": "confirmed",
            "enforced": True,
            "site": self.site,
            "pos_profile": self.profile.name,
            "company": self.company,
            "day": self.day,
            "day_status": "Open",
            "business_date": str(getdate()),
            "captured_at": str(now_datetime()),
            "sales_persons": [{"name": self.sp1}],
        }
        # Valid snapshot
        self.assertTrue(eligibility.is_attendance_snapshot_usable(base_meta, self.profile, getdate()))

        # Different profile
        bad_profile_meta = dict(base_meta, pos_profile="OTHER-PROFILE")
        self.assertFalse(eligibility.is_attendance_snapshot_usable(bad_profile_meta, self.profile, getdate()))

        # Different site
        bad_site_meta = dict(base_meta, site="OTHER-SITE")
        self.assertFalse(eligibility.is_attendance_snapshot_usable(bad_site_meta, self.profile, getdate()))

        # Closed day
        closed_meta = dict(base_meta, day_status="Closed")
        self.assertFalse(eligibility.is_attendance_snapshot_usable(closed_meta, self.profile, getdate()))

        # Cross-day / yesterday date
        yesterday_meta = dict(base_meta, business_date=str(getdate() - timedelta(days=1)))
        self.assertFalse(eligibility.is_attendance_snapshot_usable(yesterday_meta, self.profile, getdate()))

    def test_offline_admission_last_confirmed_sync_success(self):
        """P6-3: Offline sale admitted via last_confirmed snapshot succeeds on reconnect after checkout/close."""
        self.prepare_offline_context()
        self.punch("IN", at=self.clock)

        # Snapshot taken while employee is present online
        evidence = eligibility.get_eligible_salespersons(self.profile, return_meta=True)
        evidence["captured_at"] = str(self.clock + timedelta(hours=1))

        # Device goes offline. Employee subsequently checks out at 17:00
        self.punch("OUT", at=self.clock + timedelta(hours=8))

        # Manager closes the attendance day at 21:00
        self.close(self.day, effective=self.clock + timedelta(hours=10))

        # Now offline device reconnects and submits invoice outbox entry
        inv, data, req_id = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="last_confirmed_offline",
            evidence=evidence,
        )

        res = submit_invoice_outbox_entry(client_request_id=req_id, invoice=inv, data=data)
        self.assertTrue(res.get("acknowledged"))
        inv_name = res["invoice"]["name"]
        self.assertTrue(frappe.db.exists("Sales Invoice", inv_name))
        submitted_doc = frappe.get_doc("Sales Invoice", inv_name)
        self.assertEqual(submitted_doc.docstatus, 1)
        self.assertEqual(submitted_doc.custom_sales_person, self.sp1)
        self.assertEqual(submitted_doc.posa_attendance_admission_mode, "last_confirmed_offline")
        self.assertEqual(submitted_doc.posa_attendance_sealed, 1)

    def test_offline_admission_forged_evidence_rejected(self):
        """P6-4: Forged evidence (wrong site, fake day, or never-checked-in employee) is rejected."""
        self.prepare_offline_context()

        # Case A: Fake day
        fake_day_evidence = {
            "site": self.site,
            "company": self.company,
            "day": "RAD-FAKE-9999",
            "captured_at": str(now_datetime()),
        }
        inv, data, req_id = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="last_confirmed_offline",
            evidence=fake_day_evidence,
        )
        with self.assertRaises(frappe.ValidationError):
            submit_invoice_outbox_entry(client_request_id=req_id, invoice=inv, data=data)

        # Case B: Employee never checked in on that day
        not_checked_in_evidence = {
            "site": self.site,
            "company": self.company,
            "day": self.day,
            "captured_at": str(now_datetime()),
        }
        inv2, data2, req_id2 = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="last_confirmed_offline",
            evidence=not_checked_in_evidence,
        )
        with self.assertRaises(frappe.ValidationError):
            submit_invoice_outbox_entry(client_request_id=req_id2, invoice=inv2, data=data2)

    def test_offline_admission_unverified_offline_fallback_sync_success(self):
        """P6-5: Offline sale with no snapshot (unverified fallback) succeeds via authorized sync."""
        self.prepare_offline_context()

        inv, data, req_id = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="unverified_offline_fallback",
            evidence={"reason": "no_valid_snapshot", "captured_at": str(now_datetime())},
        )

        res = submit_invoice_outbox_entry(client_request_id=req_id, invoice=inv, data=data)
        self.assertTrue(res.get("acknowledged"))
        inv_name = res["invoice"]["name"]
        submitted_doc = frappe.get_doc("Sales Invoice", inv_name)
        self.assertEqual(submitted_doc.docstatus, 1)
        self.assertEqual(submitted_doc.posa_attendance_admission_mode, "unverified_offline_fallback")
        self.assertEqual(submitted_doc.posa_attendance_sealed, 1)

    def test_offline_admission_rejected_on_direct_online_submit(self):
        """P6-6: Direct online submit cannot claim offline admission modes to bypass real-time check."""
        self.prepare_offline_context()
        # Employee 0 has NOT checked in

        # Try claiming last_confirmed_offline directly
        inv, data, _ = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="last_confirmed_offline",
            evidence={"site": self.site, "day": self.day},
        )
        with self.assertRaises(frappe.ValidationError):
            submit_invoice(json.dumps(inv), json.dumps(data))

        # Try claiming unverified_offline_fallback directly
        inv2, data2, _ = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="unverified_offline_fallback",
        )
        with self.assertRaises(frappe.ValidationError):
            submit_invoice(json.dumps(inv2), json.dumps(data2))

    def test_confirmed_empty_offline_preserved(self):
        """P6-7: Confirmed empty snapshot accurately reflects zero checked-in salespeople."""
        self.prepare_pos(enforce=1)
        # Day is open, but 0 employees checked in
        meta = eligibility.get_eligible_salespersons(self.profile, return_meta=True)
        self.assertEqual(meta["status"], "confirmed")
        self.assertEqual(len(meta["sales_persons"]), 0)

    def test_offline_sync_idempotent_replay(self):
        """P6-8: Resubmitting identical client_request_id returns idempotent replay without duplication."""
        self.prepare_offline_context()
        self.punch("IN", at=self.clock)
        evidence = eligibility.get_eligible_salespersons(self.profile, return_meta=True)

        inv, data, req_id = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="last_confirmed_offline",
            evidence=evidence,
        )

        res1 = submit_invoice_outbox_entry(client_request_id=req_id, invoice=inv, data=data)
        self.assertFalse(res1.get("replayed"))

        res2 = submit_invoice_outbox_entry(client_request_id=req_id, invoice=inv, data=data)
        self.assertTrue(res2.get("replayed"))
        self.assertEqual(res1["invoice"]["name"], res2["invoice"]["name"])

    def test_offline_sync_failed_ledger_preserved(self):
        """P6-9: An invalid offline invoice is preserved in ledger with FAILED state, not lost."""
        self.prepare_offline_context()
        inv, data, req_id = self.make_invoice_payload(
            salesperson=self.sp1,
            admission_mode="unverified_offline_fallback",
        )
        # Contradictory attribution that causes failure
        inv["custom_sales_person"] = self.sp1
        inv["sales_team"] = [{"sales_person": self.sp2, "allocated_percentage": 100}]

        with self.assertRaises(frappe.ValidationError):
            submit_invoice_outbox_entry(client_request_id=req_id, invoice=inv, data=data)

        # Ledger exists and records failure
        ledger = frappe.db.get_value(
            "POS Invoice Submission Ledger",
            {"client_request_id": req_id},
            ["name", "state", "error_message"],
            as_dict=True,
        )
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.state, "FAILED")
        self.assertIn("conflicts with sales team", ledger.error_message)
