from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone

from erpnext_fiskaly_sign_at.contracts import (
	FiscalReceiptRequest,
	FiscalReceiptResult,
	ReceiptType,
	decimal_string,
)
from erpnext_fiskaly_sign_at.providers.base import ProviderCapabilities, SignAtProvider
from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError, RetryableFiskalyError
from erpnext_fiskaly_sign_at.providers.http import FiskalyHttpClient

AUTOMATIC_RECEIPT_TYPES = {
	"INITIALIZATION",
	"DECOMMISSION",
	"MONTHLY_CLOSE",
	"YEARLY_CLOSE",
	"SIGNATURE_CREATION_UNIT_FAULT_CLEARANCE",
}
FON_REQUIRED_RECEIPT_TYPES = {"INITIALIZATION", "YEARLY_CLOSE", "DECOMMISSION"}
REQUIRED_RECEIPT_FIELDS = {
	"_id",
	"_type",
	"_env",
	"_version",
	"receipt_type",
	"receipt_number",
	"time_signature",
	"cash_register_serial_number",
	"cash_register_id",
	"signature_creation_unit_id",
	"qr_code_data",
	"signed",
	"schema",
}
CASH_REGISTER_STATES = {"REGISTERED", "INITIALIZED", "DECOMMISSIONED", "DEFECTIVE", "OUTAGE"}
SCU_TARGET_STATES = {"INITIALIZED", "DECOMMISSIONED"}
SCU_SELECTABLE_STATES = {"PENDING", "CREATED", "INITIALIZED"}
SCU_LIST_PAGE_SIZE = 100


