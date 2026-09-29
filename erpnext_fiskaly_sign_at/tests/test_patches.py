import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from erpnext_fiskaly_sign_at.contracts import FiscalReceiptRequest, ReceiptType, VatBucket
from erpnext_fiskaly_sign_at.patches.v0_1.backfill_provider_request_hashes import (
	QUARANTINE_MARKER,
	_expected_v1_wire_payload,
	_quarantine_pending_receipt,
	_validated_hash,
)
from erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots import (
	_repair_pos_snapshot,
	_verified_offline_qr,
	_verified_provider_result,
)
from erpnext_fiskaly_sign_at.providers.sign_at_v1 import SignAtV1Provider


def sample_request() -> FiscalReceiptRequest:
	return FiscalReceiptRequest(
		receipt_uuid="9ca279e0-c1d1-4d30-8c74-35db704282d2",
		document_number="ACC-POS-INV-0001",
		issued_at=datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
		receipt_type=ReceiptType.NORMAL,
		currency="EUR",
		total_net=Decimal("10.00"),
		total_vat=Decimal("2.00"),
		total_gross=Decimal("12.00"),
		vat_buckets=(
			VatBucket(
				code="standard",
				rate=Decimal("20"),
				net=Decimal("10"),
				vat=Decimal("2"),
				gross=Decimal("12"),
			),
		),
		rksv_cash_amount=Decimal("12"),
		source_total_gross=Decimal("12"),
	)


