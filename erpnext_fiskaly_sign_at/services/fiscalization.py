from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, get_system_timezone, now_datetime

from erpnext_fiskaly_sign_at.contracts import (
	FiscalReceiptRequest,
	ReceiptStatus,
	ReceiptType,
	VatBucket,
)
from erpnext_fiskaly_sign_at.providers import get_provider
from erpnext_fiskaly_sign_at.providers.errors import (
	PermanentFiskalyError,
	ProviderNotAvailableError,
	RetryableFiskalyError,
)
from erpnext_fiskaly_sign_at.services.pos_snapshot import (
	POS_FISCAL_SNAPSHOT_FIELDS,
	parse_rksv_qr_data,
	vat_breakdown_rows,
	v1_bucket_amount_values,
)
from erpnext_fiskaly_sign_at.services.receipt_builder import build_receipt_request, request_from_dict
from erpnext_fiskaly_sign_at.time_utils import provider_datetime_for_db

PENDING_STATUSES = (
	ReceiptStatus.PREPARED.value,
	ReceiptStatus.SIGNING.value,
	ReceiptStatus.OFFLINE_PENDING.value,
	ReceiptStatus.RETRYING.value,
)

SUBSTITUTE_SIGNATURE_STATUS = ReceiptStatus.SUBSTITUTE_SIGNED.value
EMERGENCY_RECEIPT_STATUS = ReceiptStatus.OFFLINE_PENDING.value
AUSTRIA_TIMEZONE = ZoneInfo("Europe/Vienna")
PROVIDER_COMPLETE_STATUSES = (ReceiptStatus.SIGNED.value, SUBSTITUTE_SIGNATURE_STATUS)
PRINTABLE_RECEIPT_STATUSES = (*PROVIDER_COMPLETE_STATUSES, EMERGENCY_RECEIPT_STATUS)


def _stable_v4_uuid(key: str) -> str:
	digest = hashlib.sha256(key.encode()).digest()
	return str(uuid.UUID(bytes=digest[:16], version=4))


def _run_lifecycle_step(register, callback):
	"""Keep mutable register state and its append-only lifecycle event atomic."""

	savepoint = f"fiskaly_lifecycle_{uuid.uuid4().hex}"
	frappe.db.savepoint(savepoint)
	try:
		return callback()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		reload_method = getattr(register, "reload", None)
		if callable(reload_method):
			reload_method()
		raise


def _stable_receipt_uuid(invoice) -> str:
	"""Return a stable, v4-shaped UUID accepted by the SIGN AT v1 ReceiptId schema.

	The receipt ID must survive a rolled-back submit whose remote PUT may nevertheless
	have reached fiskaly. UUIDv5 is stable but rejected by the API's v4 pattern, so we
	derive collision-resistant bytes and set the RFC 4122 version/variant bits to v4.
	"""

	site = getattr(frappe.local, "site", None) or "erpnext"
	return _stable_v4_uuid(f"{site}:POS Invoice:{invoice.name}")


