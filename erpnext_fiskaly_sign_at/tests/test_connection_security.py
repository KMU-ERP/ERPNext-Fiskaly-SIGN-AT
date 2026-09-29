from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_api_connection.fiskaly_api_connection import (
	FiskalyAPIConnection,
	normalize_austrian_tax_id_number,
	normalize_austrian_vat_id_number,
	unified_austrian_vat_id_number,
	validate_fon_credential_format,
)


class TestConnectionMethodSecurity(TestCase):
	def test_compact_austrian_tax_numbers_are_formatted_for_unified(self):
		self.assertEqual(normalize_austrian_tax_id_number("543762173"), "54-376/2173")
		self.assertEqual(normalize_austrian_tax_id_number("1234567"), "123/4567")
		self.assertEqual(normalize_austrian_tax_id_number("54-376/2173"), "54-376/2173")

	def test_invalid_austrian_tax_number_is_rejected_locally(self):
		with self.assertRaisesRegex(frappe.ValidationError, "7 oder 9 Ziffern"):
			normalize_austrian_tax_id_number("12345")

	def test_austrian_vat_id_is_stored_customarily_and_converted_for_unified(self):
		self.assertEqual(normalize_austrian_vat_id_number(" atu77315745 "), "ATU77315745")
		self.assertEqual(normalize_austrian_vat_id_number("U77315745"), "ATU77315745")
		self.assertEqual(unified_austrian_vat_id_number("ATU77315745"), "U77315745")

	def test_invalid_austrian_vat_id_is_rejected_locally(self):
		with self.assertRaisesRegex(frappe.ValidationError, "ATU und 8 Ziffern"):
			normalize_austrian_vat_id_number("ATU123")

	def test_fon_credentials_accept_documented_test_values(self):
		validate_fon_credential_format("TESTAT01", "TESTUSER", "TESTPIN1")
		validate_fon_credential_format(
			"TESTAT01", "TEST01", "TEST01", provider="SIGN_AT_V1"
		)

	def test_fon_credentials_reject_overlong_participant_before_provider_request(self):
		with self.assertRaisesRegex(frappe.ValidationError, "aktuell: 13 Zeichen"):
			validate_fon_credential_format("TESTunified01", "TESTUSER", "TESTPIN1")

	def test_fon_credentials_reject_user_id_shorter_than_unified_schema(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Benutzer-ID muss 8 bis 12"):
			validate_fon_credential_format("TESTAT01", "TEST01", "TESTPIN1")

	def test_fon_credentials_reject_pin_shorter_than_unified_schema(self):
		with self.assertRaisesRegex(frappe.ValidationError, "PIN muss 8 bis 128"):
			validate_fon_credential_format("TESTAT01", "TESTUSER", "TEST01")

	def test_mutating_connection_methods_are_post_only(self):
		for method_name in ("test_connection", "get_unified_scopes", "authenticate_fon"):
			with self.subTest(method=method_name):
				method = getattr(FiskalyAPIConnection, method_name)
				self.assertEqual(
					frappe.allowed_http_methods_for_whitelisted_func[method],
					["POST"],
				)

	@patch("erpnext_fiskaly_sign_at.providers.get_provider")
	def test_connection_requires_document_write_permission_before_provider_call(self, get_provider):
		doc = SimpleNamespace(
			check_permission=Mock(side_effect=frappe.PermissionError),
			db_set=Mock(),
		)

		with self.assertRaises(frappe.PermissionError):
			FiskalyAPIConnection.test_connection(doc)

		doc.check_permission.assert_called_once_with("write")
		get_provider.assert_not_called()
		doc.db_set.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.providers.get_provider")
	def test_scope_listing_requires_document_write_permission(self, get_provider):
		doc = SimpleNamespace(
			provider="SIGN_AT_UNIFIED",
			check_permission=Mock(side_effect=frappe.PermissionError),
		)

		with self.assertRaises(frappe.PermissionError):
			FiskalyAPIConnection.get_unified_scopes(doc)

		doc.check_permission.assert_called_once_with("write")
		get_provider.assert_not_called()

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_api_connection.fiskaly_api_connection.frappe.db.get_value"
	)
	def test_unified_scope_cannot_be_assigned_to_different_companies(self, get_value):
		get_value.return_value = frappe._dict(
			name="RAN Soft Unified", company="RAN Soft GmbH & Co KG"
		)
		doc = SimpleNamespace(
			provider="SIGN_AT_UNIFIED",
			api_key=None,
			scope_identifier="organization-1",
			company="Huber GmbH",
			name="Huber Unified",
		)

		with self.assertRaises(frappe.ValidationError):
			FiskalyAPIConnection._validate_unified_tenant_binding(doc)

		self.assertEqual(
			get_value.call_args_list[0].args[1]["scope_identifier"], "organization-1"
		)

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_api_connection.fiskaly_api_connection.frappe.db.get_value"
	)
	def test_unified_api_key_cannot_be_shared_across_companies(self, get_value):
		get_value.return_value = frappe._dict(
			name="RAN Soft Unified", company="RAN Soft GmbH & Co KG"
		)
		doc = SimpleNamespace(
			provider="SIGN_AT_UNIFIED",
			api_key="organization-api-key",
			scope_identifier=None,
			company="Huber GmbH",
			name="Huber Unified",
		)

		with self.assertRaises(frappe.ValidationError):
			FiskalyAPIConnection._validate_unified_tenant_binding(doc)

		self.assertEqual(
			get_value.call_args_list[0].args[1]["api_key"], "organization-api-key"
		)

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_api_connection.fiskaly_api_connection.frappe.utils.now",
		return_value="2026-08-18 12:00:00",
	)
	@patch("erpnext_fiskaly_sign_at.providers.get_provider")
	def test_connection_returns_structured_success_and_updates_status(self, get_provider, now):
		provider = get_provider.return_value
		provider.test_connection.return_value = {"authenticated": True}
		doc = SimpleNamespace(check_permission=Mock(), db_set=Mock())

		result = FiskalyAPIConnection.test_connection(doc)

		self.assertEqual(
			result,
			{
				"success": True,
				"status": "CONNECTED",
				"last_tested_at": "2026-08-18 12:00:00",
				"result": {"authenticated": True},
				"error": None,
			},
		)
		doc.check_permission.assert_called_once_with("write")
		get_provider.assert_called_once_with(doc)
		provider.test_connection.assert_called_once_with()
		doc.db_set.assert_called_once_with(
			{
				"status": "CONNECTED",
				"last_error": None,
				"last_tested_at": "2026-08-18 12:00:00",
			}
		)
		now.assert_called_once_with()

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_api_connection.fiskaly_api_connection.frappe.db.commit"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_api_connection.fiskaly_api_connection.frappe.utils.now",
		return_value="2026-08-18 12:01:00",
	)
	@patch("erpnext_fiskaly_sign_at.providers.get_provider")
	def test_connection_returns_structured_failure_without_forcing_commit(self, get_provider, now, commit):
		provider = get_provider.return_value
		provider.test_connection.side_effect = RuntimeError("provider unavailable")
		doc = SimpleNamespace(check_permission=Mock(), db_set=Mock())

		result = FiskalyAPIConnection.test_connection(doc)

		self.assertEqual(
			result,
			{
				"success": False,
				"status": "ERROR",
				"last_tested_at": "2026-08-18 12:01:00",
				"result": None,
				"error": "provider unavailable",
			},
		)
		doc.check_permission.assert_called_once_with("write")
		get_provider.assert_called_once_with(doc)
		provider.test_connection.assert_called_once_with()
		doc.db_set.assert_called_once_with(
			{
				"status": "ERROR",
				"last_error": "provider unavailable",
				"last_tested_at": "2026-08-18 12:01:00",
			}
		)
		now.assert_called_once_with()
		commit.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.providers.get_provider")
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_api_connection.fiskaly_api_connection.frappe.only_for",
		side_effect=frappe.PermissionError,
	)
	def test_fon_authentication_requires_system_manager_before_provider_call(self, only_for, get_provider):
		doc = SimpleNamespace(
			provider="SIGN_AT_V1",
			check_permission=Mock(),
		)

		with self.assertRaises(frappe.PermissionError):
			FiskalyAPIConnection.authenticate_fon(doc)

		doc.check_permission.assert_called_once_with("write")
		only_for.assert_called_once_with("System Manager")
		get_provider.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.providers.get_provider")
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_api_connection.fiskaly_api_connection.frappe.only_for"
	)
	def test_fon_authentication_runs_after_both_permission_checks(self, only_for, get_provider):
		provider = get_provider.return_value
		provider.authenticate_fon.return_value = {"authentication_status": "AUTHENTICATED"}
		doc = SimpleNamespace(
			provider="SIGN_AT_V1",
			check_permission=Mock(),
		)

		result = FiskalyAPIConnection.authenticate_fon(doc)

		self.assertEqual(result, {"authentication_status": "AUTHENTICATED"})
		doc.check_permission.assert_called_once_with("write")
		only_for.assert_called_once_with("System Manager")
		get_provider.assert_called_once_with(doc)
