from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import get_datetime, get_system_timezone

from erpnext_fiskaly_sign_at.contracts import (
	AdvanceReference,
	FiscalReceiptRequest,
	Payment,
	ReceiptEntry,
	ReceiptType,
	RksvPaymentClassification,
	VatBucket,
)

CENT = Decimal("0.01")
RATE_TOLERANCE = Decimal("0.0001")
PAYMENT_CLASSIFICATION_FIELD = "fiskaly_rksv_payment_type"


def money(value) -> Decimal:
	return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _value(document, fieldname: str, default=None):
	if isinstance(document, dict):
		return document.get(fieldname, default)
	return getattr(document, fieldname, default)


def _has_field(document, fieldname: str) -> bool:
	if isinstance(document, dict):
		return fieldname in document
	if hasattr(document, "meta"):
		return bool(document.meta.get_field(fieldname))
	return hasattr(document, fieldname)


def _issued_at(invoice) -> datetime:
	stamp = get_datetime(f"{invoice.posting_date} {invoice.posting_time or '00:00:00'}")
	if stamp.tzinfo is None:
		stamp = stamp.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return stamp


def _mapping(register, rate: Decimal):
	for row in register.vat_mappings:
		if abs(Decimal(str(row.tax_rate)) - rate) < RATE_TOLERANCE:
			return row
	frappe.throw(_("No fiskaly VAT mapping exists for tax rate {0}%.").format(rate))


def _provider_vat_code(register, mapping, rate: Decimal) -> str:
	code = (
		_value(mapping, "v1_bucket") if register.provider == "SIGN_AT_V1" else _value(mapping, "unified_code")
	)
	if not code:
		frappe.throw(_("No {0} VAT code is configured for tax rate {1}%.").format(register.provider, rate))
	return code


def _tax_account_metadata(tax_row) -> tuple[str | None, Decimal | None]:
	account_type = _value(tax_row, "account_type")
	account_rate = _value(tax_row, "account_tax_rate")
	if account_type is not None:
		return account_type, Decimal(str(account_rate)) if account_rate not in (None, "") else None

	account_head = _value(tax_row, "account_head")
	if not account_head:
		return None, None
	values = frappe.get_cached_value("Account", account_head, ["account_type", "tax_rate"], as_dict=True)
	if not values:
		return None, None
	return values.account_type, Decimal(str(values.tax_rate)) if values.tax_rate not in (None, "") else None


def _is_matching_vat_row(mapping, tax_row, rate: Decimal) -> bool:
	mapped_account = _value(mapping, "tax_account")
	account_head = _value(tax_row, "account_head")
	if mapped_account:
		return account_head == mapped_account

	account_type, account_rate = _tax_account_metadata(tax_row)
	if account_type != "Tax":
		return False
	# A blank/zero Account.tax_rate is common in existing charts of accounts. A
	# configured non-zero value, however, must agree with the item-wise rate.
	return not account_rate or abs(account_rate - rate) < RATE_TOLERANCE


def _item_tax_details(invoice, item) -> list:
	"""Return persisted or transient ERPNext v16 item-wise tax rows for an item."""
	item_name = _value(item, "name")
	persisted = [
		row
		for row in (_value(invoice, "item_wise_tax_details", []) or [])
		if _value(row, "item_row") == item_name
	]
	if persisted:
		return persisted

	def belongs_to_item(row) -> bool:
		transient_item = _value(row, "item")
		if transient_item is item:
			return True
		transient_name = _value(transient_item, "name") if transient_item else None
		return bool(item_name and transient_name == item_name)

	return [
		row
		for row in (_value(invoice, "_item_wise_tax_details", []) or [])
		if belongs_to_item(row)
	]


