from __future__ import annotations

import hashlib
import hmac
import json

import frappe

from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError
from erpnext_fiskaly_sign_at.services.pos_snapshot import (
	POS_FISCAL_SNAPSHOT_FIELDS,
	parse_rksv_qr_data,
	vat_breakdown_rows,
	v1_bucket_amount_values,
)
from erpnext_fiskaly_sign_at.services.receipt_builder import request_from_dict
from erpnext_fiskaly_sign_at.time_utils import provider_datetime_for_db

POS_SNAPSHOT_FIELDS = POS_FISCAL_SNAPSHOT_FIELDS


def _sha256(payload: str) -> str:
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hash_is_valid(payload, expected_hash) -> bool:
	return bool(isinstance(payload, str) and payload and expected_hash) and hmac.compare_digest(
		_sha256(payload), str(expected_hash).strip().lower()
	)


def _is_missing(value) -> bool:
	return value is None or (isinstance(value, str) and not value.strip())


def _verified_request(row):
	if not _hash_is_valid(row.request_payload, row.payload_sha256):
		return None
	try:
		return request_from_dict(json.loads(row.request_payload))
	except Exception:
		return None


def _verified_offline_qr(row) -> str | None:
	if not _hash_is_valid(row.offline_receipt_snapshot, row.offline_snapshot_sha256):
		return None
	try:
		snapshot = json.loads(row.offline_receipt_snapshot)
	except TypeError, ValueError:
		return None
	if row.status == "SUBSTITUTE_SIGNED":
		candidate = snapshot.get("qr_code_data")
	elif row.status == "OFFLINE_PENDING":
		candidate = snapshot.get("notice")
	else:
		return None
	if not isinstance(candidate, str) or not candidate.strip():
		return None
	# The snapshot hash authenticates the candidate; require the dedicated receipt
	# field to agree as an additional guard before copying it onto the POS Invoice.
	stored = row.qr_code_data if row.status == "SUBSTITUTE_SIGNED" else row.offline_qr_code_data
	return candidate if stored == candidate else None


def _verified_provider_result(row, request) -> dict:
	"""Derive printable result fields only from a hash-verified provider response."""

	if row.status not in {"SIGNED", "SUBSTITUTE_SIGNED"} or not _hash_is_valid(
		row.response_payload, row.response_payload_sha256
	):
		return {}
	try:
		body = json.loads(row.response_payload)
		if row.provider == "SIGN_AT_V1":
			qr_code = body["qr_code_data"]
			values = {
				"provider_receipt_id": body["_id"],
				"fiskaly_receipt_number": str(body["receipt_number"]),
				"fiskaly_signed_at": provider_datetime_for_db(
					body["time_signature"], fieldname="time_signature"
				),
				"fiskaly_serial_number": body["cash_register_serial_number"],
				"fiskaly_cash_register_id": body["cash_register_serial_number"],
				"fiskaly_qr_code_data": qr_code,
				"fiskaly_signature_value": body.get("signature_value")
				or (qr_code.rsplit("_", 1)[-1] if qr_code else ""),
				"fiskaly_provider_hints": "\n".join(body.get("hints") or ()),
				"fiskaly_provider_register_id": body.get("cash_register_id"),
				"fiskaly_signature_creation_unit_id": body.get("signature_creation_unit_id"),
			}
		elif row.provider == "SIGN_AT_UNIFIED":
			content = body["content"]
			compliance = content["compliance"]
			journal = content["journal"]
			values = {
				"provider_receipt_id": content["id"],
				"fiskaly_receipt_number": str(
					(compliance.get("sequence") or {}).get("number") or request.document_number
				),
				"fiskaly_signed_at": provider_datetime_for_db(journal["signed_at"], fieldname="signed_at"),
				"fiskaly_qr_code_data": compliance["qr_code"],
				"fiskaly_signature_value": journal["signature"],
			}
		else:
			return {}
	except KeyError, PermanentFiskalyError, TypeError, ValueError:
		return {}

	# Do not use a valid response envelope to legitimize independently altered
	# receipt columns. These fields were originally written together.
	bindings = {
		"provider_receipt_id": row.provider_receipt_id,
		"fiskaly_receipt_number": row.receipt_number,
		"fiskaly_qr_code_data": row.qr_code_data,
		"fiskaly_signature_value": row.signature_value,
	}
	if any(str(values[key] or "") != str(stored or "") for key, stored in bindings.items()):
		return {}
	return values


def _safe_snapshot_values(
	row,
	request,
	company: str,
	address_display: str,
	register_values=None,
	vat_mappings=(),
) -> dict:
	register_values = register_values or {}
	values = {
		"fiskaly_receipt": row.name,
		"fiskaly_status": row.status,
		"fiskaly_provider": row.provider,
		"fiskaly_environment": getattr(row, "environment", None),
		"fiskaly_register": getattr(row, "register", None),
		"fiskaly_provider_register_id": register_values.get("provider_register_id"),
		"fiskaly_signature_creation_unit_id": register_values.get("signature_creation_unit_id"),
		"fiskaly_receipt_type": getattr(row, "receipt_type", None),
		"fiskaly_receipt_kind": getattr(row, "receipt_kind", None),
		"fiskaly_fon_validation_status": getattr(row, "fon_validation_status", None),
		"fiskaly_fon_validation_at": getattr(row, "fon_validation_at", None),
		"fiskaly_company_name": company,
		"fiskaly_company_address": address_display,
		"fiskaly_receipt_uuid": row.receipt_uuid,
		"fiskaly_cash_amount": request.rksv_cash_amount,
		"fiskaly_vat_breakdown": json.dumps(vat_breakdown_rows(request), ensure_ascii=False),
	}
	values.update(v1_bucket_amount_values(request, row.provider, vat_mappings))
	provider_values = _verified_provider_result(row, request)
	values.update(provider_values)
	if provider_values:
		values["fiskaly_provider_receipt_id"] = row.provider_receipt_id
		values.update(parse_rksv_qr_data(provider_values.get("fiskaly_qr_code_data")))
	offline_qr = _verified_offline_qr(row)
	if offline_qr:
		values["fiskaly_offline_qr_data"] = offline_qr
	if row.status == "SIGNED" and values.get("fiskaly_receipt_number"):
		values["fiskaly_print_lines"] = "\n".join(
			filter(
				None,
				(
					f"Kassen-ID: {values.get('fiskaly_serial_number')}"
					if values.get("fiskaly_serial_number")
					else None,
					f"Belegnummer: {values['fiskaly_receipt_number']}",
					f"Signaturzeit: {values.get('fiskaly_signed_at')}"
					if values.get("fiskaly_signed_at")
					else None,
					values.get("fiskaly_provider_hints"),
				),
			)
		)
	return values


