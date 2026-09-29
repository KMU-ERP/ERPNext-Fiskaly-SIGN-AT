from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from erpnext_fiskaly_sign_at.contracts import FiscalReceiptRequest, ReceiptType, VatBucket
from erpnext_fiskaly_sign_at.services.pos_snapshot import (
	parse_rksv_qr_data,
	vat_breakdown_rows,
	v1_bucket_amount_values,
)


def _request(*buckets):
	return FiscalReceiptRequest(
		receipt_uuid="9ca279e0-c1d1-4d30-8c74-35db704282d2",
		document_number="ACC-POS-INV-0001",
		issued_at=SimpleNamespace(isoformat=lambda: "2026-08-19T11:03:20+02:00"),
		receipt_type=ReceiptType.NORMAL,
		currency="EUR",
		total_net=Decimal("10"),
		total_vat=Decimal("2"),
		total_gross=Decimal("12"),
		vat_buckets=tuple(buckets),
	)


class TestPosSnapshot(TestCase):
	def test_qr_metadata_is_extracted_from_complete_rksv_record(self):
		qr = "_".join(
			(
				"",
				"R1-AT3",
				"KASSE-1",
				"42",
				"2026-08-19T11:03:20",
				"12,00",
				"0,00",
				"0,00",
				"0,00",
				"0,00",
				"encrypted-counter",
				"certificate-1",
				"previous-chain",
				"signature",
			)
		)
		self.assertEqual(
			parse_rksv_qr_data(qr),
			{
				"fiskaly_qr_format": "R1-AT3",
				"fiskaly_encrypted_turnover_counter": "encrypted-counter",
				"fiskaly_certificate_serial_number": "certificate-1",
				"fiskaly_previous_receipt_signature": "previous-chain",
			},
		)
		self.assertEqual(parse_rksv_qr_data("Sicherheitseinrichtung ausgefallen"), {})

	def test_five_bmf_amount_fields_are_populated(self):
		request = _request(
			VatBucket("standard", Decimal("20"), Decimal("10"), Decimal("2"), Decimal("12")),
			VatBucket("reduced1", Decimal("10"), Decimal("5"), Decimal("0.5"), Decimal("5.5")),
		)
		self.assertEqual(
			v1_bucket_amount_values(request, "SIGN_AT_V1"),
			{
				"fiskaly_gross_standard": "12.00",
				"fiskaly_gross_reduced_1": "5.50",
				"fiskaly_gross_reduced_2": "0.00",
				"fiskaly_gross_zero": "0.00",
				"fiskaly_gross_special": "0.00",
			},
		)

	def test_unified_codes_use_register_mapping_and_vat_json_is_complete(self):
		bucket = VatBucket(
			"AT_STANDARD",
			Decimal("20"),
			Decimal("10"),
			Decimal("2"),
			Decimal("12"),
			is_exempt=False,
		)
		request = _request(bucket)
		mappings = [SimpleNamespace(unified_code="AT_STANDARD", v1_bucket="standard")]
		self.assertEqual(
			v1_bucket_amount_values(request, "SIGN_AT_UNIFIED", mappings)["fiskaly_gross_standard"],
			"12.00",
		)
		self.assertEqual(
			vat_breakdown_rows(request)[0],
			{
				"label": "20.00 %",
				"code": "AT_STANDARD",
				"rate": "20.00",
				"net": "10.00",
				"vat": "2.00",
				"gross": "12.00",
				"is_exempt": False,
				"exemption_reason": None,
			},
		)