def _item_vat(invoice, register, item) -> tuple[Decimal, Decimal, object]:
	details = _item_tax_details(invoice, item)
	if not details:
		mapping = _mapping(register, Decimal("0"))
		return Decimal("0"), Decimal("0"), mapping

	tax_rows = {row.name: row for row in (_value(invoice, "taxes", []) or []) if _value(row, "name")}
	candidates: list[tuple[object, Decimal, Decimal, object]] = []
	unsupported: list[str] = []

	for detail in details:
		amount = money(_value(detail, "amount"))
		rate = Decimal(str(_value(detail, "rate", 0) or 0))
		tax_row_name = _value(detail, "tax_row")
		tax_row = _value(detail, "tax") or tax_rows.get(tax_row_name)
		if not tax_row:
			if amount:
				frappe.throw(
					_("Item {0}: item-wise tax row {1} cannot be resolved.").format(
						item.item_code, tax_row_name or "?"
					)
				)
			continue

		try:
			mapping = _mapping(register, rate)
		except frappe.ValidationError:
			if amount:
				unsupported.append(
					_value(tax_row, "description") or _value(tax_row, "account_head") or str(rate)
				)
			continue

		charge_type = _value(tax_row, "charge_type")
		if charge_type != "On Net Total" or not _is_matching_vat_row(mapping, tax_row, rate):
			if amount:
				unsupported.append(
					_value(tax_row, "description") or _value(tax_row, "account_head") or str(rate)
				)
			continue
		if not rate and amount:
			frappe.throw(
				_("Item {0}: a zero-percent VAT row cannot contain tax amount {1}.").format(
					item.item_code, amount
				)
			)
		if amount or rate:
			candidates.append((detail, rate, amount, mapping))

	if unsupported:
		frappe.throw(
			_(
				"Item {0} contains additional taxes or charges ({1}). They must be represented as "
				"separate fiscal items; they are not treated as Austrian VAT automatically."
			).format(item.item_code, ", ".join(unsupported))
		)
	if len(candidates) > 1:
		frappe.throw(
			_(
				"Item {0} has more than one possible VAT row. Set Tax Account on the Fiskaly VAT Mapping "
				"so exactly one row can be selected."
			).format(item.item_code)
		)
	if not candidates:
		mapping = _mapping(register, Decimal("0"))
		return Decimal("0"), Decimal("0"), mapping
	_detail, rate, amount, mapping = candidates[0]
	return rate, amount, mapping


def _company_currency(invoice) -> str | None:
	if currency := _value(invoice, "company_currency"):
		return str(currency)
	if company := _value(invoice, "company"):
		return frappe.get_cached_value("Company", company, "default_currency")
	return None


def _validate_currency_and_rounding(invoice) -> Decimal:
	if str(invoice.currency).upper() != "EUR":
		frappe.throw(_("Austrian RKSV receipts must be fiscalized in EUR."))
	company_currency = _company_currency(invoice)
	if company_currency and str(company_currency).upper() != "EUR":
		frappe.throw(_("The company default currency must be EUR for Austrian RKSV fiscalization."))

	conversion_rate = _value(invoice, "conversion_rate")
	if (
		conversion_rate not in (None, "")
		and abs(Decimal(str(conversion_rate)) - Decimal("1")) > RATE_TOLERANCE
	):
		frappe.throw(_("The POS Invoice conversion rate must be 1 for an EUR RKSV receipt."))

	document_total = money(invoice.grand_total)
	base_total = _value(invoice, "base_grand_total")
	if base_total not in (None, "") and money(base_total) != document_total:
		frappe.throw(_("POS Invoice and company-currency grand totals do not match in EUR."))

	rounded_total = _value(invoice, "rounded_total")
	if rounded_total not in (None, "", 0, 0.0) and money(rounded_total) != document_total:
		frappe.throw(
			_(
				"Rounded Total differs from Grand Total. Disable invoice rounding or represent it as a fiscal item."
			)
		)
	for fieldname in (
		"rounding_adjustment",
		"base_rounding_adjustment",
		"write_off_amount",
		"base_write_off_amount",
	):
		if money(_value(invoice, fieldname)):
			frappe.throw(
				_("{0} is not supported for RKSV allocation; represent the amount as a fiscal item.").format(
					fieldname
				)
			)
	return document_total


