from __future__ import annotations

import calendar
import hashlib
import hmac
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime, nowdate
from frappe.utils.file_manager import save_file

from erpnext_fiskaly_sign_at.providers import get_provider
from erpnext_fiskaly_sign_at.providers.errors import ProviderNotAvailableError


def _check_export_creation_permission(register: str, register_permission: str = "read"):
	"""Authorize DEP creation before exposing existing export metadata."""

	frappe.has_permission("Fiskaly DEP7 Export", "create", throw=True)
	register_doc = frappe.get_doc("Fiskaly Register", register)
	register_doc.check_permission(register_permission)
	return register_doc


def _as_utc_naive(value):
	stamp = get_datetime(value)
	if stamp.tzinfo:
		stamp = stamp.astimezone(UTC).replace(tzinfo=None)
	return stamp


def _validate_final_decommission_export(
	register,
	*,
	export_created_at=None,
	export_generated_at=None,
):
	"""Require a provider-closed register and chronologically later final evidence."""

	register_doc = frappe.get_doc("Fiskaly Register", register) if isinstance(register, str) else register
	if register_doc.provider_state != "DECOMMISSIONED" or not register_doc.closing_receipt:
		frappe.throw(_("A final DEP7 backup requires the provider-validated closing receipt first."))
	if register_doc.decommission_status != "EVIDENCE_REQUIRED":
		frappe.throw(_("A new final DEP7 backup is allowed only while decommissioning evidence is required."))
	closing = frappe.db.get_value(
		"Fiskaly Receipt",
		{
			"name": register_doc.closing_receipt,
			"register": register_doc.name,
			"receipt_kind": "CLOSING",
		},
		[
			"creation",
			"status",
			"fon_validation_status",
			"signed_at",
			"fon_validation_at",
			"print_evidence_at",
			"print_evidence_file",
		],
		as_dict=True,
	)
	if (
		not closing
		or closing.status not in {"SIGNED", "SUBSTITUTE_SIGNED"}
		or closing.fon_validation_status != "SUCCESS"
		or not closing.signed_at
		or not closing.creation
	):
		frappe.throw(_("A final DEP7 backup requires a signed, FON-validated closing receipt."))
	decommissioned_at = frappe.db.get_value(
		"Fiskaly Lifecycle Event",
		{
			"register": register_doc.name,
			"event_type": "REGISTER_DECOMMISSIONED",
			"resulting_state": "DECOMMISSIONED",
		},
		"event_time",
		order_by="event_time desc",
	)
	if not decommissioned_at:
		frappe.throw(_("The provider decommissioning event is missing; final DEP7 creation is blocked."))
	# Both values are local database timestamps from the same clock. The lifecycle
	# event is appended only after the provider/FON closing evidence was stored, so
	# this avoids comparing provider timestamps with naive Frappe database times.
	anchor = max(_as_utc_naive(closing.creation), _as_utc_naive(decommissioned_at))
	for label, value in (
		(_("creation"), export_created_at),
		(_("generation"), export_generated_at),
	):
		if value and _as_utc_naive(value) < anchor:
			frappe.throw(
				_(
					"The final DEP7 {0} predates the validated closing receipt or decommissioning event."
				).format(label)
			)
	return {"register": register_doc, "closing": closing, "anchor": anchor}


def _validate_period(date_from, date_to):
	if not date_from and not date_to:
		return None, None
	if not date_from or not date_to:
		frappe.throw(_("DEP7 period exports require both start and end."))
	start = get_datetime(date_from)
	end = get_datetime(date_to)
	if start > end:
		frappe.throw(_("DEP7 export start must not be after its end."))
	return start, end