def _payload_hash(payload: str) -> str:
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _provider_wire_payload(connection, request: FiscalReceiptRequest) -> str | None:
	provider = get_provider(connection)
	builder = getattr(provider, "build_receipt_payload", None)
	if not builder:
		return None
	return json.dumps(builder(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def settings_enabled() -> bool:
	return bool(frappe.get_cached_doc("Fiskaly Settings").enabled)


def get_register(invoice, *, required=True):
	settings = frappe.get_cached_doc("Fiskaly Settings")
	filters = {
		"company": invoice.company,
		"pos_profile": invoice.pos_profile,
		"environment": settings.operating_environment,
		"active": 1,
	}
	name = frappe.db.get_value("Fiskaly Register", filters, "name")
	if not name and required:
		frappe.throw(
			_("No active fiskaly register is configured for POS Profile {0} in environment {1}.").format(
				invoice.pos_profile, settings.operating_environment
			)
		)
	return frappe.get_doc("Fiskaly Register", name) if name else None


def _set_pos_values(pos_invoice: str, values: dict):
	meta = frappe.get_meta("POS Invoice")
	available = {key: value for key, value in values.items() if meta.has_field(key)}
	if available:
		frappe.db.set_value("POS Invoice", pos_invoice, available, update_modified=False)


def validate_pos_invoice(invoice, method=None):
	if not settings_enabled():
		return
	if get_system_timezone() != "Europe/Vienna":
		frappe.throw(
			_("Austrian RKSV receipts require the site time zone Europe/Vienna."),
			title=_("RKSV time zone invalid"),
		)
	if not frappe.utils.strip_html(invoice.get("company_address_display") or "").strip():
		frappe.throw(
			_(
				"The POS Invoice requires the company's complete address for the statutory receipt. "
				"Configure a Company Address before submitting."
			),
			title=_("Company address missing"),
		)
	register = get_register(invoice)
	if not register.initialized:
		frappe.throw(_("Fiskaly register {0} is not initialized.").format(register.name))
	connection = frappe.get_doc("Fiskaly API Connection", register.connection)
	if not connection.active:
		frappe.throw(_("Fiskaly API connection {0} is disabled.").format(connection.name))
	if register.provider_state in {"DECOMMISSIONED", "DISABLED"} or not register.active:
		frappe.throw(_("Fiskaly register {0} is not available for new receipts.").format(register.name))
	if frappe.db.get_value("POS Profile", invoice.pos_profile, "print_format") != "POS Invoice RKSV":
		frappe.throw(
			_("POS Profile {0} must use Print Format 'POS Invoice RKSV'.").format(invoice.pos_profile)
		)
	# Build and validate locally before submit; no network I/O occurs here.
	request = build_receipt_request(invoice, register, _stable_receipt_uuid(invoice))
	blocking_receipt = frappe.db.get_value(
		"Fiskaly Receipt",
		{"register": register.name, "status": ReceiptStatus.ACTION_REQUIRED.value},
		["name", "pos_invoice"],
		as_dict=True,
	)
	if request.requires_fiscalization and blocking_receipt and blocking_receipt.pos_invoice != invoice.name:
		frappe.throw(
			_(
				"RKSV receipt {0} requires intervention. Resolve it before recording another cash transaction."
			).format(blocking_receipt.name)
		)
	if request.requires_fiscalization and invoice.is_return and invoice.return_against:
		if not request.reference_receipt_id:
			frappe.throw(
				_(
					"The original POS Invoice {0} has no successfully signed RKSV receipt. "
					"It must be resolved before its return can be fiscalized."
				).format(invoice.return_against)
			)
	invoice._fiskaly_request = request


def _find_existing_outbox_receipt(invoice_name: str, receipt_uuid: str) -> str | None:
	"""Resolve a prior submit/uncertainty anchor without accepting identity collisions."""

	by_invoice = frappe.db.get_value("Fiskaly Receipt", {"pos_invoice": invoice_name}, "name")
	by_uuid = frappe.db.get_value("Fiskaly Receipt", {"receipt_uuid": receipt_uuid}, "name")
	if by_invoice and by_uuid and by_invoice != by_uuid:
		raise PermanentFiskalyError(
			f"POS Invoice {invoice_name} and stable receipt UUID {receipt_uuid} resolve to different receipts",
			code="E_RECEIPT_ID_CONFLICT",
		)
	return by_invoice or by_uuid


def _verify_existing_outbox_receipt(receipt, invoice, register, request_payload: str):
	"""Fail closed unless a resubmit is byte-for-byte identical to its immutable outbox."""

	_verify_payload_integrity(receipt)
	receipt_uuid = _stable_receipt_uuid(invoice)
	expected_binding = {
		"receipt_uuid": receipt_uuid,
		"idempotency_key": receipt_uuid,
		"pos_invoice": invoice.name,
		"company": invoice.company,
		"register": register.name,
		"connection": register.connection,
		"provider": register.provider,
		"environment": register.environment,
		"receipt_kind": "SALE",
	}
	mismatches = [
		fieldname
		for fieldname, expected in expected_binding.items()
		if getattr(receipt, fieldname, None) != expected
	]
	if mismatches:
		raise PermanentFiskalyError(
			f"Existing fiscal receipt {receipt.name} has incompatible binding fields: "
			f"{', '.join(mismatches)}",
			code="E_RECEIPT_BINDING_MISMATCH",
		)

	current_hash = _payload_hash(request_payload)
	stored_hash = (receipt.payload_sha256 or "").strip().lower()
	if not hmac.compare_digest(current_hash, stored_hash) or not hmac.compare_digest(
		request_payload, receipt.request_payload or ""
	):
		raise PermanentFiskalyError(
			f"POS Invoice {invoice.name} no longer matches immutable fiscal receipt {receipt.name}",
			code="E_FISCAL_PAYLOAD_DRIFT",
		)


def create_outbox_receipt(invoice, method=None):
	if not settings_enabled():
		return
	register = get_register(invoice)
	_lock_register_row(register.name)
	register.reload()
	if not register.active or not register.initialized or register.provider_state != "INITIALIZED":
		frappe.throw(
			_("Fiskaly register {0} is no longer available after acquiring its fiscal lock.").format(
				register.name
			)
		)
	receipt_uuid = _stable_receipt_uuid(invoice)
	# Rebuild from the document that is actually being submitted. A request cached
	# by validate() may predate another hook's mutation and is not sufficient for
	# deciding whether an immutable uncertainty anchor can safely be resumed.
	request = build_receipt_request(invoice, register, receipt_uuid)
	payload = json.dumps(request.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	if existing := _find_existing_outbox_receipt(invoice.name, receipt_uuid):
		receipt = frappe.get_doc("Fiskaly Receipt", existing)
		_verify_existing_outbox_receipt(receipt, invoice, register, payload)
		# A rolled-back POS document may have been recreated after the receipt reached
		# a terminal provider state. Restore its immutable print snapshot even though
		# fiscalize_receipt() can return immediately without processing the receipt.
		_update_pos_invoice(receipt)
		return fiscalize_receipt(receipt.name)
	if not request.requires_fiscalization:
		_set_pos_values(
			invoice.name,
			{
				"fiskaly_status": "NOT_REQUIRED",
				"fiskaly_cash_amount": 0,
				"fiskaly_vat_breakdown": "[]",
				"fiskaly_print_lines": _("No RKSV cash-equivalent payment"),
			},
		)
		return {"status": "NOT_REQUIRED"}
	connection = frappe.get_doc("Fiskaly API Connection", register.connection)
	provider_request_payload = _provider_wire_payload(connection, request)
	receipt = frappe.get_doc(
		{
			"doctype": "Fiskaly Receipt",
			"receipt_uuid": receipt_uuid,
			"pos_invoice": invoice.name,
			"company": invoice.company,
			"company_address_display": invoice.get("company_address_display"),
			"register": register.name,
			"connection": register.connection,
			"provider": register.provider,
			"environment": register.environment,
			"receipt_type": request.receipt_type.value,
			"receipt_kind": "SALE",
			"status": ReceiptStatus.PREPARED.value,
			"idempotency_key": receipt_uuid,
			"request_payload": payload,
			"provider_request_payload": provider_request_payload,
			"provider_request_payload_sha256": (
				_payload_hash(provider_request_payload) if provider_request_payload else None
			),
			"payload_sha256": _payload_hash(payload),
			"next_retry_at": now_datetime(),
		}
	).insert(ignore_permissions=True)
	_update_pos_invoice(receipt)
	# RKSV requires signature/DEP/receipt completion before the next cash sale.
	# An API outage is converted into fiskaly's immutable, non-fiscal emergency
	# receipt. Permanent configuration or payload failures roll the POS submit back.
	result = fiscalize_receipt(receipt.name)
	if result["status"] not in PRINTABLE_RECEIPT_STATUSES:
		frappe.throw(
			_(
				"RKSV fiscalization could not produce a legally printable receipt. "
				"The POS transaction was rejected; keep the document and retry it with the same number."
			),
			title=_("RKSV transaction rejected"),
		)
	return result


def prevent_signed_cancellation(invoice, method=None):
	receipt = frappe.db.get_value(
		"Fiskaly Receipt", {"pos_invoice": invoice.name}, ["name", "status"], as_dict=True
	)
	if receipt:
		frappe.throw(
			_(
				"RKSV receipt {0} ({1}) is immutable. Create and submit a POS return/cancellation receipt instead."
			).format(receipt.name, receipt.status)
		)


def _update_pos_invoice(receipt):
	if not receipt.pos_invoice:
		return
	settings = frappe.get_cached_doc("Fiskaly Settings")
	request = None
	if receipt.request_payload:
		try:
			request = request_from_dict(json.loads(receipt.request_payload))
		except TypeError, ValueError, KeyError:
			frappe.log_error(
				title=f"Invalid normalized Fiskaly payload {receipt.name}",
				message=frappe.get_traceback(),
			)
	register_values = {}
	vat_mappings = ()
	if receipt.register:
		register_values = (
			frappe.db.get_value(
				"Fiskaly Register",
				receipt.register,
				["provider_register_id", "signature_creation_unit_id"],
				as_dict=True,
			)
			or {}
		)
		vat_mappings = frappe.get_all(
			"Fiskaly VAT Mapping",
			filters={"parent": receipt.register, "parenttype": "Fiskaly Register"},
			fields=["v1_bucket", "unified_code"],
			order_by="idx asc",
		)
	hints = str(receipt.hints or "").strip()
	if receipt.status == ReceiptStatus.SIGNED.value:
		print_lines = "\n".join(
			filter(
				None,
				[
					f"Kassen-ID: {receipt.serial_number}" if receipt.serial_number else None,
					f"Belegnummer: {receipt.receipt_number}" if receipt.receipt_number else None,
					f"Signaturzeit: {receipt.signed_at}" if receipt.signed_at else None,
					hints or None,
				],
			)
		)
	elif receipt.status in {SUBSTITUTE_SIGNATURE_STATUS, EMERGENCY_RECEIPT_STATUS}:
		print_lines = settings.offline_notice
	else:
		print_lines = _("RKSV receipt is not printable; status: {0}").format(receipt.status)
	offline_qr_data = (
		receipt.qr_code_data
		if receipt.status == SUBSTITUTE_SIGNATURE_STATUS
		else (
			getattr(receipt, "offline_qr_code_data", None)
			if receipt.status == EMERGENCY_RECEIPT_STATUS
			else None
		)
	)
	values = {
		"fiskaly_receipt": receipt.name,
		"fiskaly_status": receipt.status,
		"fiskaly_provider": receipt.provider,
		"fiskaly_environment": receipt.environment,
		"fiskaly_register": receipt.register,
		"fiskaly_provider_receipt_id": receipt.provider_receipt_id,
		"fiskaly_provider_register_id": register_values.get("provider_register_id"),
		"fiskaly_signature_creation_unit_id": register_values.get("signature_creation_unit_id"),
		"fiskaly_receipt_type": receipt.receipt_type,
		"fiskaly_receipt_kind": receipt.receipt_kind,
		"fiskaly_fon_validation_status": receipt.fon_validation_status,
		"fiskaly_fon_validation_at": receipt.fon_validation_at,
		"fiskaly_company_name": receipt.company,
		"fiskaly_company_address": receipt.company_address_display,
		"fiskaly_receipt_uuid": receipt.receipt_uuid,
		"fiskaly_receipt_number": receipt.receipt_number,
		"fiskaly_signed_at": receipt.signed_at,
		"fiskaly_serial_number": receipt.serial_number,
		"fiskaly_qr_code_data": receipt.qr_code_data,
		"fiskaly_signature_value": receipt.signature_value,
		"fiskaly_cash_register_id": receipt.serial_number,
		"fiskaly_cash_amount": request.rksv_cash_amount if request else None,
		"fiskaly_vat_breakdown": json.dumps(
			vat_breakdown_rows(request) if request else [], ensure_ascii=False
		),
		"fiskaly_provider_hints": hints,
		"fiskaly_offline_qr_data": offline_qr_data,
		"fiskaly_print_lines": print_lines,
	}
	if request:
		values.update(v1_bucket_amount_values(request, receipt.provider, vat_mappings))
	values.update(parse_rksv_qr_data(receipt.qr_code_data))
	_set_pos_values(receipt.pos_invoice, values)


def _public_result(receipt) -> dict:
	pos_values = {}
	if receipt.pos_invoice:
		fields = [
			field
			for field in POS_FISCAL_SNAPSHOT_FIELDS
			if frappe.get_meta("POS Invoice").has_field(field)
		]
		if fields:
			pos_values = frappe.db.get_value("POS Invoice", receipt.pos_invoice, fields, as_dict=True) or {}
	return {
		"receipt": receipt.name,
		"status": receipt.status,
		"qr_code_data": receipt.qr_code_data,
		"receipt_number": receipt.receipt_number,
		"signed_at": str(receipt.signed_at or ""),
		"serial_number": receipt.serial_number,
		"signature_value": receipt.signature_value,
		"print_lines": pos_values.get("fiskaly_print_lines"),
		"cash_amount": pos_values.get("fiskaly_cash_amount"),
		"vat_breakdown": pos_values.get("fiskaly_vat_breakdown"),
		"provider_hints": pos_values.get("fiskaly_provider_hints"),
		"offline_qr_code_data": pos_values.get("fiskaly_offline_qr_data"),
		"snapshot": dict(pos_values),
	}


def _lock_register_row(register_name: str):
	"""Serialize every receipt of one register until the surrounding transaction commits.

	A cache lock alone is insufficient here: an ``on_submit`` transaction can release
	the cache lock a few milliseconds before its database commit. A database row lock
	keeps a concurrent POS submit or retry waiting until the predecessor is visible.
	"""

	rows = frappe.db.sql(
		"SELECT name FROM `tabFiskaly Register` WHERE name = %s FOR UPDATE",
		(register_name,),
	)
	if not rows:
		raise PermanentFiskalyError(
			f"Fiskaly register {register_name} no longer exists",
			code="E_REGISTER_NOT_FOUND",
		)


def _verify_payload_integrity(receipt):
	actual = _payload_hash(receipt.request_payload or "")
	expected = (receipt.payload_sha256 or "").strip().lower()
	if not expected or not hmac.compare_digest(actual, expected):
		raise PermanentFiskalyError(
			f"The immutable fiscal payload of {receipt.name} failed its SHA-256 integrity check",
			code="E_FISCAL_PAYLOAD_INTEGRITY",
		)
	provider_payload = receipt.provider_request_payload or ""
	provider_expected = (getattr(receipt, "provider_request_payload_sha256", None) or "").strip().lower()
	if provider_payload and (
		not provider_expected or not hmac.compare_digest(_payload_hash(provider_payload), provider_expected)
	):
		raise PermanentFiskalyError(
			f"The immutable provider request of {receipt.name} failed its SHA-256 integrity check",
			code="E_PROVIDER_PAYLOAD_INTEGRITY",
		)


def _queue_uncertain_receipt_guard(receipt):
	"""Queue a rollback-independent reconciliation anchor before the remote PUT.

	The RQ job is deliberately queued before commit. It obtains the same register
	row lock as the submitting transaction, so it can only inspect the database
	after that transaction committed or rolled back.
	"""

	anchor = {
		"receipt_uuid": receipt.receipt_uuid,
		"pos_invoice": getattr(receipt, "pos_invoice", None),
		"company": getattr(receipt, "company", None),
		"company_address_display": getattr(receipt, "company_address_display", None),
		"register": receipt.register,
		"connection": receipt.connection,
		"provider": receipt.provider,
		"environment": receipt.environment,
		"receipt_type": getattr(receipt, "receipt_type", None),
		"receipt_kind": getattr(receipt, "receipt_kind", None),
		"idempotency_key": getattr(receipt, "idempotency_key", None),
		"request_payload": receipt.request_payload,
		"payload_sha256": receipt.payload_sha256,
		"provider_request_payload": receipt.provider_request_payload,
		"provider_request_payload_sha256": getattr(receipt, "provider_request_payload_sha256", None),
	}
	frappe.enqueue(
		"erpnext_fiskaly_sign_at.services.fiscalization.reconcile_uncertain_receipt",
		queue="short",
		anchor=anchor,
		enqueue_after_commit=False,
		deduplicate=True,
		job_id=f"fiskaly-uncertain-{receipt.receipt_uuid}",
	)


def reconcile_uncertain_receipt(anchor: dict) -> dict:
	"""Create durable ACTION_REQUIRED evidence if the remote PUT outlived rollback."""

	receipt_uuid = str(anchor.get("receipt_uuid") or "")
	register_name = str(anchor.get("register") or "")
	if not receipt_uuid or not register_name:
		raise PermanentFiskalyError(
			"Uncertainty reconciliation received an incomplete anchor",
			code="E_UNCERTAIN_ANCHOR_INVALID",
		)
	_lock_register_row(register_name)
	if existing := frappe.db.exists("Fiskaly Receipt", {"receipt_uuid": receipt_uuid}):
		return {"status": "RESOLVED_LOCALLY", "receipt": existing}

	request_payload = anchor.get("request_payload") or ""
	provider_payload = anchor.get("provider_request_payload") or ""
	if not hmac.compare_digest(_payload_hash(request_payload), anchor.get("payload_sha256") or ""):
		raise PermanentFiskalyError(
			"Uncertainty anchor failed its normalized payload hash",
			code="E_UNCERTAIN_ANCHOR_INTEGRITY",
		)
	if provider_payload and not hmac.compare_digest(
		_payload_hash(provider_payload), anchor.get("provider_request_payload_sha256") or ""
	):
		raise PermanentFiskalyError(
			"Uncertainty anchor failed its provider payload hash",
			code="E_UNCERTAIN_ANCHOR_INTEGRITY",
		)

	register = frappe.get_doc("Fiskaly Register", register_name)
	connection = frappe.get_doc("Fiskaly API Connection", anchor["connection"])
	remote = None
	retrieval_error = None
	try:
		provider = get_provider(connection)
		retrieve = getattr(provider, "retrieve_receipt", None)
		if not retrieve:
			raise ProviderNotAvailableError("Provider has no receipt reconciliation contract")
		remote = retrieve(register.provider_register_id, receipt_uuid)
	except PermanentFiskalyError as exc:
		if exc.status_code == 404:
			return {"status": "NOT_FOUND", "receipt_uuid": receipt_uuid}
		retrieval_error = exc
	except Exception as exc:
		retrieval_error = exc

	response_payload = (
		json.dumps(remote, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
		if remote is not None
		else None
	)
	# Keep the deterministic document identity even when the submitting transaction
	# rolled the POS draft back. ignore_links lets the durable uncertainty evidence
	# precede recreation of that exact document name; create_outbox_receipt will then
	# validate its complete payload before reconnecting the two records.
	pos_invoice = anchor.get("pos_invoice")
	values = {
		"doctype": "Fiskaly Receipt",
		**anchor,
		"pos_invoice": pos_invoice,
		"status": ReceiptStatus.ACTION_REQUIRED.value,
		"provider_receipt_id": remote.get("_id") if remote else None,
		"receipt_number": remote.get("receipt_number") if remote else None,
		"signed_at": (
			provider_datetime_for_db(remote.get("time_signature"), fieldname="time_signature")
			if remote
			else None
		),
		"serial_number": remote.get("cash_register_serial_number") if remote else None,
		"qr_code_data": remote.get("qr_code_data") if remote else None,
		"signature_value": remote.get("signature_value") if remote else None,
		"signed": int(bool(remote and remote.get("signed"))),
		"hints": " | ".join(remote.get("hints") or ()) if remote else None,
		"response_payload": response_payload,
		"response_payload_sha256": _payload_hash(response_payload) if response_payload else None,
		"last_error": (
			"Remote receipt exists after the submitting database transaction rolled back; "
			"resubmit the original POS document with the same name or reconcile it manually."
			if remote
			else (
				"The submitting transaction rolled back and the remote receipt state could not be determined: "
				f"{retrieval_error}"
			)
		)[:1000],
	}
	try:
		receipt = frappe.get_doc(values).insert(ignore_permissions=True, ignore_links=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value("Fiskaly Receipt", {"receipt_uuid": receipt_uuid}, "name")
		return {"status": "RESOLVED_LOCALLY", "receipt": existing}
	if not pos_invoice or frappe.db.exists("POS Invoice", pos_invoice):
		_update_pos_invoice(receipt)
	return {
		"status": "REMOTE_FOUND" if remote else "UNCERTAIN",
		"receipt": receipt.name,
	}


def _outage_signature_is_valid(qr_code_data: str | None, notice: str) -> bool:
	"""Recognize only the statutory §17 RKSV failure signature in a full QR record."""

	if not qr_code_data or not qr_code_data.startswith("_R1-AT") or "_" not in qr_code_data[1:]:
		return False
	encoded_signature = qr_code_data.rsplit("_", 1)[-1].strip()
	try:
		padding = "=" * (-len(encoded_signature) % 4)
		decoded = base64.urlsafe_b64decode(encoded_signature + padding).decode("utf-8")
	except ValueError, UnicodeDecodeError:
		return False
	return hmac.compare_digest(decoded, notice)


def _result_values(result, *, status: str, settings, receipt) -> dict:
	required = {
		"provider_receipt_id": result.provider_receipt_id,
		"qr_code_data": result.qr_code_data,
		"receipt_number": result.receipt_number,
		"signed_at": result.signed_at,
		"serial_number": result.serial_number,
		"signature_value": result.signature_value,
	}
	missing = [key for key, value in required.items() if value is None or not str(value).strip()]
	if missing:
		raise PermanentFiskalyError(
			f"The provider result is not printable; missing: {', '.join(missing)}",
			code="E_INCOMPLETE_PROVIDER_RESPONSE",
			response=result.raw,
		)
	if status == SUBSTITUTE_SIGNATURE_STATUS and not _outage_signature_is_valid(
		result.qr_code_data, settings.offline_notice
	):
		raise PermanentFiskalyError(
			"The provider returned an unsigned receipt without the statutory RKSV outage signature",
			code="E_INVALID_OUTAGE_RECEIPT",
			response=result.raw,
		)
	response_payload = json.dumps(result.raw, ensure_ascii=False, default=str)
	signed_at = provider_datetime_for_db(result.signed_at, fieldname="signed_at")
	values = {
		"status": status,
		"provider_receipt_id": result.provider_receipt_id,
		"receipt_number": result.receipt_number,
		"signed_at": signed_at,
		"serial_number": result.serial_number,
		"qr_code_data": result.qr_code_data,
		"signature_value": result.signature_value,
		"signed": int(result.signed),
		"hints": "\n".join(result.hints),
		"response_payload": response_payload,
		"response_payload_sha256": _payload_hash(response_payload),
		"last_error": None,
		"next_retry_at": None,
	}
	if status == SUBSTITUTE_SIGNATURE_STATUS and not receipt.offline_receipt_snapshot:
		offline_snapshot = json.dumps(
			{
				"provider_receipt_id": result.provider_receipt_id,
				"receipt_number": str(result.receipt_number),
				"signed_at": str(result.signed_at),
				"serial_number": result.serial_number,
				"qr_code_data": result.qr_code_data,
				"signature_value": result.signature_value,
				"notice": settings.offline_notice,
			},
			ensure_ascii=False,
			separators=(",", ":"),
		)
		values.update(
			{
				"offline_issued_at": now_datetime(),
				"offline_receipt_snapshot": offline_snapshot,
				"offline_snapshot_sha256": _payload_hash(offline_snapshot),
			}
		)
	return values


def _emergency_receipt_values(receipt, register, request, settings, error) -> dict:
	"""Build the immutable fiskaly-prescribed emergency receipt for an API outage.

	This is deliberately not represented as an RKSV ``_R1-AT`` record: when the
	cloud register itself is unreachable, fiskaly owns the unavailable counter,
	chain and fiscal receipt number. The emergency QR contains only the fixed
	statutory notice and the exact request is replayed later with the same UUID.
	"""

	delay = min(
		int(settings.retry_interval_minutes or 5) * 2 ** min(int(receipt.attempt_count or 1) - 1, 4),
		60,
	)
	values = {
		"status": EMERGENCY_RECEIPT_STATUS,
		"last_error": str(error)[:1000],
		"request_id": getattr(error, "request_id", None),
		"next_retry_at": add_to_date(now_datetime(), minutes=delay),
	}
	if receipt.offline_receipt_snapshot:
		if (
			receipt.offline_qr_code_data != settings.offline_notice
			or not receipt.offline_snapshot_sha256
			or not hmac.compare_digest(
				_payload_hash(receipt.offline_receipt_snapshot), receipt.offline_snapshot_sha256
			)
		):
			raise PermanentFiskalyError(
				f"Emergency receipt {receipt.name} has an invalid immutable QR payload",
				code="E_OFFLINE_SNAPSHOT_INTEGRITY",
			)
		return values

	issued_at = now_datetime()
	snapshot = {
		"schema": "ERPNext-Fiskaly-Emergency-Receipt/1",
		"receipt_uuid": receipt.receipt_uuid,
		"pos_invoice": receipt.pos_invoice,
		"document_number": request.document_number,
		"original_issued_at": request.issued_at.isoformat(),
		"emergency_issued_at": str(issued_at),
		"provider": receipt.provider,
		"environment": receipt.environment,
		"register": receipt.register,
		"provider_register_id": register.provider_register_id,
		"provider_request": {
			"method": "PUT",
			"path": (
				f"/cash-register/{register.provider_register_id}/receipt/{receipt.receipt_uuid}"
				if receipt.provider == "SIGN_AT_V1"
				else None
			),
		},
		"request_payload": json.loads(receipt.request_payload),
		"request_payload_sha256": receipt.payload_sha256,
		"provider_request_payload": (
			json.loads(receipt.provider_request_payload) if receipt.provider_request_payload else None
		),
		"provider_request_payload_sha256": getattr(receipt, "provider_request_payload_sha256", None),
		"notice": settings.offline_notice,
	}
	snapshot_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	values.update(
		{
			"offline_issued_at": issued_at,
			"offline_qr_code_data": settings.offline_notice,
			"offline_receipt_snapshot": snapshot_payload,
			"offline_snapshot_sha256": _payload_hash(snapshot_payload),
			# The provider has not allocated these fiscal values yet.
			"provider_receipt_id": None,
			"receipt_number": None,
			"signed_at": None,
			"serial_number": None,
			"qr_code_data": None,
			"signature_value": None,
			"signed": 0,
		}
	)
	return values


def _set_failure(receipt, *, status: str, error: Exception, next_retry_at=None):
	receipt.db_set(
		{
			"status": status,
			"last_error": str(error)[:1000],
			"request_id": getattr(error, "request_id", None),
			"next_retry_at": next_retry_at,
		}
	)


def _older_receipts(receipt) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT name, status, provider_receipt_id, qr_code_data, offline_qr_code_data
		FROM `tabFiskaly Receipt`
		WHERE register = %(register)s
		  AND name != %(name)s
		  AND (
			creation < %(creation)s
			OR (creation = %(creation)s AND name < %(name)s)
		  )
		  AND status != %(signed)s
		ORDER BY creation ASC, name ASC
		""",
		{
			"register": receipt.register,
			"name": receipt.name,
			"creation": receipt.creation,
			"signed": ReceiptStatus.SIGNED.value,
		},
		as_dict=True,
	)


def _substitute_receipt_is_complete(row, settings) -> bool:
	return bool(
		row.status == SUBSTITUTE_SIGNATURE_STATUS
		and row.provider_receipt_id
		and _outage_signature_is_valid(row.qr_code_data, settings.offline_notice)
	)


def create_zero_receipt(register_name: str, receipt_kind: str, fiscal_period: str) -> dict:
	if receipt_kind not in {"MANUAL_ZERO", "CONTROL"}:
		frappe.throw(
			_(
				"Monthly, annual, recovery and decommissioning receipts are generated automatically "
				"by SIGN AT and must be synchronized instead of being submitted as normal zero receipts."
			)
		)
	register = frappe.get_doc("Fiskaly Register", register_name)
	_lock_register_row(register.name)
	register.reload()
	if register.provider != "SIGN_AT_V1":
		frappe.throw(
			_(
				"Control receipts are available only through the released SIGN AT v1 adapter. "
				"Unified SIGN AT remains TEST-only until its Austrian production contract is released."
			)
		)
	if not register.active or not register.initialized or register.provider_state != "INITIALIZED":
		frappe.throw(_("The fiskaly register must be active and INITIALIZED for a control receipt."))
	fiscal_period = str(fiscal_period or "").strip()
	if not fiscal_period:
		frappe.throw(_("A unique control reference is required."))
	if existing := frappe.db.exists(
		"Fiskaly Receipt",
		{"register": register.name, "receipt_kind": receipt_kind, "fiscal_period": fiscal_period},
	):
		return _public_result(frappe.get_doc("Fiskaly Receipt", existing))

	site = getattr(frappe.local, "site", None) or "erpnext"
	receipt_uuid = _stable_v4_uuid(f"{site}:Fiskaly Register:{register.name}:{receipt_kind}:{fiscal_period}")
	request = FiscalReceiptRequest(
		receipt_uuid=receipt_uuid,
		document_number=f"{receipt_kind[:3]}-{fiscal_period}"[-20:],
		issued_at=datetime.now(tz=AUSTRIA_TIMEZONE),
		receipt_type=ReceiptType.ZERO,
		currency="EUR",
		total_net=Decimal("0"),
		total_vat=Decimal("0"),
		total_gross=Decimal("0"),
		vat_buckets=(
			VatBucket(
				code="zero",
				rate=Decimal("0"),
				net=Decimal("0"),
				vat=Decimal("0"),
				gross=Decimal("0"),
			),
		),
		operator=frappe.session.user or "ERPNext",
		source_total_gross=Decimal("0"),
		rksv_cash_amount=Decimal("0"),
	)
	payload = json.dumps(request.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	connection = frappe.get_doc("Fiskaly API Connection", register.connection)
	provider_request_payload = _provider_wire_payload(connection, request)
	receipt = frappe.get_doc(
		{
			"doctype": "Fiskaly Receipt",
			"receipt_uuid": receipt_uuid,
			"company": register.company,
			"company_address_display": register._company_address_snapshot(),
			"register": register.name,
			"connection": register.connection,
			"provider": register.provider,
			"environment": register.environment,
			"receipt_type": ReceiptType.ZERO.value,
			"receipt_kind": receipt_kind,
			"fiscal_period": fiscal_period,
			"status": ReceiptStatus.PREPARED.value,
			"idempotency_key": receipt_uuid,
			"request_payload": payload,
			"provider_request_payload": provider_request_payload,
			"provider_request_payload_sha256": (
				_payload_hash(provider_request_payload) if provider_request_payload else None
			),
			"payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
			"next_retry_at": now_datetime(),
		}
	).insert(ignore_permissions=True)
	# Keep the outbox record even when the authority requests a control receipt
	# during an API outage. Returning (instead of throwing and rolling back) keeps
	# the exact UUID/wire payload available for idempotent retry and makes the
	# non-printable status visible to the operator.
	return fiscalize_receipt(receipt.name)


def _process_receipt(receipt, register, settings):
	if receipt.status in PROVIDER_COMPLETE_STATUSES:
		return receipt

	attempt_count = int(receipt.attempt_count or 0) + 1
	receipt.db_set(
		{
			"status": ReceiptStatus.SIGNING.value,
			"attempt_count": attempt_count,
			"last_attempt_at": now_datetime(),
		}
	)
	try:
		_verify_payload_integrity(receipt)
		connection = frappe.get_doc("Fiskaly API Connection", receipt.connection)
		request = request_from_dict(json.loads(receipt.request_payload))
		provider = get_provider(connection)
		stored_wire_payload = (
			json.loads(receipt.provider_request_payload) if receipt.provider_request_payload else None
		)
		if receipt.receipt_kind == "SALE" and receipt.pos_invoice:
			_queue_uncertain_receipt_guard(receipt)
		if stored_wire_payload is not None and hasattr(provider, "sign_receipt_payload"):
			result = provider.sign_receipt_payload(register, request, stored_wire_payload)
		else:
			if stored_wire_payload is not None:
				current_wire_payload = _provider_wire_payload(connection, request)
				if not current_wire_payload or not hmac.compare_digest(
					current_wire_payload, receipt.provider_request_payload
				):
					raise PermanentFiskalyError(
						"The provider adapter would alter the stored wire payload; replay was blocked",
						code="E_PROVIDER_PAYLOAD_DRIFT",
					)
			result = provider.sign_receipt(register, request)
		status = ReceiptStatus.SIGNED.value if result.signed else SUBSTITUTE_SIGNATURE_STATUS
		receipt.db_set(_result_values(result, status=status, settings=settings, receipt=receipt))
		if register.outage_scope == "CASH_REGISTER" and register.outage_status in {
			"ACTIVE",
			"ACTION_REQUIRED",
		}:
			try:
				_run_lifecycle_step(
					register,
					lambda: register.record_api_recovery(
						f"SIGN AT replay succeeded with receipt {receipt.receipt_uuid}"
					),
				)
			except Exception:
				frappe.log_error(
					title=f"Fiskaly API recovery lifecycle {register.name}",
					message=frappe.get_traceback(),
				)
	except RetryableFiskalyError as exc:
		if receipt.receipt_kind == "SALE" and receipt.pos_invoice:
			try:
				receipt.db_set(_emergency_receipt_values(receipt, register, request, settings, exc))
			except Exception as evidence_exc:
				# An existing emergency snapshot is fiscal evidence. If it was altered
				# or became unreadable, do not let a second provider timeout escape the
				# handler and leave the receipt indefinitely ambiguous.
				_set_failure(receipt, status=ReceiptStatus.ACTION_REQUIRED.value, error=evidence_exc)
			else:
				try:
					outage_request_id = exc.request_id
					_run_lifecycle_step(
						register,
						lambda: register.record_api_outage(
							f"SIGN AT API unreachable while processing receipt {receipt.receipt_uuid}",
							request_id=outage_request_id,
						),
					)
				except Exception:
					frappe.log_error(
						title=f"Fiskaly API outage lifecycle {register.name}",
						message=frappe.get_traceback(),
					)
		else:
			delay = min(int(settings.retry_interval_minutes or 5) * 2 ** min(attempt_count - 1, 4), 60)
			_set_failure(
				receipt,
				status=ReceiptStatus.RETRYING.value,
				error=exc,
				next_retry_at=add_to_date(now_datetime(), minutes=delay),
			)
	except PermanentFiskalyError as exc:
		_set_failure(receipt, status=ReceiptStatus.ACTION_REQUIRED.value, error=exc)
	except Exception as exc:
		frappe.log_error(title=f"Fiskaly receipt {receipt.name}", message=frappe.get_traceback())
		_set_failure(receipt, status=ReceiptStatus.ACTION_REQUIRED.value, error=exc)

	if attempt_count == int(settings.max_retries or 48):
		frappe.log_error(
			title=f"Fiskaly retry escalation {receipt.name}",
			message=(
				f"Receipt {receipt.name} reached the configured escalation threshold "
				f"of {attempt_count} attempts. Automatic retry remains enabled."
			),
		)
	receipt.reload()
	_update_pos_invoice(receipt)
	return receipt


def fiscalize_receipt(receipt_name: str) -> dict:
	receipt = frappe.get_doc("Fiskaly Receipt", receipt_name)
	_lock_register_row(receipt.register)
	receipt.reload()
	if receipt.status in PROVIDER_COMPLETE_STATUSES:
		return _public_result(receipt)
	if (
		receipt.status in PENDING_STATUSES
		and receipt.next_retry_at
		and get_datetime(receipt.next_retry_at) > now_datetime()
	):
		return _public_result(receipt)

	settings = frappe.get_cached_doc("Fiskaly Settings")
	register = frappe.get_doc("Fiskaly Register", receipt.register)
	for row in _older_receipts(receipt):
		if _substitute_receipt_is_complete(row, settings):
			continue
		if row.status == ReceiptStatus.ACTION_REQUIRED.value:
			blocker = PermanentFiskalyError(
				f"Receipt {receipt.name} is blocked by earlier receipt {row.name} requiring intervention",
				code="E_PREDECESSOR_ACTION_REQUIRED",
			)
			_set_failure(
				receipt,
				status=ReceiptStatus.RETRYING.value,
				error=blocker,
				next_retry_at=add_to_date(now_datetime(), minutes=int(settings.retry_interval_minutes or 5)),
			)
			receipt.reload()
			_update_pos_invoice(receipt)
			return _public_result(receipt)

		predecessor = _process_receipt(frappe.get_doc("Fiskaly Receipt", row.name), register, settings)
		if predecessor.status not in PROVIDER_COMPLETE_STATUSES:
			blocker = RetryableFiskalyError(
				f"Receipt {receipt.name} is waiting for earlier receipt {predecessor.name}",
				code="E_PREDECESSOR_NOT_COMPLETE",
			)
			if (
				predecessor.status == EMERGENCY_RECEIPT_STATUS
				and receipt.receipt_kind == "SALE"
				and receipt.pos_invoice
			):
				# The older emergency receipt is printable locally, but it is not yet in
				# the provider's DEP/chain. Do not give a younger receipt a chance to
				# overtake it if the network happens to recover between two calls. Issue
				# the younger sale as another immutable local emergency receipt and let
				# the per-register retry worker replay both in FIFO order.
				try:
					_verify_payload_integrity(receipt)
					request = request_from_dict(json.loads(receipt.request_payload))
					receipt.db_set(_emergency_receipt_values(receipt, register, request, settings, blocker))
				except Exception as exc:
					_set_failure(receipt, status=ReceiptStatus.ACTION_REQUIRED.value, error=exc)
			else:
				_set_failure(
					receipt,
					status=ReceiptStatus.RETRYING.value,
					error=blocker,
					next_retry_at=add_to_date(
						now_datetime(), minutes=int(settings.retry_interval_minutes or 5)
					),
				)
			receipt.reload()
			_update_pos_invoice(receipt)
			return _public_result(receipt)

	receipt = _process_receipt(receipt, register, settings)
	return _public_result(receipt)


def retry_pending_receipts():
	registers = frappe.get_all(
		"Fiskaly Receipt",
		filters={"status": ["in", PENDING_STATUSES]},
		pluck="register",
		group_by="register",
		limit_page_length=0,
	)
	now = now_datetime()
	for register_name in registers:
		head = frappe.get_all(
			"Fiskaly Receipt",
			filters={"register": register_name, "status": ["in", PENDING_STATUSES]},
			fields=["name", "next_retry_at"],
			order_by="creation asc",
			limit=1,
		)
		if not head:
			continue
		if head[0].next_retry_at and get_datetime(head[0].next_retry_at) > now:
			continue
		job_hash = hashlib.sha256(register_name.encode()).hexdigest()[:24]
		frappe.enqueue(
			"erpnext_fiskaly_sign_at.services.fiscalization.retry_register_receipts",
			queue="short",
			register_name=register_name,
			deduplicate=True,
			job_id=f"fiskaly-register-{job_hash}",
		)


def retry_register_receipts(register_name: str):
	rows = frappe.get_all(
		"Fiskaly Receipt",
		filters={"register": register_name, "status": ["in", PENDING_STATUSES]},
		fields=["name", "status", "provider_receipt_id", "next_retry_at"],
		limit=500,
		order_by="creation asc",
	)
	now = now_datetime()
	for row in rows:
		if row.next_retry_at and get_datetime(row.next_retry_at) > now:
			break
		result = fiscalize_receipt(row.name)
		if result["status"] == EMERGENCY_RECEIPT_STATUS:
			break
		if result["status"] not in PROVIDER_COMPLETE_STATUSES:
			break


def purge_old_api_logs():
	days = int(frappe.get_cached_doc("Fiskaly Settings").log_retention_days or 90)
	cutoff = add_to_date(now_datetime(), days=-days)
	frappe.db.delete("Fiskaly API Log", {"creation": ["<", cutoff]})
