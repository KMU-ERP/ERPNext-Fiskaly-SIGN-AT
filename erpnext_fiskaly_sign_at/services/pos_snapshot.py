from __future__ import annotations

from decimal import Decimal

from erpnext_fiskaly_sign_at.contracts import decimal_string


V1_BUCKET_FIELDNAMES = {
	"standard": "fiskaly_gross_standard",
	"reduced1": "fiskaly_gross_reduced_1",
	"reduced2": "fiskaly_gross_reduced_2",
	"zero": "fiskaly_gross_zero",
	"special": "fiskaly_gross_special",
}

# Every persisted field in this tuple is an immutable fiscal snapshot. The
# fiscalization service is the only writer; ordinary post-submit changes are
# rejected by the compliance hook.
POS_FISCAL_SNAPSHOT_FIELDS = (
	"fiskaly_receipt",
	"fiskaly_status",
	"fiskaly_provider",
	"fiskaly_environment",
	"fiskaly_register",
	"fiskaly_provider_receipt_id",
	"fiskaly_provider_register_id",
	"fiskaly_signature_creation_unit_id",
	"fiskaly_receipt_type",
	"fiskaly_receipt_kind",
	"fiskaly_fon_validation_status",
	"fiskaly_fon_validation_at",
	"fiskaly_company_name",
	"fiskaly_company_address",
	"fiskaly_receipt_uuid",
	"fiskaly_receipt_number",
	"fiskaly_cash_amount",
	"fiskaly_vat_breakdown",
	"fiskaly_gross_standard",
	"fiskaly_gross_reduced_1",
	"fiskaly_gross_reduced_2",
	"fiskaly_gross_zero",
	"fiskaly_gross_special",
	"fiskaly_signed_at",
	"fiskaly_serial_number",
	"fiskaly_cash_register_id",
	"fiskaly_qr_format",
	"fiskaly_encrypted_turnover_counter",
	"fiskaly_certificate_serial_number",
	"fiskaly_previous_receipt_signature",
	"fiskaly_qr_code_data",
	"fiskaly_offline_qr_data",
	"fiskaly_signature_value",
	"fiskaly_provider_hints",
	"fiskaly_first_printed_at",
	"fiskaly_print_count",
	"fiskaly_print_lines",
)


def vat_breakdown_rows(request) -> list[dict]:
	"""Return a complete, print-format-friendly snapshot of the fiscal VAT buckets."""

	return [
		{
			"label": f"{decimal_string(bucket.rate)} %",
			"code": bucket.code,
			"rate": decimal_string(bucket.rate),
			"net": decimal_string(bucket.net),
			"vat": decimal_string(bucket.vat),
			"gross": decimal_string(bucket.gross),
			"is_exempt": bool(bucket.is_exempt),
			"exemption_reason": bucket.exemption_reason,
		}
		for bucket in request.vat_buckets
	]


def v1_bucket_amount_values(request, provider: str | None, vat_mappings=()) -> dict:
	"""Map provider VAT codes back to the five statutory RKSV amount fields."""

	code_to_bucket = {}
	for mapping in vat_mappings or ():
		v1_bucket = _value(mapping, "v1_bucket")
		provider_code = (
			_value(mapping, "unified_code") if provider == "SIGN_AT_UNIFIED" else v1_bucket
		)
		if provider_code and v1_bucket:
			code_to_bucket[str(provider_code)] = str(v1_bucket)

	amounts = {fieldname: Decimal("0") for fieldname in V1_BUCKET_FIELDNAMES.values()}
	for bucket in request.vat_buckets:
		v1_bucket = code_to_bucket.get(str(bucket.code), str(bucket.code))
		fieldname = V1_BUCKET_FIELDNAMES.get(v1_bucket)
		if fieldname:
			amounts[fieldname] += bucket.gross
	return {fieldname: decimal_string(amount) for fieldname, amount in amounts.items()}


def parse_rksv_qr_data(qr_code_data: str | None) -> dict:
	"""Extract stable metadata from the Austrian ``_R1-AT`` QR record.

	Amounts are deliberately not copied from the QR string. They are populated
	from the hash-bound normalized request so the POS snapshot has one authoritative
	source for VAT values.
	"""

	if not isinstance(qr_code_data, str) or not qr_code_data.startswith("_R1-AT"):
		return {}
	parts = qr_code_data.split("_")
	if len(parts) != 14 or parts[0] or not parts[1].startswith("R1-AT"):
		return {}
	return {
		"fiskaly_qr_format": parts[1],
		"fiskaly_encrypted_turnover_counter": parts[10],
		"fiskaly_certificate_serial_number": parts[11],
		"fiskaly_previous_receipt_signature": parts[12],
	}


def _value(row, fieldname: str):
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)