@frappe.whitelist(methods=["POST"])
def create_dep7_export(
	register: str,
	date_from: str | None = None,
	date_to: str | None = None,
	purpose: str = "MANUAL",
	fiscal_year: int | None = None,
	fiscal_quarter: int | None = None,
):
	register_doc = _check_export_creation_permission(register)
	start, end = _validate_period(date_from, date_to)
	purpose = (purpose or "MANUAL").upper()
	if purpose not in {"MANUAL", "AUDIT", "QUARTERLY_BACKUP", "DECOMMISSION_FINAL"}:
		frappe.throw(_("Unsupported DEP7 export purpose."))
	if purpose in {"QUARTERLY_BACKUP", "DECOMMISSION_FINAL"} and (start or end):
		frappe.throw(_("Compliance DEP7 backups must always be complete snapshots."))
	if purpose == "DECOMMISSION_FINAL":
		if fiscal_year or fiscal_quarter:
			frappe.throw(_("A final decommissioning DEP7 cannot carry a fiscal period."))
		return create_final_decommission_export(register)
	return _create_dep7_export_doc(
		register_doc,
		start=start,
		end=end,
		purpose=purpose,
		fiscal_year=fiscal_year,
		fiscal_quarter=fiscal_quarter,
	)


def _create_dep7_export_doc(
	register_doc,
	*,
	start=None,
	end=None,
	purpose="MANUAL",
	fiscal_year=None,
	fiscal_quarter=None,
):
	doc = frappe.get_doc(
		{
			"doctype": "Fiskaly DEP7 Export",
			"register": register_doc.name,
			"connection": register_doc.connection,
			"purpose": purpose,
			"export_scope": "PERIOD" if start and end else "COMPLETE",
			"fiscal_year": fiscal_year,
			"fiscal_quarter": fiscal_quarter,
			"date_from": start,
			"date_to": end,
			"status": "QUEUED",
		}
	).insert()
	frappe.enqueue(
		"erpnext_fiskaly_sign_at.api.exports.process_dep7_export",
		queue="long",
		export_name=doc.name,
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"fiskaly-dep7-{doc.name}",
	)
	return doc.name


@frappe.whitelist(methods=["POST"])
def create_complete_dep7_export(register: str, purpose: str = "AUDIT"):
	return create_dep7_export(register, purpose=purpose)


def _quarter_bounds(year: int, quarter: int) -> tuple[datetime, datetime]:
	if quarter not in {1, 2, 3, 4}:
		frappe.throw(_("Fiscal quarter must be between 1 and 4."))
	start_month = (quarter - 1) * 3 + 1
	end_month = start_month + 2
	last_day = calendar.monthrange(year, end_month)[1]
	return datetime(year, start_month, 1), datetime(year, end_month, last_day, 23, 59, 59)


@frappe.whitelist(methods=["POST"])
def create_quarterly_backup(register: str, fiscal_year: int, fiscal_quarter: int):
	_check_export_creation_permission(register)
	year = int(fiscal_year)
	quarter = int(fiscal_quarter)
	_start, end = _quarter_bounds(year, quarter)
	if end > now_datetime():
		frappe.throw(_("A quarterly DEP7 backup can only be created after the quarter has ended."))
	existing = frappe.db.get_value(
		"Fiskaly DEP7 Export",
		{
			"register": register,
			"purpose": "QUARTERLY_BACKUP",
			"fiscal_year": year,
			"fiscal_quarter": quarter,
			"status": ["in", ["QUEUED", "PROCESSING", "READY"]],
		},
		"name",
	)
	if existing:
		return existing
	# The RKSV requires a complete DEP backup at least quarterly. Keep the year
	# and quarter as evidence of when the snapshot was taken, but deliberately do
	# not limit the provider export to that three-month period. A complete snapshot
	# also captures receipts replayed after an outage in a later quarter.
	return create_dep7_export(
		register,
		purpose="QUARTERLY_BACKUP",
		fiscal_year=year,
		fiscal_quarter=quarter,
	)