class SignAtV1Provider(SignAtProvider):
	"""Official SIGN AT v1 adapter.

	The API creates initialization, closing, decommissioning and SCU fault-clearance
	receipts itself. Client code must retrieve those receipts instead of submitting
	ordinary zero-value receipts under a special local label.
	"""

	code = "SIGN_AT_V1"
	api_version = "v1"
	base_url = "https://rksv.fiskaly.com/api/v1"
	capabilities = ProviderCapabilities(
		live=True,
		register_lifecycle=True,
		scu_lifecycle=True,
		automatic_closing_receipts=True,
		fon_receipt_validation=True,
	)

	def __init__(self, connection):
		super().__init__(connection)
		self.http = FiskalyHttpClient(connection, connection.base_url_override or self.base_url)

	def _token(self) -> str:
		cache_key = f"fiskaly:v1:token:{self.connection.name}"
		cache_environment_key = f"{cache_key}:environment"
		token = frappe.cache.get_value(cache_key, shared=True)
		cached_environment = frappe.cache.get_value(cache_environment_key, shared=True)
		if token and cached_environment == self.connection.environment:
			return token
		if token or cached_environment:
			# Tokens cached by an older adapter version have no verified environment
			# binding. Never reuse those for fiscal API calls.
			frappe.cache.delete_value([cache_key, cache_environment_key], shared=True)
		body = self.http.request(
			"POST",
			"/auth",
			json_data={
				"api_key": self.connection.api_key,
				"api_secret": self.connection.get_password("api_secret"),
			},
		)
		if (
			not isinstance(body, dict)
			or not isinstance(body.get("access_token"), str)
			or not body["access_token"].strip()
		):
			raise PermanentFiskalyError(
				"fiskaly returned an incomplete authentication response",
				code="E_INCOMPLETE_PROVIDER_RESPONSE",
			)
		claims = body.get("access_token_claims")
		authentication_evidence = {
			"access_token_claims": {"env": claims.get("env")} if isinstance(claims, dict) else None
		}
		if not isinstance(claims, dict) or not claims.get("env"):
			raise PermanentFiskalyError(
				"fiskaly authentication did not identify the token environment",
				code="E_PROVIDER_ENVIRONMENT_UNVERIFIED",
				response=authentication_evidence,
			)
		self._validate_provider_environment(claims["env"], "authentication", authentication_evidence)
		token = body["access_token"]
		frappe.cache.set_value(
			cache_key,
			token,
			expires_in_sec=max(
				int(body.get("access_token_expires_in", body.get("expires_in", 600))) - 30, 30
			),
			shared=True,
		)
		frappe.cache.set_value(
			cache_environment_key,
			self.connection.environment,
			expires_in_sec=max(
				int(body.get("access_token_expires_in", body.get("expires_in", 600))) - 30, 30
			),
			shared=True,
		)
		return token

	def _headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self._token()}"}

	@staticmethod
	def _require_fields(body: Any, fields: set[str], resource: str):
		if not isinstance(body, dict):
			raise PermanentFiskalyError(
				f"fiskaly returned a non-object {resource} response",
				code="E_INCOMPLETE_PROVIDER_RESPONSE",
				response=body,
			)
		missing = [
			field
			for field in sorted(fields)
			if field not in body
			or body[field] is None
			or (isinstance(body[field], str) and not body[field].strip())
		]
		if missing:
			raise PermanentFiskalyError(
				f"fiskaly returned an incomplete {resource} response; missing: {', '.join(missing)}",
				code="E_INCOMPLETE_PROVIDER_RESPONSE",
				response=body,
			)

	def _validate_provider_environment(
		self, provider_environment: Any, resource: str, response: Any = None
	) -> str:
		if provider_environment not in {"TEST", "LIVE"}:
			raise PermanentFiskalyError(
				f"fiskaly returned no valid environment for {resource}",
				code="E_PROVIDER_ENVIRONMENT_UNVERIFIED",
				response=response,
			)
		if provider_environment != self.connection.environment:
			raise PermanentFiskalyError(
				f"fiskaly returned {resource} from {provider_environment}, expected "
				f"{self.connection.environment}",
				code="E_PROVIDER_ENVIRONMENT_MISMATCH",
				response=response,
			)
		return provider_environment

	def _validate_receipt_response(
		self,
		body: Any,
		*,
		expected_register_id: str | None = None,
		expected_receipt_type: str | None = None,
		require_fon_validations: bool = True,
	) -> dict[str, Any]:
		required_fields = REQUIRED_RECEIPT_FIELDS | (
			{"fon_validations"} if require_fon_validations else set()
		)
		self._require_fields(body, required_fields, "receipt")
		self._validate_provider_environment(body["_env"], "receipt", body)
		if body["_type"] != "RECEIPT" or not isinstance(body["signed"], bool):
			raise PermanentFiskalyError(
				"fiskaly returned an invalid receipt discriminator or signed flag",
				code="E_INVALID_PROVIDER_RESPONSE",
				response=body,
			)
		if not isinstance(body["schema"], dict) or "raw" not in body["schema"]:
			raise PermanentFiskalyError(
				"fiskaly receipt response does not contain the mandatory signed raw schema",
				code="E_INVALID_PROVIDER_RESPONSE",
				response=body,
			)
		if require_fon_validations and not isinstance(body["fon_validations"], list):
			raise PermanentFiskalyError(
				"fiskaly receipt response has an invalid fon_validations value",
				code="E_INVALID_PROVIDER_RESPONSE",
				response=body,
			)
		if expected_register_id and body["cash_register_id"] != expected_register_id:
			raise PermanentFiskalyError(
				"fiskaly receipt belongs to a different cash register",
				code="E_PROVIDER_RESOURCE_MISMATCH",
				response=body,
			)
		if expected_receipt_type and body["receipt_type"] != expected_receipt_type:
			raise PermanentFiskalyError(
				f"Expected a {expected_receipt_type} receipt, got {body['receipt_type']}",
				code="E_PROVIDER_RESOURCE_MISMATCH",
				response=body,
			)
		return body

	@staticmethod
	def _validate_fon_result(result: Any) -> dict[str, Any]:
		if (
			not isinstance(result, dict)
			or not result.get("validation_result")
			or not result.get("time_validation")
		):
			raise PermanentFiskalyError(
				"FinanzOnline returned an incomplete receipt validation",
				code="E_INCOMPLETE_FON_VALIDATION",
				response=result,
			)
		if result["validation_result"] != "SUCCESS":
			raise PermanentFiskalyError(
				f"FinanzOnline receipt validation failed: {result['validation_result']}",
				code="E_FON_VALIDATION_FAILED",
				response=result,
			)
		return result

	def test_connection(self) -> dict[str, Any]:
		self._token()
		return {
			"authenticated": True,
			"provider": self.code,
			"environment": self.connection.environment,
			"environment_verified": True,
		}

	def authenticate_fon(self) -> dict[str, Any]:
		body = self.http.request(
			"PUT",
			"/fon/auth",
			json_data={
				"fon_participant_id": self.connection.fon_participant_id,
				"fon_user_id": self.connection.fon_user_id,
				"fon_user_pin": self.connection.get_password("fon_user_pin"),
			},
			headers=self._headers(),
		)
		self._require_fields(
			body,
			{"fon_participant_id", "fon_user_id", "authentication_status", "time_authentication"},
			"FinanzOnline authentication",
		)
		if body["authentication_status"] != "AUTHENTICATED":
			raise PermanentFiskalyError(
				"FinanzOnline authentication was not confirmed",
				code="E_FON_NOT_AUTHENTICATED",
				response=body,
			)
		return body

	def _legal_entity(self, register) -> tuple[dict[str, str], str]:
		if self.connection.vat_id_number:
			legal_entity_id = {"vat_id": self.connection.vat_id_number.strip().upper()}
		elif self.connection.tax_id_number:
			legal_entity_id = {"tax_id": self.connection.tax_id_number.strip()}
		else:
			raise PermanentFiskalyError(
				"Austrian VAT ID or tax ID is required to create a SIGN AT v1 SCU",
				code="E_LEGAL_ENTITY_ID_REQUIRED",
			)
		company_name = frappe.db.get_value("Company", register.company, "company_name") or register.company
		return legal_entity_id, company_name

	def create_scu(self, register, scu_id: str | None = None) -> dict[str, Any]:
		scu_id = scu_id or str(uuid.uuid4())
		legal_entity_id, legal_entity_name = self._legal_entity(register)
		return self.http.request(
			"PUT",
			f"/signature-creation-unit/{scu_id}",
			json_data={"legal_entity_id": legal_entity_id, "legal_entity_name": legal_entity_name},
			headers=self._headers(),
		)

	def list_scus(self) -> list[dict[str, Any]]:
		"""Return all SCUs owned by this connection's organization."""
		resources: list[dict[str, Any]] = []
		offset = 0
		while True:
			body = self.http.request(
				"GET",
				f"/signature-creation-unit?{urlencode({'limit': SCU_LIST_PAGE_SIZE, 'offset': offset})}",
				headers=self._headers(),
			)
			self._require_fields(body, {"data", "count", "_type", "_env"}, "SCU list")
			self._validate_provider_environment(body["_env"], "SCU list", body)
			if body["_type"] != "SIGNATURE_CREATION_UNIT_LIST" or not isinstance(body["data"], list):
				raise PermanentFiskalyError(
					"fiskaly returned an invalid SCU list",
					code="E_INCOMPLETE_PROVIDER_RESPONSE",
					response=body,
				)
			try:
				total = int(body["count"])
			except TypeError, ValueError:
				raise PermanentFiskalyError(
					"fiskaly returned an invalid SCU count",
					code="E_INCOMPLETE_PROVIDER_RESPONSE",
					response=body,
				) from None
			page = body["data"]
			for scu in page:
				self._require_fields(
					scu,
					{"_id", "_type", "_env", "state", "legal_entity_id"},
					"SCU list entry",
				)
				self._validate_provider_environment(scu["_env"], "SCU list entry", scu)
				if scu["_type"] != "SIGNATURE_CREATION_UNIT":
					raise PermanentFiskalyError(
						"fiskaly returned a non-SCU resource in the SCU list",
						code="E_PROVIDER_RESOURCE_MISMATCH",
						response=scu,
					)
			resources.extend(page)
			if len(resources) >= total:
				return resources
			if not page:
				raise PermanentFiskalyError(
					"fiskaly returned an incomplete paginated SCU list",
					code="E_INCOMPLETE_PROVIDER_RESPONSE",
					response=body,
				)
			offset += len(page)

	@staticmethod
	def _normalized_legal_entity_id(value: Any) -> dict[str, str]:
		if not isinstance(value, dict):
			return {}
		if value.get("vat_id"):
			return {"vat_id": str(value["vat_id"]).strip().upper()}
		if value.get("tax_id"):
			return {"tax_id": str(value["tax_id"]).strip()}
		return {}

	def list_selectable_scus(self, register) -> list[dict[str, Any]]:
		"""Return SCUs that may be explicitly assigned to this register."""
		expected_legal_entity_id, _legal_entity_name = self._legal_entity(register)
		expected = self._normalized_legal_entity_id(expected_legal_entity_id)
		return [
			scu
			for scu in self.list_scus()
			if scu["state"] in SCU_SELECTABLE_STATES
			and self._normalized_legal_entity_id(scu["legal_entity_id"]) == expected
		]

	def _validate_scu_assignment(self, register, scu: dict[str, Any]) -> dict[str, Any]:
		expected_legal_entity_id, _legal_entity_name = self._legal_entity(register)
		if self._normalized_legal_entity_id(scu.get("legal_entity_id")) != self._normalized_legal_entity_id(
			expected_legal_entity_id
		):
			raise PermanentFiskalyError(
				"The selected SIGN AT SCU belongs to a different legal entity",
				code="E_SCU_LEGAL_ENTITY_MISMATCH",
				response=scu,
			)
		if scu.get("state") not in SCU_SELECTABLE_STATES:
			raise PermanentFiskalyError(
				f"The selected SIGN AT SCU cannot be used in state {scu.get('state')}",
				code="E_SCU_NOT_SELECTABLE",
				response=scu,
			)
		return scu

	def retrieve_scu(self, register) -> dict[str, Any]:
		body = self.http.request(
			"GET",
			f"/signature-creation-unit/{register.signature_creation_unit_id}",
			headers=self._headers(),
		)
		self._require_fields(body, {"_id", "_type", "_env", "state", "legal_entity_id"}, "SCU")
		self._validate_provider_environment(body["_env"], "SCU", body)
		if body["_id"] != register.signature_creation_unit_id or body["_type"] != "SIGNATURE_CREATION_UNIT":
			raise PermanentFiskalyError(
				"fiskaly returned a different SCU",
				code="E_PROVIDER_RESOURCE_MISMATCH",
				response=body,
			)
		return body

	def update_scu_state(self, scu_id: str, state: str) -> dict[str, Any]:
		if state not in SCU_TARGET_STATES:
			raise PermanentFiskalyError(f"Unsupported SIGN AT v1 SCU target state: {state}")
		return self.http.request(
			"PATCH", f"/signature-creation-unit/{scu_id}", json_data={"state": state}, headers=self._headers()
		)

	def transition_scu_state(self, register, state: str) -> dict[str, Any]:
		body = self.update_scu_state(register.signature_creation_unit_id, state.upper())
		self._require_fields(body, {"_id", "_type", "_env", "state", "legal_entity_id"}, "SCU")
		self._validate_provider_environment(body["_env"], "SCU", body)
		if body["_id"] != register.signature_creation_unit_id or body["state"] != state.upper():
			raise PermanentFiskalyError(
				f"SCU did not reach requested state {state.upper()}",
				code="E_PROVIDER_STATE_MISMATCH",
				response=body,
			)
		return body

	def create_register(self, register_id: str, description: str | None = None) -> dict[str, Any]:
		return self.http.request(
			"PUT",
			f"/cash-register/{register_id}",
			json_data={"description": description} if description else {},
			headers=self._headers(),
		)

	def retrieve_register(self, register) -> dict[str, Any]:
		body = self.http.request(
			"GET", f"/cash-register/{register.provider_register_id}", headers=self._headers()
		)
		self._require_fields(body, {"_id", "_type", "_env", "state", "serial_number"}, "cash register")
		self._validate_provider_environment(body["_env"], "cash register", body)
		if body["_id"] != register.provider_register_id or body["_type"] != "CASH_REGISTER":
			raise PermanentFiskalyError(
				"fiskaly returned a different cash register",
				code="E_PROVIDER_RESOURCE_MISMATCH",
				response=body,
			)
		return body

	def update_register_state(self, register_id: str, state: str) -> dict[str, Any]:
		state = state.upper()
		if state not in CASH_REGISTER_STATES:
			raise PermanentFiskalyError(f"Unsupported SIGN AT v1 cash-register target state: {state}")
		return self.http.request(
			"PATCH", f"/cash-register/{register_id}", json_data={"state": state}, headers=self._headers()
		)

	def transition_register_state(self, register, state: str) -> dict[str, Any]:
		state = state.upper()
		body = self.update_register_state(register.provider_register_id, state)
		self._require_fields(body, {"_id", "_type", "_env", "state", "serial_number"}, "cash register")
		self._validate_provider_environment(body["_env"], "cash register", body)
		if body["_id"] != register.provider_register_id or body["state"] != state:
			raise PermanentFiskalyError(
				f"Cash register did not reach requested state {state}",
				code="E_PROVIDER_STATE_MISMATCH",
				response=body,
			)
		return body

	def retrieve_receipt(self, register_id: str, receipt_id: str) -> dict[str, Any]:
		body = self.http.request(
			"GET", f"/cash-register/{register_id}/receipt/{receipt_id}", headers=self._headers()
		)
		return self._validate_receipt_response(body, expected_register_id=register_id)

	def validate_receipt_with_fon(self, register, receipt_id: str) -> dict[str, Any]:
		result = self.http.request(
			"POST",
			f"/cash-register/{register.provider_register_id}/receipt/{receipt_id}/validation",
			headers=self._headers(),
		)
		return self._validate_fon_result(result)

	def retrieve_configuration(self) -> dict[str, Any]:
		configuration = self.http.request("GET", "/configuration", headers=self._headers())
		if not isinstance(configuration, dict):
			raise PermanentFiskalyError(
				"fiskaly returned an invalid configuration response",
				code="E_INCOMPLETE_PROVIDER_RESPONSE",
				response=configuration,
			)
		if "_env" in configuration:
			self._validate_provider_environment(configuration["_env"], "configuration", configuration)
		return configuration

	def ensure_automatic_closing_validation(self) -> dict[str, Any]:
		configuration = self.retrieve_configuration()
		if configuration.get("monthly_receipt_validation_enabled") and configuration.get(
			"yearly_receipt_validation_enabled"
		):
			return configuration
		configuration = self.http.request(
			"PATCH",
			"/configuration",
			json_data={
				"monthly_receipt_validation_enabled": True,
				"yearly_receipt_validation_enabled": True,
			},
			headers=self._headers(),
		)
		if not isinstance(configuration, dict):
			raise PermanentFiskalyError(
				"fiskaly returned an invalid configuration response",
				code="E_INCOMPLETE_PROVIDER_RESPONSE",
				response=configuration,
			)
		if "_env" in configuration:
			self._validate_provider_environment(configuration["_env"], "configuration", configuration)
		return configuration

	def _retrieve_or_create_scu(self, register, scu_id: str) -> tuple[dict[str, Any], bool]:
		assignment_mode = getattr(register, "scu_assignment_mode", None)
		if assignment_mode == "EXISTING":
			if not scu_id:
				raise PermanentFiskalyError(
					"Select an existing SIGN AT SCU before provisioning",
					code="E_SCU_SELECTION_REQUIRED",
				)
			return self._validate_scu_assignment(register, self.retrieve_scu(register)), True
		if assignment_mode != "NEW":
			raise PermanentFiskalyError(
				"Choose whether an existing SIGN AT SCU is used or a new SCU is created",
				code="E_SCU_ASSIGNMENT_MODE_REQUIRED",
			)

		# A deterministic ID makes retries resume a remotely created SCU after a
		# later provisioning step failed. NEW deliberately never falls back to a
		# different organization-wide SCU.
		try:
			scu = self.retrieve_scu(register)
		except PermanentFiskalyError as exc:
			if exc.status_code != 404:
				raise
			scu = self.create_scu(register, scu_id)
		return self._validate_scu_assignment(register, scu), False

	def _retrieve_or_create_register(self, register, register_id: str) -> dict[str, Any]:
		try:
			return self.retrieve_register(register)
		except PermanentFiskalyError as exc:
			if exc.status_code != 404:
				raise
		return self.create_register(register_id, getattr(register, "register_name", None))

	def provision_register(self, register) -> dict[str, Any]:
		fon_auth = self.authenticate_fon()
		configuration = self.ensure_automatic_closing_validation()

		scu_id = register.signature_creation_unit_id or str(uuid.uuid4())
		scu, reused_scu = self._retrieve_or_create_scu(register, scu_id)
		self._require_fields(scu, {"_id", "_type", "_env", "state", "legal_entity_id"}, "SCU")
		self._validate_provider_environment(scu["_env"], "SCU", scu)
		if (not reused_scu and scu["_id"] != scu_id) or scu["_type"] != "SIGNATURE_CREATION_UNIT":
			raise PermanentFiskalyError(
				"fiskaly returned a different SCU during provisioning",
				code="E_PROVIDER_RESOURCE_MISMATCH",
				response=scu,
			)
		scu_id = scu["_id"]
		# Lifecycle helpers read the ID from the register document. Keep the
		# in-memory document aligned when an organization-wide SCU was selected.
		register.signature_creation_unit_id = scu_id
		if scu.get("state") == "PENDING":
			raise RetryableFiskalyError(
				"The SIGN AT signature creation unit is still PENDING; retry provisioning later",
				code="E_SCU_PENDING",
				response=scu,
			)
		if scu.get("state") == "CREATED":
			scu = self.transition_scu_state(register, "INITIALIZED")
		if scu.get("state") != "INITIALIZED":
			raise PermanentFiskalyError(
				f"SCU is in unexpected state {scu.get('state')}",
				code="E_PROVIDER_STATE_MISMATCH",
				response=scu,
			)

		register_id = register.provider_register_id or str(uuid.uuid4())
		cash_register = self._retrieve_or_create_register(register, register_id)
		self._require_fields(
			cash_register, {"_id", "_type", "_env", "state", "serial_number"}, "cash register"
		)
		self._validate_provider_environment(cash_register["_env"], "cash register", cash_register)
		if cash_register["_id"] != register_id or cash_register["_type"] != "CASH_REGISTER":
			raise PermanentFiskalyError(
				"fiskaly returned a different cash register during provisioning",
				code="E_PROVIDER_RESOURCE_MISMATCH",
				response=cash_register,
			)
		if cash_register.get("state") == "CREATED":
			cash_register = self.transition_register_state(register, "REGISTERED")
		if cash_register.get("state") == "REGISTERED":
			cash_register = self.transition_register_state(register, "INITIALIZED")
		if cash_register.get("state") != "INITIALIZED":
			raise PermanentFiskalyError(
				f"Cash register is in unexpected state {cash_register.get('state')}",
				code="E_PROVIDER_STATE_MISMATCH",
				response=cash_register,
			)

		initialization_receipt_id = cash_register.get("initialization_receipt_id")
		if not initialization_receipt_id:
			raise PermanentFiskalyError(
				"Initialized cash register response has no initialization_receipt_id",
				code="E_INITIALIZATION_RECEIPT_MISSING",
				response=cash_register,
			)
		start_receipt = self.retrieve_receipt(register_id, initialization_receipt_id)
		self._validate_receipt_response(
			start_receipt,
			expected_register_id=register_id,
			expected_receipt_type="INITIALIZATION",
		)
		if not start_receipt["signed"]:
			raise PermanentFiskalyError(
				"The initialization receipt was not signed",
				code="E_INITIALIZATION_RECEIPT_UNSIGNED",
				response=start_receipt,
			)
		fon_validation = self.validate_receipt_with_fon(register, initialization_receipt_id)
		start_receipt["fon_validations"] = [*start_receipt.get("fon_validations", []), fon_validation]

		return {
			"provider_register_id": register_id,
			"signature_creation_unit_id": scu_id,
			"serial_number": cash_register.get("serial_number"),
			"state": cash_register["state"],
			"mode": cash_register.get("mode"),
			"resources": [{"type": "SCU", "id": scu_id, "data": scu}],
			"start_receipt": start_receipt,
			"fon_authentication": fon_auth,
			"configuration": configuration,
			"raw": cash_register,
		}

	def build_receipt_payload(self, request: FiscalReceiptRequest) -> dict[str, Any]:
		"""Return the exact deterministic v1 wire payload used for signing and offline evidence."""
		field_names = {
			"standard": "gross_amount_standard",
			"reduced1": "gross_amount_reduced_1",
			"reduced2": "gross_amount_reduced_2",
			"special": "gross_amount_special",
			"zero": "gross_amount_zero",
		}
		amount_totals = {fieldname: Decimal("0") for fieldname in field_names.values()}
		for bucket in request.vat_buckets:
			try:
				fieldname = field_names[bucket.code]
			except KeyError as exc:
				raise PermanentFiskalyError(
					f"Unsupported SIGN AT v1 VAT bucket: {bucket.code}",
					code="E_UNSUPPORTED_VAT_BUCKET",
				) from exc
			amount_totals[fieldname] += bucket.gross
		amounts = {fieldname: decimal_string(value) for fieldname, value in amount_totals.items()}
		for required_bucket in field_names.values():
			amounts.setdefault(required_bucket, "0.00")
		payload = {
			"receipt_type": "CANCELLATION" if request.receipt_type == ReceiptType.CANCELLATION else "NORMAL",
			"schema": {"raw": amounts},
		}
		if request.receipt_type == ReceiptType.TRAINING:
			payload["receipt_type"] = "TRAINING"
		return payload

	def sign_receipt(self, register, request: FiscalReceiptRequest) -> FiscalReceiptResult:
		return self.sign_receipt_payload(register, request, self.build_receipt_payload(request))

	def sign_receipt_payload(
		self, register, request: FiscalReceiptRequest, provider_payload: dict[str, Any]
	) -> FiscalReceiptResult:
		"""Replay a previously persisted v1 wire payload without rebuilding or mutating it."""
		if not isinstance(provider_payload, dict) or provider_payload.get("receipt_type") not in {
			"NORMAL",
			"CANCELLATION",
			"TRAINING",
		}:
			raise PermanentFiskalyError(
				"Stored SIGN AT v1 provider payload is invalid",
				code="E_INVALID_STORED_PROVIDER_PAYLOAD",
				response=provider_payload,
			)
		body = self.http.request(
			"PUT",
			f"/cash-register/{register.provider_register_id}/receipt/{request.receipt_uuid}",
			json_data=provider_payload,
			headers=self._headers(),
			receipt=getattr(request, "receipt_uuid", None),
		)
		body = self._validate_receipt_response(
			body,
			expected_register_id=register.provider_register_id,
			expected_receipt_type=provider_payload["receipt_type"],
			require_fon_validations=False,
		)
		if body["_id"] != request.receipt_uuid:
			raise PermanentFiskalyError(
				"fiskaly returned a different receipt ID for the signing request",
				code="E_PROVIDER_RESOURCE_MISMATCH",
				response=body,
			)
		expected_raw = provider_payload.get("schema", {}).get("raw")
		actual_raw = body["schema"].get("raw")
		if not isinstance(expected_raw, dict) or not isinstance(actual_raw, dict):
			raise PermanentFiskalyError(
				"fiskaly returned an invalid raw receipt schema",
				code="E_INVALID_PROVIDER_RESPONSE",
				response=body,
			)
		mismatched_amounts = []
		for fieldname, expected in expected_raw.items():
			try:
				matches = Decimal(str(actual_raw[fieldname])) == Decimal(str(expected))
			except KeyError, ValueError, TypeError:
				matches = False
			if not matches:
				mismatched_amounts.append(fieldname)
		if mismatched_amounts:
			raise PermanentFiskalyError(
				f"fiskaly signed different gross amounts: {', '.join(mismatched_amounts)}",
				code="E_PROVIDER_AMOUNT_MISMATCH",
				response=body,
			)
		qr_code_data = body["qr_code_data"]
		signed_at = body["time_signature"]
		if isinstance(signed_at, int | float):
			signed_at = datetime.fromtimestamp(signed_at, tz=UTC).isoformat()
		return FiscalReceiptResult(
			provider_receipt_id=body["_id"],
			qr_code_data=qr_code_data,
			signature_value=body.get("signature_value")
			or (qr_code_data.rsplit("_", 1)[-1] if qr_code_data else ""),
			receipt_number=str(body["receipt_number"]),
			signed_at=signed_at,
			serial_number=body["cash_register_serial_number"],
			signed=body["signed"] is True,
			state=body.get("state"),
			hints=tuple(body.get("hints") or ()),
			raw=body,
		)

	def list_automatic_receipts(
		self, register, receipt_types: tuple[str, ...] | list[str]
	) -> list[dict[str, Any]]:
		types = tuple(dict.fromkeys(receipt_type.upper() for receipt_type in receipt_types))
		unsupported = set(types) - AUTOMATIC_RECEIPT_TYPES
		if unsupported:
			raise PermanentFiskalyError(
				f"Unsupported automatic SIGN AT receipt type(s): {', '.join(sorted(unsupported))}"
			)
		results: list[dict[str, Any]] = []
		offset = 0
		while True:
			params: list[tuple[str, str | int]] = [
				("order_by", "receipt_number"),
				("order", "DESC"),
				("limit", 100),
				("offset", offset),
			]
			params.extend(("receipt_types", receipt_type) for receipt_type in types)
			body = self.http.request(
				"GET",
				f"/cash-register/{register.provider_register_id}/receipt?{urlencode(params)}",
				headers=self._headers(),
			)
			self._require_fields(body, {"data", "count", "_type", "_env", "_version"}, "receipt list")
			self._validate_provider_environment(body["_env"], "receipt list", body)
			if body["_type"] != "RECEIPT_LIST":
				raise PermanentFiskalyError(
					"fiskaly returned an invalid receipt-list discriminator",
					code="E_INVALID_PROVIDER_RESPONSE",
					response=body,
				)
			if not isinstance(body["data"], list):
				raise PermanentFiskalyError("fiskaly returned an invalid automatic receipt list")
			for receipt in body["data"]:
				results.append(
					self._validate_receipt_response(
						receipt, expected_register_id=register.provider_register_id
					)
				)
			offset += len(body["data"])
			if not body["data"] or offset >= int(body["count"]):
				break
		return results

	@staticmethod
	def _signature_timestamp(value) -> int:
		dt = get_datetime(value)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
		return int(dt.timestamp())

	def export_dep7(self, register, date_from=None, date_to=None) -> dict[str, Any]:
		params = []
		if date_from:
			params.append(("start_time_signature", self._signature_timestamp(date_from)))
		if date_to:
			params.append(("end_time_signature", self._signature_timestamp(date_to)))
		path = f"/cash-register/{register.provider_register_id}/export"
		if params:
			path = f"{path}?{urlencode(params)}"
		body = self.http.request("GET", path, headers=self._headers())
		if not isinstance(body, dict) or not isinstance(body.get("Belege-Gruppe"), list):
			raise PermanentFiskalyError(
				"fiskaly did not return a valid DEP7 JSON document",
				code="E_INVALID_DEP7_EXPORT",
				response=body,
			)
		return body
