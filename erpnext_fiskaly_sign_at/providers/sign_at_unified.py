from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, ClassVar

import frappe

from erpnext_fiskaly_sign_at.contracts import FiscalReceiptRequest, FiscalReceiptResult, decimal_string
from erpnext_fiskaly_sign_at.providers.base import ProviderCapabilities, SignAtProvider
from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError, ProviderNotAvailableError
from erpnext_fiskaly_sign_at.providers.http import FiskalyHttpClient


class SignAtUnifiedProvider(SignAtProvider):
	"""Unified SIGN AT adapter for API 2026-06-01.

	The adapter is deliberately isolated because the Austrian Unified contract is
	still integration-ready. LIVE requires an explicit feature gate in settings.
	"""

	code = "SIGN_AT_UNIFIED"
	api_version = "2026-06-01"
	base_urls: ClassVar[dict[str, str]] = {
		"TEST": "https://test.api.fiskaly.com",
		"LIVE": "https://live.api.fiskaly.com",
	}
	capabilities = ProviderCapabilities(live=False)

	def __init__(self, connection):
		super().__init__(connection)
		base_url = connection.base_url_override or self.base_urls[connection.environment]
		self.http = FiskalyHttpClient(
			connection, base_url, {"X-Api-Version": connection.api_version or self.api_version}
		)

	def _ensure_available(self):
		if self.connection.environment == "LIVE":
			raise ProviderNotAvailableError(
				"Unified SIGN AT LIVE is not released for Austrian production use and is hard-disabled. "
				"The setting 'Allow Unified LIVE' cannot override this compliance guard."
			)

	def _token(self) -> str:
		self._ensure_available()
		cache_key = f"fiskaly:unified:token:{self.connection.name}"
		cached = frappe.cache.get_value(cache_key, shared=True)
		if isinstance(cached, dict) and cached.get("bearer") and cached.get("organization"):
			self._token_organization_id = cached.get("organization")
			return cached["bearer"]
		if cached:
			# Tokens cached before organization metadata was retained cannot provide
			# the API key's own selectable scope. Refresh them once after the upgrade.
			frappe.cache.delete_value(cache_key, shared=True)
		body = self.http.request(
			"POST",
			"/tokens",
			headers={"X-Idempotency-Key": str(uuid.uuid4())},
			json_data={
				"content": {
					"type": "API_KEY",
					"key": self.connection.api_key,
					"secret": self.connection.get_password("api_secret"),
				}
			},
		)
		content = body.get("content", {})
		authentication = content.get("authentication", {})
		token = authentication["bearer"]
		organization = content.get("organization", {})
		self._token_organization_id = organization.get("id")
		if not self._token_organization_id:
			raise PermanentFiskalyError(
				"fiskaly Unified authentication did not identify the API key organization",
				code="E_UNIFIED_ORGANIZATION_MISSING",
			)
		frappe.cache.set_value(
			cache_key,
			{"bearer": token, "organization": self._token_organization_id},
			expires_in_sec=540,
			shared=True,
		)
		return token

	def _headers(
		self,
		idempotency_key: str | None = None,
		*,
		include_scope: bool = True,
	) -> dict[str, str]:
		headers = {"Authorization": f"Bearer {self._token()}"}
		organization = self._token_organization_id
		configured_scope = (self.connection.scope_identifier or "").strip()
		if configured_scope and configured_scope != organization:
			raise PermanentFiskalyError(
				"The configured Unified scope does not belong to this API key. "
				"Use separate organization credentials for each ERPNext company.",
				code="E_UNIFIED_SCOPE_MISMATCH",
			)
		if include_scope:
			headers["X-Scope-Identifier"] = organization
		if idempotency_key:
			headers["X-Idempotency-Key"] = self._uuid_idempotency_key(idempotency_key)
		return headers

	def _uuid_idempotency_key(self, value: str) -> str:
		"""Return a stable UUIDv3/v4 accepted by the Unified API."""
		try:
			parsed = uuid.UUID(str(value))
		except (ValueError, AttributeError, TypeError):
			parsed = None
		if parsed and parsed.version in {3, 4}:
			return str(parsed)
		seed = f"erpnext_fiskaly_sign_at:{self.connection.name}:{value}"
		return str(uuid.uuid3(uuid.NAMESPACE_URL, seed))

	def _payload_idempotency_key(self, operation: str, payload: dict[str, Any]) -> str:
		"""Bind provisioning retries to their canonical payload."""
		canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
		digest = hashlib.sha256(canonical.encode()).hexdigest()
		return f"{operation}:{digest}"

	def list_scopes(self) -> list[dict[str, str]]:
		"""Return only the organization owned by this API key.

		Child or sibling organizations must never be exposed as selectable scopes.
		Each ERPNext company needs credentials created in its own fiskaly organization.
		"""
		self._ensure_available()
		self._token()
		organization = self._token_organization_id
		return [
			{
				"value": organization,
				"label": f"API-Key-Organisation · {organization}",
				"description": "Ausschließlich die Organisation dieses Unified API-Keys",
			}
		]

	def test_connection(self) -> dict[str, Any]:
		scopes = self.list_scopes()
		if self.connection.scope_identifier:
			self._headers()
		return {
			"authenticated": True,
			"scopes": scopes,
			"scope_count": len(scopes),
		}

	def _commission(self, resource: str, resource_id: str, key: str) -> dict[str, Any]:
		payload = {"content": {"state": "COMMISSIONED"}}
		return self.http.request(
			"PATCH",
			f"/{resource}/{resource_id}",
			json_data=payload,
			headers=self._headers(self._payload_idempotency_key(key, payload)),
		)

	def _address(self, address) -> dict[str, Any]:
		match = re.match(
			r"^(?P<street>.+?)\s+(?P<number>\d+[A-Za-z0-9\-/]*)$", (address.address_line1 or "").strip()
		)
		if not match:
			raise ProviderNotAvailableError(
				"The linked address must end in a street number (for example 'Hauptstraße 12')."
			)
		country = (frappe.db.get_value("Country", address.country, "code") or "").upper()
		return {
			"line": {
				"type": "STREET_NUMBER",
				"street": match.group("street"),
				"number": match.group("number"),
			},
			"code": address.pincode,
			"city": address.city,
			"country": country,
			**({"region": address.state} if address.state else {}),
		}

	def _austrian_tax_identifier(self) -> dict[str, str]:
		"""Select exactly one company identifier as required by Unified SIGN AT."""
		if self.connection.vat_id_number:
			return {"vat_id_number": self.connection.vat_id_number}
		if self.connection.tax_id_number:
			return {"tax_id_number": self.connection.tax_id_number}
		return {}

	def provision_register(self, register) -> dict[str, Any]:
		self._ensure_available()
		if not register.location_address:
			raise ProviderNotAvailableError(
				"A location address is required for Unified SIGN AT provisioning."
			)
		address = frappe.get_doc("Address", register.location_address)
		company = frappe.get_doc("Company", register.company)
		api_address = self._address(address)
		fon_pin = self.connection.get_password("fon_user_pin", raise_exception=False)
		missing_fon = [
			label
			for label, value in (
				("participant_id", self.connection.fon_participant_id),
				("user_id", self.connection.fon_user_id),
				("pin", fon_pin),
			)
			if not value
		]
		if missing_fon:
			raise PermanentFiskalyError(
				"Unified taxpayer provisioning requires FinanzOnline credentials; missing: "
				+ ", ".join(missing_fon),
				code="E_FON_CREDENTIALS_MISSING",
			)
		credentials = {
			"type": "FON",
			"participant_id": self.connection.fon_participant_id,
			"user_id": self.connection.fon_user_id,
			"pin": fon_pin,
		}
		fiscalization = {
			"type": "AT",
			"credentials": credentials,
			**self._austrian_tax_identifier(),
		}

		resources = []
		taxpayer_id = register.taxpayer_id
		if not taxpayer_id:
			taxpayer_payload = {
				"content": {
					"type": "COMPANY",
					"name": {"legal": company.company_name, "trade": company.company_name},
					"address": api_address,
					"fiscalization": fiscalization,
				}
			}
			taxpayer = self.http.request(
				"POST",
				"/taxpayers",
				json_data=taxpayer_payload,
				headers=self._headers(
					self._payload_idempotency_key(f"{register.name}:taxpayer", taxpayer_payload)
				),
			)
			taxpayer_id = taxpayer["content"]["id"]
			taxpayer = self._commission("taxpayers", taxpayer_id, f"{register.name}:taxpayer:commission")
			resources.append({"type": "TAXPAYER", "id": taxpayer_id, "data": taxpayer})

		location_id = register.location_id
		if not location_id:
			location_payload = {
				"content": {
					"type": "BRANCH",
					"taxpayer": {"id": taxpayer_id},
					"name": register.register_name,
					"address": api_address,
				}
			}
			location = self.http.request(
				"POST",
				"/locations",
				json_data=location_payload,
				headers=self._headers(
					self._payload_idempotency_key(f"{register.name}:location", location_payload)
				),
			)
			location_id = location["content"]["id"]
			location = self._commission("locations", location_id, f"{register.name}:location:commission")
			resources.append({"type": "LOCATION", "id": location_id, "data": location})

		system_id = register.provider_register_id
		if not system_id:
			system_payload = {
				"content": {
					"type": "FISCAL_DEVICE",
					"kind": "INTERNAL",
					"location": {"id": location_id},
					"producer": {
						"type": "MPN",
						"number": "ERPNext-Fiskaly-RKSV",
						"details": {"name": "ERPNext Fiskaly SIGN AT"},
					},
					"software": {"name": "erpnext_fiskaly_sign_at", "version": "0.1.0"},
				}
			}
			system = self.http.request(
				"POST",
				"/systems",
				json_data=system_payload,
				headers=self._headers(
					self._payload_idempotency_key(f"{register.name}:system", system_payload)
				),
			)
			system_id = system["content"]["id"]
			system = self._commission("systems", system_id, f"{register.name}:system:commission")
			resources.append({"type": "SYSTEM", "id": system_id, "data": system})
		else:
			system = self.http.request("GET", f"/systems/{system_id}", headers=self._headers())

		content = system["content"]
		return {
			"provider_register_id": system_id,
			"taxpayer_id": taxpayer_id,
			"location_id": location_id,
			"serial_number": ((content.get("journal") or {}).get("cryptography") or {}).get("serial_number"),
			"state": content.get("state"),
			"mode": content.get("mode"),
			"vat_rates": content.get("vat_rates") or [],
			"resources": resources,
			"raw": system,
		}

	def _vat(self, entry_or_bucket) -> dict[str, Any]:
		code = getattr(entry_or_bucket, "vat_code", None) or entry_or_bucket.code
		if getattr(entry_or_bucket, "is_exempt", False):
			result = {"type": "VAT_EXEMPTION", "code": code}
			if getattr(entry_or_bucket, "exemption_reason", None):
				result["reason"] = entry_or_bucket.exemption_reason
			return result
		rate = getattr(entry_or_bucket, "vat_rate", None)
		if rate is None:
			rate = entry_or_bucket.rate
		return {
			"type": "VAT_RATE",
			"code": code,
			"percentage": decimal_string(rate),
			"amount": decimal_string(abs(entry_or_bucket.vat)),
			"exclusive": decimal_string(abs(entry_or_bucket.net)),
			"inclusive": decimal_string(abs(entry_or_bucket.gross)),
		}

	def sign_receipt(self, register, request: FiscalReceiptRequest) -> FiscalReceiptResult:
		self._ensure_available()
		creator = [{"type": "PERSON", "label": request.operator[:128]}]
		intention_payload = {
			"content": {
				"type": "INTENTION",
				"system": {"id": register.provider_register_id},
				"operation": {
					"type": "TRANSACTION",
					"details": {"creators": creator, "training": request.receipt_type.value == "TRAINING"},
				},
			}
		}
		intention = self.http.request(
			"POST",
			"/records",
			json_data=intention_payload,
			headers=self._headers(f"{request.receipt_uuid}:intention"),
			receipt=request.receipt_uuid,
		)
		intention_id = intention["content"]["id"]
		entries = []
		for item in request.entries:
			entry_type = "RETURN" if item.is_return else "SALE"
			entries.append(
				{
					"type": entry_type,
					"data": {
						"type": "ITEM",
						"text": item.label[:256],
						"value": {"base": decimal_string(abs(item.net))},
						"unit": {
							"quantity": decimal_string(abs(item.quantity)),
							"measure": (item.uom or "unit")[:32],
							"price": {
								"inclusive": decimal_string(abs(item.gross / item.quantity))
								if item.quantity
								else "0.00",
								"exclusive": decimal_string(abs(item.net / item.quantity))
								if item.quantity
								else "0.00",
							},
						},
						"vat": self._vat(item),
					},
					"details": {
						"concept": "GOOD",
						"label": item.label[:256],
						"properties": {"erpnext_item": item.code[:64]},
					},
				}
			)
		payments = []
		for payment in request.payments:
			if payment.type == "CASH":
				payments.append({"type": "CASH", "details": {"amount": decimal_string(abs(payment.amount))}})
			else:
				payments.append(
					{
						"type": "OTHER",
						"name": (payment.label or payment.type)[:128],
						"details": {"amount": decimal_string(abs(payment.amount))},
					}
				)
		if not payments:
			payments = [{"type": "CASH", "details": {"amount": decimal_string(abs(request.total_gross))}}]
		operation = {
			"type": "RECEIPT",
			"document": {
				"number": request.document_number[-20:].upper().replace(" ", "_"),
				"issued_at": request.issued_at.isoformat(),
				"simplified_invoice": True,
			},
			"entries": entries,
			"breakdown": [self._vat(bucket) for bucket in request.vat_buckets],
			"totals": {
				"vat": {
					"amount": decimal_string(request.total_vat),
					"exclusive": decimal_string(request.total_net),
					"inclusive": decimal_string(request.total_gross),
				}
			},
			"payments": payments,
			"details": {"creators": creator},
		}
		transaction_payload = {
			"content": {
				"type": "TRANSACTION",
				"record": {"id": intention_id},
				"operation": operation,
			}
		}
		body = self.http.request(
			"POST",
			"/records",
			json_data=transaction_payload,
			headers=self._headers(request.receipt_uuid),
			receipt=request.receipt_uuid,
		)
		content = body.get("content", {})
		compliance = content.get("compliance") or {}
		journal = content.get("journal") or {}
		sequence = compliance.get("sequence") or {}
		return FiscalReceiptResult(
			provider_receipt_id=content.get("id", ""),
			qr_code_data=compliance.get("qr_code", ""),
			signature_value=journal.get("signature", ""),
			receipt_number=str(sequence.get("number") or request.document_number),
			signed_at=journal.get("signed_at"),
			serial_number=register.serial_number,
			signed=content.get("mode") == "FINISHED" and content.get("state") == "COMPLETED",
			state=content.get("state"),
			mode=content.get("mode"),
			raw=body,
		)

	def export_dep7(self, register, date_from=None, date_to=None) -> dict[str, Any]:
		self._ensure_available()
		raise ProviderNotAvailableError(
			"The unpublished Unified SIGN AT contract does not yet define a verified Austrian DEP7 "
			"export/download workflow. Use SIGN AT v1; no speculative /files request was sent."
		)
