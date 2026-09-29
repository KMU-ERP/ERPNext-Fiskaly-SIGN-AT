from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.jinja import validate_template

from erpnext_fiskaly_sign_at.providers.http import redact
from erpnext_fiskaly_sign_at.services.receipt_builder import build_receipt_request


class TestInstallation(IntegrationTestCase):
	def test_all_standard_desk_documents_are_installed(self):
		for doctype, name in (
			("Workspace", "Fiskaly RKSV"),
			("Workspace Sidebar", "Fiskaly RKSV"),
			("Desktop Icon", "Fiskaly RKSV"),
			("Report", "Fiskaly Receipt Status"),
			("Print Format", "POS Invoice RKSV"),
			("Print Format", "Fiskaly Closing Receipt RKSV"),
		):
			self.assertTrue(frappe.db.exists(doctype, name), f"Missing {doctype} {name}")
		self.assertTrue(frappe.db.exists("DocType", "Fiskaly Lifecycle Event"))

	def test_pos_invoice_custom_fields_are_post_submit_safe(self):
		meta = frappe.get_meta("POS Invoice")
		for fieldname in (
			"fiskaly_receipt",
			"fiskaly_status",
			"fiskaly_company_name",
			"fiskaly_company_address",
			"fiskaly_receipt_uuid",
			"fiskaly_provider_receipt_id",
			"fiskaly_provider_register_id",
			"fiskaly_signature_creation_unit_id",
			"fiskaly_gross_standard",
			"fiskaly_gross_reduced_1",
			"fiskaly_gross_reduced_2",
			"fiskaly_gross_zero",
			"fiskaly_gross_special",
			"fiskaly_qr_format",
			"fiskaly_encrypted_turnover_counter",
			"fiskaly_certificate_serial_number",
			"fiskaly_previous_receipt_signature",
			"fiskaly_qr_code_data",
			"fiskaly_signature_value",
			"fiskaly_print_lines",
		):
			field = meta.get_field(fieldname)
			self.assertIsNotNone(field)
			self.assertEqual(field.read_only, 1)
			self.assertEqual(field.allow_on_submit, 1)

	def test_fiskaly_fields_use_a_dedicated_pos_invoice_tab(self):
		meta = frappe.get_meta("POS Invoice")
		tab = meta.get_field("fiskaly_tab")
		section = meta.get_field("fiskaly_section")
		self.assertIsNotNone(tab)
		self.assertEqual(tab.fieldtype, "Tab Break")
		self.assertEqual(tab.insert_after, "title")
		self.assertEqual(section.insert_after, "fiskaly_tab")
		self.assertEqual(section.collapsible, 0)
		self.assertEqual(meta.get_field("fiskaly_audit_section").collapsible, 1)

		field_order = [field.fieldname for field in meta.fields]
		tab_index = field_order.index("fiskaly_tab")
		for fieldname in (
			"fiskaly_receipt",
			"fiskaly_status",
			"fiskaly_company_name",
			"fiskaly_company_address",
			"fiskaly_qr_code_data",
			"fiskaly_signature_value",
			"fiskaly_print_lines",
		):
			self.assertGreater(field_order.index(fieldname), tab_index)

	def test_compliance_evidence_fields_are_installed(self):
		receipt_meta = frappe.get_meta("Fiskaly Receipt")
		for fieldname in (
			"company",
			"company_address_display",
			"annual_compliance_status",
			"annual_action_required_reason",
			"provider_request_payload",
			"provider_request_payload_sha256",
			"offline_issued_at",
			"offline_qr_code_data",
			"offline_receipt_snapshot",
			"offline_snapshot_sha256",
			"response_payload_sha256",
			"fon_validation_status",
			"verification_deadline",
			"verification_status",
			"print_evidence_at",
		):
			self.assertIsNotNone(receipt_meta.get_field(fieldname))
		self.assertIn("SUBSTITUTE_SIGNED", receipt_meta.get_field("status").options)
		self.assertIn("RECOVERY", receipt_meta.get_field("receipt_kind").options)

		register_meta = frappe.get_meta("Fiskaly Register")
		for fieldname in (
			"outage_scope",
			"outage_started_at",
			"outage_deadline_at",
			"fon_outage_status",
			"last_quarterly_backup_at",
		):
			self.assertIsNotNone(register_meta.get_field(fieldname))

		self.assertIsNotNone(frappe.get_meta("Fiskaly VAT Mapping").get_field("tax_account"))

		dep7_meta = frappe.get_meta("Fiskaly DEP7 Export")
		for fieldname in (
			"supplementary_export_file",
			"supplementary_file_hash",
			"supplementary_file_size",
			"supplementary_integrity_verified_at",
		):
			self.assertIsNotNone(dep7_meta.get_field(fieldname))

	def test_print_format_jinja_is_valid(self):
		for name in ("POS Invoice RKSV", "Fiskaly Closing Receipt RKSV"):
			html = frappe.db.get_value("Print Format", name, "html")
			validate_template(html)
			self.assertIn("fiskaly_qr_svg", html)
		special_html = frappe.db.get_value("Print Format", "Fiskaly Closing Receipt RKSV", "html")
		for expected in (
			"company_address_display",
			"SUBSTITUTE_SIGNED",
			"Barzahlungsbeträge",
			"4,9 %",
			"Kassen-ID",
		):
			self.assertIn(expected, special_html)

	def test_secret_redaction_is_recursive(self):
		data = {
			"api_key": "key",
			"content": {
				"secret": "secret",
				"pin": "pin",
				"fon_user_pin": "fon-pin",
				"refresh_token": "refresh-token",
			},
			"safe": "value",
		}
		self.assertEqual(
			redact(data),
			{
				"api_key": "***",
				"content": {
					"secret": "***",
					"pin": "***",
					"fon_user_pin": "***",
					"refresh_token": "***",
				},
				"safe": "value",
			},
		)

	def test_pos_tax_breakdown_builds_exact_fiscal_total(self):
		invoice = SimpleNamespace(
			posting_date="2026-08-18",
			posting_time="10:30:00",
			currency="EUR",
			grand_total=12,
			is_return=0,
			name="ACC-POS-INV-0001",
			owner="Administrator",
			items=[
				SimpleNamespace(
					name="row-1",
					item_code="ITEM-1",
					item_name="Test item",
					description="",
					qty=1,
					net_amount=10,
					amount=10,
					uom="Nos",
				)
			],
			item_wise_tax_details=[SimpleNamespace(item_row="row-1", tax_row="tax-1", rate=20, amount=2)],
			taxes=[
				SimpleNamespace(
					name="tax-1",
					charge_type="On Net Total",
					account_head="VAT 20",
					account_type="Tax",
					account_tax_rate=20,
					description="VAT 20%",
				)
			],
			payments=[
				SimpleNamespace(
					type="Cash",
					amount=12,
					mode_of_payment="Cash",
					fiskaly_rksv_payment_type="",
				)
			],
		)
		register = SimpleNamespace(
			provider="SIGN_AT_V1",
			vat_mappings=[
				SimpleNamespace(
					tax_rate=20,
					v1_bucket="standard",
					unified_code=None,
					tax_account=None,
					is_exempt=0,
					exemption_reason=None,
				)
			],
		)
		request = build_receipt_request(invoice, register, "9ca279e0-c1d1-4d30-8c74-35db704282d2")
		self.assertEqual(str(request.total_gross), "12.00")
		self.assertEqual(request.vat_buckets[0].code, "standard")