@frappe.whitelist(methods=["POST"])
def create_final_decommission_export(register: str):
	from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

	register_doc = _check_export_creation_permission(register, register_permission="write")
	_lock_register_row(register)
	register_doc.reload()
	if register_doc.decommission_status == "COMPLETE":
		linked = frappe.db.get_value(
			"Fiskaly DEP7 Export",
			{
				"name": register_doc.final_dep7_export,
				"register": register,
				"purpose": "DECOMMISSION_FINAL",
				"status": "READY",
			},
			"name",
		)
		if linked:
			return linked
		frappe.throw(_("The completed register has no valid linked final DEP7 evidence."))
	preconditions = _validate_final_decommission_export(register_doc)
	existing = frappe.db.get_value(
		"Fiskaly DEP7 Export",
		{
			"register": register,
			"purpose": "DECOMMISSION_FINAL",
			"status": ["in", ["QUEUED", "PROCESSING", "READY"]],
			"creation": [">=", preconditions["anchor"]],
		},
		[
			"name",
			"status",
			"creation",
			"generated_at",
			"export_file",
			"file_hash",
			"supplementary_export_file",
			"supplementary_file_hash",
		],
		as_dict=True,
		order_by="creation desc",
	)
	if existing:
		if existing.status == "READY" and not all(
			(
				existing.generated_at,
				existing.export_file,
				existing.file_hash,
				existing.supplementary_export_file,
				existing.supplementary_file_hash,
			)
		):
			existing = None
		else:
			try:
				_validate_final_decommission_export(
					register_doc,
					export_created_at=existing.creation,
					export_generated_at=existing.generated_at if existing.status == "READY" else None,
				)
			except frappe.ValidationError:
				existing = None
	if not existing:
		controlled = getattr(frappe.flags, "in_final_decommission_export_creation", False)
		frappe.flags.in_final_decommission_export_creation = True
		try:
			existing_name = _create_dep7_export_doc(
				register_doc,
				purpose="DECOMMISSION_FINAL",
			)
		finally:
			frappe.flags.in_final_decommission_export_creation = controlled
	else:
		existing_name = existing.name
	register_doc.db_set("final_dep7_export", existing_name, update_modified=False)
	return existing_name


def _next_quarter_due(year: int, quarter: int):
	if quarter == 4:
		year += 1
		quarter = 1
	else:
		quarter += 1
	_, end = _quarter_bounds(year, quarter)
	return end.date()


def _build_supplementary_receipt_items(register: str, date_from=None, date_to=None) -> dict:
	"""Export immutable receipt-item evidence not contained in the DEP7 schema.

	The source is the SHA-256 protected request snapshot, not the mutable current
	state of the POS Invoice child table. This matters for mixed payments because
	the provider entries are aliquoted to the cash share while the statutory
	receipt still needs the actual sold quantity and customary description.
	"""
	filters = {
		"register": register,
		"pos_invoice": ["is", "set"],
		"status": ["in", ["SIGNED", "SUBSTITUTE_SIGNED", "OFFLINE_PENDING"]],
	}
	receipts = frappe.get_all(
		"Fiskaly Receipt",
		filters=filters,
		fields=[
			"receipt_uuid",
			"provider_receipt_id",
			"receipt_number",
			"signed_at",
			"offline_issued_at",
			"status",
			"pos_invoice",
			"request_payload",
			"payload_sha256",
		],
		order_by="signed_at asc, receipt_number asc",
	)

	def local_naive(value):
		stamp = get_datetime(value)
		if stamp.tzinfo:
			stamp = stamp.astimezone(ZoneInfo("Europe/Vienna")).replace(tzinfo=None)
		return stamp

	period_start = local_naive(date_from) if date_from else None
	period_end = local_naive(date_to) if date_to else None
	evidence = []
	for row in receipts:
		request_payload = row.request_payload or ""
		actual_hash = hashlib.sha256(request_payload.encode("utf-8")).hexdigest()
		if not row.payload_sha256 or not hmac.compare_digest(actual_hash, row.payload_sha256):
			frappe.throw(
				_("Receipt {0} failed its immutable payload check; DEP supplement was not created.").format(
					row.receipt_uuid
				)
			)
		request = json.loads(request_payload)
		evidence_time = row.signed_at or row.offline_issued_at or request.get("issued_at")
		if not evidence_time:
			frappe.throw(_("Receipt {0} has no auditable issue time.").format(row.receipt_uuid))
		stamp = local_naive(evidence_time)
		if period_start and period_end and not period_start <= stamp <= period_end:
			continue
		source_entries = request.get("source_entries") or request.get("entries") or []
		evidence.append(
			{
				"receipt_uuid": row.receipt_uuid,
				"provider_receipt_id": row.provider_receipt_id,
				"receipt_number": str(row.receipt_number) if row.receipt_number is not None else None,
				"signed_at": str(row.signed_at) if row.signed_at else None,
				"offline_issued_at": str(row.offline_issued_at) if row.offline_issued_at else None,
				"status": row.status,
				"erp_pos_invoice": row.pos_invoice,
				"request_payload_sha256": row.payload_sha256,
				"items": [
					{
						"line_number": index,
						"quantity": str(item.get("quantity")),
						"uom": item.get("uom"),
						"item_code": item.get("code"),
						"item_name": item.get("label"),
						"description": item.get("description") or item.get("label"),
					}
					for index, item in enumerate(source_entries, start=1)
				],
			}
		)
	return {
		"format": "RKSV_SUPPLEMENTARY_RECEIPT_ITEMS",
		"version": 1,
		"register": register,
		"date_from": str(date_from) if date_from else None,
		"date_to": str(date_to) if date_to else None,
		"receipts": evidence,
	}


