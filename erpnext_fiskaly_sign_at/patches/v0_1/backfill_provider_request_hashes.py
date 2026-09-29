from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import frappe

from erpnext_fiskaly_sign_at.contracts import ReceiptType, decimal_string
from erpnext_fiskaly_sign_at.services.receipt_builder import request_from_dict

PENDING_STATUSES = frozenset({"PREPARED", "SIGNING", "OFFLINE_PENDING", "RETRYING"})
QUARANTINE_MARKER = "RKSV upgrade integrity quarantine"


def _sha256(payload: str) -> str:
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_v1_wire_payload(normalized_payload: str) -> str:
	"""Rebuild the historical deterministic v1 body without mutable provider code."""

	request = request_from_dict(json.loads(normalized_payload))
	field_names = {
		"standard": "gross_amount_standard",
		"reduced1": "gross_amount_reduced_1",
		"reduced2": "gross_amount_reduced_2",
		"special": "gross_amount_special",
		"zero": "gross_amount_zero",
	}
	amount_totals = {fieldname: Decimal("0") for fieldname in field_names.values()}
	for bucket in request.vat_buckets:
		if bucket.code not in field_names:
			raise ValueError(f"unsupported historical SIGN AT v1 VAT bucket: {bucket.code}")
		amount_totals[field_names[bucket.code]] += bucket.gross
	wire_body = {
		"receipt_type": ("CANCELLATION" if request.receipt_type == ReceiptType.CANCELLATION else "NORMAL"),
		"schema": {"raw": {fieldname: decimal_string(value) for fieldname, value in amount_totals.items()}},
	}
	if request.receipt_type == ReceiptType.TRAINING:
		wire_body["receipt_type"] = "TRAINING"
	return json.dumps(wire_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validated_hash(row) -> tuple[str | None, str | None]:
	"""Return a safe backfill hash or a reason why the row must not be trusted."""

	normalized_payload = row.request_payload or ""
	normalized_hash = (row.payload_sha256 or "").strip().lower()
	if not normalized_payload or not normalized_hash:
		return None, "the normalized fiscal payload or its SHA-256 is missing"
	if not hmac.compare_digest(_sha256(normalized_payload), normalized_hash):
		return None, "the normalized fiscal payload failed its existing SHA-256 check"

	provider_payload = row.provider_request_payload or ""
	if not isinstance(provider_payload, str) or not provider_payload:
		return None, "the stored provider request body is missing or is not text"
	if row.provider != "SIGN_AT_V1":
		return None, f"provider {row.provider or '<missing>'} has no deterministic legacy backfill contract"

	try:
		expected_payload = _expected_v1_wire_payload(normalized_payload)
	except Exception as exc:
		return (
			None,
			f"the verified normalized payload cannot rebuild a SIGN AT v1 body ({type(exc).__name__})",
		)

	if not hmac.compare_digest(provider_payload, expected_payload):
		return (
			None,
			"the stored provider request differs from the deterministic SIGN AT v1 body "
			f"(stored SHA-256 {_sha256(provider_payload)}, expected SHA-256 {_sha256(expected_payload)})",
		)

	provider_hash = _sha256(provider_payload)
	existing_hash = (row.provider_request_payload_sha256 or "").strip().lower()
	if existing_hash and not hmac.compare_digest(provider_hash, existing_hash):
		return None, "the existing provider request SHA-256 does not match the stored request body"
	return (provider_hash if not existing_hash else None), None


def _quarantine_pending_receipt(row, reason: str):
	previous_error = str(row.last_error or "").strip()
	evidence = f"{QUARANTINE_MARKER}: {reason}. The stored fiscal and provider payloads were not changed."
	if previous_error and QUARANTINE_MARKER not in previous_error:
		evidence = f"{evidence}\nPrevious processing error: {previous_error}"

	values = {"last_error": evidence[:1000]}
	if row.status in PENDING_STATUSES:
		values["status"] = "ACTION_REQUIRED"
	if row.status in PENDING_STATUSES or (
		row.status == "ACTION_REQUIRED" and QUARANTINE_MARKER not in previous_error
	):
		frappe.db.set_value("Fiskaly Receipt", row.name, values, update_modified=False)
		pos_invoice = getattr(row, "pos_invoice", None)
		if (
			values.get("status") == "ACTION_REQUIRED"
			and pos_invoice
			and frappe.db.exists("POS Invoice", pos_invoice)
			and frappe.get_meta("POS Invoice").has_field("fiskaly_status")
		):
			frappe.db.set_value(
				"POS Invoice", pos_invoice, "fiskaly_status", "ACTION_REQUIRED", update_modified=False
			)
		return

	# A completed fiscal receipt is historical evidence. Report the inconsistency,
	# but never change its status, result, payload or error fields during an upgrade.
	frappe.logger("erpnext_fiskaly_sign_at").error(
		"%s: Fiskaly Receipt %s: %s", QUARANTINE_MARKER, row.name, reason
	)


def execute():
	"""Backfill only provider bodies proven to match their authenticated normalized request."""

	if not frappe.db.exists("DocType", "Fiskaly Receipt"):
		return
	meta = frappe.get_meta("Fiskaly Receipt")
	if not meta.has_field("provider_request_payload_sha256"):
		return

	for row in frappe.get_all(
		"Fiskaly Receipt",
		filters={"provider_request_payload": ["is", "set"]},
		fields=[
			"name",
			"pos_invoice",
			"provider",
			"status",
			"last_error",
			"request_payload",
			"payload_sha256",
			"provider_request_payload",
			"provider_request_payload_sha256",
		],
		limit_page_length=0,
	):
		provider_hash, reason = _validated_hash(row)
		if reason:
			_quarantine_pending_receipt(row, reason)
		elif provider_hash:
			frappe.db.set_value(
				"Fiskaly Receipt",
				row.name,
				"provider_request_payload_sha256",
				provider_hash,
				update_modified=False,
			)
