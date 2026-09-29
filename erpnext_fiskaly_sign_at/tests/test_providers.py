import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from erpnext_fiskaly_sign_at.contracts import (
	FiscalReceiptRequest,
	Payment,
	ReceiptEntry,
	ReceiptType,
	VatBucket,
)
from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError, ProviderNotAvailableError
from erpnext_fiskaly_sign_at.providers.sign_at_unified import SignAtUnifiedProvider
from erpnext_fiskaly_sign_at.providers.sign_at_v1 import SignAtV1Provider
from erpnext_fiskaly_sign_at.services.receipt_builder import request_from_dict


def sample_request():
	return FiscalReceiptRequest(
		receipt_uuid="9ca279e0-c1d1-4d30-8c74-35db704282d2",
		document_number="ACC-POS-INV-0001",
		issued_at=datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
		receipt_type=ReceiptType.NORMAL,
		currency="EUR",
		total_net=Decimal("10.00"),
		total_vat=Decimal("2.00"),
		total_gross=Decimal("12.00"),
		vat_buckets=(
			VatBucket(
				code="standard",
				rate=Decimal("20"),
				net=Decimal("10"),
				vat=Decimal("2"),
				gross=Decimal("12"),
			),
		),
		entries=(
			ReceiptEntry(
				code="ITEM-1",
				label="Test item",
				quantity=Decimal("1"),
				net=Decimal("10"),
				vat=Decimal("2"),
				gross=Decimal("12"),
				vat_code="STANDARD",
				vat_rate=Decimal("20"),
			),
		),
		payments=(Payment(type="CASH", amount=Decimal("12"), label="Cash"),),
		operator="Administrator",
	)


class RecordingHttp:
	def __init__(self, responses):
		self.responses = list(responses)
		self.calls = []

	def request(self, method, path, **kwargs):
		self.calls.append((method, path, kwargs))
		return self.responses.pop(0)


def complete_v1_sign_response(*, signed=True, environment="TEST"):
	return {
		"_id": sample_request().receipt_uuid,
		"_type": "RECEIPT",
		"_env": environment,
		"_version": "1.2.6",
		"receipt_type": "NORMAL",
		"receipt_number": 7,
		"time_signature": 1787049000,
		"cash_register_serial_number": "REG-1",
		"cash_register_id": "register-id",
		"signature_creation_unit_id": "scu-id",
		"qr_code_data": "_R1-AT3_payload_signature",
		"signed": signed,
		"schema": {
			"raw": {
				"gross_amount_standard": "12.00",
				"gross_amount_reduced_1": "0.00",
				"gross_amount_reduced_2": "0.00",
				"gross_amount_special": "0.00",
				"gross_amount_zero": "0.00",
			}
		},
	}