def _repair_pos_snapshot(row, company: str, address_display: str):
	if not row.pos_invoice or not frappe.db.exists("POS Invoice", row.pos_invoice):
		return
	request = _verified_request(row)
	if request is None:
		frappe.logger("erpnext_fiskaly_sign_at").error(
			"Snapshot repair skipped Fiskaly Receipt %s: normalized payload integrity failed",
			row.name,
		)
		return

	meta = frappe.get_meta("POS Invoice")
	installed_fields = [field for field in POS_SNAPSHOT_FIELDS if meta.has_field(field)]
	current = frappe.db.get_value(
		"POS Invoice",
		row.pos_invoice,
		["docstatus", "company", "company_address_display", *installed_fields],
		as_dict=True,
	)
	if not current or current.docstatus not in {1, 2}:
		return
	if current.get("fiskaly_receipt") and current.fiskaly_receipt != row.name:
		frappe.logger("erpnext_fiskaly_sign_at").error(
			"Snapshot repair refused POS Invoice %s: it already references Fiskaly Receipt %s, not %s",
			row.pos_invoice,
			current.fiskaly_receipt,
			row.name,
		)
		return

	register_values = {}
	vat_mappings = ()
	register = getattr(row, "register", None)
	if register and frappe.db.exists("Fiskaly Register", register):
		register_values = (
			frappe.db.get_value(
				"Fiskaly Register",
				register,
				["provider_register_id", "signature_creation_unit_id"],
				as_dict=True,
			)
			or {}
		)
		vat_mappings = frappe.get_all(
			"Fiskaly VAT Mapping",
			filters={"parent": register, "parenttype": "Fiskaly Register"},
			fields=["v1_bucket", "unified_code"],
			order_by="idx asc",
		)
	candidates = _safe_snapshot_values(
		row, request, company, address_display, register_values, vat_mappings
	)
	values = {
		field: value
		for field, value in candidates.items()
		if field in installed_fields and _is_missing(current.get(field)) and not _is_missing(value)
	}
	# Unlike receipt identity and amounts, processing status is intentionally
	# synchronized. This exposes an integrity quarantine performed by an earlier
	# patch instead of leaving the submitted POS Invoice looking printable.
	if "fiskaly_status" in installed_fields and current.get("fiskaly_status") != row.status:
		values["fiskaly_status"] = row.status
	if values:
		frappe.db.set_value("POS Invoice", row.pos_invoice, values, update_modified=False)


def execute():
	"""Repair only snapshots provable from immutable receipt or submitted POS data."""

	# Post-model patches run before the app's ``after_migrate`` hook. Create the
	# newly shipped POS fields so both sides can be backfilled in this transaction.
	from erpnext_fiskaly_sign_at.install import setup_custom_fields

	setup_custom_fields()
	if not frappe.db.exists("DocType", "Fiskaly Receipt"):
		return

	for row in frappe.get_all(
		"Fiskaly Receipt",
		fields=[
			"name",
			"pos_invoice",
			"company",
			"company_address_display",
			"provider",
			"environment",
			"register",
			"receipt_type",
			"receipt_kind",
			"status",
			"receipt_uuid",
			"receipt_number",
			"provider_receipt_id",
			"signed_at",
			"serial_number",
			"qr_code_data",
			"signature_value",
			"hints",
			"request_payload",
			"payload_sha256",
			"response_payload",
			"response_payload_sha256",
			"fon_validation_status",
			"fon_validation_at",
			"offline_qr_code_data",
			"offline_receipt_snapshot",
			"offline_snapshot_sha256",
		],
		limit_page_length=0,
	):
		company = row.company
		address_display = row.company_address_display
		pos_source = None
		if row.pos_invoice and frappe.db.exists("POS Invoice", row.pos_invoice):
			pos_source = frappe.db.get_value(
				"POS Invoice",
				row.pos_invoice,
				["docstatus", "company", "company_address_display"],
				as_dict=True,
			)
			if pos_source and pos_source.docstatus in {1, 2}:
				company = company or pos_source.company
				address_display = address_display or pos_source.company_address_display

		# Never synthesize a historical merchant address from today's Address master.
		# Automatic receipts without their own snapshot remain visibly incomplete and
		# require documentary remediation instead of receiving fabricated evidence.
		receipt_values = {}
		if not row.company and company:
			receipt_values["company"] = company
		if not row.company_address_display and address_display:
			receipt_values["company_address_display"] = address_display
		if receipt_values:
			frappe.db.set_value("Fiskaly Receipt", row.name, receipt_values, update_modified=False)

		_repair_pos_snapshot(row, company, address_display)
