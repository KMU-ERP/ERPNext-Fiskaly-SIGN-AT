from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, get_datetime

from erpnext_fiskaly_sign_at.install import (
	PRINT_FORMAT_HTML,
	RKSV_CLOSING_PRINT_FORMAT,
	RKSV_OFFLINE_NOTICE,
	SPECIAL_RECEIPT_HTML,
)
from erpnext_fiskaly_sign_at.services.pos_snapshot import POS_FISCAL_SNAPSHOT_FIELDS

RKSV_PRINT_FORMAT = "POS Invoice RKSV"
RKSV_CASH_EQUIVALENT = "RKSV Cash Equivalent"
RKSV_NON_CASH = "Non-Cash"
CONTROLLED_PRINT_CLAIM_FLAG = "fiskaly_rksv_print_claim"
PRINT_PREVIEW_COMMAND = "frappe.www.printview.get_html_and_style"
FISCAL_RECEIPT_STATUSES = {
	"SIGNED",
	"SUBSTITUTE_SIGNED",
	"OFFLINE_PENDING",
	"PREPARED",
	"SIGNING",
	"RETRYING",
	"ACTION_REQUIRED",
}


def _settings():
	try:
		return frappe.get_cached_doc("Fiskaly Settings")
	except frappe.DoesNotExistError:
		return None


def _enabled_settings():
	settings = _settings()
	return settings if settings and settings.enabled else None


def _active_register_for(company: str | None, pos_profile: str | None):
	settings = _enabled_settings()
	if not settings or not company or not pos_profile:
		return None
	return frappe.db.get_value(
		"Fiskaly Register",
		{
			"company": company,
			"pos_profile": pos_profile,
			"environment": settings.operating_environment,
			"active": 1,
		},
		"name",
	)


def payment_classification(mode_of_payment: str | None, fallback_type: str | None = None) -> str | None:
	"""Return the explicit RKSV classification for an ERPNext payment method.

	Only ERPNext's own ``Cash`` type has a safe backwards-compatible default.
	Every other payment method must be classified explicitly so that on-site card
	payments are not confused with bank transfers or remote online payments.
	"""

	classification = None
	mode_type = fallback_type
	if mode_of_payment and frappe.db.exists("Mode of Payment", mode_of_payment):
		meta = frappe.get_meta("Mode of Payment")
		fields = ["type"]
		if meta.has_field("fiskaly_rksv_payment_type"):
			fields.append("fiskaly_rksv_payment_type")
		values = frappe.db.get_value("Mode of Payment", mode_of_payment, fields, as_dict=True) or {}
		mode_type = values.get("type") or mode_type
		classification = values.get("fiskaly_rksv_payment_type")
	if classification in {RKSV_CASH_EQUIVALENT, RKSV_NON_CASH}:
		return classification
	if str(mode_type or "").strip().lower() == "cash":
		return RKSV_CASH_EQUIVALENT
	return None


def _normalized_amount(value) -> Decimal:
	try:
		return Decimal(str(value or 0)).quantize(Decimal("0.000001"))
	except (InvalidOperation, TypeError, ValueError):
		return Decimal("NaN")


def _payment_totals(rows) -> dict[tuple[str, str], tuple[Decimal, Decimal]]:
	totals: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
	for row in rows or []:
		key = (str(row.get("mode_of_payment") or ""), str(row.get("account") or ""))
		amount, base_amount = totals.get(key, (Decimal(0), Decimal(0)))
		totals[key] = (
			amount + _normalized_amount(row.get("amount")),
			base_amount + _normalized_amount(row.get("base_amount")),
		)
	return totals


