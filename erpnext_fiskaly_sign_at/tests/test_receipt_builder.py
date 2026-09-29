from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

from erpnext_fiskaly_sign_at.contracts import ReceiptType
from erpnext_fiskaly_sign_at.services.receipt_builder import build_receipt_request, request_from_dict


def ns(**values):
	return SimpleNamespace(**values)


def vat_mapping(rate, bucket, *, tax_account=None, exempt=False):
	return ns(
		tax_rate=rate,
		v1_bucket=bucket,
		unified_code=bucket.upper(),
		tax_account=tax_account,
		is_exempt=exempt,
		exemption_reason="Exempt" if exempt else None,
	)


def payment(label, amount, *, mop_type, classification, reference_no=None):
	return ns(
		mode_of_payment=label,
		type=mop_type,
		amount=amount,
		base_amount=amount,
		fiskaly_rksv_payment_type=classification,
		reference_no=reference_no,
	)


def item(row_name, code, net, vat, rate, tax_row, *, qty=1):
	return (
		ns(
			name=row_name,
			item_code=code,
			item_name=code,
			description=code,
			qty=qty,
			net_amount=net,
			base_net_amount=net,
			amount=net,
			uom="Nos",
		),
		ns(item_row=row_name, tax_row=tax_row, rate=rate, amount=vat, taxable_amount=net),
	)


def tax_row(name, rate, *, account_head=None, account_type="Tax", charge_type="On Net Total"):
	return ns(
		name=name,
		charge_type=charge_type,
		account_head=account_head or f"VAT {rate}",
		account_type=account_type,
		account_tax_rate=rate,
		description=f"Tax {rate}%",
	)


def invoice(*, items, details, taxes, payments, total, **overrides):
	values = {
		"posting_date": "2026-08-18",
		"posting_time": "10:30:00",
		"currency": "EUR",
		"company_currency": "EUR",
		"conversion_rate": 1,
		"grand_total": total,
		"base_grand_total": total,
		"rounded_total": 0,
		"rounding_adjustment": 0,
		"base_rounding_adjustment": 0,
		"write_off_amount": 0,
		"base_write_off_amount": 0,
		"change_amount": 0,
		"is_return": 0,
		"return_against": None,
		"reference_receipt_id": None,
		"name": "ACC-POS-INV-TEST",
		"owner": "Administrator",
		"items": items,
		"item_wise_tax_details": details,
		"taxes": taxes,
		"payments": payments,
		"advances": [],
		"total_advance": 0,
		"redeem_loyalty_points": 0,
		"loyalty_amount": 0,
	}
	values.update(overrides)
	return ns(**values)


def register(*mappings):
	return ns(provider="SIGN_AT_V1", vat_mappings=list(mappings))


