import hashlib
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_dep7_export.fiskaly_dep7_export import (
	FiskalyDEP7Export,
	prevent_compliance_file_deletion,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_receipt.fiskaly_receipt import (
	FiskalyReceipt,
)
from erpnext_fiskaly_sign_at.install import RKSV_CLOSING_PRINT_FORMAT, SPECIAL_RECEIPT_HTML


class TestFiscalEvidenceRetention(TestCase):
	def setUp(self):
		self.previous_in_uninstall = getattr(frappe.flags, "in_uninstall", False)
		frappe.flags.in_uninstall = False

	def tearDown(self):
		frappe.flags.in_uninstall = self.previous_in_uninstall

	def test_dep7_export_cannot_be_deleted(self):
		with self.assertRaises(frappe.ValidationError):
			FiskalyDEP7Export.on_trash(SimpleNamespace())

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt._check_evidence_write_permission"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_receipt.fiskaly_receipt.save_file"
	)
	@patch("frappe.get_print", return_value=b"%PDF-1.7 protected receipt")
	def test_closing_receipt_is_rendered_and_archived_as_private_pdf(self, get_print, save_file, _permission):
		save_file.return_value = SimpleNamespace(file_url="/private/files/closing-auto.pdf")
		receipt = SimpleNamespace(
			name="CLOSING-1",
			doctype="Fiskaly Receipt",
			register="REGISTER-1",
			receipt_kind="CLOSING",
			status="SIGNED",
			print_evidence_at=None,
			print_evidence_file=None,
			check_permission=Mock(),
			mark_print_evidence=Mock(
				return_value={
					"print_evidence_at": "2026-08-19 10:00:00",
					"print_evidence_file": "/private/files/closing-auto.pdf",
				}
			),
		)

		result = FiskalyReceipt.create_and_archive_print_evidence(receipt)

		self.assertFalse(result["idempotent"])
		get_print.assert_called_once_with(
			"Fiskaly Receipt",
			"CLOSING-1",
			RKSV_CLOSING_PRINT_FORMAT,
			doc=receipt,
			as_pdf=True,
			no_letterhead=1,
			pdf_generator="wkhtmltopdf",
		)
		self.assertTrue(save_file.call_args.kwargs["is_private"])
		receipt.mark_print_evidence.assert_called_once_with("/private/files/closing-auto.pdf")

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt._check_evidence_write_permission"
	)
	@patch("frappe.get_print")
	def test_existing_automatic_archive_is_idempotent(self, get_print, _permission):
		receipt = SimpleNamespace(
			receipt_kind="CLOSING",
			print_evidence_at="2026-08-19 10:00:00",
			print_evidence_file="/private/files/closing.pdf",
			check_permission=Mock(),
		)

		result = FiskalyReceipt.create_and_archive_print_evidence(receipt)

		self.assertTrue(result["idempotent"])
		get_print.assert_not_called()

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt._check_evidence_write_permission"
	)
	@patch("frappe.log_error")
	@patch("frappe.get_print", side_effect=TimeoutError("Chromium took too long to start"))
	def test_pdf_engine_timeout_is_reported_as_actionable_archive_error(
		self, get_print, log_error, _permission
	):
		receipt = SimpleNamespace(
			name="CLOSING-1",
			doctype="Fiskaly Receipt",
			register="REGISTER-1",
			receipt_kind="CLOSING",
			status="SIGNED",
			print_evidence_at=None,
			print_evidence_file=None,
			check_permission=Mock(),
		)

		with self.assertRaisesRegex(frappe.ValidationError, "PDF-Archivierung erneut"):
			FiskalyReceipt.create_and_archive_print_evidence(receipt)

		self.assertEqual(get_print.call_args.kwargs["pdf_generator"], "wkhtmltopdf")
		log_error.assert_called_once()

	@patch("frappe.db.exists")
	def test_dep7_file_cannot_be_deleted(self, exists):
		exists.return_value = "DEP7-0001"

		with self.assertRaises(frappe.ValidationError):
			prevent_compliance_file_deletion(SimpleNamespace(file_url="/private/files/dep7.json"))

		exists.assert_called_once_with("Fiskaly DEP7 Export", {"export_file": "/private/files/dep7.json"})

	@patch("frappe.db.exists")
	def test_annual_print_evidence_file_cannot_be_deleted(self, exists):
		exists.side_effect = [None, None, "RECEIPT-0001"]

		with self.assertRaises(frappe.ValidationError):
			prevent_compliance_file_deletion(SimpleNamespace(file_url="/private/files/yearly.pdf"))

		self.assertEqual(exists.call_count, 3)

	@patch("frappe.db.exists")
	def test_supplementary_dep7_file_cannot_be_deleted(self, exists):
		exists.side_effect = [None, "DEP7-0001"]

		with self.assertRaises(frappe.ValidationError):
			prevent_compliance_file_deletion(SimpleNamespace(file_url="/private/files/dep7-supplement.json"))

		self.assertEqual(exists.call_count, 2)

	@patch("frappe.db.exists")
	def test_unreferenced_file_can_be_deleted(self, exists):
		exists.return_value = None

		prevent_compliance_file_deletion(SimpleNamespace(file_url="/private/files/other.pdf"))

		self.assertEqual(exists.call_count, 3)

	@patch("frappe.db.exists")
	def test_uninstall_bypasses_retention_guards(self, exists):
		frappe.flags.in_uninstall = True

		FiskalyDEP7Export.on_trash(SimpleNamespace())
		prevent_compliance_file_deletion(SimpleNamespace(file_url="/private/files/dep7.json"))

		exists.assert_not_called()

	def test_receipt_result_and_retry_evidence_is_immutable(self):
		expected = {
			"status",
			"provider_receipt_id",
			"receipt_number",
			"signed_at",
			"serial_number",
			"qr_code_data",
			"signature_value",
			"response_payload",
			"response_payload_sha256",
			"fon_validation_response",
			"annual_compliance_status",
			"annual_action_required_reason",
			"verification_status",
			"print_evidence_file",
			"attempt_count",
			"next_retry_at",
			"last_error",
		}
		self.assertTrue(expected.issubset(FiskalyReceipt.IMMUTABLE_FIELDS))

	def test_dep7_scope_metadata_is_immutable(self):
		expected = {
			"register",
			"connection",
			"purpose",
			"export_scope",
			"fiscal_year",
			"fiscal_quarter",
			"date_from",
			"date_to",
		}
		self.assertTrue(expected.issubset(FiskalyDEP7Export.IMMUTABLE_EVIDENCE_FIELDS))

	def test_special_receipt_print_requires_valid_response_hash(self):
		response_payload = '{"signed":true}'
		receipt = frappe._dict(
			receipt_kind="YEARLY",
			status="SIGNED",
			provider_receipt_id="receipt-1",
			receipt_number="1",
			signed_at="2026-12-31 23:59:59",
			serial_number="register-1",
			qr_code_data="_R1-AT_test",
			signature_value="test",
			response_payload=response_payload,
			response_payload_sha256=hashlib.sha256(response_payload.encode()).hexdigest(),
			signed=1,
			fon_validation_status="PENDING",
		)

		old_form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.form_dict = frappe._dict(format=RKSV_CLOSING_PRINT_FORMAT)
		definition = frappe._dict(
			doc_type="Fiskaly Receipt",
			print_format_type="Jinja",
			custom_format=1,
			disabled=0,
			html=SPECIAL_RECEIPT_HTML,
		)
		try:
			with patch("frappe.db.get_value", return_value=definition):
				FiskalyReceipt.before_print(receipt)
				receipt.response_payload_sha256 = "invalid"
				with self.assertRaises(frappe.ValidationError):
					FiskalyReceipt.before_print(receipt)
		finally:
			frappe.local.form_dict = old_form_dict

	def test_special_receipt_rejects_standard_print_format(self):
		receipt = frappe._dict(receipt_kind="YEARLY", status="SIGNED")
		old_form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.form_dict = frappe._dict(format="Standard")
		try:
			with self.assertRaises(frappe.ValidationError):
				FiskalyReceipt.before_print(receipt)
		finally:
			frappe.local.form_dict = old_form_dict

	def test_external_copy_evidence_cannot_be_overwritten(self):
		export = SimpleNamespace(
			status="READY",
			export_file="/private/files/dep7.json",
			file_hash="abc",
			external_copy_confirmed=1,
			external_copy_confirmed_at="2026-08-18 10:00:00",
			external_copy_confirmed_by="Administrator",
			external_storage_reference="WORM-01/2026-Q2",
			action_required_reason=None,
			check_permission=lambda permission: None,
		)

		result = FiskalyDEP7Export.mark_external_copy_confirmed(export, "WORM-01/2026-Q2")
		self.assertEqual(result["external_storage_reference"], "WORM-01/2026-Q2")
		with self.assertRaises(frappe.ValidationError):
			FiskalyDEP7Export.mark_external_copy_confirmed(export, "USB-CHANGED")

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt._check_evidence_write_permission"
	)
	def test_annual_evidence_cannot_be_overwritten(self, _permission):
		receipt = SimpleNamespace(
			receipt_kind="YEARLY",
			print_evidence_at="2026-01-02 10:00:00",
			print_evidence_file="/private/files/yearly.pdf",
			verification_at="2026-01-03 10:00:00",
			verification_status="SUCCESS",
			verification_note="BMF geprüft",
			check_permission=lambda permission: None,
		)
		with self.assertRaises(frappe.ValidationError):
			FiskalyReceipt.mark_print_evidence(receipt, None)
		with self.assertRaises(frappe.ValidationError):
			FiskalyReceipt.record_annual_verification(receipt, "FAILED", "geändert")

		result = FiskalyReceipt.record_annual_verification(receipt, "SUCCESS", "BMF geprüft")
		self.assertEqual(result["verification_at"], "2026-01-03 10:00:00")