def _is_verified_pos_consolidation(doc) -> bool:
	"""Recognize only ERPNext's accounting consolidation of submitted POS invoices.

	A POS Closing Entry creates an ``is_consolidated`` Sales Invoice after the
	customer-facing POS invoices have already passed through RKSV fiscalization.
	The read-only flag alone is not trusted: the merge log, every source item,
	payment totals, document totals, and the source fiscal states must agree.
	"""

	if not cint(doc.get("is_pos")) or not cint(doc.get("is_consolidated")):
		return False
	items = doc.get("items") or []
	if not items:
		return False
	item_links = [
		(str(row.get("pos_invoice") or ""), str(row.get("pos_invoice_item") or ""))
		for row in items
	]
	if any(not pos_invoice or not pos_invoice_item for pos_invoice, pos_invoice_item in item_links):
		return False
	if len(item_links) != len(set(item_links)):
		return False

	source_names = sorted({pos_invoice for pos_invoice, _item in item_links})
	merge_refs = frappe.get_all(
		"POS Invoice Reference",
		filters={
			"parenttype": "POS Invoice Merge Log",
			"parentfield": "pos_invoices",
			"pos_invoice": ["in", source_names],
		},
		fields=["parent", "pos_invoice"],
	)
	refs_by_merge: dict[str, set[str]] = {}
	for ref in merge_refs:
		refs_by_merge.setdefault(ref.parent, set()).add(ref.pos_invoice)
	candidate_merges = [
		name for name, references in refs_by_merge.items() if set(source_names).issubset(references)
	]
	if not candidate_merges:
		return False
	merge_logs = frappe.get_all(
		"POS Invoice Merge Log",
		filters={
			"name": ["in", candidate_merges],
			"docstatus": 1,
			"pos_closing_entry": ["is", "set"],
			"company": doc.get("company"),
		},
		fields=["name"],
		limit_page_length=1,
	)
	if not merge_logs:
		return False

	sources = frappe.get_all(
		"POS Invoice",
		filters={"name": ["in", source_names]},
		fields=[
			"name",
			"docstatus",
			"company",
			"pos_profile",
			"is_return",
			"consolidated_invoice",
			"fiskaly_status",
			"grand_total",
			"base_grand_total",
		],
	)
	if {source.name for source in sources} != set(source_names):
		return False
	for source in sources:
		if (
			cint(source.docstatus) != 1
			or source.consolidated_invoice
			or source.company != doc.get("company")
			or cint(source.is_return) != cint(doc.get("is_return"))
			or (doc.get("pos_profile") and source.pos_profile != doc.get("pos_profile"))
		):
			return False

	source_items = frappe.get_all(
		"POS Invoice Item",
		filters={"parent": ["in", source_names], "parenttype": "POS Invoice", "docstatus": 1},
		fields=["name", "parent"],
	)
	if {(row.parent, row.name) for row in source_items} != set(item_links):
		return False

	source_payments = frappe.get_all(
		"Sales Invoice Payment",
		filters={"parent": ["in", source_names], "parenttype": "POS Invoice", "docstatus": 1},
		fields=["parent", "mode_of_payment", "type", "account", "amount", "base_amount"],
	)
	if _payment_totals(source_payments) != _payment_totals(doc.get("payments") or []):
		return False
	payments_by_source: dict[str, list] = {source_name: [] for source_name in source_names}
	for payment in source_payments:
		payments_by_source[payment.parent].append(payment)
	for source in sources:
		payments = payments_by_source[source.name]
		if not payments:
			return False
		has_cash_component = any(
			payment_classification(payment.mode_of_payment, payment.get("type")) != RKSV_NON_CASH
			for payment in payments
		)
		if has_cash_component and source.fiskaly_status not in FISCAL_RECEIPT_STATUSES:
			return False

	if _normalized_amount(doc.get("grand_total")) != sum(
		(_normalized_amount(source.grand_total) for source in sources), Decimal(0)
	):
		return False
	if _normalized_amount(doc.get("base_grand_total")) != sum(
		(_normalized_amount(source.base_grand_total) for source in sources), Decimal(0)
	):
		return False
	return True


def validate_pos_profile(doc, method=None):
	"""Prevent an active RKSV register from silently switching to an unsafe format."""

	if doc.get("fiskaly_manual_cash_entry") and doc.get("set_grand_total_to_default_mop"):
		frappe.throw(
			_(
				"Disable 'Set Grand Total to Default Payment Method' before enabling manual cash entry."
			),
			title=_("Conflicting POS payment settings"),
		)

	if not _active_register_for(doc.company, doc.name):
		return
	if doc.print_format != RKSV_PRINT_FORMAT:
		frappe.throw(
			_(
				"POS Profile {0} is linked to an active Fiskaly RKSV register. Its Print Format must remain {1}."
			).format(frappe.bold(doc.name), frappe.bold(RKSV_PRINT_FORMAT)),
			title=_("RKSV print format required"),
		)


def _requested_print_format() -> str | None:
	form_dict = getattr(frappe.local, "form_dict", None)
	if not form_dict:
		return None
	return form_dict.get("print_format") or form_dict.get("format")