def _retention_until(generated_at) -> date:
	"""Return the end of the seventh following calendar year (BAO retention)."""
	generated = getdate(generated_at)
	return date(generated.year + 7, 12, 31)


def _save_private_json(filename: str, value: dict, attached_to_doctype: str, attached_to_name: str):
	payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
	file_doc = save_file(filename, payload, attached_to_doctype, attached_to_name, is_private=1)
	return file_doc, payload, hashlib.sha256(payload).hexdigest()


def process_dep7_export(export_name: str):
	locked = frappe.db.sql(
		"select status from `tabFiskaly DEP7 Export` where name = %s for update",
		(export_name,),
		as_dict=True,
	)
	if not locked:
		return {"status": "MISSING", "export": export_name}
	doc = frappe.get_doc("Fiskaly DEP7 Export", export_name)
	if doc.status in {"READY", "ACTION_REQUIRED"}:
		return {"status": doc.status, "export": doc.name, "idempotent": True}
	if doc.status == "PROCESSING":
		# A second worker waited on the row lock and must never repeat the remote
		# export or replace already generated evidence. Truly abandoned rows are
		# requeued by ``reconcile_processing_exports`` after a conservative lease.
		return {"status": "PROCESSING", "export": doc.name, "idempotent": True}
	if doc.status != "QUEUED":
		return {"status": doc.status, "export": doc.name, "idempotent": True}
	if doc.purpose == "DECOMMISSION_FINAL":
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		_lock_register_row(doc.register)
		try:
			_validate_final_decommission_export(doc.register, export_created_at=doc.creation)
		except frappe.ValidationError as exc:
			message = str(exc)[:1000]
			doc.db_set(
				{
					"status": "ACTION_REQUIRED",
					"action_required_reason": message,
					"last_error": message,
				}
			)
			return {"status": "ACTION_REQUIRED", "export": doc.name}
	doc.db_set({"status": "PROCESSING", "last_error": None, "action_required_reason": None})
	frappe.db.savepoint("fiskaly_dep7_generation")
	try:
		register = frappe.get_doc("Fiskaly Register", doc.register)
		connection = frappe.get_doc("Fiskaly API Connection", doc.connection)
		result = get_provider(connection).export_dep7(register, doc.date_from, doc.date_to)
		if connection.provider != "SIGN_AT_V1":
			raise ProviderNotAvailableError(
				"A verified DEP7 file contract is not available for this provider version."
			)
		file_name = f"DEP7-{frappe.scrub(doc.register)}-{doc.name}.json"
		file_doc, payload, file_hash = _save_private_json(file_name, result, doc.doctype, doc.name)
		supplementary = _build_supplementary_receipt_items(doc.register, doc.date_from, doc.date_to)
		supplementary_name = f"DEP7-SUPPLEMENT-{frappe.scrub(doc.register)}-{doc.name}.json"
		supplementary_file, supplementary_payload, supplementary_hash = _save_private_json(
			supplementary_name, supplementary, doc.doctype, doc.name
		)
		generated_at = now_datetime()
		metadata = {
			"format": "DEP7_JSON",
			"sha256": file_hash,
			"size": len(payload),
			"receipt_group_count": len(result.get("Belege-Gruppe", [])),
			"date_from": str(doc.date_from),
			"date_to": str(doc.date_to),
			"supplementary_format": supplementary["format"],
			"supplementary_receipt_count": len(supplementary["receipts"]),
			"supplementary_sha256": supplementary_hash,
		}
		doc.db_set(
			{
				"status": "READY",
				"generated_at": generated_at,
				"retention_until": _retention_until(generated_at),
				"export_file": file_doc.file_url,
				"file_hash": file_hash,
				"file_size": len(payload),
				"supplementary_export_file": supplementary_file.file_url,
				"supplementary_file_hash": supplementary_hash,
				"supplementary_file_size": len(supplementary_payload),
				"provider_response": json.dumps(metadata, ensure_ascii=False),
				"action_required_reason": (
					"Copy the complete DEP7 and supplementary receipt-item files to an external medium "
					"and record the immutable storage reference."
					if doc.purpose in {"QUARTERLY_BACKUP", "DECOMMISSION_FINAL"}
					else None
				),
				"last_error": None,
			}
		)
		if doc.purpose == "QUARTERLY_BACKUP":
			frappe.db.set_value(
				"Fiskaly Register",
				doc.register,
				{
					"last_quarterly_backup_at": generated_at,
					"next_quarterly_backup_due": _next_quarter_due(
						int(doc.fiscal_year), int(doc.fiscal_quarter)
					),
				},
				update_modified=False,
			)
		return {"status": "READY", "export": doc.name}
	except ProviderNotAvailableError as exc:
		frappe.db.rollback(save_point="fiskaly_dep7_generation")
		doc.reload()
		doc.db_set(
			{
				"status": "ACTION_REQUIRED",
				"action_required_reason": str(exc)[:1000],
				"last_error": str(exc)[:1000],
			}
		)
		return {"status": "ACTION_REQUIRED", "export": doc.name}
	except Exception as exc:
		frappe.db.rollback(save_point="fiskaly_dep7_generation")
		doc.reload()
		doc.db_set({"status": "FAILED", "last_error": str(exc)[:1000]})
		frappe.log_error(title=f"Fiskaly DEP7 export {doc.name}", message=frappe.get_traceback())
		return {"status": "FAILED", "export": doc.name}