def _mode_of_payment_configuration(payment_row) -> tuple[str, str]:
	mode_of_payment = _value(payment_row, "mode_of_payment")
	if not mode_of_payment:
		frappe.throw(_("Every POS payment row requires a Mode of Payment."))

	standard_type = _value(payment_row, "type") or ""
	classification = (
		_value(payment_row, PAYMENT_CLASSIFICATION_FIELD)
		if _has_field(payment_row, PAYMENT_CLASSIFICATION_FIELD)
		else None
	)
	if classification is None:
		fields = ["type"]
		if frappe.get_meta("Mode of Payment").get_field(PAYMENT_CLASSIFICATION_FIELD):
			fields.append(PAYMENT_CLASSIFICATION_FIELD)
		values = frappe.get_cached_value("Mode of Payment", mode_of_payment, fields, as_dict=True)
		if not values:
			frappe.throw(_("Mode of Payment {0} does not exist.").format(mode_of_payment))
		standard_type = values.type or standard_type
		classification = values.get(PAYMENT_CLASSIFICATION_FIELD) if len(fields) > 1 else ""

	classification = str(classification or "").strip()
	if classification not in {"", *(item.value for item in RksvPaymentClassification)}:
		frappe.throw(
			_("Mode of Payment {0} has unsupported RKSV classification {1}.").format(
				mode_of_payment, classification
			)
		)
	if not classification:
		if str(standard_type).casefold() != "cash":
			frappe.throw(
				_(
					"Classify Mode of Payment {0} as 'RKSV Cash Equivalent' or 'Non-Cash'. "
					"Only ERPNext type Cash is accepted as a safe fallback."
				).format(mode_of_payment)
			)
		classification = RksvPaymentClassification.CASH_EQUIVALENT.value
	return str(standard_type), classification


def _resolve_payments(invoice, document_total: Decimal) -> tuple[tuple[Payment, ...], Decimal, Decimal]:
	rows: list[dict] = []
	raw_payment_total = Decimal("0")
	document_sign = Decimal("-1") if document_total < 0 else Decimal("1")

	for row in _value(invoice, "payments", []) or []:
		amount = money(_value(row, "amount"))
		if not amount:
			continue
		if amount * document_sign < 0:
			frappe.throw(
				_("Payment {0} has the opposite sign of the POS Invoice.").format(row.mode_of_payment)
			)
		base_amount = _value(row, "base_amount")
		if base_amount not in (None, "", 0, 0.0) and money(base_amount) != amount:
			frappe.throw(
				_("Payment {0} differs in transaction and company currency.").format(row.mode_of_payment)
			)

		standard_type, classification = _mode_of_payment_configuration(row)
		included = classification == RksvPaymentClassification.CASH_EQUIVALENT.value
		rows.append(
			{
				"amount": amount,
				"included": included,
				"provider_type": "CASH" if str(standard_type).casefold() == "cash" else "OTHER",
				"label": row.mode_of_payment,
				"reference_no": _value(row, "reference_no"),
			}
		)
		raw_payment_total += amount

	change = money(_value(invoice, "change_amount"))
	if change < 0 or (document_total < 0 and change):
		frappe.throw(_("Change Amount must be zero or positive and is not supported on returns."))
	if change:
		remaining_change = change
		for row in reversed(rows):
			if not row["included"] or row["provider_type"] != "CASH" or row["amount"] <= 0:
				continue
			deduction = min(row["amount"], remaining_change)
			row["amount"] -= deduction
			remaining_change -= deduction
			if not remaining_change:
				break
		if remaining_change:
			frappe.throw(_("Change Amount {0} cannot be assigned to an RKSV cash payment.").format(change))

	net_paid = money(raw_payment_total - change)
	if net_paid * document_sign < 0 or abs(net_paid) > abs(document_total):
		frappe.throw(
			_("Net POS payments {0} exceed or contradict document total {1}.").format(
				net_paid, document_total
			)
		)

	payments = tuple(
		Payment(
			type=row["provider_type"],
			amount=money(row["amount"]),
			label=row["label"],
			rksv_classification=RksvPaymentClassification.CASH_EQUIVALENT,
			reference_no=row["reference_no"],
		)
		for row in rows
		if row["included"] and money(row["amount"])
	)
	rksv_cash_amount = money(sum((payment.amount for payment in payments), Decimal("0")))
	if abs(rksv_cash_amount) > abs(document_total):
		frappe.throw(
			_("RKSV cash-equivalent payments {0} exceed document total {1}.").format(
				rksv_cash_amount, document_total
			)
		)
	return payments, rksv_cash_amount, net_paid