def _apply_rksv_print_context(doc):
	"""Label only output rendered by the controlled POST endpoint as a receipt.

	The Desk preview is deliberately non-fiscal: it carries a visible preview
	watermark and the template suppresses every RKSV QR code. All other callers
	must hold a server-local claim created after locking the POS Invoice row.
	Client request parameters can never manufacture this flag.
	"""

	claim = getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, None)
	if claim:
		ordinal = cint(claim.get("ordinal"))
		if claim.get("pos_invoice") != doc.name or ordinal < 1:
			frappe.throw(
				_("The controlled RKSV print claim does not match this POS invoice."),
				title=_("Invalid RKSV print claim"),
			)
		doc.fiskaly_is_preview = 0
		doc.fiskaly_is_duplicate = ordinal > 1
		return

	form_dict = getattr(frappe.local, "form_dict", None)
	if form_dict and form_dict.get("cmd") == PRINT_PREVIEW_COMMAND:
		doc.fiskaly_is_preview = 1
		doc.fiskaly_is_duplicate = 0
		return

	frappe.throw(
		_(
			"Fiscal RKSV receipts must be generated through the controlled Print action. "
			"Direct print, PDF, full-page, and server-print endpoints are blocked."
		),
		title=_("Controlled RKSV print required"),
	)


def _receipt_evidence(doc):
	if not doc.get("fiskaly_receipt"):
		return None
	return frappe.db.get_value(
		"Fiskaly Receipt",
		{"name": doc.fiskaly_receipt, "pos_invoice": doc.name},
		[
			"status",
			"company",
			"company_address_display",
			"provider_receipt_id",
			"receipt_number",
			"signed_at",
			"payload_sha256",
			"request_payload",
			"qr_code_data",
			"serial_number",
			"signature_value",
			"hints",
			"response_payload",
			"response_payload_sha256",
			"offline_qr_code_data",
			"offline_receipt_snapshot",
			"offline_snapshot_sha256",
		],
		as_dict=True,
	)


def _same_datetime(left, right) -> bool:
	if not left and not right:
		return True
	if not left or not right:
		return False
	try:
		return get_datetime(left) == get_datetime(right)
	except TypeError, ValueError:
		return str(left) == str(right)


def _result_snapshot_matches(doc, evidence) -> bool:
	return bool(
		str(evidence.receipt_number or "") == str(doc.get("fiskaly_receipt_number") or "")
		and _same_datetime(evidence.signed_at, doc.get("fiskaly_signed_at"))
		and str(evidence.serial_number or "") == str(doc.get("fiskaly_cash_register_id") or "")
		and str(evidence.serial_number or "") == str(doc.get("fiskaly_serial_number") or "")
		and str(evidence.qr_code_data or "") == str(doc.get("fiskaly_qr_code_data") or "")
		and str(evidence.signature_value or "") == str(doc.get("fiskaly_signature_value") or "")
		and str(evidence.hints or "").strip() == str(doc.get("fiskaly_provider_hints") or "").strip()
		and str(evidence.offline_qr_code_data or "") == str(doc.get("fiskaly_offline_qr_data") or "")
	)


def _require_canonical_print_format():
	definition = frappe.db.get_value(
		"Print Format",
		RKSV_PRINT_FORMAT,
		["doc_type", "print_format_type", "custom_format", "disabled", "html"],
		as_dict=True,
	)
	if not definition or (
		definition.doc_type != "POS Invoice"
		or definition.print_format_type != "Jinja"
		or not cint(definition.custom_format)
		or cint(definition.disabled)
		or definition.html != PRINT_FORMAT_HTML
	):
		frappe.throw(
			_(
				"The protected RKSV print format differs from the application version. Run bench migrate "
				"before printing fiscal receipts."
			),
			title=_("RKSV print format integrity failed"),
		)


def require_canonical_pos_print_setup(pos_profile: str):
	"""Fail before provisioning if fiscal receipts could use a non-canonical format."""

	_require_canonical_print_format()
	configured_format = frappe.db.get_value("POS Profile", pos_profile, "print_format")
	if configured_format != RKSV_PRINT_FORMAT:
		frappe.throw(
			_("POS Profile {0} must use the protected print format {1} before provisioning.").format(
				frappe.bold(pos_profile), frappe.bold(RKSV_PRINT_FORMAT)
			),
			title=_("RKSV print setup incomplete"),
		)


def _pos_snapshot_matches(doc, request_payload: str) -> bool:
	try:
		request = json.loads(request_payload)
		cash_amount = Decimal(str(doc.get("fiskaly_cash_amount")))
		expected_cash = Decimal(str(request["rksv_cash_amount"]))
		printed_buckets = {
			(
				str(row["code"]),
				Decimal(str(row["rate"])),
				Decimal(str(row["gross"])),
			)
			for row in json.loads(doc.get("fiskaly_vat_breakdown") or "[]")
		}
		expected_buckets = {
			(
				str(row["code"]),
				Decimal(str(row["rate"])),
				Decimal(str(row["gross"])),
			)
			for row in request["vat_buckets"]
		}
	except KeyError, TypeError, ValueError, InvalidOperation:
		return False
	return cash_amount == expected_cash and printed_buckets == expected_buckets