def _last_completed_quarter(as_of) -> tuple[int, int]:
	as_of = getdate(as_of)
	quarter = (as_of.month - 1) // 3 + 1
	if quarter == 1:
		return as_of.year - 1, 4
	return as_of.year, quarter - 1


def check_quarterly_backup_requirements(as_of=None) -> list[dict]:
	"""Return missing external backup evidence; safe for a daily scheduler hook."""
	year, quarter = _last_completed_quarter(as_of or nowdate())
	issues = []
	for register in frappe.get_all(
		"Fiskaly Register",
		filters={"active": 1, "initialized": 1, "environment": "LIVE"},
		pluck="name",
	):
		export = frappe.db.get_value(
			"Fiskaly DEP7 Export",
			{
				"register": register,
				"purpose": "QUARTERLY_BACKUP",
				"fiscal_year": year,
				"fiscal_quarter": quarter,
				"status": "READY",
			},
			["name", "external_copy_confirmed"],
			as_dict=True,
		)
		if not export:
			issues.append(
				{
					"register": register,
					"fiscal_year": year,
					"fiscal_quarter": quarter,
					"issue": "MISSING_EXPORT",
				}
			)
		elif not export.external_copy_confirmed:
			issues.append(
				{
					"register": register,
					"export": export.name,
					"fiscal_year": year,
					"fiscal_quarter": quarter,
					"issue": "EXTERNAL_COPY_UNCONFIRMED",
				}
			)
	return issues