def _advance_references(invoice) -> tuple[tuple[AdvanceReference, ...], Decimal]:
	references = tuple(
		AdvanceReference(
			reference_type=_value(row, "reference_type"),
			reference_name=_value(row, "reference_name"),
			allocated_amount=money(_value(row, "allocated_amount")),
		)
		for row in (_value(invoice, "advances", []) or [])
		if money(_value(row, "allocated_amount"))
	)
	calculated = money(sum((row.allocated_amount for row in references), Decimal("0")))
	declared = money(_value(invoice, "total_advance"))
	if declared and calculated and declared != calculated:
		frappe.throw(
			_("Allocated advances {0} do not match POS Invoice Total Advance {1}.").format(
				calculated, declared
			)
		)
	return references, declared or calculated


def _reference_values(invoice) -> tuple[str | None, str | None]:
	reference_document = _value(invoice, "return_against") or _value(invoice, "reference_document_number")
	reference_receipt = _value(invoice, "reference_receipt_id")
	if reference_document and not reference_receipt:
		reference_receipt = frappe.db.get_value(
			"Fiskaly Receipt",
			{
				"pos_invoice": reference_document,
				"status": ["in", ["SIGNED", "SUBSTITUTE_SIGNED"]],
			},
			"provider_receipt_id",
		)
	return reference_document, reference_receipt


def _build_source_entries(invoice, register) -> tuple[ReceiptEntry, ...]:
	entries: list[ReceiptEntry] = []
	for item in invoice.items:
		rate, vat, mapping = _item_vat(invoice, register, item)
		net_source = _value(item, "base_net_amount")
		if net_source in (None, ""):
			net_source = item.net_amount if item.net_amount is not None else item.amount
		net = money(net_source)
		gross = money(net + vat)
		if gross and net * gross < 0:
			frappe.throw(
				_("Item {0} contains a negative VAT component that exceeds its gross amount.").format(
					item.item_code
				)
			)
		code = _provider_vat_code(register, mapping, rate)
		entries.append(
			ReceiptEntry(
				code=item.item_code or item.name,
				label=item.item_name or item.description or item.item_code,
				quantity=Decimal(str(item.qty or 0)),
				net=net,
				vat=vat,
				gross=gross,
				vat_code=code,
				vat_rate=rate,
				uom=item.uom,
				description=item.description,
				is_return=bool(invoice.is_return or (item.qty or 0) < 0),
				is_exempt=bool(_value(mapping, "is_exempt")),
				exemption_reason=_value(mapping, "exemption_reason"),
			)
		)
	return tuple(entries)


def _allocate_gross(entries: tuple[ReceiptEntry, ...], target: Decimal) -> tuple[Decimal, ...]:
	target_cents = int((abs(target) * 100).to_integral_value())
	weights = [abs(entry.gross) for entry in entries]
	total_weight = sum(weights, Decimal("0"))
	if not target_cents:
		return tuple(Decimal("0") for _entry in entries)
	if not total_weight:
		frappe.throw(_("Cannot allocate an RKSV payment to zero-value receipt entries."))

	floors: list[int] = []
	remainders: list[tuple[Decimal, int]] = []
	for index, weight in enumerate(weights):
		raw_cents = weight * target_cents / total_weight
		floor_cents = int(raw_cents.to_integral_value(rounding=ROUND_FLOOR))
		floors.append(floor_cents)
		remainders.append((raw_cents - floor_cents, index))
	for _remainder, index in sorted(remainders, key=lambda row: (-row[0], row[1]))[
		: target_cents - sum(floors)
	]:
		floors[index] += 1
	sign = Decimal("-1") if target < 0 else Decimal("1")
	return tuple(sign * Decimal(cents) / 100 for cents in floors)