def _require_print_snapshot(doc, expected_status: str):
	evidence = _receipt_evidence(doc)
	if (
		not evidence
		or evidence.status != expected_status
		or not evidence.payload_sha256
		or not evidence.request_payload
		or not hmac.compare_digest(
			hashlib.sha256(evidence.request_payload.encode("utf-8")).hexdigest(),
			evidence.payload_sha256,
		)
		or not evidence.offline_receipt_snapshot
		or not evidence.offline_snapshot_sha256
		or not hmac.compare_digest(
			hashlib.sha256(evidence.offline_receipt_snapshot.encode("utf-8")).hexdigest(),
			evidence.offline_snapshot_sha256,
		)
		or doc.get("fiskaly_cash_amount") is None
		or not doc.get("fiskaly_vat_breakdown")
		or not _pos_snapshot_matches(doc, evidence.request_payload)
		or evidence.company != doc.get("fiskaly_company_name")
		or evidence.company_address_display != doc.get("fiskaly_company_address")
		or not _result_snapshot_matches(doc, evidence)
	):
		frappe.throw(
			_("The immutable cash/VAT snapshot for this receipt is incomplete."),
			title=_("RKSV receipt evidence incomplete"),
		)
	return evidence


def validate_pos_print(doc, method=None, print_settings=None):
	"""Fail closed before a taxable POS receipt is rendered without RKSV data."""

	active_register = _active_register_for(doc.company, doc.pos_profile)
	status = doc.get("fiskaly_status")
	if not active_register and not doc.get("fiskaly_receipt") and status not in FISCAL_RECEIPT_STATUSES:
		return
	if status == "NOT_REQUIRED":
		return
	_require_canonical_print_format()
	requested_format = _requested_print_format()
	if requested_format != RKSV_PRINT_FORMAT:
		frappe.throw(
			_(
				"RKSV POS invoices may only be printed with {0}; an omitted format would fall back "
				"to an unsafe Standard print."
			).format(frappe.bold(RKSV_PRINT_FORMAT)),
			title=_("Unsafe print format blocked"),
		)
	if status == "SIGNED":
		evidence = _receipt_evidence(doc)
		if (
			not evidence
			or evidence.status != status
			or not evidence.provider_receipt_id
			or not evidence.request_payload
			or not evidence.payload_sha256
			or not hmac.compare_digest(
				hashlib.sha256(evidence.request_payload.encode("utf-8")).hexdigest(),
				evidence.payload_sha256,
			)
			or evidence.qr_code_data != doc.get("fiskaly_qr_code_data")
			or evidence.serial_number != doc.get("fiskaly_cash_register_id")
			or not evidence.response_payload
			or not evidence.response_payload_sha256
			or not hmac.compare_digest(
				hashlib.sha256(evidence.response_payload.encode("utf-8")).hexdigest(),
				evidence.response_payload_sha256,
			)
			or doc.get("fiskaly_cash_amount") is None
			or not doc.get("fiskaly_vat_breakdown")
			or not _pos_snapshot_matches(doc, evidence.request_payload)
			or evidence.company != doc.get("fiskaly_company_name")
			or evidence.company_address_display != doc.get("fiskaly_company_address")
			or not _result_snapshot_matches(doc, evidence)
		):
			frappe.throw(
				_("The signed RKSV receipt or its immutable fiscal payload is incomplete."),
				title=_("RKSV receipt incomplete"),
			)
		_apply_rksv_print_context(doc)
		return
	if status == "SUBSTITUTE_SIGNED":
		evidence = _require_print_snapshot(doc, status)
		if (
			not evidence.provider_receipt_id
			or not evidence.response_payload
			or not evidence.response_payload_sha256
			or not hmac.compare_digest(
				hashlib.sha256(evidence.response_payload.encode("utf-8")).hexdigest(),
				evidence.response_payload_sha256,
			)
			or not str(doc.get("fiskaly_offline_qr_data") or "").startswith("_R1-AT")
			or evidence.qr_code_data != doc.get("fiskaly_offline_qr_data")
		):
			frappe.throw(
				_("The provider's complete RKSV substitute-signature code is missing."),
				title=_("Substitute-signature receipt incomplete"),
			)
		_apply_rksv_print_context(doc)
		return
	if status == "OFFLINE_PENDING":
		evidence = _require_print_snapshot(doc, status)
		notice = RKSV_OFFLINE_NOTICE
		if (
			doc.get("fiskaly_offline_qr_data") != notice
			or evidence.offline_qr_code_data != notice
			or evidence.provider_receipt_id
		):
			frappe.throw(
				_("The API-outage emergency receipt does not contain the fixed outage notice."),
				title=_("Emergency receipt incomplete"),
			)
		_apply_rksv_print_context(doc)
		return
	frappe.throw(
		_("This POS invoice cannot be printed while its RKSV status is {0}.").format(
			frappe.bold(status or _("not set"))
		),
		title=_("RKSV fiscalization not completed"),
	)


