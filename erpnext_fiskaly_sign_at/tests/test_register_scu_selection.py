from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register import (
	FiskalyRegister,
	_decommission_workflow_state,
	_is_active_scu_limit_error,
	get_available_scus,
	get_decommission_workflow,
	get_terminal_status_summary,
)
from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError


class TestRegisterScuSelection(TestCase):
	def test_active_scu_limit_is_detected_by_provider_code(self):
		error = PermanentFiskalyError(
			"Limit of 1 active signature creation units reached.",
			status_code=400,
			code="E_SCU_LIMIT_REACHED",
		)

		self.assertTrue(_is_active_scu_limit_error(error))

	def test_unrelated_provider_error_is_not_treated_as_scu_limit(self):
		error = PermanentFiskalyError(
			"The selected resource is invalid.",
			status_code=400,
			code="E_INVALID_RESOURCE",
		)

		self.assertFalse(_is_active_scu_limit_error(error))

	def test_decommission_workflow_endpoint_is_read_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[get_decommission_workflow],
			["GET"],
		)

	def test_terminal_status_endpoint_is_read_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[get_terminal_status_summary],
			["GET"],
		)

	def test_scu_list_endpoint_is_read_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[get_available_scus],
			["GET"],
		)

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_register.fiskaly_register.get_provider"
	)
	@patch("frappe.get_doc")
	def test_scu_list_requires_connection_read_permission_before_provider_call(self, get_doc, get_provider):
		get_doc.return_value = SimpleNamespace(
			check_permission=Mock(side_effect=frappe.PermissionError),
			company="COMPANY-1",
			provider="SIGN_AT_V1",
		)

		with self.assertRaises(frappe.PermissionError):
			get_available_scus("CONNECTION-1", "COMPANY-1")

		get_doc.return_value.check_permission.assert_called_once_with("read")
		get_provider.assert_not_called()

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_register.fiskaly_register.get_provider"
	)
	@patch("frappe.get_doc")
	def test_scu_list_returns_only_safe_dropdown_fields(self, get_doc, get_provider):
		connection = SimpleNamespace(
			check_permission=Mock(),
			company="COMPANY-1",
			provider="SIGN_AT_V1",
		)
		get_doc.return_value = connection
		get_provider.return_value.list_selectable_scus.return_value = [
			{
				"_id": "scu-id",
				"state": "INITIALIZED",
				"legal_entity_name": "Company One",
				"provider_secret": "must-not-leak",
			}
		]

		result = get_available_scus("CONNECTION-1", "COMPANY-1")

		self.assertEqual(
			result,
			[
				{
					"id": "scu-id",
					"state": "INITIALIZED",
					"legal_entity_name": "Company One",
				}
			],
		)
		connection.check_permission.assert_called_once_with("read")
		get_provider.return_value.list_selectable_scus.assert_called_once()

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("erpnext_fiskaly_sign_at.services.compliance.require_canonical_pos_print_setup")
	@patch("frappe.only_for")
	def test_provisioning_translates_active_scu_limit_into_actionable_guidance(
		self, only_for, require_print_setup, lock
	):
		provider = SimpleNamespace(
			provision_register=Mock(
				side_effect=PermanentFiskalyError(
					"Limit of 1 active (INITIALIZED, OUTAGE) signature creation units reached.",
					status_code=400,
					code="E_SCU_LIMIT_REACHED",
				)
			)
		)
		connection = frappe._dict(
			provider="SIGN_AT_V1",
			environment="TEST",
			fon_participant_id="TESTAT01",
			fon_user_id="TESTUSER",
			fon_user_pin="******",
			get_password=lambda _fieldname, raise_exception=True: "TESTPIN1",
			tax_id_number="1234567",
			vat_id_number=None,
		)
		register = SimpleNamespace(
			name="REGISTER-1",
			initialized=0,
			provider_state=None,
			pos_profile="POS-1",
			scu_assignment_mode="NEW",
			signature_creation_unit_id=None,
			provider_register_id=None,
			check_permission=Mock(),
			reload=Mock(),
			_validate_austrian_runtime=Mock(),
			_provider=Mock(return_value=(connection, provider)),
			_stable_provider_uuid=Mock(side_effect=["SCU-1", "REGISTER-ID-1"]),
			db_set=Mock(),
		)

		with self.assertRaisesRegex(frappe.ValidationError, "Bestehende SCU auswählen"):
			FiskalyRegister.provision_register(register)

		register.check_permission.assert_called_with("write")
		only_for.assert_called_once_with("System Manager")
		lock.assert_called_once_with("REGISTER-1")
		require_print_setup.assert_called_once_with("POS-1")
		provider.provision_register.assert_called_once_with(register)
		self.assertEqual(connection.tax_id_number, "123/4567")

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("erpnext_fiskaly_sign_at.services.compliance.require_canonical_pos_print_setup")
	@patch("frappe.only_for")
	def test_unified_provisioning_requires_visible_fon_dummy_values(
		self, only_for, require_print_setup, lock
	):
		provider = SimpleNamespace(provision_register=Mock())
		connection = frappe._dict(
			provider="SIGN_AT_UNIFIED",
			environment="TEST",
			fon_participant_id=None,
			fon_user_id=None,
			fon_user_pin=None,
			get_password=lambda _fieldname, raise_exception=True: None,
			tax_id_number="123/4567",
			vat_id_number=None,
		)
		register = SimpleNamespace(
			name="REGISTER-UNIFIED-1",
			initialized=0,
			provider_state=None,
			pos_profile="POS-1",
			check_permission=Mock(),
			reload=Mock(),
			_validate_austrian_runtime=Mock(),
			_provider=Mock(return_value=(connection, provider)),
		)

		with self.assertRaisesRegex(frappe.ValidationError, "Dummy-Daten"):
			FiskalyRegister.provision_register(register)

		provider.provision_register.assert_not_called()
		lock.assert_called_once_with("REGISTER-UNIFIED-1")
		require_print_setup.assert_called_once_with("POS-1")
		only_for.assert_called_once_with("System Manager")

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("erpnext_fiskaly_sign_at.services.compliance.require_canonical_pos_print_setup")
	@patch("frappe.only_for")
	def test_unified_provisioning_rejects_invalid_fon_format_before_provider_call(
		self, only_for, require_print_setup, lock
	):
		provider = SimpleNamespace(provision_register=Mock())
		connection = frappe._dict(
			provider="SIGN_AT_UNIFIED",
			environment="TEST",
			fon_participant_id="TESTunified01",
			fon_user_id="TESTUSER",
			fon_user_pin="******",
			get_password=lambda _fieldname, raise_exception=True: "TESTPIN1",
			tax_id_number="123/4567",
			vat_id_number=None,
		)
		register = SimpleNamespace(
			name="REGISTER-UNIFIED-1",
			initialized=0,
			provider_state=None,
			pos_profile="POS-1",
			check_permission=Mock(),
			reload=Mock(),
			_validate_austrian_runtime=Mock(),
			_provider=Mock(return_value=(connection, provider)),
		)

		with self.assertRaisesRegex(frappe.ValidationError, "aktuell: 13 Zeichen"):
			FiskalyRegister.provision_register(register)

		provider.provision_register.assert_not_called()
		lock.assert_called_once_with("REGISTER-UNIFIED-1")
		require_print_setup.assert_called_once_with("POS-1")
		only_for.assert_called_once_with("System Manager")

	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	@patch("erpnext_fiskaly_sign_at.services.compliance.require_canonical_pos_print_setup")
	@patch("frappe.only_for")
	def test_unified_provisioning_converts_customary_austrian_vat_id_for_api(
		self, only_for, require_print_setup, lock
	):
		provider = SimpleNamespace(
			provision_register=Mock(side_effect=PermanentFiskalyError("stop after preflight"))
		)
		connection = frappe._dict(
			provider="SIGN_AT_UNIFIED",
			environment="TEST",
			fon_participant_id="TESTAT01",
			fon_user_id="TESTUSER",
			fon_user_pin="********",
			get_password=lambda _fieldname, raise_exception=True: "TESTPIN1",
			tax_id_number="543762173",
			vat_id_number="ATU77315745",
		)
		register = SimpleNamespace(
			name="REGISTER-UNIFIED-1",
			initialized=0,
			provider_state=None,
			pos_profile="POS-1",
			check_permission=Mock(),
			reload=Mock(),
			_validate_austrian_runtime=Mock(),
			_provider=Mock(return_value=(connection, provider)),
		)

		with self.assertRaisesRegex(PermanentFiskalyError, "stop after preflight"):
			FiskalyRegister.provision_register(register)

		self.assertEqual(connection.tax_id_number, "54-376/2173")
		self.assertEqual(connection.vat_id_number, "U77315745")
		provider.provision_register.assert_called_once_with(register)
		lock.assert_called_once_with("REGISTER-UNIFIED-1")
		require_print_setup.assert_called_once_with("POS-1")
		only_for.assert_called_once_with("System Manager")

	@patch("frappe.db.get_value")
	def test_decommission_workflow_reports_each_completed_evidence_step(self, get_value):
		get_value.side_effect = [
			frappe._dict(
				name="CLOSING-1",
				status="SIGNED",
				fon_validation_status="SUCCESS",
				print_evidence_at="2026-08-19 10:00:00",
				print_evidence_file="/private/files/closing.pdf",
			),
			frappe._dict(
				name="DEP7-FINAL",
				status="READY",
				export_file="/private/files/dep7.json",
				file_hash="abc",
				supplementary_export_file="/private/files/supplement.json",
				supplementary_file_hash="def",
				integrity_verified_at="2026-08-19 10:01:00",
				supplementary_integrity_verified_at="2026-08-19 10:01:00",
				external_copy_confirmed=1,
				external_storage_reference="WORM-2026-08",
				action_required_reason=None,
				last_error=None,
			),
		]
		register = SimpleNamespace(
			name="REGISTER-1",
			provider_state="DECOMMISSIONED",
			decommission_status="COMPLETE",
			closing_receipt="CLOSING-1",
			final_dep7_export="DEP7-FINAL",
		)

		state = _decommission_workflow_state(register)

		self.assertTrue(state["closing_archived"])
		self.assertTrue(state["export_ready"])
		self.assertTrue(state["integrity_verified"])
		self.assertTrue(state["external_copy_confirmed"])
		self.assertTrue(state["complete"])

	@patch("frappe.db.get_value")
	def test_decommission_workflow_requires_both_final_export_files(self, get_value):
		get_value.side_effect = [
			frappe._dict(
				name="CLOSING-1",
				status="SIGNED",
				fon_validation_status="SUCCESS",
				print_evidence_at="2026-08-19 10:00:00",
				print_evidence_file="/private/files/closing.pdf",
			),
			frappe._dict(
				name="DEP7-FINAL",
				status="READY",
				export_file="/private/files/dep7.json",
				file_hash="abc",
				supplementary_export_file=None,
				supplementary_file_hash=None,
				integrity_verified_at=None,
				supplementary_integrity_verified_at=None,
				external_copy_confirmed=0,
				external_storage_reference=None,
				action_required_reason=None,
				last_error=None,
			),
		]
		register = SimpleNamespace(
			name="REGISTER-1",
			provider_state="DECOMMISSIONED",
			decommission_status="EVIDENCE_REQUIRED",
			closing_receipt="CLOSING-1",
			final_dep7_export="DEP7-FINAL",
		)

		state = _decommission_workflow_state(register)

		self.assertFalse(state["export_ready"])
		self.assertFalse(state["integrity_verified"])

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_register.fiskaly_register._decommission_workflow_state",
		return_value={"closing_archived": True},
	)
	@patch("frappe.get_doc")
	@patch("frappe.only_for")
	def test_combined_decommission_archives_the_generated_closing_receipt(
		self, only_for, get_doc, workflow_state
	):
		closing = SimpleNamespace(create_and_archive_print_evidence=Mock())
		get_doc.return_value = closing
		register = SimpleNamespace(
			name="REGISTER-1",
			provider_state="ACTIVE",
			closing_receipt="CLOSING-1",
			check_permission=Mock(),
			decommission_register=Mock(),
			reload=Mock(),
		)

		result = FiskalyRegister.decommission_and_archive_closing_receipt(
			register, "Filiale wird geschlossen"
		)

		self.assertEqual(result, {"closing_archived": True})
		register.check_permission.assert_called_once_with("write")
		only_for.assert_called_once_with("System Manager")
		register.decommission_register.assert_called_once_with("Filiale wird geschlossen")
		get_doc.assert_called_once_with("Fiskaly Receipt", "CLOSING-1")
		closing.create_and_archive_print_evidence.assert_called_once_with()
		workflow_state.assert_called_once_with(register)

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_register.fiskaly_register._decommission_workflow_state",
		return_value={"closing_ready": True, "closing_archived": False},
	)
	@patch("frappe.msgprint")
	@patch("frappe.get_doc")
	@patch("frappe.only_for")
	def test_pdf_failure_does_not_roll_back_provider_decommissioning(
		self, only_for, get_doc, msgprint, workflow_state
	):
		closing = SimpleNamespace(
			create_and_archive_print_evidence=Mock(
				side_effect=frappe.ValidationError("PDF service unavailable")
			)
		)
		get_doc.return_value = closing
		register = SimpleNamespace(
			name="REGISTER-1",
			provider_state="ACTIVE",
			closing_receipt="CLOSING-1",
			check_permission=Mock(),
			decommission_register=Mock(),
			reload=Mock(),
		)

		result = FiskalyRegister.decommission_and_archive_closing_receipt(
			register, "Filiale wird geschlossen"
		)

		self.assertTrue(result["closing_ready"])
		self.assertFalse(result["closing_archived"])
		self.assertIn("nicht erneut geschlossen", result["archive_warning"])
		register.decommission_register.assert_called_once_with("Filiale wird geschlossen")
		closing.create_and_archive_print_evidence.assert_called_once_with()
		msgprint.assert_called_once()
		only_for.assert_called_once_with("System Manager")
		workflow_state.assert_called_once_with(register)

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_register.fiskaly_register._decommission_workflow_state",
		return_value={"integrity_verified": True},
	)
	@patch("frappe.only_for")
	def test_final_integrity_step_uses_only_the_linked_export(self, only_for, workflow_state):
		export = SimpleNamespace(verify_export_integrity=Mock())
		register = SimpleNamespace(
			check_permission=Mock(),
			_final_dep7_document=Mock(return_value=export),
		)

		result = FiskalyRegister.verify_final_dep7_integrity(register)

		self.assertEqual(result, {"integrity_verified": True})
		register.check_permission.assert_called_once_with("write")
		only_for.assert_called_once_with("System Manager")
		export.verify_export_integrity.assert_called_once_with()
		workflow_state.assert_called_once_with(register)

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_register.fiskaly_register._decommission_workflow_state",
		return_value={"external_copy_confirmed": True},
	)
	@patch("frappe.only_for")
	def test_external_copy_step_records_the_storage_reference(self, only_for, workflow_state):
		export = SimpleNamespace(mark_external_copy_confirmed=Mock())
		register = SimpleNamespace(
			check_permission=Mock(),
			_final_dep7_document=Mock(return_value=export),
		)

		result = FiskalyRegister.confirm_final_dep7_external_copy(register, "Tresor-USB-02")

		self.assertEqual(result, {"external_copy_confirmed": True})
		register.check_permission.assert_called_once_with("write")
		only_for.assert_called_once_with("System Manager")
		export.mark_external_copy_confirmed.assert_called_once_with("Tresor-USB-02")
		workflow_state.assert_called_once_with(register)

	@patch("frappe.get_doc")
	def test_decommission_workflow_requires_register_read_permission(self, get_doc):
		register = SimpleNamespace(check_permission=Mock(side_effect=frappe.PermissionError))
		get_doc.return_value = register

		with self.assertRaises(frappe.PermissionError):
			get_decommission_workflow("REGISTER-1")

		register.check_permission.assert_called_once_with("read")

	@patch("frappe.db.get_value")
	@patch("frappe.get_doc")
	def test_defective_register_summary_contains_recorded_reason(self, get_doc, get_value):
		register = SimpleNamespace(
			name="REGISTER-DEFECTIVE",
			provider_state="DEFECTIVE",
			decommission_status="NONE",
			check_permission=Mock(),
		)
		get_doc.return_value = register
		get_value.return_value = frappe._dict(
			name="EVENT-1",
			register=register.name,
			event_time="2026-08-19 12:00:00",
			actor="Administrator",
			reason="Druckersteuerung dauerhaft beschädigt",
			action_required_reason=None,
		)

		result = get_terminal_status_summary(register.name)

		self.assertTrue(result["terminal"])
		self.assertEqual(result["kind"], "DEFECTIVE")
		self.assertEqual(result["reason"], "Druckersteuerung dauerhaft beschädigt")
		register.check_permission.assert_called_once_with("read")
		self.assertEqual(get_value.call_args.args[1]["event_type"], "REGISTER_DEFECTIVE")

	@patch("frappe.db.get_value", return_value=None)
	@patch("frappe.get_doc")
	def test_decommissioned_register_summary_reports_open_evidence(self, get_doc, _get_value):
		register = SimpleNamespace(
			name="REGISTER-CLOSED",
			provider_state="DECOMMISSIONED",
			decommission_status="EVIDENCE_REQUIRED",
			check_permission=Mock(),
		)
		get_doc.return_value = register

		result = get_terminal_status_summary(register.name)

		self.assertEqual(result["kind"], "DECOMMISSIONED")
		self.assertIn("noch offen", result["status_detail"])
		self.assertIsNone(result["reason"])

	@patch("frappe.get_doc")
	def test_terminal_status_requires_register_read_permission(self, get_doc):
		register = SimpleNamespace(check_permission=Mock(side_effect=frappe.PermissionError))
		get_doc.return_value = register

		with self.assertRaises(frappe.PermissionError):
			get_terminal_status_summary("REGISTER-1")

		register.check_permission.assert_called_once_with("read")
