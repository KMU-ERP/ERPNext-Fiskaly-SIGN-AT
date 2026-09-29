from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ProviderCode(StrEnum):
	SIGN_AT_V1 = "SIGN_AT_V1"
	SIGN_AT_UNIFIED = "SIGN_AT_UNIFIED"


class Environment(StrEnum):
	TEST = "TEST"
	LIVE = "LIVE"


class ReceiptType(StrEnum):
	NORMAL = "NORMAL"
	CANCELLATION = "CANCELLATION"
	TRAINING = "TRAINING"
	ZERO = "ZERO"


class ReceiptStatus(StrEnum):
	PREPARED = "PREPARED"
	SIGNING = "SIGNING"
	SIGNED = "SIGNED"
	SUBSTITUTE_SIGNED = "SUBSTITUTE_SIGNED"
	OFFLINE_PENDING = "OFFLINE_PENDING"
	RETRYING = "RETRYING"
	ACTION_REQUIRED = "ACTION_REQUIRED"


class RksvPaymentClassification(StrEnum):
	CASH_EQUIVALENT = "RKSV Cash Equivalent"
	NON_CASH = "Non-Cash"


def decimal_string(value: Decimal | str | float | int) -> str:
	return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def quantity_string(value: Decimal | str | float | int) -> str:
	value = Decimal(str(value)).quantize(Decimal("0.000001"))
	return format(value, "f").rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True)
class VatBucket:
	code: str
	rate: Decimal
	net: Decimal
	vat: Decimal
	gross: Decimal
	is_exempt: bool = False
	exemption_reason: str | None = None

	def as_dict(self) -> dict[str, Any]:
		return {
			"code": self.code,
			"rate": decimal_string(self.rate),
			"net": decimal_string(self.net),
			"vat": decimal_string(self.vat),
			"gross": decimal_string(self.gross),
			"is_exempt": self.is_exempt,
			"exemption_reason": self.exemption_reason,
		}


@dataclass(frozen=True)
class ReceiptEntry:
	code: str
	label: str
	quantity: Decimal
	net: Decimal
	vat: Decimal
	gross: Decimal
	vat_code: str
	vat_rate: Decimal
	uom: str | None = None
	description: str | None = None
	is_return: bool = False
	is_exempt: bool = False
	exemption_reason: str | None = None

	def as_dict(self) -> dict[str, Any]:
		result = asdict(self)
		result["quantity"] = quantity_string(result["quantity"])
		for key in ("net", "vat", "gross", "vat_rate"):
			result[key] = decimal_string(result[key])
		return result


@dataclass(frozen=True)
class Payment:
	type: str
	amount: Decimal
	label: str | None = None
	rksv_classification: RksvPaymentClassification = RksvPaymentClassification.CASH_EQUIVALENT
	reference_no: str | None = None

	def as_dict(self) -> dict[str, Any]:
		return {
			"type": self.type,
			"amount": decimal_string(self.amount),
			"label": self.label,
			"rksv_classification": self.rksv_classification.value,
			"reference_no": self.reference_no,
		}


@dataclass(frozen=True)
class AdvanceReference:
	reference_type: str | None
	reference_name: str | None
	allocated_amount: Decimal

	def as_dict(self) -> dict[str, Any]:
		return {
			"reference_type": self.reference_type,
			"reference_name": self.reference_name,
			"allocated_amount": decimal_string(self.allocated_amount),
		}


@dataclass(frozen=True)
class FiscalReceiptRequest:
	receipt_uuid: str
	document_number: str
	issued_at: datetime
	receipt_type: ReceiptType
	currency: str
	total_net: Decimal
	total_vat: Decimal
	total_gross: Decimal
	vat_buckets: tuple[VatBucket, ...]
	entries: tuple[ReceiptEntry, ...] = field(default_factory=tuple)
	# Immutable, unscaled ERPNext line snapshot. ``entries`` may be aliquoted to
	# the RKSV cash share for a mixed payment; source_entries keeps the actual
	# quantity and customary description required alongside the DEP.
	source_entries: tuple[ReceiptEntry, ...] = field(default_factory=tuple)
	payments: tuple[Payment, ...] = field(default_factory=tuple)
	operator: str = "ERPNext POS"
	reference_receipt_id: str | None = None
	reference_document_number: str | None = None
	advance_references: tuple[AdvanceReference, ...] = field(default_factory=tuple)
	advance_amount: Decimal = Decimal("0")
	source_total_gross: Decimal | None = None
	rksv_cash_amount: Decimal | None = None
	requires_fiscalization: bool = True

	def as_dict(self) -> dict[str, Any]:
		return {
			"receipt_uuid": self.receipt_uuid,
			"document_number": self.document_number,
			"issued_at": self.issued_at.isoformat(),
			"receipt_type": self.receipt_type.value,
			"currency": self.currency,
			"total_net": decimal_string(self.total_net),
			"total_vat": decimal_string(self.total_vat),
			"total_gross": decimal_string(self.total_gross),
			"vat_buckets": [bucket.as_dict() for bucket in self.vat_buckets],
			"entries": [entry.as_dict() for entry in self.entries],
			"source_entries": [entry.as_dict() for entry in self.source_entries],
			"payments": [payment.as_dict() for payment in self.payments],
			"operator": self.operator,
			"reference_receipt_id": self.reference_receipt_id,
			"reference_document_number": self.reference_document_number,
			"advance_references": [advance.as_dict() for advance in self.advance_references],
			"advance_amount": decimal_string(self.advance_amount),
			"source_total_gross": (
				decimal_string(self.source_total_gross) if self.source_total_gross is not None else None
			),
			"rksv_cash_amount": (
				decimal_string(self.rksv_cash_amount) if self.rksv_cash_amount is not None else None
			),
			"requires_fiscalization": self.requires_fiscalization,
		}


@dataclass(frozen=True)
class FiscalReceiptResult:
	provider_receipt_id: str
	qr_code_data: str
	signature_value: str
	receipt_number: str
	signed_at: str | None
	serial_number: str | None
	signed: bool
	state: str | None = None
	mode: str | None = None
	hints: tuple[str, ...] = field(default_factory=tuple)
	raw: dict[str, Any] = field(default_factory=dict)
