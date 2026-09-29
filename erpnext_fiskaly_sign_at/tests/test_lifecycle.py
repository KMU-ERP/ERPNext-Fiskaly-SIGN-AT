import hashlib
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from erpnext_fiskaly_sign_at.api.exports import (
	_build_supplementary_receipt_items,
	_quarter_bounds,
	_retention_until,
	_validate_final_decommission_export,
	create_dep7_export,
	create_final_decommission_export,
	create_quarterly_backup,
	process_dep7_export,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_dep7_export.fiskaly_dep7_export import (
	FiskalyDEP7Export,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_receipt.fiskaly_receipt import (
	check_annual_receipt_requirements,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register import (
	PROTECTED_INITIALIZED_FIELDS,
	FiskalyRegister,
)


class LocalRegisterStub(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def db_set(self, field_or_values, value=None, **kwargs):
		values = field_or_values if isinstance(field_or_values, dict) else {field_or_values: value}
		for fieldname, fieldvalue in values.items():
			setattr(self, fieldname, fieldvalue)

	def _record_lifecycle_event(self, event_type, **values):
		self.events.append((event_type, values))
		return f"event-{len(self.events)}"


class TestLifecycleEvidence(TestCase):
	@patch("frappe.db.set_value")
	@patch("frappe.get_all")
	def test_overdue_annual_receipt_requires_validation_and_print_evidence(self, get_all, set_value):
		get_all.return_value = [
			frappe._dict(
				name="YEARLY-2025",
				fon_validation_status="FAILED",
				verification_status="PENDING",
				verification_by=None,
				print_evidence_at=None,
				annual_compliance_status="PENDING",
			)
		]

		issues = check_annual_receipt_requirements("2026-02-15")

		self.assertEqual(issues[0]["receipt"], "YEARLY-2025")
		self.assertIn("validation", issues[0]["reason"])
		self.assertIn("print/archive", issues[0]["reason"])
		self.assertEqual(set_value.call_args.args[2]["annual_compliance_status"], "ACTION_REQUIRED")

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_api_outage_is_local_and_idempotent(self, lock):
		register = LocalRegisterStub(
			name="REGISTER-1",
			outage_status="NONE",
			outage_reference=None,
			outage_scope=None,
			provider_state="INITIALIZED",
			events=[],
			reload=lambda: None,
		)

		first = FiskalyRegister.record_api_outage(register, "SIGN_AT_API_UNREACHABLE", "request-1")
		second = FiskalyRegister.record_api_outage(register, "SIGN_AT_API_UNREACHABLE", "request-2")

		self.assertEqual(first["status"], "ACTION_REQUIRED")
		self.assertEqual(register.outage_scope, "CASH_REGISTER")
		self.assertIsNotNone(register.outage_deadline_at)
		self.assertEqual(len(register.events), 1)
		self.assertTrue(second["idempotent"])
		self.assertEqual(second["outage_reference"], first["outage_reference"])
		self.assertEqual(lock.call_count, 2)

	@patch.object(FiskalyRegister, "_sync_automatic_receipts", return_value={"received": 0})
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_api_recovery_does_not_require_user_write_or_claim_fon_report(
		self, lock, _sync_automatic_receipts
	):
		class Provider:
			def retrieve_register(self, register):
				return {"state": "INITIALIZED"}

			def transition_register_state(self, register, state):
				raise AssertionError("PATCH must not be called when remote register is not OUTAGE")

		check_permission = Mock(side_effect=frappe.PermissionError)
		register = LocalRegisterStub(
			name="REGISTER-1",
			outage_status="ACTION_REQUIRED",
			outage_reference="outage-1",
			outage_scope="CASH_REGISTER",
			outage_started_at=datetime(2026, 8, 18, 10, 0),
			outage_deadline_at=datetime(2026, 8, 20, 10, 0),
			outage_ended_at=None,
			outage_reason="SIGN_AT_API_UNREACHABLE",
			provider_state="INITIALIZED",
			fon_outage_status="ACTION_REQUIRED",
			events=[],
			_provider=lambda: (SimpleNamespace(), Provider()),
			check_permission=check_permission,
			reload=lambda: None,
		)

		with patch(
			"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register.now_datetime",
			return_value=datetime(2026, 8, 20, 9, 0),
		):
			result = FiskalyRegister.record_api_recovery(register, "SIGN_AT_API_RECOVERED")

		self.assertEqual(result["status"], "ENDED")
		self.assertEqual(register.fon_outage_status, "NOT_APPLICABLE")
		self.assertEqual(register.events[-1][1]["fon_reporting_mode"], "NOT_APPLICABLE")
		check_permission.assert_not_called()
		lock.assert_called_once_with("REGISTER-1")

	@patch.object(FiskalyRegister, "_sync_automatic_receipts", return_value={"received": 0})
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_api_recovery_after_48_hours_keeps_manual_fon_action_open(self, lock, _sync_automatic_receipts):
		class Provider:
			def retrieve_register(self, register):
				return {"state": "INITIALIZED"}

		register = LocalRegisterStub(
			name="REGISTER-1",
			outage_status="ACTION_REQUIRED",
			outage_reference="outage-overdue",
			outage_scope="CASH_REGISTER",
			outage_started_at=datetime(2026, 8, 14, 10, 0),
			outage_deadline_at=datetime(2026, 8, 16, 10, 0),
			outage_ended_at=None,
			outage_reason="SIGN_AT_API_UNREACHABLE",
			provider_state="INITIALIZED",
			fon_outage_status="ACTION_REQUIRED",
			events=[],
			_provider=lambda: (SimpleNamespace(), Provider()),
			check_permission=lambda permission: None,
			reload=lambda: None,
		)

		result = FiskalyRegister.end_outage(register, "SIGN_AT_API_RECOVERED")

		self.assertEqual(result["status"], "ACTION_REQUIRED")
		self.assertEqual(register.outage_status, "ACTION_REQUIRED")
		self.assertEqual(register.fon_outage_status, "ACTION_REQUIRED")
		self.assertEqual(register.events[-1][0], "OUTAGE_RECOVERED_FON_PENDING")
		self.assertTrue(register.events[-1][1]["action_required"])
		lock.assert_called_once_with("REGISTER-1")

	@patch.object(FiskalyRegister, "_sync_automatic_receipts", return_value={"received": 0})
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_manual_fon_begin_and_end_are_separate_immutable_evidence(self, lock, _sync_automatic_receipts):
		class Provider:
			def retrieve_register(self, register):
				return {"state": "INITIALIZED"}

		register = LocalRegisterStub(
			name="REGISTER-1",
			outage_status="ACTION_REQUIRED",
			outage_reference="outage-overdue",
			outage_scope="CASH_REGISTER",
			outage_started_at=datetime(2026, 8, 14, 10, 0),
			outage_deadline_at=datetime(2026, 8, 16, 10, 0),
			outage_ended_at=None,
			outage_reason="SIGN_AT_API_UNREACHABLE",
			provider_state="INITIALIZED",
			fon_outage_status="ACTION_REQUIRED",
			fon_outage_begin_reference=None,
			fon_outage_begin_reported_at=None,
			fon_outage_end_reference=None,
			fon_outage_end_reported_at=None,
			events=[],
			_provider=lambda: (SimpleNamespace(), Provider()),
			check_permission=lambda permission: None,
			reload=lambda: None,
		)

		FiskalyRegister.end_outage(register, "SIGN_AT_API_RECOVERED")
		begin = FiskalyRegister.record_manual_fon_report(
			register, "FON-BEGIN-1", phase="BEGIN", note="Beginn gemeldet"
		)
		self.assertEqual(begin["status"], "BEGIN_REPORTED")
		self.assertEqual(register.outage_status, "ACTION_REQUIRED")
		self.assertEqual(register.fon_outage_begin_reference, "FON-BEGIN-1")
		self.assertIsNone(register.fon_outage_end_reference)
		self.assertEqual(register.events[-1][0], "MANUAL_FON_BEGIN_REPORTED")

		end = FiskalyRegister.record_manual_fon_report(
			register, "FON-END-1", phase="END", note="Ende gemeldet"
		)
		self.assertEqual(end["status"], "MANUALLY_REPORTED")
		self.assertEqual(register.outage_status, "ENDED")
		self.assertEqual(register.fon_outage_end_reference, "FON-END-1")
		self.assertEqual(register.events[-1][0], "MANUAL_FON_END_REPORTED")

		idempotent = FiskalyRegister.record_manual_fon_report(register, "FON-END-1", phase="END")
		self.assertTrue(idempotent["idempotent"])
		with self.assertRaises(frappe.ValidationError):
			FiskalyRegister.record_manual_fon_report(register, "OTHER-END", phase="END")
		self.assertEqual(lock.call_count, 5)
		for call in lock.call_args_list:
			self.assertEqual(call.args, ("REGISTER-1",))

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_manual_fon_end_requires_begin_and_technical_recovery(self, lock):
		register = LocalRegisterStub(
			name="REGISTER-1",
			outage_status="ACTION_REQUIRED",
			outage_reference="outage-1",
			outage_scope="CASH_REGISTER",
			outage_ended_at=None,
			provider_state="INITIALIZED",
			fon_outage_status="ACTION_REQUIRED",
			fon_outage_begin_reference=None,
			fon_outage_begin_reported_at=None,
			fon_outage_end_reference=None,
			fon_outage_end_reported_at=None,
			events=[],
			check_permission=lambda permission: None,
			reload=lambda: None,
		)
		with self.assertRaises(frappe.ValidationError):
			FiskalyRegister.record_manual_fon_report(register, "FON-END", phase="END")

		FiskalyRegister.record_manual_fon_report(register, "FON-BEGIN", phase="BEGIN")
		with self.assertRaises(frappe.ValidationError):
			FiskalyRegister.record_manual_fon_report(register, "FON-END", phase="END")
		self.assertEqual(lock.call_count, 3)

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_start_outage_locks_before_idempotency_check(self, lock):
		register = LocalRegisterStub(
			name="REGISTER-1",
			outage_status="ACTIVE",
			outage_reference="OUTAGE-1",
			outage_scope="CASH_REGISTER",
			check_permission=lambda permission: None,
			reload=lambda: None,
		)

		result = FiskalyRegister.start_outage(register, "Provider unavailable")

		self.assertTrue(result["idempotent"])
		lock.assert_called_once_with("REGISTER-1")

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_deadline_check_reloads_ended_outage_after_lock(self, lock):
		register = LocalRegisterStub(
			name="REGISTER-1",
			outage_scope="CASH_REGISTER",
			outage_status="ACTIVE",
			outage_reference="OUTAGE-1",
			outage_deadline_at=datetime(2026, 8, 1),
			fon_outage_status="ACTION_REQUIRED",
			check_permission=lambda permission: None,
		)
		register.reload = lambda: setattr(register, "outage_status", "ENDED")

		result = FiskalyRegister.check_outage_deadline(register)

		self.assertEqual(result, {"alert": False})
		lock.assert_called_once_with("REGISTER-1")
		self.assertEqual(register.events if hasattr(register, "events") else [], [])

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_provider_refresh_and_automatic_sync_are_serialized(self, lock):
		class Provider:
			capabilities = SimpleNamespace(
				register_lifecycle=True,
				scu_lifecycle=False,
				automatic_closing_receipts=True,
			)

			def retrieve_register(self, register):
				return {"state": "INITIALIZED", "serial_number": "SERIAL-1"}

			def list_automatic_receipts(self, register, receipt_types):
				return []

		provider = Provider()
		register = LocalRegisterStub(
			name="REGISTER-1",
			provider_state="INITIALIZED",
			signature_creation_unit_id=None,
			events=[],
			check_permission=lambda permission: None,
			reload=lambda: None,
			_provider=lambda: (SimpleNamespace(provider="SIGN_AT_V1"), provider),
		)

		FiskalyRegister.refresh_provider_state(register)
		result = FiskalyRegister.sync_automatic_receipts(register)

		self.assertEqual(result, {"received": 0, "stored": []})
		self.assertEqual(lock.call_args_list[0].args, ("REGISTER-1",))
		self.assertEqual(lock.call_args_list[1].args, ("REGISTER-1",))

	@patch("frappe.db.exists", return_value=None)
	@patch("frappe.db.get_value")
	@patch("frappe.get_doc")
	def test_provisioned_register_fields_remain_protected_after_decommission(
		self, get_doc, get_value, _exists
	):
		connection = SimpleNamespace(company="COMPANY-1", provider="SIGN_AT_V1", environment="TEST")
		address = SimpleNamespace(has_link=lambda doctype, name: True)
		get_doc.side_effect = lambda doctype, name: (
			connection if doctype == "Fiskaly API Connection" else address
		)
		get_value.side_effect = lambda doctype, name, fieldname, **kwargs: (
			"EUR" if doctype == "Company" else "COMPANY-1"
		)
		register = LocalRegisterStub(
			name="REGISTER-1",
			company="COMPANY-1",
			pos_profile="POS-PROFILE-1",
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="TEST",
			location_address="ADDRESS-1",
			scu_assignment_mode="EXISTING",
			active=0,
			initialized=0,
			provider_register_id="TAMPERED-PROVIDER-ID",
			start_receipt="START-1",
			vat_mappings=[
				SimpleNamespace(
					tax_rate=20,
					v1_bucket="standard",
					is_exempt=0,
					tax_account=None,
				)
			],
			is_new=lambda: False,
			meta=SimpleNamespace(
				get_field=lambda fieldname: SimpleNamespace(fieldtype="Data"),
				get_label=lambda fieldname: fieldname,
			),
		)
		for fieldname in PROTECTED_INITIALIZED_FIELDS:
			if not hasattr(register, fieldname):
				setattr(register, fieldname, None)
		old = frappe._dict({fieldname: register.get(fieldname) for fieldname in PROTECTED_INITIALIZED_FIELDS})
		old.initialized = 0
		old.provider_register_id = "PROVIDER-ID-1"
		register.get_doc_before_save = lambda: old

		with self.assertRaisesRegex(frappe.ValidationError, "provider_register_id"):
			FiskalyRegister.validate(register)

	@patch("frappe.db.exists", return_value=None)
	@patch("frappe.db.get_value")
	@patch("frappe.get_doc")
	def test_browser_datetime_values_do_not_block_vat_mapping_changes(
		self, get_doc, get_value, _exists
	):
		connection = SimpleNamespace(company="COMPANY-1", provider="SIGN_AT_V1", environment="TEST")
		address = SimpleNamespace(has_link=lambda doctype, name: True)
		get_doc.side_effect = lambda doctype, name: (
			connection if doctype == "Fiskaly API Connection" else address
		)
		get_value.side_effect = lambda doctype, name, fieldname, **kwargs: (
			"EUR" if doctype == "Company" else "COMPANY-1"
		)
		browser_timestamp = "2026-08-19 14:21:26.695759"
		register = LocalRegisterStub(
			name="REGISTER-1",
			company="COMPANY-1",
			pos_profile="POS-PROFILE-1",
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="TEST",
			location_address="ADDRESS-1",
			scu_assignment_mode="EXISTING",
			active=1,
			initialized=1,
			provider_register_id="PROVIDER-ID-1",
			start_receipt="START-1",
			last_lifecycle_sync_at=browser_timestamp,
			next_quarterly_backup_due="2026-10-01",
			vat_mappings=[
				SimpleNamespace(
					tax_rate=20,
					v1_bucket="standard",
					is_exempt=0,
					tax_account=None,
				),
				SimpleNamespace(
					tax_rate=0,
					v1_bucket="zero",
					is_exempt=0,
					tax_account=None,
				),
			],
			is_new=lambda: False,
			meta=SimpleNamespace(
				get_field=lambda fieldname: SimpleNamespace(
					fieldtype=(
						"Datetime"
						if fieldname == "last_lifecycle_sync_at"
						else "Date"
						if fieldname == "next_quarterly_backup_due"
						else "Data"
					)
				),
				get_label=lambda fieldname: fieldname,
			),
		)
		for fieldname in PROTECTED_INITIALIZED_FIELDS:
			if not hasattr(register, fieldname):
				setattr(register, fieldname, None)
		old = frappe._dict({fieldname: register.get(fieldname) for fieldname in PROTECTED_INITIALIZED_FIELDS})
		old.last_lifecycle_sync_at = datetime(2026, 8, 19, 14, 21, 26, 695759)
		old.next_quarterly_backup_due = date(2026, 10, 1)
		register.get_doc_before_save = lambda: old

		FiskalyRegister.validate(register)

		register.last_lifecycle_sync_at = "2026-08-19 14:21:27.695759"
		with self.assertRaisesRegex(frappe.ValidationError, "last_lifecycle_sync_at"):
			FiskalyRegister.validate(register)

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("frappe.db.get_value", return_value="RECEIPT-PENDING")
	def test_decommission_is_blocked_before_any_provider_call(self, _get_value, _lock):
		register = LocalRegisterStub(
			name="REGISTER-1",
			decommission_status="NONE",
			closing_receipt=None,
			outage_status="NONE",
			check_permission=lambda permission: None,
			reload=lambda: None,
			_provider=lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")),
		)
		with self.assertRaises(frappe.ValidationError):
			FiskalyRegister.decommission_register(register, "Kasse geschlossen")

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("frappe.db.exists")
	def test_scu_decommission_is_blocked_by_own_active_or_initialized_register(self, exists, lock):
		for active, initialized in ((1, 1), (1, 0), (0, 1)):
			with self.subTest(active=active, initialized=initialized):
				register = LocalRegisterStub(
					name="REGISTER-1",
					active=active,
					initialized=initialized,
					signature_creation_unit_id="SCU-1",
					check_permission=lambda permission: None,
					reload=lambda: None,
					_provider=lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")),
				)

				with self.assertRaises(frappe.ValidationError):
					FiskalyRegister.decommission_scu(register, "SCU retirement")

				lock.assert_called_once_with("REGISTER-1")
				exists.assert_not_called()
				lock.reset_mock()

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("frappe.db.get_value", return_value="RECEIPT-PENDING")
	def test_mark_defective_is_blocked_by_open_receipt_before_provider_call(self, get_value, lock):
		register = LocalRegisterStub(
			name="REGISTER-1",
			check_permission=lambda permission: None,
			reload=lambda: None,
			_provider=lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")),
		)

		with self.assertRaises(frappe.ValidationError):
			FiskalyRegister.mark_register_defective(register, "Irreparable register defect")

		lock.assert_called_once_with("REGISTER-1")
		self.assertEqual(
			get_value.call_args_list[0].args,
			(
				"Fiskaly Receipt",
				{
					"register": "REGISTER-1",
					"status": [
						"in",
						("PREPARED", "SIGNING", "OFFLINE_PENDING", "RETRYING", "ACTION_REQUIRED"),
					],
				},
				"name",
			),
		)

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("frappe.db.get_value")
	def test_local_decommission_completes_only_with_archive_and_external_dep(self, get_value, _lock):
		get_value.side_effect = [
			None,
			frappe._dict(
				name="DEP7-FINAL",
				creation="2026-08-18 10:02:00",
				generated_at="2026-08-18 10:03:00",
				integrity_verified_at="2026-08-18 10:05:00",
				supplementary_integrity_verified_at="2026-08-18 10:05:00",
				external_copy_confirmed=1,
				external_storage_reference="WORM-01",
			),
			frappe._dict(
				creation="2026-08-18 10:00:45",
				status="SIGNED",
				fon_validation_status="SUCCESS",
				signed_at="2026-08-18 10:00:00",
				fon_validation_at="2026-08-18 10:00:30",
				print_evidence_at="2026-08-18 10:04:00",
				print_evidence_file="/private/files/closing.pdf",
			),
			"2026-08-18 10:01:00",
		]
		register = LocalRegisterStub(
			name="REGISTER-1",
			provider="SIGN_AT_V1",
			environment="LIVE",
			connection="CONNECTION-1",
			provider_state="DECOMMISSIONED",
			decommission_status="EVIDENCE_REQUIRED",
			closing_receipt="CLOSING-1",
			final_dep7_export="DEP7-FINAL",
			outage_scope=None,
			outage_reference=None,
			outage_started_at=None,
			outage_deadline_at=None,
			initialized=1,
			active=0,
			events=[],
			check_permission=lambda permission: None,
			reload=lambda: None,
		)

		result = FiskalyRegister.complete_decommission(register)

		self.assertEqual(result["status"], "COMPLETE")
		self.assertEqual(register.decommission_status, "COMPLETE")
		self.assertEqual(register.initialized, 0)
		self.assertEqual(register.events[-1][0], "REGISTER_DECOMMISSION_COMPLETED")

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("frappe.db.get_value", return_value="UNCERTAIN-RECEIPT-1")
	def test_decommission_completion_rechecks_open_receipt_under_lock(self, get_value, lock):
		register = LocalRegisterStub(
			name="REGISTER-1",
			check_permission=lambda permission: None,
			reload=lambda: None,
		)

		with self.assertRaisesRegex(frappe.ValidationError, "UNCERTAIN-RECEIPT-1"):
			FiskalyRegister.complete_decommission(register)

		lock.assert_called_once_with("REGISTER-1")
		self.assertEqual(
			get_value.call_args_list[0].args,
			(
				"Fiskaly Receipt",
				{
					"register": "REGISTER-1",
					"status": [
						"in",
						("PREPARED", "SIGNING", "OFFLINE_PENDING", "RETRYING", "ACTION_REQUIRED"),
					],
				},
				"name",
			),
		)

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("frappe.db.get_value")
	def test_stale_final_export_cannot_complete_decommission(self, get_value, _lock):
		get_value.side_effect = [
			None,
			frappe._dict(
				name="DEP7-STALE",
				creation="2026-08-18 09:58:00",
				generated_at="2026-08-18 10:03:00",
				integrity_verified_at="2026-08-18 10:05:00",
				supplementary_integrity_verified_at="2026-08-18 10:05:00",
				external_copy_confirmed=1,
				external_storage_reference="WORM-STALE",
			),
			frappe._dict(
				creation="2026-08-18 10:00:45",
				status="SIGNED",
				fon_validation_status="SUCCESS",
				signed_at="2026-08-18 10:00:00",
				fon_validation_at="2026-08-18 10:00:30",
				print_evidence_at="2026-08-18 10:04:00",
				print_evidence_file="/private/files/closing.pdf",
			),
			"2026-08-18 10:01:00",
		]
		register = LocalRegisterStub(
			name="REGISTER-1",
			provider_state="DECOMMISSIONED",
			decommission_status="EVIDENCE_REQUIRED",
			closing_receipt="CLOSING-1",
			final_dep7_export="DEP7-STALE",
			initialized=1,
			active=0,
			check_permission=lambda permission: None,
			reload=lambda: None,
		)

		with self.assertRaisesRegex(frappe.ValidationError, "predates"):
			FiskalyRegister.complete_decommission(register)

		self.assertEqual(register.decommission_status, "EVIDENCE_REQUIRED")
		self.assertEqual(register.initialized, 1)

	@patch("erpnext_fiskaly_sign_at.api.exports._validate_final_decommission_export")
	def test_direct_final_export_doctype_creation_is_blocked(self, validate_final):
		doc = frappe.new_doc("Fiskaly DEP7 Export")
		doc.update({"register": "REGISTER-1", "purpose": "DECOMMISSION_FINAL"})
		controlled = getattr(frappe.flags, "in_final_decommission_export_creation", False)
		frappe.flags.in_final_decommission_export_creation = False
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "controlled register action"):
				FiskalyDEP7Export.validate(doc)
		finally:
			frappe.flags.in_final_decommission_export_creation = controlled

		validate_final.assert_called_once_with("REGISTER-1", export_created_at=None)

	@patch("erpnext_fiskaly_sign_at.api.exports._create_dep7_export_doc")
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("erpnext_fiskaly_sign_at.api.exports._check_export_creation_permission")
	def test_generic_final_export_is_blocked_before_provider_decommission(
		self, check_permission, lock, create_export
	):
		register = LocalRegisterStub(
			name="REGISTER-1",
			connection="CONNECTION-1",
			provider_state="INITIALIZED",
			closing_receipt=None,
			decommission_status="NONE",
			reload=lambda: None,
		)
		check_permission.return_value = register

		with self.assertRaisesRegex(frappe.ValidationError, "provider-validated closing receipt"):
			create_dep7_export("REGISTER-1", purpose="DECOMMISSION_FINAL")

		lock.assert_called_once_with("REGISTER-1")
		create_export.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.api.exports._create_dep7_export_doc", return_value="DEP7-FINAL")
	@patch("erpnext_fiskaly_sign_at.api.exports._validate_final_decommission_export")
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("erpnext_fiskaly_sign_at.api.exports._check_export_creation_permission")
	@patch("frappe.db.get_value")
	def test_parallel_final_export_requests_deduplicate_under_register_lock(
		self, get_value, check_permission, lock, _validate_final, create_export
	):
		register = LocalRegisterStub(
			name="REGISTER-1",
			connection="CONNECTION-1",
			provider_state="DECOMMISSIONED",
			closing_receipt="CLOSING-1",
			decommission_status="EVIDENCE_REQUIRED",
			reload=Mock(),
		)
		register.db_set = Mock()
		check_permission.return_value = register
		get_value.side_effect = [
			None,
			frappe._dict(
				name="DEP7-FINAL",
				status="QUEUED",
				creation="2026-08-18 10:02:00",
				generated_at=None,
			),
		]

		first = create_final_decommission_export("REGISTER-1")
		second = create_final_decommission_export("REGISTER-1")

		self.assertEqual((first, second), ("DEP7-FINAL", "DEP7-FINAL"))
		self.assertEqual(lock.call_count, 2)
		create_export.assert_called_once_with(register, purpose="DECOMMISSION_FINAL")
		self.assertEqual(register.db_set.call_count, 2)

	@patch("erpnext_fiskaly_sign_at.api.exports._create_dep7_export_doc", return_value="DEP7-NEW")
	@patch("erpnext_fiskaly_sign_at.api.exports._validate_final_decommission_export")
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("erpnext_fiskaly_sign_at.api.exports._check_export_creation_permission")
	@patch("frappe.db.get_value")
	def test_incomplete_ready_final_export_is_replaced(
		self, get_value, check_permission, _lock, _validate_final, create_export
	):
		register = LocalRegisterStub(
			name="REGISTER-1",
			connection="CONNECTION-1",
			provider_state="DECOMMISSIONED",
			closing_receipt="CLOSING-1",
			decommission_status="EVIDENCE_REQUIRED",
			reload=Mock(),
			db_set=Mock(),
		)
		check_permission.return_value = register
		get_value.return_value = frappe._dict(
			name="DEP7-INCOMPLETE",
			status="READY",
			creation="2026-08-18 10:02:00",
			generated_at="2026-08-18 10:03:00",
			export_file="/private/files/dep7.json",
			file_hash="abc",
			supplementary_export_file=None,
			supplementary_file_hash=None,
		)

		result = create_final_decommission_export("REGISTER-1")

		self.assertEqual(result, "DEP7-NEW")
		create_export.assert_called_once_with(register, purpose="DECOMMISSION_FINAL")
		register.db_set.assert_called_once_with("final_dep7_export", "DEP7-NEW", update_modified=False)

	@patch("erpnext_fiskaly_sign_at.api.exports.get_provider")
	@patch(
		"erpnext_fiskaly_sign_at.api.exports._validate_final_decommission_export",
		side_effect=frappe.ValidationError("stale final export"),
	)
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("frappe.get_doc")
	@patch("frappe.db.sql", return_value=[frappe._dict(status="QUEUED")])
	def test_worker_refuses_stale_final_export_before_provider_call(
		self, _sql, get_doc, lock, _validate_final, get_provider_mock
	):
		doc = LocalRegisterStub(
			name="DEP7-STALE",
			status="QUEUED",
			purpose="DECOMMISSION_FINAL",
			register="REGISTER-1",
			creation="2026-08-18 09:59:00",
		)
		get_doc.return_value = doc

		result = process_dep7_export("DEP7-STALE")

		self.assertEqual(result["status"], "ACTION_REQUIRED")
		self.assertEqual(doc.status, "ACTION_REQUIRED")
		lock.assert_called_once_with("REGISTER-1")
		get_provider_mock.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._outage_signature_is_valid", return_value=True)
	@patch("frappe.db.set_value")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists", return_value="AUTO-RECEIPT-1")
	def test_terminal_special_receipt_rejects_provider_evidence_drift_but_accepts_fon_update(
		self, _exists, get_doc, set_value, _outage_signature_is_valid
	):
		signed_at = int(datetime(2026, 8, 18, 10, 0, tzinfo=UTC).timestamp())
		data = {
			"_id": "AUTO-RECEIPT-1",
			"receipt_type": "YEARLY_CLOSE",
			"receipt_number": "42",
			"time_signature": signed_at,
			"cash_register_serial_number": "SERIAL-1",
			"qr_code_data": "_R1-AT0_REGISTER_42_2026-08-18T10:00:00_0_0_0_0_0_CHAIN_SIGNATURE",
			"signed": True,
			"hints": ["stable provider hint"],
			"fon_validations": [],
		}
		existing = LocalRegisterStub(
			company="COMPANY-1",
			company_address_display="Stored address",
			register="REGISTER-1",
			provider_receipt_id="AUTO-RECEIPT-1",
			receipt_type="YEARLY_CLOSE",
			receipt_kind="YEARLY",
			receipt_number="42",
			serial_number="SERIAL-1",
			qr_code_data=data["qr_code_data"],
			status="SIGNED",
			signed_at=FiskalyRegister._as_datetime(signed_at),
			signature_value="SIGNATURE",
			signed=1,
			hints="stable provider hint",
			verification_by=None,
			print_evidence_at=None,
		)
		get_doc.return_value = existing
		register = LocalRegisterStub(
			name="REGISTER-1",
			company="COMPANY-1",
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="LIVE",
			_company_address_snapshot=lambda: "Current address",
			_closing_period=FiskalyRegister._closing_period,
			_fon_evidence=FiskalyRegister._fon_evidence,
			_as_datetime=FiskalyRegister._as_datetime,
		)

		fon_update = {
			**data,
			"fon_validations": [{"validation_result": "SUCCESS", "time_validation": signed_at}],
		}
		FiskalyRegister._store_provider_receipt(register, fon_update)
		stored_values = set_value.call_args.args[2]
		self.assertEqual(stored_values["fon_validation_status"], "SUCCESS")
		self.assertEqual(stored_values["signed_at"], existing.signed_at)

		mutations = (
			("signed_at", {**data, "time_signature": signed_at + 1}),
			("hints", {**data, "hints": ["changed provider hint"]}),
			("status", {**data, "signed": False}),
		)
		for fieldname, changed_data in mutations:
			with self.subTest(fieldname=fieldname):
				with self.assertRaisesRegex(frappe.ValidationError, fieldname):
					FiskalyRegister._store_provider_receipt(register, changed_data)

	def test_closing_period_belongs_to_previous_period(self):
		january_first = int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())
		self.assertEqual(
			FiskalyRegister._closing_period(
				{"receipt_type": "YEARLY_CLOSE", "time_signature": january_first}
			),
			"2026",
		)
		self.assertEqual(
			FiskalyRegister._closing_period(
				{"receipt_type": "MONTHLY_CLOSE", "time_signature": january_first}
			),
			"2026-12",
		)

	def test_quarter_bounds_cover_complete_quarter(self):
		start, end = _quarter_bounds(2026, 2)
		self.assertEqual(start, datetime(2026, 4, 1))
		self.assertEqual(end, datetime(2026, 6, 30, 23, 59, 59))

	def test_retention_runs_to_end_of_seventh_following_calendar_year(self):
		self.assertEqual(_retention_until(datetime(2026, 4, 2, 9, 0)), date(2033, 12, 31))

	@patch("erpnext_fiskaly_sign_at.api.exports.create_dep7_export")
	@patch("erpnext_fiskaly_sign_at.api.exports._check_export_creation_permission")
	@patch("frappe.db.get_value", return_value=None)
	def test_quarterly_backup_is_a_complete_dep_snapshot(self, _get_value, _check_permission, create_export):
		create_export.return_value = "DEP7-0001"

		result = create_quarterly_backup("REGISTER-1", 2026, 2)

		self.assertEqual(result, "DEP7-0001")
		create_export.assert_called_once_with(
			"REGISTER-1",
			purpose="QUARTERLY_BACKUP",
			fiscal_year=2026,
			fiscal_quarter=2,
		)

	def test_quarterly_backup_rejects_period_scope_in_doctype(self):
		doc = frappe.new_doc("Fiskaly DEP7 Export")
		doc.update(
			{
				"purpose": "QUARTERLY_BACKUP",
				"fiscal_year": 2026,
				"fiscal_quarter": 2,
				"date_from": datetime(2026, 4, 1),
				"date_to": datetime(2026, 6, 30, 23, 59, 59),
			}
		)
		with self.assertRaises(frappe.ValidationError):
			FiskalyDEP7Export.validate(doc)

	def test_dep7_creation_timestamp_is_immutable_evidence(self):
		doc = LocalRegisterStub(
			IMMUTABLE_EVIDENCE_FIELDS=FiskalyDEP7Export.IMMUTABLE_EVIDENCE_FIELDS,
			creation="2026-08-18 10:05:00",
			purpose="MANUAL",
			date_from=None,
			date_to=None,
			is_new=lambda: False,
			get_doc_before_save=lambda: frappe._dict(creation="2026-08-18 09:00:00"),
		)

		with self.assertRaisesRegex(frappe.ValidationError, "creation"):
			FiskalyDEP7Export.validate(doc)

	@patch("frappe.get_doc")
	@patch("frappe.db.sql")
	def test_ready_dep7_export_is_never_generated_twice(self, sql, get_doc):
		sql.return_value = [frappe._dict(status="READY")]
		get_doc.return_value = SimpleNamespace(status="READY", name="DEP7-READY")

		result = process_dep7_export("DEP7-READY")

		self.assertEqual(result["status"], "READY")
		self.assertTrue(result["idempotent"])

	@patch("frappe.get_all")
	def test_supplementary_export_contains_customary_item_details(self, get_all):
		payload = json.dumps(
			{
				"issued_at": "2026-08-18T10:00:00+02:00",
				"entries": [],
				"source_entries": [
					{
						"code": "APFEL",
						"label": "Bio-Apfel",
						"description": "Bio-Apfel lose",
						"quantity": "2",
						"uom": "Stk",
					}
				],
			},
			separators=(",", ":"),
		)
		get_all.return_value = [
			frappe._dict(
				receipt_uuid="9ca279e0-c1d1-4d30-8c74-35db704282d2",
				provider_receipt_id="9ca279e0-c1d1-4d30-8c74-35db704282d2",
				receipt_number="7",
				signed_at=datetime(2026, 8, 18, 10, 0),
				offline_issued_at=None,
				status="SIGNED",
				pos_invoice="ACC-POS-INV-0007",
				request_payload=payload,
				payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
			)
		]

		result = _build_supplementary_receipt_items(
			"REGISTER-1", datetime(2026, 8, 1), datetime(2026, 8, 31, 23, 59, 59)
		)

		self.assertEqual(result["format"], "RKSV_SUPPLEMENTARY_RECEIPT_ITEMS")
		self.assertEqual(result["version"], 1)
		self.assertEqual(result["receipts"][0]["erp_pos_invoice"], "ACC-POS-INV-0007")
		self.assertEqual(result["receipts"][0]["items"][0]["quantity"], "2")
		self.assertEqual(result["receipts"][0]["items"][0]["item_name"], "Bio-Apfel")
		self.assertEqual(result["receipts"][0]["items"][0]["description"], "Bio-Apfel lose")
		self.assertEqual(get_all.call_count, 1)
