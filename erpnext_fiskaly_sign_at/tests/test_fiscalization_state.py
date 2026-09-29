import base64
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from frappe.tests import IntegrationTestCase

from erpnext_fiskaly_sign_at.contracts import (
	FiscalReceiptRequest,
	FiscalReceiptResult,
	ReceiptType,
	VatBucket,
)
from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError, RetryableFiskalyError
from erpnext_fiskaly_sign_at.services.fiscalization import (
	SUBSTITUTE_SIGNATURE_STATUS,
	_emergency_receipt_values,
	_outage_signature_is_valid,
	_process_receipt,
	_queue_uncertain_receipt_guard,
	_result_values,
	_stable_receipt_uuid,
	_verify_payload_integrity,
	create_outbox_receipt,
	fiscalize_receipt,
	reconcile_uncertain_receipt,
)

NOTICE = "Sicherheitseinrichtung ausgefallen"


class ReceiptStub(SimpleNamespace):
	def db_set(self, field_or_values, value=None, **kwargs):
		values = field_or_values if isinstance(field_or_values, dict) else {field_or_values: value}
		for fieldname, fieldvalue in values.items():
			setattr(self, fieldname, fieldvalue)

	def reload(self):
		return self


def request():
	return FiscalReceiptRequest(
		receipt_uuid="9ca279e0-c1d1-4d30-8c74-35db704282d2",
		document_number="ACC-POS-INV-0001",
		issued_at=datetime(2026, 8, 18, 10, 30, tzinfo=ZoneInfo("Europe/Vienna")),
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


class TestFiscalizationState(IntegrationTestCase):
	def test_stable_receipt_id_matches_v1_uuid4_pattern(self):
		invoice = SimpleNamespace(name="ACC-POS-INV-0001")
		first = uuid.UUID(_stable_receipt_uuid(invoice))
		second = uuid.UUID(_stable_receipt_uuid(invoice))
		self.assertEqual(first, second)
		self.assertEqual(first.version, 4)
		self.assertEqual(first.variant, uuid.RFC_4122)

	def test_only_full_rksv_substitute_signature_is_recognized(self):
		encoded = base64.urlsafe_b64encode(NOTICE.encode()).decode()
		self.assertTrue(_outage_signature_is_valid(f"_R1-AT1_payload_{encoded}", NOTICE))
		self.assertFalse(_outage_signature_is_valid(NOTICE, NOTICE))
		self.assertFalse(_outage_signature_is_valid("_R1-AT1_payload_invalid", NOTICE))

	def test_provider_substitute_keeps_provider_qr_separate_from_emergency_qr(self):
		encoded = base64.urlsafe_b64encode(NOTICE.encode()).decode()
		qr_data = f"_R1-AT1_payload_{encoded}"
		result = FiscalReceiptResult(
			provider_receipt_id="provider-receipt",
			qr_code_data=qr_data,
			signature_value=encoded,
			receipt_number="7",
			signed_at="2026-08-18T10:30:00+02:00",
			serial_number="REGISTER-1",
			signed=False,
			hints=(NOTICE,),
			raw={"signed": False, "qr_code_data": qr_data},
		)
		values = _result_values(
			result,
			status=SUBSTITUTE_SIGNATURE_STATUS,
			settings=SimpleNamespace(offline_notice=NOTICE),
			receipt=SimpleNamespace(offline_receipt_snapshot=None),
		)
		self.assertEqual(values["qr_code_data"], qr_data)
		self.assertNotIn("offline_qr_code_data", values)
		self.assertEqual(
			values["response_payload_sha256"],
			hashlib.sha256(values["response_payload"].encode()).hexdigest(),
		)

	def test_api_outage_snapshot_contains_exact_wire_request_and_hash(self):
		normalized = json.dumps(request().as_dict(), sort_keys=True, separators=(",", ":"))
		wire = json.dumps(
			{
				"receipt_type": "NORMAL",
				"schema": {"raw": {"gross_amount_standard": "12.00"}},
			},
			sort_keys=True,
			separators=(",", ":"),
		)
		receipt = SimpleNamespace(
			name=request().receipt_uuid,
			receipt_uuid=request().receipt_uuid,
			pos_invoice="ACC-POS-INV-0001",
			provider="SIGN_AT_V1",
			environment="TEST",
			register="REGISTER-1",
			attempt_count=1,
			request_payload=normalized,
			payload_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
			provider_request_payload=wire,
			provider_request_payload_sha256=hashlib.sha256(wire.encode()).hexdigest(),
			offline_receipt_snapshot=None,
			offline_qr_code_data=None,
		)
		values = _emergency_receipt_values(
			receipt,
			SimpleNamespace(provider_register_id="provider-register"),
			request(),
			SimpleNamespace(offline_notice=NOTICE, retry_interval_minutes=5),
			RetryableFiskalyError("network timeout", request_id="request-1"),
		)
		self.assertEqual(values["status"], "OFFLINE_PENDING")
		self.assertEqual(values["offline_qr_code_data"], NOTICE)
		self.assertIsNone(values["provider_receipt_id"])
		self.assertIsNone(values["receipt_number"])
		self.assertEqual(
			values["offline_snapshot_sha256"],
			hashlib.sha256(values["offline_receipt_snapshot"].encode()).hexdigest(),
		)
		snapshot = json.loads(values["offline_receipt_snapshot"])
		self.assertEqual(snapshot["provider_request_payload"], json.loads(wire))
		self.assertEqual(
			snapshot["provider_request_payload_sha256"], hashlib.sha256(wire.encode()).hexdigest()
		)
		self.assertEqual(snapshot["provider_request"]["method"], "PUT")

	def test_tampered_provider_wire_payload_is_rejected(self):
		normalized = json.dumps(request().as_dict(), sort_keys=True, separators=(",", ":"))
		wire = '{"receipt_type":"NORMAL"}'
		receipt = SimpleNamespace(
			name="RECEIPT-1",
			request_payload=normalized,
			payload_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
			provider_request_payload=wire.replace("NORMAL", "CANCELLATION"),
			provider_request_payload_sha256=hashlib.sha256(wire.encode()).hexdigest(),
		)

		with self.assertRaises(PermanentFiskalyError) as raised:
			_verify_payload_integrity(receipt)

		self.assertEqual(raised.exception.code, "E_PROVIDER_PAYLOAD_INTEGRITY")

	def test_corrupt_emergency_snapshot_becomes_action_required_on_timeout(self):
		normalized = json.dumps(request().as_dict(), sort_keys=True, separators=(",", ":"))
		wire = json.dumps(
			{"receipt_type": "NORMAL", "schema": {"raw": {"gross_amount_standard": "12.00"}}},
			sort_keys=True,
			separators=(",", ":"),
		)
		receipt = ReceiptStub(
			name="RECEIPT-1",
			receipt_uuid=request().receipt_uuid,
			status="OFFLINE_PENDING",
			attempt_count=1,
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="TEST",
			register="REGISTER-1",
			pos_invoice="ACC-POS-INV-0001",
			receipt_kind="SALE",
			request_payload=normalized,
			payload_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
			provider_request_payload=wire,
			provider_request_payload_sha256=hashlib.sha256(wire.encode()).hexdigest(),
			offline_receipt_snapshot="tampered",
			offline_snapshot_sha256=hashlib.sha256(b"original").hexdigest(),
			offline_qr_code_data=NOTICE,
		)

		class Provider:
			def sign_receipt_payload(self, register, fiscal_request, provider_payload):
				raise RetryableFiskalyError("network timeout")

		settings = SimpleNamespace(offline_notice=NOTICE, retry_interval_minutes=5, max_retries=48)
		register = SimpleNamespace(provider_register_id="provider-register")
		module = "erpnext_fiskaly_sign_at.services.fiscalization"
		with (
			patch(f"{module}.frappe.get_doc", return_value=SimpleNamespace()),
			patch(f"{module}.get_provider", return_value=Provider()),
			patch(f"{module}._queue_uncertain_receipt_guard"),
			patch(f"{module}._update_pos_invoice"),
		):
			result = _process_receipt(receipt, register, settings)

		self.assertIs(result, receipt)
		self.assertEqual(receipt.status, "ACTION_REQUIRED")
		self.assertIn("invalid immutable QR payload", receipt.last_error)

	@patch("frappe.enqueue")
	def test_uncertainty_guard_is_queued_before_commit(self, enqueue):
		receipt = SimpleNamespace(
			receipt_uuid=request().receipt_uuid,
			pos_invoice="ACC-POS-INV-0001",
			company="Example Company",
			company_address_display="Example Street 1",
			register="REGISTER-1",
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="TEST",
			receipt_type="NORMAL",
			receipt_kind="SALE",
			idempotency_key=request().receipt_uuid,
			request_payload="{}",
			payload_sha256=hashlib.sha256(b"{}").hexdigest(),
			provider_request_payload="{}",
			provider_request_payload_sha256=hashlib.sha256(b"{}").hexdigest(),
		)

		_queue_uncertain_receipt_guard(receipt)

		self.assertFalse(enqueue.call_args.kwargs["enqueue_after_commit"])
		self.assertEqual(enqueue.call_args.kwargs["anchor"]["receipt_uuid"], request().receipt_uuid)
		self.assertIn(request().receipt_uuid, enqueue.call_args.kwargs["job_id"])

	def test_uncertainty_anchor_keeps_missing_pos_invoice_identity(self):
		normalized = "{}"
		wire = "{}"
		anchor = {
			"receipt_uuid": request().receipt_uuid,
			"pos_invoice": "ACC-POS-INV-ROLLED-BACK",
			"company": "Example Company",
			"company_address_display": "Example Street 1",
			"register": "REGISTER-1",
			"connection": "CONNECTION-1",
			"provider": "SIGN_AT_V1",
			"environment": "TEST",
			"receipt_type": "NORMAL",
			"receipt_kind": "SALE",
			"idempotency_key": request().receipt_uuid,
			"request_payload": normalized,
			"payload_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
			"provider_request_payload": wire,
			"provider_request_payload_sha256": hashlib.sha256(wire.encode()).hexdigest(),
		}
		insert_kwargs = {}
		created = None

		class AnchorReceipt(SimpleNamespace):
			def insert(self, **kwargs):
				insert_kwargs.update(kwargs)
				return self

		def get_doc(doctype_or_values, name=None):
			nonlocal created
			if doctype_or_values == "Fiskaly Register":
				return SimpleNamespace(provider_register_id="provider-register")
			if doctype_or_values == "Fiskaly API Connection":
				return SimpleNamespace(name=name)
			if isinstance(doctype_or_values, dict):
				created = AnchorReceipt(name="RECEIPT-ANCHOR", **doctype_or_values)
				return created
			raise AssertionError(f"Unexpected document lookup: {doctype_or_values} {name}")

		provider = SimpleNamespace(
			retrieve_receipt=lambda register_id, receipt_uuid: {
				"_id": receipt_uuid,
				"signed": True,
				"receipt_number": "17",
				"time_signature": "2026-08-18T10:30:00+02:00",
				"cash_register_serial_number": register_id,
				"qr_code_data": "_R1-AT1_payload_signature",
				"signature_value": "signature",
			},
		)
		module = "erpnext_fiskaly_sign_at.services.fiscalization"

		with (
			patch(f"{module}._lock_register_row"),
			patch(f"{module}.frappe.db.exists", return_value=False),
			patch(f"{module}.frappe.get_doc", side_effect=get_doc),
			patch(f"{module}.get_provider", return_value=provider),
			patch(f"{module}._update_pos_invoice") as update_pos,
		):
			result = reconcile_uncertain_receipt(anchor)

		self.assertEqual(result, {"status": "REMOTE_FOUND", "receipt": "RECEIPT-ANCHOR"})
		self.assertEqual(created.pos_invoice, "ACC-POS-INV-ROLLED-BACK")
		self.assertEqual(insert_kwargs, {"ignore_permissions": True, "ignore_links": True})
		update_pos.assert_not_called()

	def test_identical_terminal_resubmit_reuses_receipt_and_restores_pos_snapshot(self):
		invoice = SimpleNamespace(name="ACC-POS-INV-0001", company="Example Company")
		register = ReceiptStub(
			name="REGISTER-1",
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="TEST",
			active=1,
			initialized=1,
			provider_state="INITIALIZED",
		)
		receipt_uuid = _stable_receipt_uuid(invoice)
		fiscal_request = replace(request(), receipt_uuid=receipt_uuid)
		normalized = json.dumps(
			fiscal_request.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
		)
		wire = "{}"
		receipt = ReceiptStub(
			name="RECEIPT-1",
			receipt_uuid=receipt_uuid,
			idempotency_key=receipt_uuid,
			pos_invoice=invoice.name,
			company=invoice.company,
			register=register.name,
			connection=register.connection,
			provider=register.provider,
			environment=register.environment,
			receipt_kind="SALE",
			status="SIGNED",
			request_payload=normalized,
			payload_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
			provider_request_payload=wire,
			provider_request_payload_sha256=hashlib.sha256(wire.encode()).hexdigest(),
		)
		calls = []
		module = "erpnext_fiskaly_sign_at.services.fiscalization"

		with (
			patch(f"{module}.settings_enabled", return_value=True),
			patch(f"{module}.get_register", return_value=register),
			patch(f"{module}._lock_register_row"),
			patch(f"{module}.build_receipt_request", return_value=fiscal_request) as build,
			patch(f"{module}.frappe.db.get_value", return_value=receipt.name),
			patch(f"{module}.frappe.get_doc", return_value=receipt),
			patch(f"{module}._update_pos_invoice", side_effect=lambda doc: calls.append("snapshot")),
			patch(
				f"{module}.fiscalize_receipt",
				side_effect=lambda name: calls.append("fiscalize") or {"status": "SIGNED"},
			) as fiscalize,
		):
			result = create_outbox_receipt(invoice)

		self.assertEqual(result, {"status": "SIGNED"})
		self.assertEqual(calls, ["snapshot", "fiscalize"])
		build.assert_called_once_with(invoice, register, receipt_uuid)
		fiscalize.assert_called_once_with(receipt.name)

	def test_changed_resubmit_payload_is_rejected_before_provider_processing(self):
		invoice = SimpleNamespace(name="ACC-POS-INV-0001", company="Example Company")
		register = ReceiptStub(
			name="REGISTER-1",
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="TEST",
			active=1,
			initialized=1,
			provider_state="INITIALIZED",
		)
		receipt_uuid = _stable_receipt_uuid(invoice)
		original = replace(request(), receipt_uuid=receipt_uuid)
		changed = replace(original, total_gross=Decimal("13.00"))
		normalized = json.dumps(original.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
		receipt = ReceiptStub(
			name="RECEIPT-1",
			receipt_uuid=receipt_uuid,
			idempotency_key=receipt_uuid,
			pos_invoice=invoice.name,
			company=invoice.company,
			register=register.name,
			connection=register.connection,
			provider=register.provider,
			environment=register.environment,
			receipt_kind="SALE",
			request_payload=normalized,
			payload_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
			provider_request_payload="{}",
			provider_request_payload_sha256=hashlib.sha256(b"{}").hexdigest(),
		)
		module = "erpnext_fiskaly_sign_at.services.fiscalization"

		with (
			patch(f"{module}.settings_enabled", return_value=True),
			patch(f"{module}.get_register", return_value=register),
			patch(f"{module}._lock_register_row"),
			patch(f"{module}.build_receipt_request", return_value=changed),
			patch(f"{module}.frappe.db.get_value", return_value=receipt.name),
			patch(f"{module}.frappe.get_doc", return_value=receipt),
			patch(f"{module}._update_pos_invoice") as update_pos,
			patch(f"{module}.fiscalize_receipt") as fiscalize,
		):
			with self.assertRaises(PermanentFiskalyError) as raised:
				create_outbox_receipt(invoice)

		self.assertEqual(raised.exception.code, "E_FISCAL_PAYLOAD_DRIFT")
		update_pos.assert_not_called()
		fiscalize.assert_not_called()

	def test_resubmit_with_different_environment_binding_is_rejected(self):
		invoice = SimpleNamespace(name="ACC-POS-INV-0001", company="Example Company")
		register = ReceiptStub(
			name="REGISTER-1",
			connection="CONNECTION-1",
			provider="SIGN_AT_V1",
			environment="TEST",
			active=1,
			initialized=1,
			provider_state="INITIALIZED",
		)
		receipt_uuid = _stable_receipt_uuid(invoice)
		fiscal_request = replace(request(), receipt_uuid=receipt_uuid)
		normalized = json.dumps(
			fiscal_request.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
		)
		receipt = ReceiptStub(
			name="RECEIPT-1",
			receipt_uuid=receipt_uuid,
			idempotency_key=receipt_uuid,
			pos_invoice=invoice.name,
			company=invoice.company,
			register=register.name,
			connection=register.connection,
			provider=register.provider,
			environment="PRODUCTION",
			receipt_kind="SALE",
			request_payload=normalized,
			payload_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
			provider_request_payload="{}",
			provider_request_payload_sha256=hashlib.sha256(b"{}").hexdigest(),
		)
		module = "erpnext_fiskaly_sign_at.services.fiscalization"

		with (
			patch(f"{module}.settings_enabled", return_value=True),
			patch(f"{module}.get_register", return_value=register),
			patch(f"{module}._lock_register_row"),
			patch(f"{module}.build_receipt_request", return_value=fiscal_request),
			patch(f"{module}.frappe.db.get_value", return_value=receipt.name),
			patch(f"{module}.frappe.get_doc", return_value=receipt),
			patch(f"{module}.fiscalize_receipt") as fiscalize,
		):
			with self.assertRaises(PermanentFiskalyError) as raised:
				create_outbox_receipt(invoice)

		self.assertEqual(raised.exception.code, "E_RECEIPT_BINDING_MISMATCH")
		fiscalize.assert_not_called()

	def test_younger_receipt_cannot_overtake_offline_predecessor(self):
		normalized = json.dumps(request().as_dict(), sort_keys=True, separators=(",", ":"))
		wire = json.dumps(
			{
				"receipt_type": "NORMAL",
				"schema": {"raw": {"gross_amount_standard": "12.00"}},
			},
			sort_keys=True,
			separators=(",", ":"),
		)
		younger = ReceiptStub(
			name="RECEIPT-B",
			receipt_uuid=request().receipt_uuid,
			pos_invoice="ACC-POS-INV-0002",
			provider="SIGN_AT_V1",
			environment="TEST",
			register="REGISTER-1",
			receipt_kind="SALE",
			status="PREPARED",
			next_retry_at=None,
			attempt_count=0,
			request_payload=normalized,
			payload_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
			provider_request_payload=wire,
			provider_request_payload_sha256=hashlib.sha256(wire.encode()).hexdigest(),
			offline_receipt_snapshot=None,
			offline_qr_code_data=None,
		)
		predecessor = ReceiptStub(name="RECEIPT-A", status="OFFLINE_PENDING")
		older_row = SimpleNamespace(
			name=predecessor.name,
			status=predecessor.status,
			provider_receipt_id=None,
			qr_code_data=None,
		)
		register = SimpleNamespace(provider_register_id="provider-register")
		settings = SimpleNamespace(offline_notice=NOTICE, retry_interval_minutes=5)

		def get_doc(doctype, name):
			if doctype == "Fiskaly Receipt":
				return younger if name == younger.name else predecessor
			if doctype == "Fiskaly Register":
				return register
			raise AssertionError(f"Unexpected document lookup: {doctype} {name}")

		module = "erpnext_fiskaly_sign_at.services.fiscalization"
		with (
			patch(f"{module}._lock_register_row"),
			patch(f"{module}.frappe.get_doc", side_effect=get_doc),
			patch(f"{module}.frappe.get_cached_doc", return_value=settings),
			patch(f"{module}._older_receipts", return_value=[older_row]),
			patch(f"{module}._process_receipt", return_value=predecessor) as process_receipt,
			patch(f"{module}._update_pos_invoice"),
			patch(
				f"{module}._public_result",
				side_effect=lambda receipt: {"receipt": receipt.name, "status": receipt.status},
			),
		):
			result = fiscalize_receipt(younger.name)

		self.assertEqual(process_receipt.call_count, 1)
		self.assertEqual(process_receipt.call_args.args[0].name, predecessor.name)
		self.assertEqual(younger.status, "OFFLINE_PENDING")
		self.assertEqual(result["status"], "OFFLINE_PENDING")
		self.assertEqual(younger.offline_qr_code_data, NOTICE)
