from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.jinja import validate_template
from markupsafe import Markup

from erpnext_fiskaly_sign_at.api.printing import _lock_pos_print_row, render_pos_receipt
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_api_connection.fiskaly_api_connection import (
	validate_base_url_override,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_settings.fiskaly_settings import (
	FiskalySettings,
)
from erpnext_fiskaly_sign_at.install import PRINT_FORMAT_HTML, RKSV_OFFLINE_NOTICE, RKSV_PRINT_FORMAT
from erpnext_fiskaly_sign_at.print_utils import fiskaly_qr_svg
from erpnext_fiskaly_sign_at.services.compliance import (
	CONTROLLED_PRINT_CLAIM_FLAG,
	PRINT_PREVIEW_COMMAND,
	_apply_rksv_print_context,
	_is_verified_pos_consolidation,
	prevent_unfiscalized_cash_payment_entry,
	prevent_unfiscalized_sales_invoice,
	validate_pos_profile,
	validate_pos_print,
)


class _RecordingQRCode:
	def __init__(self):
		self.options = None

	def svg(self, stream, **options):
		self.options = options
		stream.write(b'<svg class="rksv-qr-svg"></svg>')


class TestUIPrintSecurity(IntegrationTestCase):
	@patch("erpnext_fiskaly_sign_at.services.compliance._active_register_for")
	def test_manual_cash_entry_rejects_conflicting_erpnext_prefill(self, active_register):
		doc = frappe._dict(
			company="Example Company",
			name="POS-1",
			fiskaly_manual_cash_entry=1,
			set_grand_total_to_default_mop=1,
		)

		with self.assertRaisesRegex(frappe.ValidationError, "Set Grand Total"):
			validate_pos_profile(doc)
		active_register.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.services.compliance._active_register_for", return_value=None)
	def test_manual_cash_entry_allows_disabled_erpnext_prefill(self, active_register):
		doc = frappe._dict(
			company="Example Company",
			name="POS-1",
			fiskaly_manual_cash_entry=1,
			set_grand_total_to_default_mop=0,
		)

		validate_pos_profile(doc)
		active_register.assert_called_once_with("Example Company", "POS-1")

	@patch("erpnext_fiskaly_sign_at.services.compliance._active_register_for", return_value=None)
	def test_before_print_hook_accepts_frappe_16_print_settings_argument(self, _active_register):
		doc = frappe._dict(
			company="Example Company",
			pos_profile="POS-1",
			fiskaly_status="NOT_CONFIGURED",
		)

		validate_pos_print(doc, "before_print", {})

	@patch("erpnext_fiskaly_sign_at.services.compliance._active_register_for", return_value="REGISTER-1")
	def test_omitted_print_format_cannot_fall_back_to_standard(self, _active_register):
		doc = frappe._dict(
			company="Example Company",
			pos_profile="POS-1",
			fiskaly_status="SIGNED",
		)
		old_form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.form_dict = frappe._dict()
		try:
			with self.assertRaises(frappe.ValidationError):
				validate_pos_print(doc)
		finally:
			frappe.local.form_dict = old_form_dict

	def test_qr_renderer_enforces_quiet_zone_and_white_background(self):
		qr = _RecordingQRCode()
		with patch("erpnext_fiskaly_sign_at.print_utils.pyqrcode.create", return_value=qr):
			result = fiskaly_qr_svg("_R1-AT1_test", scale=1, quiet_zone=1)

		self.assertIsInstance(result, Markup)
		self.assertEqual(qr.options["quiet_zone"], 4)
		self.assertEqual(qr.options["scale"], 2)
		self.assertEqual(qr.options["background"], "#fff")
		self.assertFalse(qr.options["xmldecl"])

	def test_endpoint_override_is_restricted_without_developer_mode(self):
		self.assertEqual(
			validate_base_url_override("https://sandbox.fiskaly.com/custom/", "TEST", developer_mode=False),
			"https://sandbox.fiskaly.com/custom",
		)
		self.assertEqual(
			validate_base_url_override("http://localhost:8123/api/", "TEST", developer_mode=False),
			"http://localhost:8123/api",
		)
		for unsafe in (
			"http://api.fiskaly.com/api",
			"https://fiskaly.com.example.org/api",
			"https://user:password@api.fiskaly.com/api",
			"https://api.fiskaly.com/api?tenant=other",
		):
			with self.subTest(unsafe=unsafe), self.assertRaises(frappe.ValidationError):
				validate_base_url_override(unsafe, "TEST", developer_mode=False)
		with self.assertRaises(frappe.ValidationError):
			validate_base_url_override("http://localhost:8123/api", "LIVE", developer_mode=False)

	def test_developer_mode_is_explicit_endpoint_escape_hatch(self):
		self.assertEqual(
			validate_base_url_override("http://mock-fiskaly.internal:9000/v3/", "TEST", developer_mode=True),
			"http://mock-fiskaly.internal:9000/v3",
		)
		with self.assertRaises(frappe.ValidationError):
			validate_base_url_override("http://mock-fiskaly.internal:9000/v3/", "LIVE", developer_mode=True)

	def test_unified_live_is_rejected_server_side(self):
		doc = frappe.new_doc("Fiskaly API Connection")
		doc.update(
			{
				"connection_name": "unified-live-must-fail",
				"company": "not-needed-for-validation",
				"provider": "SIGN_AT_UNIFIED",
				"environment": "LIVE",
				"api_key": "test",
				"api_secret": "test",
				"critical_change_confirmation": 1,
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_critical_settings_change_requires_one_shot_confirmation(self):
		doc = frappe.get_doc("Fiskaly Settings")
		doc.critical_change_confirmation = 0
		with (
			patch.object(FiskalySettings, "_critical_changes", return_value=("enabled",)),
			self.assertRaises(frappe.ValidationError),
		):
			doc.validate()

	def test_confirmed_settings_change_is_audited_and_confirmation_is_reset(self):
		doc = frappe.get_doc("Fiskaly Settings")
		doc.enabled = 0
		doc.enforce_pos_only_cash_receipts = 0 if doc.enforce_pos_only_cash_receipts else 1
		doc.critical_change_confirmation = 1
		with patch.object(FiskalySettings, "_block_switch_with_pending_receipts"):
			doc.save(ignore_permissions=True)
		self.assertEqual(doc.critical_change_confirmation, 0)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": "Fiskaly Settings",
					"reference_name": "Fiskaly Settings",
					"content": ["like", "%enforce_pos_only_cash_receipts%"],
				},
			)
		)

	def test_endpoint_override_requires_system_manager_even_in_test(self):
		doc = frappe.new_doc("Fiskaly API Connection")
		doc.update(
			{
				"connection_name": "privileged-override-must-fail",
				"company": "not-needed-for-validation",
				"provider": "SIGN_AT_V1",
				"environment": "TEST",
				"api_key": "test",
				"api_secret": "test",
				"base_url_override": "https://sandbox.fiskaly.com/mock",
				"critical_change_confirmation": 1,
			}
		)
		with (
			patch(
				"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_api_connection.fiskaly_api_connection._is_system_manager",
				return_value=False,
			),
			self.assertRaises(frappe.ValidationError),
		):
			doc.validate()

	def test_settings_and_endpoint_security_metadata(self):
		settings_meta = frappe.get_meta("Fiskaly Settings")
		self.assertEqual(settings_meta.track_changes, 1)
		self.assertEqual(settings_meta.get_field("allow_unified_live").hidden, 1)
		self.assertEqual(settings_meta.get_field("offline_notice").hidden, 1)
		self.assertEqual(settings_meta.get_field("critical_change_confirmation").permlevel, 1)
		self.assertEqual(
			settings_meta.get_field("enforce_pos_only_cash_receipts").default,
			"1",
		)
		for removed_fieldname in (
			"default_test_connection",
			"default_live_connection",
			"print_section",
			"print_help",
		):
			self.assertIsNone(settings_meta.get_field(removed_fieldname))

		connection_meta = frappe.get_meta("Fiskaly API Connection")
		self.assertEqual(connection_meta.get_field("base_url_override").permlevel, 1)
		for fieldname in (
			"fon_section",
			"fon_help_html",
			"fon_participant_id",
			"fon_column",
			"fon_user_id",
			"fon_user_pin",
		):
			self.assertFalse(connection_meta.get_field(fieldname).depends_on)

	def test_active_rksv_requires_cash_bypass_protection(self):
		with self.assertRaises(frappe.ValidationError):
			FiskalySettings._validate_enabled_operation(SimpleNamespace(enforce_pos_only_cash_receipts=0))

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_settings.fiskaly_settings.get_system_timezone",
		return_value="Europe/Vienna",
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_settings.fiskaly_settings.frappe.get_doc"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_settings.fiskaly_settings.frappe.get_all"
	)
	def test_active_rksv_validates_the_connection_linked_to_each_register(
		self, get_all, get_doc, _timezone
	):
		get_all.return_value = [
			frappe._dict(
				name="REGISTER-1",
				company="Example GmbH",
				provider="SIGN_AT_V1",
				initialized=1,
				pos_profile="POS-1",
				connection="CONNECTION-1",
				location_address="ADDRESS-1",
			)
		]
		get_doc.return_value = frappe._dict(
			name="CONNECTION-1",
			active=0,
			company="Example GmbH",
			provider="SIGN_AT_V1",
			environment="TEST",
		)
		settings = SimpleNamespace(
			enforce_pos_only_cash_receipts=1,
			operating_environment="TEST",
		)

		with self.assertRaises(frappe.ValidationError):
			FiskalySettings._validate_enabled_operation(settings)
		get_doc.assert_called_once_with("Fiskaly API Connection", "CONNECTION-1")

	@patch("erpnext_fiskaly_sign_at.services.compliance.payment_classification", return_value=None)
	@patch(
		"erpnext_fiskaly_sign_at.services.compliance._enabled_settings",
		return_value=SimpleNamespace(enforce_pos_only_cash_receipts=1),
	)
	def test_unclassified_direct_customer_payment_is_blocked(self, _settings, _classification):
		doc = frappe._dict(
			payment_type="Receive",
			party_type="Customer",
			mode_of_payment="Unclassified payment",
		)
		with self.assertRaises(frappe.ValidationError):
			prevent_unfiscalized_cash_payment_entry(doc)

	@patch("erpnext_fiskaly_sign_at.services.compliance.payment_classification", return_value=None)
	@patch(
		"erpnext_fiskaly_sign_at.services.compliance._enabled_settings",
		return_value=SimpleNamespace(enforce_pos_only_cash_receipts=1),
	)
	def test_customer_payment_without_mode_is_blocked(self, _settings, _classification):
		doc = frappe._dict(
			payment_type="Receive",
			party_type="Customer",
			mode_of_payment=None,
		)
		with self.assertRaises(frappe.ValidationError):
			prevent_unfiscalized_cash_payment_entry(doc)

	def test_direct_core_output_is_blocked_without_server_claim(self):
		old_form_dict = getattr(frappe.local, "form_dict", None)
		previous_claim = getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, None)
		frappe.local.form_dict = frappe._dict(format=RKSV_PRINT_FORMAT)
		frappe.flags.pop(CONTROLLED_PRINT_CLAIM_FLAG, None)
		try:
			with self.assertRaises(frappe.ValidationError):
				_apply_rksv_print_context(frappe._dict(name="POS-PRINT-DIRECT"))
		finally:
			frappe.local.form_dict = old_form_dict
			if previous_claim is not None:
				setattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, previous_claim)

	def test_desk_preview_is_marked_non_fiscal_without_claiming(self):
		old_form_dict = getattr(frappe.local, "form_dict", None)
		previous_claim = getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, None)
		frappe.local.form_dict = frappe._dict(cmd=PRINT_PREVIEW_COMMAND, print_format=RKSV_PRINT_FORMAT)
		frappe.flags.pop(CONTROLLED_PRINT_CLAIM_FLAG, None)
		try:
			doc = frappe._dict(name="POS-PRINT-PREVIEW")
			_apply_rksv_print_context(doc)
			self.assertEqual(doc.fiskaly_is_preview, 1)
			self.assertEqual(doc.fiskaly_is_duplicate, 0)
		finally:
			frappe.local.form_dict = old_form_dict
			if previous_claim is not None:
				setattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, previous_claim)

	def test_controlled_claim_labels_only_later_copies_as_duplicate(self):
		old_form_dict = getattr(frappe.local, "form_dict", None)
		previous_claim = getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, None)
		frappe.local.form_dict = frappe._dict(format=RKSV_PRINT_FORMAT)
		try:
			for ordinal, expected_duplicate in ((1, 0), (2, 1)):
				with self.subTest(ordinal=ordinal):
					setattr(
						frappe.flags,
						CONTROLLED_PRINT_CLAIM_FLAG,
						frappe._dict(pos_invoice="POS-PRINT-CLAIMED", ordinal=ordinal),
					)
					doc = frappe._dict(name="POS-PRINT-CLAIMED")
					_apply_rksv_print_context(doc)
					self.assertEqual(doc.fiskaly_is_preview, 0)
					self.assertEqual(doc.fiskaly_is_duplicate, expected_duplicate)
		finally:
			frappe.local.form_dict = old_form_dict
			frappe.flags.pop(CONTROLLED_PRINT_CLAIM_FLAG, None)
			if previous_claim is not None:
				setattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, previous_claim)

	@patch("erpnext_fiskaly_sign_at.api.printing._set_inline_print_response")
	@patch("erpnext_fiskaly_sign_at.api.printing.now_datetime", return_value="2026-08-18 11:30:00")
	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.db.set_value")
	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.get_print")
	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.get_doc")
	@patch("erpnext_fiskaly_sign_at.api.printing._lock_pos_print_row")
	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.has_permission", return_value=True)
	def test_post_print_claim_serializes_original_then_duplicate(
		self,
		_has_permission,
		lock_row,
		get_doc,
		get_print,
		set_value,
		_now,
		set_response,
	):
		doc = frappe._dict(
			name="POS-PRINT-ATOMIC",
			doctype="POS Invoice",
			docstatus=1,
			fiskaly_status="SIGNED",
		)
		doc.check_permission = Mock()
		get_doc.return_value = doc
		lock_row.side_effect = [
			frappe._dict(fiskaly_print_count=0, fiskaly_first_printed_at=None),
			frappe._dict(
				fiskaly_print_count=1,
				fiskaly_first_printed_at="2026-08-18 11:30:00",
			),
		]
		claims = []

		def render(*args, **kwargs):
			claim = getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG)
			claims.append((claim.pos_invoice, claim.ordinal, frappe.local.form_dict.trigger_print))
			return f"<html>copy-{claim.ordinal}</html>"

		get_print.side_effect = render
		old_form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.form_dict = frappe._dict(cmd="erpnext_fiskaly_sign_at.api.printing.render_pos_receipt")
		try:
			render_pos_receipt("POS-PRINT-ATOMIC", output="html")
			render_pos_receipt("POS-PRINT-ATOMIC", output="html")
		finally:
			frappe.local.form_dict = old_form_dict

		self.assertEqual(claims, [("POS-PRINT-ATOMIC", 1, 1), ("POS-PRINT-ATOMIC", 2, 1)])
		self.assertEqual(set_value.call_args_list[0].args[2]["fiskaly_print_count"], 1)
		self.assertEqual(
			set_value.call_args_list[0].args[2]["fiskaly_first_printed_at"],
			"2026-08-18 11:30:00",
		)
		self.assertEqual(set_value.call_args_list[1].args[2], {"fiskaly_print_count": 2})
		self.assertEqual(set_response.call_count, 2)
		self.assertIsNone(getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, None))

	@patch("erpnext_fiskaly_sign_at.api.printing._set_inline_print_response")
	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.db.set_value")
	@patch(
		"erpnext_fiskaly_sign_at.api.printing.frappe.get_print",
		side_effect=RuntimeError("render failed"),
	)
	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.get_doc")
	@patch("erpnext_fiskaly_sign_at.api.printing._lock_pos_print_row")
	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.has_permission", return_value=True)
	def test_failed_render_does_not_persist_print_claim(
		self,
		_has_permission,
		lock_row,
		get_doc,
		_get_print,
		set_value,
		set_response,
	):
		doc = frappe._dict(
			name="POS-PRINT-FAILED",
			doctype="POS Invoice",
			docstatus=1,
			fiskaly_status="SIGNED",
		)
		doc.check_permission = Mock()
		get_doc.return_value = doc
		lock_row.return_value = frappe._dict(fiskaly_print_count=0, fiskaly_first_printed_at=None)
		old_form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.form_dict = frappe._dict(trigger_print=7)
		try:
			with self.assertRaisesRegex(RuntimeError, "render failed"):
				render_pos_receipt("POS-PRINT-FAILED")
			self.assertEqual(frappe.local.form_dict.trigger_print, 7)
		finally:
			frappe.local.form_dict = old_form_dict
		set_value.assert_not_called()
		set_response.assert_not_called()
		self.assertIsNone(getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, None))

	@patch("erpnext_fiskaly_sign_at.api.printing._lock_pos_print_row")
	@patch(
		"erpnext_fiskaly_sign_at.api.printing.frappe.has_permission",
		side_effect=frappe.PermissionError,
	)
	def test_controlled_print_requires_explicit_print_permission(self, _has_permission, lock_row):
		with self.assertRaises(frappe.PermissionError):
			render_pos_receipt("POS-PRINT-FORBIDDEN")
		lock_row.assert_not_called()

	def test_controlled_print_endpoint_allows_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[render_pos_receipt], ["POST"])

	@patch("erpnext_fiskaly_sign_at.api.printing.frappe.db.sql")
	def test_print_claim_uses_pos_invoice_row_lock(self, sql):
		sql.return_value = [frappe._dict(name="POS-LOCKED")]
		self.assertEqual(_lock_pos_print_row("POS-LOCKED").name, "POS-LOCKED")
		self.assertIn("for update", sql.call_args.args[0].lower())
		self.assertEqual(sql.call_args.args[1], ("POS-LOCKED",))

	@patch("erpnext_fiskaly_sign_at.services.compliance.payment_classification", return_value=None)
	@patch(
		"erpnext_fiskaly_sign_at.services.compliance._enabled_settings",
		return_value=SimpleNamespace(enforce_pos_only_cash_receipts=1),
	)
	def test_unclassified_paid_sales_invoice_is_blocked(self, _settings, _classification):
		doc = frappe._dict(
			is_pos=1,
			is_paid=1,
			payments=[frappe._dict(mode_of_payment="Unclassified payment", type="Bank")],
		)
		with self.assertRaises(frappe.ValidationError):
			prevent_unfiscalized_sales_invoice(doc)

	@patch(
		"erpnext_fiskaly_sign_at.services.compliance.payment_classification",
		return_value="RKSV Cash Equivalent",
	)
	@patch("erpnext_fiskaly_sign_at.services.compliance.frappe.get_all")
	def test_verified_pos_consolidation_reuses_fiscalized_source(self, get_all, _classification):
		def rows(doctype, **_kwargs):
			return {
				"POS Invoice Reference": [
					frappe._dict(parent="MERGE-1", pos_invoice="POS-1")
				],
				"POS Invoice Merge Log": [frappe._dict(name="MERGE-1")],
				"POS Invoice": [
					frappe._dict(
						name="POS-1",
						docstatus=1,
						company="Example Company",
						pos_profile="POS-1",
						is_return=0,
						consolidated_invoice=None,
						fiskaly_status="SIGNED",
						grand_total=120,
						base_grand_total=120,
					)
				],
				"POS Invoice Item": [frappe._dict(name="POS-ITEM-1", parent="POS-1")],
				"Sales Invoice Payment": [
					frappe._dict(
						parent="POS-1",
						mode_of_payment="Card",
						type="Bank",
						account="Card Clearing",
						amount=120,
						base_amount=120,
					)
				],
			}[doctype]

		get_all.side_effect = rows
		doc = frappe._dict(
			is_pos=1,
			is_consolidated=1,
			is_return=0,
			company="Example Company",
			pos_profile="POS-1",
			grand_total=120,
			base_grand_total=120,
			items=[frappe._dict(pos_invoice="POS-1", pos_invoice_item="POS-ITEM-1")],
			payments=[
				frappe._dict(
					mode_of_payment="Card",
					account="Card Clearing",
					amount=120,
					base_amount=120,
				)
			],
		)
		self.assertTrue(_is_verified_pos_consolidation(doc))

	@patch(
		"erpnext_fiskaly_sign_at.services.compliance._is_verified_pos_consolidation",
		return_value=True,
	)
	@patch(
		"erpnext_fiskaly_sign_at.services.compliance._enabled_settings",
		return_value=SimpleNamespace(enforce_pos_only_cash_receipts=1),
	)
	def test_verified_pos_consolidation_is_allowed(self, _settings, verified_consolidation):
		doc = frappe._dict(
			is_pos=1,
			is_consolidated=1,
			payments=[frappe._dict(mode_of_payment="Card", type="Bank")],
		)
		prevent_unfiscalized_sales_invoice(doc)
		verified_consolidation.assert_called_once_with(doc)

	@patch("erpnext_fiskaly_sign_at.services.compliance._is_verified_pos_consolidation")
	@patch("erpnext_fiskaly_sign_at.services.compliance.payment_classification")
	@patch(
		"erpnext_fiskaly_sign_at.services.compliance._enabled_settings",
		return_value=SimpleNamespace(enforce_pos_only_cash_receipts=1),
	)
	def test_forged_pos_consolidation_remains_blocked(
		self, _settings, classification, verified_consolidation
	):
		verified_consolidation.return_value = False
		classification.return_value = "RKSV Cash Equivalent"
		doc = frappe._dict(
			is_pos=1,
			is_consolidated=1,
			is_paid=1,
			payments=[frappe._dict(mode_of_payment="Card", type="Bank")],
		)
		with self.assertRaises(frappe.ValidationError):
			prevent_unfiscalized_sales_invoice(doc)

	def test_required_print_snapshot_fields_are_installed(self):
		pos_meta = frappe.get_meta("POS Invoice")
		for fieldname in (
			"fiskaly_company_name",
			"fiskaly_company_address",
			"fiskaly_cash_amount",
			"fiskaly_vat_breakdown",
			"fiskaly_provider_hints",
			"fiskaly_cash_register_id",
			"fiskaly_provider_receipt_id",
			"fiskaly_signature_creation_unit_id",
			"fiskaly_gross_standard",
			"fiskaly_gross_reduced_1",
			"fiskaly_gross_reduced_2",
			"fiskaly_gross_zero",
			"fiskaly_gross_special",
			"fiskaly_encrypted_turnover_counter",
			"fiskaly_certificate_serial_number",
			"fiskaly_previous_receipt_signature",
			"fiskaly_offline_qr_data",
			"fiskaly_first_printed_at",
			"fiskaly_print_count",
		):
			with self.subTest(fieldname=fieldname):
				field = pos_meta.get_field(fieldname)
				self.assertIsNotNone(field)
				self.assertEqual(field.read_only, 1)
				self.assertEqual(field.allow_on_submit, 1)

		payment_field = frappe.get_meta("Mode of Payment").get_field("fiskaly_rksv_payment_type")
		self.assertIsNotNone(payment_field)
		self.assertIn("RKSV Cash Equivalent", payment_field.options)
		self.assertIn("Non-Cash", payment_field.options)
		self.assertIn("welcher Zahlungsanteil als RKSV-Barumsatz fiskalisiert wird", payment_field.description)
		self.assertIn("Debit- oder Kreditkarte am POS", payment_field.description)
		self.assertIn("Remote- oder Online-Kartenzahlung", payment_field.description)
		self.assertIn("<br><br>", payment_field.description)

		manual_cash_field = frappe.get_meta("POS Profile").get_field("fiskaly_manual_cash_entry")
		self.assertIsNotNone(manual_cash_field)
		self.assertEqual(manual_cash_field.fieldtype, "Check")
		self.assertEqual(manual_cash_field.default, "0")
		self.assertIn("tatsächlich erhaltene Barbetrag", manual_cash_field.description)

	def test_rksv_print_format_contains_complete_visible_fields(self):
		html = frappe.db.get_value("Print Format", RKSV_PRINT_FORMAT, "html")
		validate_template(html)
		for expected in (
			"company_address_display",
			"Beleg-Nr.",
			"Datum/Uhrzeit",
			"item.qty",
			"item.uom",
			"Tatsächlicher Barzahlungsbetrag",
			"fiskaly_vat_breakdown",
			"fiskaly_cash_register_id",
			"fiskaly_offline_qr_data",
			"fiskaly_provider_hints",
			"fiskaly_is_duplicate",
			"fiskaly_is_preview",
			"VORSCHAU &ndash; KEIN BELEG",
			"RKSV-Code in der Vorschau unterdrückt",
			RKSV_OFFLINE_NOTICE,
			"overflow: visible",
		):
			with self.subTest(expected=expected):
				self.assertIn(expected, html)
		# Legacy free-form lines once contained the internal Register name as Kassen-ID.
		self.assertNotIn("doc.fiskaly_print_lines", html)
		self.assertIn('doc.fiskaly_status == "SUBSTITUTE_SIGNED"', html)
		self.assertIn('doc.fiskaly_status == "OFFLINE_PENDING"', html)
		self.assertIn('doc.fiskaly_offline_qr_data == "Sicherheitseinrichtung ausgefallen"', html)
		self.assertNotIn('doc.fiskaly_status in ("OFFLINE_PENDING"', html)

	def test_fiscal_preview_is_watermarked_and_contains_no_qr_svg(self):
		base = {
			"company": "Beispiel GmbH",
			"company_address_display": "Testgasse 1<br>1010 Wien",
			"fiskaly_company_name": "Beispiel GmbH",
			"fiskaly_company_address": "Testgasse 1<br>1010 Wien",
			"name": "POS-INV-PREVIEW",
			"posting_date": "2026-08-18",
			"posting_time": "10:30:00",
			"currency": "EUR",
			"net_total": 10,
			"total_taxes_and_charges": 2,
			"grand_total": 12,
			"change_amount": 0,
			"items": [],
			"payments": [],
			"taxes": [],
			"item_wise_tax_details": [],
			"fiskaly_cash_amount": 12,
			"fiskaly_vat_breakdown": "[]",
			"fiskaly_cash_register_id": "REGISTER-1",
			"fiskaly_serial_number": "REGISTER-1",
			"fiskaly_receipt_number": "42",
			"fiskaly_signed_at": "2026-08-18 10:30:00",
			"fiskaly_provider_hints": None,
			"fiskaly_is_preview": 1,
			"fiskaly_is_duplicate": 0,
		}
		for status, qr_field, qr_value in (
			("SIGNED", "fiskaly_qr_code_data", "_R1-AT1_SECRET_SIGNED"),
			("SUBSTITUTE_SIGNED", "fiskaly_offline_qr_data", "_R1-AT1_SECRET_SUBSTITUTE"),
			("OFFLINE_PENDING", "fiskaly_offline_qr_data", RKSV_OFFLINE_NOTICE),
		):
			with self.subTest(status=status):
				values = {**base, "fiskaly_status": status, qr_field: qr_value}
				rendered = frappe.render_template(PRINT_FORMAT_HTML, {"doc": SimpleNamespace(**values)})
				self.assertIn("VORSCHAU &ndash; KEIN BELEG", rendered)
				self.assertIn("Vorschau unterdrückt", rendered)
				self.assertNotIn("<svg", rendered)
				if status != "OFFLINE_PENDING":
					self.assertNotIn(qr_value, rendered)

	def test_print_pages_use_controlled_post_endpoint(self):
		script = (Path(__file__).parents[1] / "public" / "js" / "rksv_print.js").read_text(encoding="utf-8")
		for expected in (
			"window.open_url_post",
			"erpnext_fiskaly_sign_at.api.printing.render_pos_receipt",
			"frappe.utils.print",
			"PrintView",
			"prototype.printit",
			"prototype.render_page",
			"prototype.render_pdf",
			"prototype.print_by_server",
		):
			with self.subTest(expected=expected):
				self.assertIn(expected, script)
		self.assertIn(
			"public/js/rksv_print.js",
			frappe.get_hooks("page_js").get("point-of-sale", []),
		)
		self.assertIn(
			"public/js/rksv_print.js",
			frappe.get_hooks("page_js").get("print", []),
		)

	def test_not_required_print_keeps_invoice_details_without_rksv_warnings(self):
		html = frappe.db.get_value("Print Format", RKSV_PRINT_FORMAT, "html")
		doc = SimpleNamespace(
			**{
				"company": "Beispiel GmbH",
				"company_address_display": "Testgasse 1<br>1010 Wien",
				"name": "POS-INV-TEST-0001",
				"posting_date": "2026-08-18",
				"posting_time": "10:30:00",
				"currency": "EUR",
				"net_total": 100,
				"total_taxes_and_charges": 18,
				"grand_total": 118,
				"change_amount": 0,
				"items": [
					SimpleNamespace(
						**{
							"qty": 1,
							"uom": "Stk",
							"item_name": "Gemischter Umsatz",
						}
					)
				],
				"payments": [
					SimpleNamespace(mode_of_payment="Remote-Karte", amount=60),
					SimpleNamespace(mode_of_payment="Überweisung", amount=58),
				],
				"taxes": [
					SimpleNamespace(
						**{
							"description": "USt 20 %",
							"rate": 20,
							"tax_amount_after_discount_amount": 10,
						}
					),
					SimpleNamespace(
						**{
							"description": "USt 10 %",
							"rate": 10,
							"tax_amount_after_discount_amount": 8,
						}
					),
				],
				"item_wise_tax_details": [
					SimpleNamespace(rate=20, taxable_amount=50, amount=10),
					SimpleNamespace(rate=10, taxable_amount=80, amount=8),
				],
				"fiskaly_status": "NOT_REQUIRED",
				"fiskaly_is_duplicate": 0,
			}
		)

		rendered = frappe.render_template(html, {"doc": doc})

		for expected in (
			"Beispiel GmbH",
			"POS-INV-TEST-0001",
			"Gemischter Umsatz",
			"Remote-Karte",
			"Überweisung",
			"USt 20 %",
			"USt 10 %",
			"Nettobetrag",
			"Umsatzsteuer je Position und Steuersatz",
			"Bruttobetrag",
			"Keine RKSV-Baräquivalentzahlung &ndash; keine Fiskalisierung erforderlich",
		):
			with self.subTest(expected=expected):
				self.assertIn(expected, rendered)
		for forbidden in (
			"Tatsächlicher Barzahlungsbetrag",
			"RKSV-Steueraufteilung fehlt",
			"RKSV-Bar-Bucket-Aufschlüsselung",
			"Nicht druckbereit",
		):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, rendered)

	def test_invoice_snapshot_gaps_are_visible_on_receipt(self):
		html = frappe.db.get_value("Print Format", RKSV_PRINT_FORMAT, "html")
		doc = SimpleNamespace(
			**{
				"company": "Beispiel GmbH",
				"company_address_display": "Testgasse 1<br>1010 Wien",
				"name": "POS-INV-TEST-0002",
				"posting_date": "2026-08-18",
				"posting_time": "10:30:00",
				"currency": "EUR",
				"net_total": None,
				"total_taxes_and_charges": None,
				"grand_total": 0,
				"items": [],
				"payments": [],
				"taxes": [],
				"item_wise_tax_details": [],
				"fiskaly_status": "NOT_REQUIRED",
				"fiskaly_is_duplicate": 0,
			}
		)

		rendered = frappe.render_template(html, {"doc": doc})

		self.assertIn("Zahlungsaufstellung fehlt im POS-Invoice-Snapshot", rendered)
		self.assertIn("Steuer-/Abgabenaufschlüsselung fehlt im POS-Invoice-Snapshot", rendered)
		self.assertGreaterEqual(rendered.count("FEHLT"), 2)