class TestProviderContracts(TestCase):
	@patch("erpnext_fiskaly_sign_at.providers.sign_at_v1.frappe.cache")
	def test_v1_authentication_rejects_token_environment_mismatch(self, cache):
		cache.get_value.return_value = None
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(
			name="LIVE-CONNECTION",
			environment="LIVE",
			api_key="key",
			get_password=lambda _fieldname: "secret",
		)
		provider.http = RecordingHttp(
			[
				{
					"access_token": "token",
					"access_token_claims": {"env": "TEST"},
					"access_token_expires_in": 600,
				}
			]
		)

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider.test_connection()

		self.assertEqual(failure.exception.code, "E_PROVIDER_ENVIRONMENT_MISMATCH")
		self.assertNotIn("access_token", failure.exception.response)
		cache.set_value.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.providers.sign_at_v1.frappe.cache")
	def test_v1_authentication_without_environment_claim_fails_closed(self, cache):
		cache.get_value.return_value = None
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(
			name="LIVE-CONNECTION",
			environment="LIVE",
			api_key="key",
			get_password=lambda _fieldname: "secret",
		)
		provider.http = RecordingHttp([{"access_token": "token"}])

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider.test_connection()

		self.assertEqual(failure.exception.code, "E_PROVIDER_ENVIRONMENT_UNVERIFIED")
		cache.set_value.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.providers.sign_at_v1.frappe.cache")
	def test_v1_authentication_caches_only_verified_environment(self, cache):
		cache.get_value.return_value = None
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(
			name="TEST-CONNECTION",
			environment="TEST",
			api_key="key",
			get_password=lambda _fieldname: "secret",
		)
		provider.http = RecordingHttp(
			[
				{
					"access_token": "token",
					"access_token_claims": {"env": "TEST"},
					"access_token_expires_in": 600,
				}
			]
		)

		result = provider.test_connection()

		self.assertTrue(result["environment_verified"])
		self.assertEqual(result["environment"], "TEST")
		self.assertEqual(cache.set_value.call_count, 2)
		self.assertEqual(cache.set_value.call_args_list[1].args[1], "TEST")

	def test_v1_provisioning_rejects_scu_environment_mismatch(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="LIVE")
		provider.authenticate_fon = lambda: {"authentication_status": "AUTHENTICATED"}
		provider.ensure_automatic_closing_validation = lambda: {}
		provider._retrieve_or_create_scu = lambda _register, _scu_id: (
			{
				"_id": "scu-id",
				"_type": "SIGNATURE_CREATION_UNIT",
				"_env": "TEST",
				"state": "INITIALIZED",
				"legal_entity_id": {"tax_id": "123"},
			},
			False,
		)
		provider._retrieve_or_create_register = lambda *_args: self.fail(
			"cash-register provisioning must not continue after an SCU environment mismatch"
		)
		register = SimpleNamespace(
			signature_creation_unit_id="scu-id",
			provider_register_id="register-id",
		)

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider.provision_register(register)

		self.assertEqual(failure.exception.code, "E_PROVIDER_ENVIRONMENT_MISMATCH")

	def test_v1_provisioning_reuses_matching_organization_scu(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		provider.authenticate_fon = lambda: {"authentication_status": "AUTHENTICATED"}
		provider.ensure_automatic_closing_validation = lambda: {}
		provider._retrieve_or_create_scu = lambda _register, _scu_id: (
			{
				"_id": "shared-scu-id",
				"_type": "SIGNATURE_CREATION_UNIT",
				"_env": "TEST",
				"state": "INITIALIZED",
				"legal_entity_id": {"vat_id": "ATU77315745"},
			},
			True,
		)
		provider._retrieve_or_create_register = lambda _register, _register_id: {
			"_id": "register-id",
			"_type": "CASH_REGISTER",
			"_env": "TEST",
			"state": "INITIALIZED",
			"serial_number": "REG-1",
			"initialization_receipt_id": "initialization-id",
		}
		provider.retrieve_receipt = lambda _register_id, _receipt_id: {
			"signed": True,
			"fon_validations": [],
		}
		provider._validate_receipt_response = lambda *_args, **_kwargs: None
		provider.validate_receipt_with_fon = lambda _register, _receipt_id: {"validation_status": "SUCCESS"}
		register = SimpleNamespace(
			signature_creation_unit_id="new-register-scu-id",
			provider_register_id="register-id",
		)

		result = provider.provision_register(register)

		self.assertEqual(register.signature_creation_unit_id, "shared-scu-id")
		self.assertEqual(result["signature_creation_unit_id"], "shared-scu-id")
		self.assertEqual(result["resources"][0]["id"], "shared-scu-id")

	def test_v1_selectable_scus_require_matching_legal_entity_and_usable_state(self):
		provider = object.__new__(SignAtV1Provider)
		provider._legal_entity = lambda _register: ({"vat_id": "atu77315745"}, "Company")
		provider.list_scus = lambda: [
			{
				"_id": "other-company-scu",
				"state": "INITIALIZED",
				"legal_entity_id": {"vat_id": "ATU00000000"},
			},
			{
				"_id": "matching-company-scu",
				"state": "INITIALIZED",
				"legal_entity_id": {"vat_id": "ATU77315745"},
			},
			{
				"_id": "matching-company-scu-in-outage",
				"state": "OUTAGE",
				"legal_entity_id": {"vat_id": "ATU77315745"},
			},
		]

		selected = provider.list_selectable_scus(SimpleNamespace())

		self.assertEqual([scu["_id"] for scu in selected], ["matching-company-scu"])

	def test_v1_existing_scu_assignment_rejects_different_legal_entity(self):
		provider = object.__new__(SignAtV1Provider)
		provider._legal_entity = lambda _register: ({"vat_id": "ATU77315745"}, "Company")
		provider.retrieve_scu = lambda _register: {
			"_id": "other-company-scu",
			"state": "INITIALIZED",
			"legal_entity_id": {"vat_id": "ATU00000000"},
		}
		register = SimpleNamespace(
			scu_assignment_mode="EXISTING",
			signature_creation_unit_id="other-company-scu",
		)

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider._retrieve_or_create_scu(register, register.signature_creation_unit_id)

		self.assertEqual(failure.exception.code, "E_SCU_LEGAL_ENTITY_MISMATCH")

	def test_v1_new_scu_assignment_creates_deterministic_id_without_auto_selection(self):
		provider = object.__new__(SignAtV1Provider)
		provider._legal_entity = lambda _register: ({"vat_id": "ATU77315745"}, "Company")
		provider.retrieve_scu = lambda _register: (_ for _ in ()).throw(
			PermanentFiskalyError("not found", status_code=404)
		)
		provider.list_scus = lambda: self.fail("NEW must not search for another existing SCU")
		provider.create_scu = lambda _register, scu_id: {
			"_id": scu_id,
			"state": "CREATED",
			"legal_entity_id": {"vat_id": "ATU77315745"},
		}
		register = SimpleNamespace(
			scu_assignment_mode="NEW",
			signature_creation_unit_id="deterministic-scu-id",
		)

		scu, reused = provider._retrieve_or_create_scu(register, register.signature_creation_unit_id)

		self.assertEqual(scu["_id"], "deterministic-scu-id")
		self.assertFalse(reused)

	def test_v1_provisioning_rejects_cash_register_environment_mismatch(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="LIVE")
		provider.authenticate_fon = lambda: {"authentication_status": "AUTHENTICATED"}
		provider.ensure_automatic_closing_validation = lambda: {}
		provider._retrieve_or_create_scu = lambda _register, _scu_id: (
			{
				"_id": "scu-id",
				"_type": "SIGNATURE_CREATION_UNIT",
				"_env": "LIVE",
				"state": "INITIALIZED",
				"legal_entity_id": {"tax_id": "123"},
			},
			False,
		)
		provider._retrieve_or_create_register = lambda _register, _register_id: {
			"_id": "register-id",
			"_type": "CASH_REGISTER",
			"_env": "TEST",
			"state": "INITIALIZED",
			"serial_number": "REG-1",
			"initialization_receipt_id": "initialization-id",
		}
		register = SimpleNamespace(
			signature_creation_unit_id="scu-id",
			provider_register_id="register-id",
		)

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider.provision_register(register)

		self.assertEqual(failure.exception.code, "E_PROVIDER_ENVIRONMENT_MISMATCH")

	def test_v1_provisioning_configuration_rejects_environment_mismatch(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="LIVE")
		provider.http = RecordingHttp(
			[
				{
					"_env": "TEST",
					"_version": "1.2.6",
					"monthly_receipt_validation_enabled": True,
					"yearly_receipt_validation_enabled": True,
				}
			]
		)
		provider._headers = lambda: {}

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider.ensure_automatic_closing_validation()

		self.assertEqual(failure.exception.code, "E_PROVIDER_ENVIRONMENT_MISMATCH")

	def test_v1_automatic_special_receipt_rejects_environment_mismatch(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="LIVE")
		receipt = complete_v1_sign_response(environment="TEST")
		receipt.update(
			{
				"_id": "yearly-receipt-id",
				"receipt_type": "YEARLY_CLOSE",
				"fon_validations": [],
			}
		)
		provider.http = RecordingHttp(
			[
				{
					"data": [receipt],
					"count": 1,
					"_type": "RECEIPT_LIST",
					"_env": "LIVE",
					"_version": "1.2.6",
				}
			]
		)
		provider._headers = lambda: {}

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider.list_automatic_receipts(
				SimpleNamespace(provider_register_id="register-id"), ["YEARLY_CLOSE"]
			)

		self.assertEqual(failure.exception.code, "E_PROVIDER_ENVIRONMENT_MISMATCH")

	def test_request_round_trip_is_lossless(self):
		request = sample_request()
		self.assertEqual(request_from_dict(request.as_dict()).as_dict(), request.as_dict())

	def test_v1_uses_official_raw_gross_field_names(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		provider.http = RecordingHttp([complete_v1_sign_response()])
		provider._headers = lambda: {}

		result = provider.sign_receipt(SimpleNamespace(provider_register_id="register-id"), sample_request())

		payload = provider.http.calls[0][2]["json_data"]
		self.assertEqual(payload["schema"]["raw"]["gross_amount_standard"], "12.00")
		self.assertEqual(payload["schema"]["raw"]["gross_amount_reduced_1"], "0.00")
		self.assertNotIn("currency", payload["schema"]["raw"])
		self.assertTrue(result.signed)

	def test_v1_sign_response_does_not_require_get_only_fon_validations(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		provider.http = RecordingHttp([complete_v1_sign_response()])
		provider._headers = lambda: {}

		result = provider.sign_receipt(SimpleNamespace(provider_register_id="register-id"), sample_request())

		self.assertTrue(result.signed)

	def test_v1_complete_substitute_signature_is_accepted(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		response = complete_v1_sign_response(signed=False)
		response["hints"] = ["Sicherheitseinrichtung ausgefallen"]
		provider.http = RecordingHttp([response])
		provider._headers = lambda: {}

		result = provider.sign_receipt(SimpleNamespace(provider_register_id="register-id"), sample_request())

		self.assertFalse(result.signed)
		self.assertEqual(result.hints, ("Sicherheitseinrichtung ausgefallen",))
		self.assertTrue(result.qr_code_data.startswith("_R1-AT3_"))

	def test_v1_incomplete_success_response_is_rejected(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		response = complete_v1_sign_response()
		del response["qr_code_data"]
		provider.http = RecordingHttp([response])
		provider._headers = lambda: {}

		with self.assertRaises(PermanentFiskalyError):
			provider.sign_receipt(SimpleNamespace(provider_register_id="register-id"), sample_request())

	def test_v1_response_with_different_signed_amount_is_rejected(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		response = complete_v1_sign_response()
		response["schema"]["raw"]["gross_amount_standard"] = "99.00"
		provider.http = RecordingHttp([response])
		provider._headers = lambda: {}

		with self.assertRaises(PermanentFiskalyError):
			provider.sign_receipt(SimpleNamespace(provider_register_id="register-id"), sample_request())

	def test_v1_retrieved_receipt_requires_fon_validations(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		provider.http = RecordingHttp([complete_v1_sign_response()])
		provider._headers = lambda: {}

		with self.assertRaises(PermanentFiskalyError):
			provider.retrieve_receipt("register-id", sample_request().receipt_uuid)

	def test_v1_wire_payload_is_deterministic_and_reusable_offline(self):
		provider = object.__new__(SignAtV1Provider)
		first = provider.build_receipt_payload(sample_request())
		second = provider.build_receipt_payload(sample_request())

		self.assertEqual(first, second)
		self.assertEqual(first["schema"]["raw"]["gross_amount_standard"], "12.00")
		self.assertEqual(first["receipt_type"], "NORMAL")

	def test_v1_combines_19_and_4_9_percent_in_special_bucket(self):
		provider = object.__new__(SignAtV1Provider)
		request = replace(
			sample_request(),
			vat_buckets=(
				VatBucket("special", Decimal("19"), Decimal("10"), Decimal("1.90"), Decimal("11.90")),
				VatBucket("special", Decimal("4.9"), Decimal("20"), Decimal("0.98"), Decimal("20.98")),
			),
		)

		payload = provider.build_receipt_payload(request)

		self.assertEqual(payload["schema"]["raw"]["gross_amount_special"], "32.88")

	def test_v1_replay_sends_persisted_wire_payload_unchanged(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		provider.http = RecordingHttp([complete_v1_sign_response()])
		provider._headers = lambda: {}
		payload = provider.build_receipt_payload(sample_request())

		provider.sign_receipt_payload(
			SimpleNamespace(provider_register_id="register-id"), sample_request(), payload
		)

		self.assertIs(provider.http.calls[0][2]["json_data"], payload)

	def test_v1_complete_dep7_export_uses_official_endpoint_without_filters(self):
		provider = object.__new__(SignAtV1Provider)
		provider.connection = SimpleNamespace(environment="TEST")
		provider.http = RecordingHttp([{"Belege-Gruppe": []}])
		provider._headers = lambda: {}

		result = provider.export_dep7(SimpleNamespace(provider_register_id="register-id"))

		self.assertEqual(result, {"Belege-Gruppe": []})
		self.assertEqual(provider.http.calls[0][1], "/cash-register/register-id/export")

	def test_unified_creates_intention_then_transaction(self):
		request = sample_request()
		request = FiscalReceiptRequest(
			**{
				**request.__dict__,
				"vat_buckets": (
					VatBucket("STANDARD", Decimal("20"), Decimal("10"), Decimal("2"), Decimal("12")),
				),
			}
		)
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(environment="TEST")
		provider.http = RecordingHttp(
			[
				{"content": {"id": "intention-id"}},
				{
					"content": {
						"id": "record-id",
						"state": "COMPLETED",
						"mode": "FINISHED",
						"journal": {"signature": "signature", "signed_at": "2026-08-18T10:30:00Z"},
						"compliance": {"qr_code": "qr", "sequence": {"number": 8}},
					}
				},
			]
		)
		provider._ensure_available = lambda: None
		provider._headers = lambda key=None: {"X-Idempotency-Key": key} if key else {}

		result = provider.sign_receipt(
			SimpleNamespace(provider_register_id="system-id", serial_number="REG-1"), request
		)

		self.assertEqual([call[1] for call in provider.http.calls], ["/records", "/records"])
		intention = provider.http.calls[0][2]["json_data"]["content"]
		transaction = provider.http.calls[1][2]["json_data"]["content"]
		self.assertEqual(intention["type"], "INTENTION")
		self.assertEqual(transaction["record"]["id"], "intention-id")
		self.assertEqual(transaction["operation"]["breakdown"][0]["code"], "STANDARD")
		self.assertTrue(result.signed)

	@patch("erpnext_fiskaly_sign_at.providers.sign_at_unified.frappe.cache")
	def test_unified_token_creation_sends_idempotency_key(self, cache):
		cache.get_value.return_value = None
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			name="TEST-UNIFIED",
			environment="TEST",
			api_key="api-key",
			get_password=lambda _fieldname: "api-secret",
		)
		provider.http = RecordingHttp(
			[
				{
					"content": {
						"authentication": {"bearer": "token"},
						"organization": {"id": "organization-1"},
					}
				}
			]
		)

		token = provider._token()

		self.assertEqual(token, "token")
		headers = provider.http.calls[0][2]["headers"]
		self.assertEqual(len(headers["X-Idempotency-Key"]), 36)
		self.assertEqual(provider.http.calls[0][1], "/tokens")
		cache.set_value.assert_called_once_with(
			"fiskaly:unified:token:TEST-UNIFIED",
			{"bearer": "token", "organization": "organization-1"},
			expires_in_sec=540,
			shared=True,
		)

	@patch("erpnext_fiskaly_sign_at.providers.sign_at_unified.frappe.get_doc")
	def test_unified_taxpayer_provisioning_rejects_missing_fon_pin_cleanly(self, get_doc):
		get_doc.return_value = SimpleNamespace()
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			fon_participant_id="TESTAT01",
			fon_user_id="TESTUSER",
			get_password=lambda _fieldname, raise_exception=True: None,
		)
		provider.http = RecordingHttp([])
		provider._ensure_available = lambda: None
		provider._address = lambda _address: {}
		register = SimpleNamespace(
			location_address="ADDRESS-1",
			company="COMPANY-1",
		)

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider.provision_register(register)

		self.assertEqual(failure.exception.code, "E_FON_CREDENTIALS_MISSING")
		self.assertIn("pin", str(failure.exception))
		self.assertEqual(provider.http.calls, [])

	def test_unified_taxpayer_uses_only_vat_id_when_both_identifiers_exist(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			vat_id_number="U77315745",
			tax_id_number="54-376/2173",
		)

		self.assertEqual(provider._austrian_tax_identifier(), {"vat_id_number": "U77315745"})

	def test_unified_taxpayer_falls_back_to_tax_id_without_vat_id(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			vat_id_number=None,
			tax_id_number="54-376/2173",
		)

		self.assertEqual(provider._austrian_tax_identifier(), {"tax_id_number": "54-376/2173"})

	def test_unified_scope_listing_returns_only_api_key_organization(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(environment="TEST", scope_identifier=None)
		provider._ensure_available = lambda: None

		def token():
			provider._token_organization_id = "organization-1"
			return "token"

		provider._token = token

		options = provider.list_scopes()

		self.assertEqual(
			options,
			[
				{
					"value": "organization-1",
					"label": "API-Key-Organisation · organization-1",
					"description": "Ausschließlich die Organisation dieses Unified API-Keys",
				}
			],
		)

	def test_unified_headers_reject_scope_from_another_organization(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			environment="TEST", scope_identifier="foreign-organization"
		)
		provider._ensure_available = lambda: None

		def token():
			provider._token_organization_id = "organization-1"
			return "token"

		provider._token = token

		with self.assertRaises(PermanentFiskalyError) as failure:
			provider._headers()

		self.assertEqual(failure.exception.code, "E_UNIFIED_SCOPE_MISMATCH")

	def test_unified_headers_use_only_api_key_organization(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(environment="TEST", scope_identifier=None)

		def token():
			provider._token_organization_id = "organization-1"
			return "token"

		provider._token = token

		headers = provider._headers()

		self.assertEqual(headers["X-Scope-Identifier"], "organization-1")

	def test_unified_headers_convert_text_idempotency_keys_to_stable_uuids(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			name="TEST-UNIFIED", environment="TEST", scope_identifier=None
		)

		def token():
			provider._token_organization_id = "organization-1"
			return "token"

		provider._token = token

		first = provider._headers("Test Kassa 1 Unified:taxpayer")["X-Idempotency-Key"]
		second = provider._headers("Test Kassa 1 Unified:taxpayer")["X-Idempotency-Key"]
		other = provider._headers("Test Kassa 1 Unified:location")["X-Idempotency-Key"]

		self.assertEqual(uuid.UUID(first).version, 3)
		self.assertEqual(first, second)
		self.assertNotEqual(first, other)

	def test_unified_headers_preserve_existing_uuid_idempotency_keys(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			name="TEST-UNIFIED", environment="TEST", scope_identifier=None
		)

		def token():
			provider._token_organization_id = "organization-1"
			return "token"

		provider._token = token
		receipt_uuid = "9E36759B-C64B-4EE0-8F23-FBEAE9919A71"

		headers = provider._headers(receipt_uuid)

		self.assertEqual(headers["X-Idempotency-Key"], receipt_uuid.lower())

	def test_unified_headers_replace_uuid_versions_rejected_by_api(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(
			name="TEST-UNIFIED", environment="TEST", scope_identifier=None
		)

		def token():
			provider._token_organization_id = "organization-1"
			return "token"

		provider._token = token
		unsupported_uuid = "605aee08-b687-5e15-92ae-a915c4ed8059"

		headers = provider._headers(unsupported_uuid)

		self.assertEqual(uuid.UUID(headers["X-Idempotency-Key"]).version, 3)

	def test_unified_provisioning_idempotency_changes_only_with_payload(self):
		provider = object.__new__(SignAtUnifiedProvider)
		first = {"content": {"name": "Company", "address": {"city": "Wien"}}}
		same_different_order = {"content": {"address": {"city": "Wien"}, "name": "Company"}}
		changed = {"content": {"name": "Company", "address": {"city": "Graz"}}}

		first_key = provider._payload_idempotency_key("REGISTER-1:taxpayer", first)

		self.assertEqual(
			first_key,
			provider._payload_idempotency_key("REGISTER-1:taxpayer", same_different_order),
		)
		self.assertNotEqual(
			first_key,
			provider._payload_idempotency_key("REGISTER-1:taxpayer", changed),
		)

	def test_unified_live_is_hard_disabled(self):
		provider = object.__new__(SignAtUnifiedProvider)
		provider.connection = SimpleNamespace(environment="LIVE")

		with self.assertRaises(ProviderNotAvailableError):
			provider._ensure_available()