def protect_pos_fiscal_snapshot_update(doc, method=None):
	"""Reject ordinary post-submit edits; internal fiscal code uses controlled db_set calls."""

	old = doc.get_doc_before_save()
	if not old:
		return
	changed = [
		fieldname for fieldname in POS_FISCAL_SNAPSHOT_FIELDS if old.get(fieldname) != doc.get(fieldname)
	]
	if changed:
		frappe.throw(
			_("Immutable RKSV POS snapshot fields cannot be changed: {0}").format(", ".join(changed)),
			title=_("RKSV snapshot is immutable"),
		)


def protect_rksv_print_format(doc, method=None):
	expected = {
		RKSV_PRINT_FORMAT: ("POS Invoice", PRINT_FORMAT_HTML),
		RKSV_CLOSING_PRINT_FORMAT: ("Fiskaly Receipt", SPECIAL_RECEIPT_HTML),
	}.get(doc.name)
	if not expected:
		return
	doc_type, html = expected
	if (
		doc.doc_type != doc_type
		or doc.print_format_type != "Jinja"
		or not cint(doc.custom_format)
		or cint(doc.disabled)
		or doc.html != html
	):
		frappe.throw(
			_("The application-managed RKSV print format cannot be changed or disabled."),
			title=_("Protected RKSV print format"),
		)


def prevent_rksv_print_format_deletion(doc, method=None):
	if doc.name in {RKSV_PRINT_FORMAT, RKSV_CLOSING_PRINT_FORMAT} and not getattr(
		frappe.flags, "in_uninstall", False
	):
		frappe.throw(_("Application-managed RKSV print formats cannot be deleted."))


def _incoming_customer_payment(doc) -> bool:
	return doc.payment_type == "Receive" and doc.party_type == "Customer"


def prevent_unfiscalized_cash_payment_entry(doc, method=None):
	settings = _enabled_settings()
	if not settings or not getattr(settings, "enforce_pos_only_cash_receipts", True):
		return
	if not _incoming_customer_payment(doc):
		return
	classification = payment_classification(doc.mode_of_payment)
	if classification != RKSV_NON_CASH:
		classification_hint = (
			_("It is classified as an RKSV cash equivalent.")
			if classification == RKSV_CASH_EQUIVALENT
			else _("It has no explicit Non-Cash classification.")
		)
		payment_method = doc.mode_of_payment or _("no explicitly classified payment method")
		frappe.throw(
			_(
				"Incoming customer payments using {0} cannot be submitted "
				"as a direct Payment Entry while Fiskaly RKSV is enabled. Record the payment through a "
				"configured POS Profile so that a signed receipt is created. {1}"
			).format(frappe.bold(payment_method), classification_hint),
			title=_("Unfiscalized cash payment blocked"),
		)


def prevent_unfiscalized_sales_invoice(doc, method=None):
	settings = _enabled_settings()
	if not settings or not getattr(settings, "enforce_pos_only_cash_receipts", True):
		return
	if not (doc.get("is_pos") or doc.get("is_paid")):
		return
	if _is_verified_pos_consolidation(doc):
		return
	payments = doc.get("payments") or []
	if not payments:
		frappe.throw(
			_(
				"A paid/POS Sales Invoice without explicitly classified payment rows could bypass the "
				"supported RKSV flow. Use POS Invoice with a configured Fiskaly Register."
			),
			title=_("Unfiscalized cash sale blocked"),
		)
	for payment in payments:
		classification = payment_classification(payment.mode_of_payment, payment.get("type"))
		if classification != RKSV_NON_CASH:
			frappe.throw(
				_(
					"Sales Invoice uses payment method {0}, which is not explicitly classified as Non-Cash. "
					"Use POS Invoice with a configured Fiskaly Register so a possible cash receipt cannot "
					"bypass fiscalization."
				).format(frappe.bold(payment.mode_of_payment)),
				title=_("Unfiscalized cash sale blocked"),
			)