def _scale_entries(
	entries: tuple[ReceiptEntry, ...], source_total: Decimal, target: Decimal
) -> tuple[ReceiptEntry, ...]:
	if target == source_total:
		return entries
	allocated_gross = _allocate_gross(entries, target)
	result: list[ReceiptEntry] = []
	for entry, gross in zip(entries, allocated_gross, strict=True):
		if not gross:
			continue
		if not entry.gross:
			frappe.throw(_("Cannot allocate payment to zero-value item {0}.").format(entry.code))
		gross_abs = abs(gross)
		net_abs = money(gross_abs * abs(entry.net) / abs(entry.gross))
		if net_abs > gross_abs:
			frappe.throw(_("Item {0} has an unsupported negative VAT allocation.").format(entry.code))
		sign = Decimal("-1") if gross < 0 else Decimal("1")
		net = sign * net_abs
		vat = money(gross - net)
		line_ratio = gross_abs / abs(entry.gross)
		quantity = (entry.quantity * line_ratio).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
		result.append(replace(entry, quantity=quantity, net=net, vat=vat, gross=money(gross)))
	return tuple(result)


def _vat_buckets(entries: tuple[ReceiptEntry, ...]) -> tuple[VatBucket, ...]:
	buckets: dict[tuple[str, Decimal, bool, str | None], dict[str, Decimal]] = defaultdict(
		lambda: {"net": Decimal("0"), "vat": Decimal("0"), "gross": Decimal("0")}
	)
	for entry in entries:
		key = (entry.vat_code, entry.vat_rate, entry.is_exempt, entry.exemption_reason)
		buckets[key]["net"] += entry.net
		buckets[key]["vat"] += entry.vat
		buckets[key]["gross"] += entry.gross
	return tuple(
		VatBucket(
			code=code,
			rate=rate,
			net=money(values["net"]),
			vat=money(values["vat"]),
			gross=money(values["gross"]),
			is_exempt=is_exempt,
			exemption_reason=reason,
		)
		for (code, rate, is_exempt, reason), values in sorted(buckets.items(), key=lambda row: row[0][1])
	)


def build_receipt_request(invoice, register, receipt_uuid: str) -> FiscalReceiptRequest:
	document_total = _validate_currency_and_rounding(invoice)
	payments, rksv_cash_amount, net_paid = _resolve_payments(invoice, document_total)
	advance_references, advance_amount = _advance_references(invoice)
	loyalty_amount = (
		money(_value(invoice, "loyalty_amount")) if _value(invoice, "redeem_loyalty_points") else Decimal("0")
	)
	if document_total and abs(net_paid + advance_amount + loyalty_amount) > abs(document_total):
		frappe.throw(_("Payments, advances and loyalty amount exceed the POS Invoice total."))
	reference_document, reference_receipt = _reference_values(invoice)

	common = {
		"receipt_uuid": receipt_uuid,
		"document_number": invoice.name,
		"issued_at": _issued_at(invoice),
		"receipt_type": ReceiptType.CANCELLATION if invoice.is_return else ReceiptType.NORMAL,
		"currency": invoice.currency,
		"payments": payments,
		"operator": invoice.owner or "ERPNext POS",
		"reference_receipt_id": reference_receipt,
		"reference_document_number": reference_document,
		"advance_references": advance_references,
		"advance_amount": advance_amount,
		"source_total_gross": document_total,
		"rksv_cash_amount": rksv_cash_amount,
	}
	if not rksv_cash_amount:
		return FiscalReceiptRequest(
			total_net=Decimal("0"),
			total_vat=Decimal("0"),
			total_gross=Decimal("0"),
			vat_buckets=(),
			entries=(),
			requires_fiscalization=False,
			**common,
		)

	source_entries = _build_source_entries(invoice, register)
	source_entry_total = money(sum((entry.gross for entry in source_entries), Decimal("0")))
	if source_entry_total != document_total:
		frappe.throw(
			_(
				"Fiscal item total {0} does not match POS Invoice grand total {1}. Check VAT and additional charges."
			).format(source_entry_total, document_total)
		)
	if any(entry.gross and entry.gross * document_total < 0 for entry in source_entries):
		frappe.throw(
			_("Mixed positive and negative item lines cannot be allocated to one RKSV payment receipt.")
		)

	entries = _scale_entries(source_entries, document_total, rksv_cash_amount)
	vat_buckets = _vat_buckets(entries)
	total_net = money(sum((bucket.net for bucket in vat_buckets), Decimal("0")))
	total_vat = money(sum((bucket.vat for bucket in vat_buckets), Decimal("0")))
	total_gross = money(sum((bucket.gross for bucket in vat_buckets), Decimal("0")))
	if total_gross != rksv_cash_amount or money(total_net + total_vat) != total_gross:
		frappe.throw(_("Internal RKSV allocation did not preserve the cash/VAT total."))

	return FiscalReceiptRequest(
		total_net=total_net,
		total_vat=total_vat,
		total_gross=total_gross,
		vat_buckets=vat_buckets,
		entries=entries,
		source_entries=source_entries,
		requires_fiscalization=True,
		**common,
	)