def check_decommission_requirements() -> list[dict]:
	"""Keep provider-closed registers visible until every local archive step is complete."""

	issues = []
	for row in frappe.get_all(
		"Fiskaly Register",
		filters={"decommission_status": "EVIDENCE_REQUIRED"},
		fields=["name", "closing_receipt", "final_dep7_export"],
		limit_page_length=0,
	):
		closing = (
			frappe.db.get_value(
				"Fiskaly Receipt",
				row.closing_receipt,
				["print_evidence_at", "print_evidence_file"],
				as_dict=True,
			)
			if row.closing_receipt
			else None
		)
		export = (
			frappe.db.get_value(
				"Fiskaly DEP7 Export",
				row.final_dep7_export,
				[
					"status",
					"integrity_verified_at",
					"supplementary_integrity_verified_at",
					"external_copy_confirmed",
				],
				as_dict=True,
			)
			if row.final_dep7_export
			else None
		)
		missing = []
		if not closing or not closing.print_evidence_at or not closing.print_evidence_file:
			missing.append("CLOSING_RECEIPT_ARCHIVE")
		if not export or export.status != "READY":
			missing.append("FINAL_DEP7")
		elif not (
			export.integrity_verified_at
			and export.supplementary_integrity_verified_at
			and export.external_copy_confirmed
		):
			missing.append("FINAL_DEP7_EXTERNAL_EVIDENCE")
		if missing:
			issues.append({"register": row.name, "missing": missing})
	return issues


def ensure_quarterly_backups(as_of=None) -> dict[str, list[str]]:
	"""Idempotently queue private DEP7 files for the last completed LIVE quarter."""
	year, quarter = _last_completed_quarter(as_of or nowdate())
	created = []
	existing = []
	for register in frappe.get_all(
		"Fiskaly Register",
		filters={
			"active": 1,
			"initialized": 1,
			"environment": "LIVE",
			"provider": "SIGN_AT_V1",
		},
		pluck="name",
	):
		name = frappe.db.get_value(
			"Fiskaly DEP7 Export",
			{
				"register": register,
				"purpose": "QUARTERLY_BACKUP",
				"fiscal_year": year,
				"fiscal_quarter": quarter,
				"status": ["in", ["QUEUED", "PROCESSING", "READY"]],
			},
			"name",
		)
		if name:
			existing.append(name)
			continue
		name = create_quarterly_backup(register, year, quarter)
		created.append(name)
	return {"created": created, "existing": existing}


def reconcile_processing_exports():
	"""Recover abandoned export leases without allowing duplicate file generation."""
	cutoff = add_to_date(now_datetime(), hours=-2)
	requeued = []
	action_required = []
	for name in frappe.get_all(
		"Fiskaly DEP7 Export",
		filters={"status": "PROCESSING", "modified": ["<=", cutoff]},
		pluck="name",
		limit=100,
	):
		frappe.db.sql(
			"select name from `tabFiskaly DEP7 Export` where name = %s for update",
			(name,),
		)
		doc = frappe.get_doc("Fiskaly DEP7 Export", name)
		if doc.status != "PROCESSING" or get_datetime(doc.modified) > get_datetime(cutoff):
			continue
		connection = frappe.get_doc("Fiskaly API Connection", doc.connection)
		if connection.provider == "SIGN_AT_UNIFIED":
			doc.db_set(
				{
					"status": "ACTION_REQUIRED",
					"action_required_reason": (
						"Unified SIGN AT has no released, verified Austrian DEP7 poll/download contract."
					),
				}
			)
			action_required.append(name)
			continue
		doc.db_set(
			{
				"status": "QUEUED",
				"last_error": "Abandoned PROCESSING lease was requeued safely.",
			}
		)
		frappe.enqueue(
			"erpnext_fiskaly_sign_at.api.exports.process_dep7_export",
			queue="long",
			export_name=name,
			enqueue_after_commit=True,
			deduplicate=True,
			job_id=f"fiskaly-dep7-{name}",
		)
		requeued.append(name)
	return {"requeued": requeued, "action_required": action_required}