class TestReceiptBuilder(IntegrationTestCase):
	def test_cash_fallback_fiscalizes_full_receipt(self):
		line, detail = item("item-1", "ITEM-1", 10, 2, 20, "tax-20")
		doc = invoice(
			items=[line],
			details=[detail],
			taxes=[tax_row("tax-20", 20)],
			payments=[payment("Cash", 12, mop_type="Cash", classification="")],
			total=12,
		)
		request = build_receipt_request(
			doc,
			register(vat_mapping(20, "standard"), vat_mapping(0, "zero")),
			"00000000-0000-0000-0000-000000000001",
		)

		self.assertTrue(request.requires_fiscalization)
		self.assertEqual(request.rksv_cash_amount, Decimal("12.00"))
		self.assertEqual(str(request.total_gross), "12.00")
		self.assertEqual(request.payments[0].type, "CASH")

	def test_before_submit_uses_transient_erpnext_item_wise_tax_details(self):
		line, _detail = item("item-1", "ITEM-1", 100, 20, 20, "tax-20")
		tax = tax_row("tax-20", 20)
		doc = invoice(
			items=[line],
			details=[],
			taxes=[tax],
			payments=[
				payment(
					"Terminal Card",
					120,
					mop_type="Bank",
					classification="RKSV Cash Equivalent",
				)
			],
			total=120,
			_item_wise_tax_details=[
				ns(item=line, tax=tax, rate=20, amount=20, taxable_amount=100)
			],
		)

		request = build_receipt_request(
			doc,
			register(vat_mapping(20, "standard"), vat_mapping(0, "zero")),
			"00000000-0000-0000-0000-000000000009",
		)

		self.assertEqual(request.total_net, Decimal("100.00"))
		self.assertEqual(request.total_vat, Decimal("20.00"))
		self.assertEqual(request.total_gross, Decimal("120.00"))
		self.assertEqual(request.entries[0].vat_rate, Decimal("20"))

	def test_mixed_card_and_transfer_are_allocated_to_cash_share(self):
		line_20, detail_20 = item("item-20", "ITEM-20", 50, 10, 20, "tax-20")
		line_10, detail_10 = item("item-10", "ITEM-10", 36.36, 3.64, 10, "tax-10")
		doc = invoice(
			items=[line_20, line_10],
			details=[detail_20, detail_10],
			taxes=[tax_row("tax-20", 20), tax_row("tax-10", 10)],
			payments=[
				payment(
					"Terminal Card",
					33.33,
					mop_type="Bank",
					classification="RKSV Cash Equivalent",
					reference_no="CARD-1",
				),
				payment("Bank Transfer", 66.67, mop_type="Bank", classification="Non-Cash"),
			],
			total=100,
		)
		request = build_receipt_request(
			doc,
			register(
				vat_mapping(20, "standard"),
				vat_mapping(10, "reduced1"),
				vat_mapping(0, "zero"),
			),
			"00000000-0000-0000-0000-000000000002",
		)

		self.assertEqual(str(request.source_total_gross), "100.00")
		self.assertEqual(str(request.rksv_cash_amount), "33.33")
		self.assertEqual(str(request.total_gross), "33.33")
		self.assertEqual(sum((row.gross for row in request.entries), 0), request.total_gross)
		self.assertEqual(sum((row.gross for row in request.vat_buckets), 0), request.total_gross)
		self.assertTrue(all(row.net + row.vat == row.gross for row in request.entries))
		self.assertEqual([str(row.gross) for row in request.entries], ["20.00", "13.33"])
		self.assertEqual([str(row.quantity) for row in request.source_entries], ["1", "1"])
		self.assertEqual([row.uom for row in request.source_entries], ["Nos", "Nos"])
		self.assertEqual([row.description for row in request.source_entries], ["ITEM-20", "ITEM-10"])
		self.assertEqual(len(request.payments), 1)
		self.assertEqual(request.payments[0].type, "OTHER")
		self.assertEqual(request.payments[0].reference_no, "CARD-1")

	def test_pure_non_cash_receipt_is_marked_not_required(self):
		doc = invoice(
			items=[],
			details=[],
			taxes=[],
			payments=[payment("Bank Transfer", 100, mop_type="Bank", classification="Non-Cash")],
			total=100,
		)
		request = build_receipt_request(
			doc,
			register(vat_mapping(0, "zero")),
			"00000000-0000-0000-0000-000000000003",
		)

		self.assertFalse(request.requires_fiscalization)
		self.assertEqual(str(request.rksv_cash_amount), "0.00")
		self.assertEqual(request.entries, ())
		self.assertEqual(request.vat_buckets, ())

	def test_non_cash_erpnext_type_requires_explicit_classification(self):
		doc = invoice(
			items=[],
			details=[],
			taxes=[],
			payments=[payment("Unclassified Bank", 10, mop_type="Bank", classification="")],
			total=10,
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Classify Mode of Payment"):
			build_receipt_request(
				doc,
				register(vat_mapping(0, "zero")),
				"00000000-0000-0000-0000-000000000004",
			)

	def test_change_is_deducted_from_included_cash(self):
		line, detail = item("item-1", "ITEM-1", 10, 2, 20, "tax-20")
		doc = invoice(
			items=[line],
			details=[detail],
			taxes=[tax_row("tax-20", 20)],
			payments=[payment("Cash", 20, mop_type="Cash", classification="")],
			total=12,
			change_amount=8,
		)
		request = build_receipt_request(
			doc,
			register(vat_mapping(20, "standard"), vat_mapping(0, "zero")),
			"00000000-0000-0000-0000-000000000005",
		)

		self.assertEqual(str(request.total_gross), "12.00")
		self.assertEqual(str(request.payments[0].amount), "12.00")

	def test_additional_charge_is_not_treated_as_vat(self):
		line, vat_detail = item("item-1", "ITEM-1", 10, 2, 20, "tax-20")
		fee_detail = ns(item_row="item-1", tax_row="fee", rate=10, amount=1, taxable_amount=10)
		doc = invoice(
			items=[line],
			details=[vat_detail, fee_detail],
			taxes=[tax_row("tax-20", 20), tax_row("fee", 10, account_type="Income Account")],
			payments=[payment("Cash", 13, mop_type="Cash", classification="")],
			total=13,
		)
		with self.assertRaisesRegex(frappe.ValidationError, "additional taxes or charges"):
			build_receipt_request(
				doc,
				register(
					vat_mapping(20, "standard"),
					vat_mapping(10, "reduced1"),
					vat_mapping(0, "zero"),
				),
				"00000000-0000-0000-0000-000000000006",
			)

	def test_tax_account_mapping_and_4_9_percent_rate(self):
		line, detail = item("item-1", "ITEM-1", 100, 4.9, 4.9, "vat-49")
		doc = invoice(
			items=[line],
			details=[detail],
			taxes=[tax_row("vat-49", 4.9, account_head="VAT 4.9 - AT")],
			payments=[payment("Cash", 104.9, mop_type="Cash", classification="")],
			total=104.9,
		)
		request = build_receipt_request(
			doc,
			register(
				vat_mapping(4.9, "special", tax_account="VAT 4.9 - AT"),
				vat_mapping(0, "zero"),
			),
			"00000000-0000-0000-0000-000000000007",
		)

		self.assertEqual(request.vat_buckets[0].rate, Decimal("4.9"))
		self.assertEqual(request.vat_buckets[0].code, "special")
		self.assertEqual(str(request.total_gross), "104.90")

	def test_return_and_advance_references_survive_round_trip(self):
		line, detail = item("item-1", "ITEM-1", -25, -5, 20, "tax-20", qty=-1)
		doc = invoice(
			items=[line],
			details=[detail],
			taxes=[tax_row("tax-20", 20)],
			payments=[payment("Cash", -30, mop_type="Cash", classification="")],
			total=-30,
			is_return=1,
			return_against="ACC-POS-INV-ORIGINAL",
			reference_receipt_id="fiskaly-original",
			advances=[ns(reference_type="Payment Entry", reference_name="PE-1", allocated_amount=-5)],
			total_advance=-5,
		)
		# A return advance and payment may not together exceed the return document.
		doc.payments[0].amount = -25
		doc.payments[0].base_amount = -25
		request = build_receipt_request(
			doc,
			register(vat_mapping(20, "standard"), vat_mapping(0, "zero")),
			"00000000-0000-0000-0000-000000000008",
		)
		restored = request_from_dict(deepcopy(request.as_dict()))

		self.assertEqual(restored.as_dict(), request.as_dict())
		self.assertEqual(restored.receipt_type, ReceiptType.CANCELLATION)
		self.assertEqual(restored.reference_document_number, "ACC-POS-INV-ORIGINAL")
		self.assertEqual(restored.reference_receipt_id, "fiskaly-original")
		self.assertEqual(restored.advance_references[0].reference_name, "PE-1")
		self.assertEqual(str(restored.rksv_cash_amount), "-25.00")

	def test_non_eur_company_and_invoice_rounding_fail_closed(self):
		base = invoice(
			items=[],
			details=[],
			taxes=[],
			payments=[payment("Bank Transfer", 10, mop_type="Bank", classification="Non-Cash")],
			total=10,
		)
		wrong_currency = deepcopy(base)
		wrong_currency.company_currency = "CHF"
		with self.assertRaisesRegex(frappe.ValidationError, "company default currency"):
			build_receipt_request(
				wrong_currency,
				register(vat_mapping(0, "zero")),
				"00000000-0000-0000-0000-000000000009",
			)

		rounded = deepcopy(base)
		rounded.rounded_total = 9.95
		with self.assertRaisesRegex(frappe.ValidationError, "Rounded Total differs"):
			build_receipt_request(
				rounded,
				register(vat_mapping(0, "zero")),
				"00000000-0000-0000-0000-000000000010",
			)