def request_from_dict(data: dict) -> FiscalReceiptRequest:
	return FiscalReceiptRequest(
		receipt_uuid=data["receipt_uuid"],
		document_number=data["document_number"],
		issued_at=datetime.fromisoformat(data["issued_at"]),
		receipt_type=ReceiptType(data["receipt_type"]),
		currency=data["currency"],
		total_net=Decimal(data["total_net"]),
		total_vat=Decimal(data["total_vat"]),
		total_gross=Decimal(data["total_gross"]),
		vat_buckets=tuple(
			VatBucket(
				code=row["code"],
				rate=Decimal(row["rate"]),
				net=Decimal(row["net"]),
				vat=Decimal(row["vat"]),
				gross=Decimal(row["gross"]),
				is_exempt=bool(row.get("is_exempt")),
				exemption_reason=row.get("exemption_reason"),
			)
			for row in data["vat_buckets"]
		),
		entries=tuple(
			ReceiptEntry(
				code=row["code"],
				label=row["label"],
				quantity=Decimal(row["quantity"]),
				net=Decimal(row["net"]),
				vat=Decimal(row["vat"]),
				gross=Decimal(row["gross"]),
				vat_code=row["vat_code"],
				vat_rate=Decimal(row["vat_rate"]),
				uom=row.get("uom"),
				description=row.get("description"),
				is_return=bool(row.get("is_return")),
				is_exempt=bool(row.get("is_exempt")),
				exemption_reason=row.get("exemption_reason"),
			)
			for row in data.get("entries", [])
		),
		source_entries=tuple(
			ReceiptEntry(
				code=row["code"],
				label=row["label"],
				quantity=Decimal(row["quantity"]),
				net=Decimal(row["net"]),
				vat=Decimal(row["vat"]),
				gross=Decimal(row["gross"]),
				vat_code=row["vat_code"],
				vat_rate=Decimal(row["vat_rate"]),
				uom=row.get("uom"),
				description=row.get("description"),
				is_return=bool(row.get("is_return")),
				is_exempt=bool(row.get("is_exempt")),
				exemption_reason=row.get("exemption_reason"),
			)
			for row in data.get("source_entries", data.get("entries", []))
		),
		payments=tuple(
			Payment(
				type=row["type"],
				amount=Decimal(row["amount"]),
				label=row.get("label"),
				rksv_classification=RksvPaymentClassification(
					row.get("rksv_classification", RksvPaymentClassification.CASH_EQUIVALENT.value)
				),
				reference_no=row.get("reference_no"),
			)
			for row in data.get("payments", [])
		),
		operator=data.get("operator") or "ERPNext POS",
		reference_receipt_id=data.get("reference_receipt_id"),
		reference_document_number=data.get("reference_document_number"),
		advance_references=tuple(
			AdvanceReference(
				reference_type=row.get("reference_type"),
				reference_name=row.get("reference_name"),
				allocated_amount=Decimal(row["allocated_amount"]),
			)
			for row in data.get("advance_references", [])
		),
		advance_amount=Decimal(data.get("advance_amount", "0")),
		source_total_gross=(
			Decimal(data["source_total_gross"]) if data.get("source_total_gross") is not None else None
		),
		rksv_cash_amount=(
			Decimal(data["rksv_cash_amount"]) if data.get("rksv_cash_amount") is not None else None
		),
		requires_fiscalization=bool(data.get("requires_fiscalization", True)),
	)