def normalized_payload() -> str:
	return json.dumps(sample_request().as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(payload: str) -> str:
	return hashlib.sha256(payload.encode()).hexdigest()


def provider_hash_row(**overrides):
	normalized = normalized_payload()
	wire = _expected_v1_wire_payload(normalized)
	values = {
		"name": "FISK-1",
		"provider": "SIGN_AT_V1",
		"status": "PREPARED",
		"last_error": None,
		"request_payload": normalized,
		"payload_sha256": digest(normalized),
		"provider_request_payload": wire,
		"provider_request_payload_sha256": None,
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def snapshot_row(**overrides):
	normalized = normalized_payload()
	values = {
		"name": "FISK-1",
		"pos_invoice": "ACC-POS-INV-0001",
		"company": "Example GmbH",
		"company_address_display": "Testgasse 1, 1010 Wien",
		"provider": "SIGN_AT_V1",
		"status": "PREPARED",
		"receipt_uuid": sample_request().receipt_uuid,
		"receipt_number": None,
		"provider_receipt_id": None,
		"signed_at": None,
		"serial_number": None,
		"qr_code_data": None,
		"signature_value": None,
		"hints": None,
		"request_payload": normalized,
		"payload_sha256": digest(normalized),
		"response_payload": None,
		"response_payload_sha256": None,
		"offline_qr_code_data": None,
		"offline_receipt_snapshot": None,
		"offline_snapshot_sha256": None,
	}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestProviderRequestHashPatch(TestCase):
	def test_frozen_historical_builder_matches_v1_wire_contract(self):
		expected = json.dumps(
			SignAtV1Provider.build_receipt_payload(None, sample_request()),
			sort_keys=True,
			separators=(",", ":"),
			ensure_ascii=False,
		)
		self.assertEqual(_expected_v1_wire_payload(normalized_payload()), expected)

	def test_backfills_only_exact_deterministic_v1_body(self):
		row = provider_hash_row()
		provider_hash, reason = _validated_hash(row)
		self.assertEqual(provider_hash, digest(row.provider_request_payload))
		self.assertIsNone(reason)

	def test_does_not_legitimize_tampered_wire_body(self):
		row = provider_hash_row()
		row.provider_request_payload = row.provider_request_payload.replace("12.00", "120.00")
		provider_hash, reason = _validated_hash(row)
		self.assertIsNone(provider_hash)
		self.assertIn("differs from the deterministic SIGN AT v1 body", reason)

	def test_requires_valid_normalized_payload_hash(self):
		row = provider_hash_row(payload_sha256="0" * 64)
		provider_hash, reason = _validated_hash(row)
		self.assertIsNone(provider_hash)
		self.assertIn("normalized fiscal payload failed", reason)

	def test_existing_matching_hash_is_idempotent(self):
		row = provider_hash_row()
		row.provider_request_payload_sha256 = digest(row.provider_request_payload)
		self.assertEqual(_validated_hash(row), (None, None))

	@patch("erpnext_fiskaly_sign_at.patches.v0_1.backfill_provider_request_hashes.frappe.db.set_value")
	def test_pending_mismatch_is_quarantined_without_payload_rewrite(self, set_value):
		row = provider_hash_row(last_error="network timeout")
		_quarantine_pending_receipt(row, "wire mismatch")
		values = set_value.call_args.args[2]
		self.assertEqual(values["status"], "ACTION_REQUIRED")
		self.assertIn(QUARANTINE_MARKER, values["last_error"])
		self.assertIn("network timeout", values["last_error"])
		self.assertNotIn("request_payload", values)
		self.assertNotIn("provider_request_payload", values)

	@patch("erpnext_fiskaly_sign_at.patches.v0_1.backfill_provider_request_hashes.frappe.logger")
	@patch("erpnext_fiskaly_sign_at.patches.v0_1.backfill_provider_request_hashes.frappe.db.set_value")
	def test_signed_mismatch_is_reported_without_history_rewrite(self, set_value, logger):
		row = provider_hash_row(status="SIGNED")
		_quarantine_pending_receipt(row, "wire mismatch")
		set_value.assert_not_called()
		logger.return_value.error.assert_called_once()


class TestSnapshotRepairPatch(TestCase):
	def test_offline_qr_requires_hash_and_field_agreement(self):
		snapshot = json.dumps({"notice": "Sicherheitseinrichtung ausgefallen"})
		row = snapshot_row(
			status="OFFLINE_PENDING",
			offline_qr_code_data="Sicherheitseinrichtung ausgefallen",
			offline_receipt_snapshot=snapshot,
			offline_snapshot_sha256=digest(snapshot),
		)
		self.assertEqual(_verified_offline_qr(row), "Sicherheitseinrichtung ausgefallen")
		row.offline_qr_code_data = "changed"
		self.assertIsNone(_verified_offline_qr(row))

	def test_provider_result_requires_hash_and_receipt_column_agreement(self):
		body = {
			"_id": sample_request().receipt_uuid,
			"receipt_number": 7,
			"time_signature": "2026-08-18T10:30:00+00:00",
			"cash_register_serial_number": "REG-1",
			"qr_code_data": "_R1-AT3_payload_signature",
			"signature_value": "signature",
		}
		response = json.dumps(body)
		row = snapshot_row(
			status="SIGNED",
			provider_receipt_id=sample_request().receipt_uuid,
			receipt_number="7",
			qr_code_data=body["qr_code_data"],
			signature_value="signature",
			response_payload=response,
			response_payload_sha256=digest(response),
		)
		self.assertEqual(_verified_provider_result(row, sample_request())["fiskaly_receipt_number"], "7")
		row.signature_value = "altered"
		self.assertEqual(_verified_provider_result(row, sample_request()), {})

	@patch("erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots.frappe.db.set_value")
	@patch("erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots.frappe.db.get_value")
	@patch("erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots.frappe.get_meta")
	@patch(
		"erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots.frappe.db.exists",
		return_value=True,
	)
	def test_partial_repair_fills_only_missing_snapshot_fields(self, exists, get_meta, get_value, set_value):
		get_meta.return_value.has_field.return_value = True
		current = MagicMock()
		current.docstatus = 1
		current.fiskaly_receipt = "FISK-1"
		current.get.side_effect = lambda key: {
			"fiskaly_receipt": "FISK-1",
			"fiskaly_status": "RETRYING",
			"fiskaly_company_name": "Original GmbH",
			"fiskaly_company_address": "Originalgasse 1",
			"fiskaly_receipt_uuid": None,
			"fiskaly_cash_amount": None,
			"fiskaly_vat_breakdown": None,
		}.get(key)
		get_value.return_value = current

		_repair_pos_snapshot(snapshot_row(), "Current GmbH", "Current master-data address")
		values = set_value.call_args.args[2]
		self.assertNotIn("fiskaly_company_name", values)
		self.assertNotIn("fiskaly_company_address", values)
		self.assertEqual(values["fiskaly_receipt_uuid"], sample_request().receipt_uuid)
		self.assertEqual(values["fiskaly_status"], "PREPARED")
