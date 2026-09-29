from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
	add_to_date,
	flt,
	get_datetime,
	get_system_timezone,
	get_time,
	getdate,
	now_datetime,
	nowdate,
)

from erpnext_fiskaly_sign_at.providers import get_provider
from erpnext_fiskaly_sign_at.providers.errors import (
	FiskalyError,
	PermanentFiskalyError,
	ProviderNotAvailableError,
)
from erpnext_fiskaly_sign_at.time_utils import provider_datetime_for_db

AUTOMATIC_RECEIPT_TYPES = (
	"INITIALIZATION",
	"DECOMMISSION",
	"MONTHLY_CLOSE",
	"YEARLY_CLOSE",
	"SIGNATURE_CREATION_UNIT_FAULT_CLEARANCE",
)
FON_REQUIRED_RECEIPT_TYPES = {"INITIALIZATION", "YEARLY_CLOSE", "DECOMMISSION"}
RECEIPT_KIND_BY_PROVIDER_TYPE = {
	"INITIALIZATION": "START",
	"DECOMMISSION": "CLOSING",
	"MONTHLY_CLOSE": "MONTHLY",
	"YEARLY_CLOSE": "YEARLY",
	"SIGNATURE_CREATION_UNIT_FAULT_CLEARANCE": "RECOVERY",
}
OPEN_RECEIPT_STATUSES = ("PREPARED", "SIGNING", "OFFLINE_PENDING", "RETRYING", "ACTION_REQUIRED")
PROTECTED_INITIALIZED_FIELDS = (
	"active",
	"company",
	"pos_profile",
	"connection",
	"provider",
	"environment",
	"location_address",
	"scu_assignment_mode",
	"taxpayer_id",
	"location_id",
	"signature_creation_unit_id",
	"provider_register_id",
	"serial_number",
	"provider_state",
	"provider_mode",
	"initialized",
	"start_receipt",
	"start_receipt_fon_status",
	"last_lifecycle_sync_at",
	"outage_status",
	"outage_scope",
	"outage_reference",
	"outage_started_at",
	"outage_deadline_at",
	"outage_ended_at",
	"outage_reason",
	"fon_outage_status",
	"fon_outage_begin_reference",
	"fon_outage_begin_reported_at",
	"fon_outage_end_reference",
	"fon_outage_end_reported_at",
	"decommission_status",
	"closing_receipt",
	"final_dep7_export",
	"last_closing_sync_at",
	"last_quarterly_backup_at",
	"next_quarterly_backup_due",
	"provider_data",
)


def _protected_field_value(fieldtype: str | None, value):
	"""Normalize browser and database representations before lifecycle comparison."""
	if value in (None, ""):
		return None
	if fieldtype == "Datetime":
		return get_datetime(value) or value
	if fieldtype == "Date":
		return getdate(value)
	if fieldtype == "Time":
		return get_time(value)
	return value


@frappe.whitelist(methods=["GET"])
def get_available_scus(connection: str, company: str) -> list[dict[str, str]]:
	"""Return selectable SIGN AT v1 SCUs without exposing provider credentials."""
	if not connection or not company:
		frappe.throw(_("API connection and company are required to load existing SCUs."))
	connection_doc = frappe.get_doc("Fiskaly API Connection", connection)
	connection_doc.check_permission("read")
	if connection_doc.company != company:
		frappe.throw(_("The selected API connection belongs to a different company."))
	if connection_doc.provider != "SIGN_AT_V1":
		return []

	provider = get_provider(connection_doc)
	scus = provider.list_selectable_scus(frappe._dict(company=company))
	return [
		{
			"id": scu["_id"],
			"state": scu["state"],
			"legal_entity_name": scu.get("legal_entity_name") or company,
		}
		for scu in scus
	]


def _is_active_scu_limit_error(error: PermanentFiskalyError) -> bool:
	code = (error.code or "").upper()
	if code in {"E_SCU_LIMIT_REACHED", "E_ACTIVE_SCU_LIMIT_REACHED"}:
		return True
	message = str(error).upper()
	return (
		"LIMIT" in message
		and "ACTIVE" in message
		and ("SIGNATURE CREATION UNIT" in message or "SCU" in message)
	)


def _raise_actionable_scu_limit_error(connection, error: PermanentFiskalyError) -> None:
	if not _is_active_scu_limit_error(error):
		return
	environment = frappe.utils.escape_html(connection.environment or "TEST")
	frappe.throw(
		_(
			"Fiskaly erlaubt für diesen {0}-Mandanten derzeit nur eine aktive "
			"Signaturerstellungseinheit (SCU). Es besteht bereits eine SCU im Status "
			"INITIALIZED oder OUTAGE."
		).format(frappe.bold(environment))
		+ _(
			"<p><strong>So geht es weiter:</strong></p>"
			"<ol>"
			"<li>Stellen Sie bei „SCU-Bereitstellung“ auf „Bestehende SCU auswählen“.</li>"
			"<li>Wählen Sie die dort angezeigte aktive SCU.</li>"
			"<li>Speichern und provisionieren Sie die Kasse erneut.</li>"
			"</ol>"
			"<p>Eine neue SCU kann erst erstellt werden, nachdem die bisherige SCU "
			"außer Betrieb genommen wurde. Das ist nur zulässig, wenn keine andere "
			"aktive Kasse diese SCU mehr verwendet.</p>"
		),
		title=_("Keine weitere aktive SCU möglich"),
	)


def _decommission_workflow_state(register_doc) -> dict:
	closing = (
		frappe.db.get_value(
			"Fiskaly Receipt",
			{
				"name": register_doc.closing_receipt,
				"register": register_doc.name,
				"receipt_kind": "CLOSING",
			},
			[
				"name",
				"status",
				"fon_validation_status",
				"print_evidence_at",
				"print_evidence_file",
			],
			as_dict=True,
		)
		if register_doc.closing_receipt
		else None
	)
	export = (
		frappe.db.get_value(
			"Fiskaly DEP7 Export",
			{
				"name": register_doc.final_dep7_export,
				"register": register_doc.name,
				"purpose": "DECOMMISSION_FINAL",
			},
			[
				"name",
				"status",
				"export_file",
				"file_hash",
				"supplementary_export_file",
				"supplementary_file_hash",
				"integrity_verified_at",
				"supplementary_integrity_verified_at",
				"external_copy_confirmed",
				"external_storage_reference",
				"action_required_reason",
				"last_error",
			],
			as_dict=True,
		)
		if register_doc.final_dep7_export
		else None
	)
	closing_ready = bool(
		closing
		and closing.status in {"SIGNED", "SUBSTITUTE_SIGNED"}
		and closing.fon_validation_status == "SUCCESS"
	)
	closing_archived = bool(closing_ready and closing.print_evidence_at and closing.print_evidence_file)
	export_ready = bool(
		export
		and export.status == "READY"
		and export.export_file
		and export.file_hash
		and export.supplementary_export_file
		and export.supplementary_file_hash
	)
	integrity_verified = bool(
		export_ready and export.integrity_verified_at and export.supplementary_integrity_verified_at
	)
	external_copy_confirmed = bool(
		integrity_verified and export.external_copy_confirmed and export.external_storage_reference
	)
	return {
		"register": register_doc.name,
		"provider_state": register_doc.provider_state,
		"decommission_status": register_doc.decommission_status,
		"closing_receipt": dict(closing) if closing else None,
		"closing_ready": closing_ready,
		"closing_archived": closing_archived,
		"final_dep7_export": dict(export) if export else None,
		"export_ready": export_ready,
		"integrity_verified": integrity_verified,
		"external_copy_confirmed": external_copy_confirmed,
		"complete": register_doc.decommission_status == "COMPLETE",
	}


@frappe.whitelist(methods=["GET"])
def get_decommission_workflow(register: str) -> dict:
	register_doc = frappe.get_doc("Fiskaly Register", register)
	register_doc.check_permission("read")
	return _decommission_workflow_state(register_doc)


@frappe.whitelist(methods=["GET"])
def get_terminal_status_summary(register: str) -> dict:
	"""Return a safe operator-facing summary for a closed or defective register."""
	register_doc = frappe.get_doc("Fiskaly Register", register)
	register_doc.check_permission("read")
	if register_doc.provider_state == "DEFECTIVE":
		kind = "DEFECTIVE"
		event_type = "REGISTER_DEFECTIVE"
		label = _("Dauerhaft defekt")
		status_detail = _("Diese Kasse wurde dauerhaft als defekt gemeldet und ist nicht mehr aktiv.")
	elif register_doc.provider_state == "DECOMMISSIONED" or register_doc.decommission_status in {
		"EVIDENCE_REQUIRED",
		"COMPLETE",
	}:
		kind = "DECOMMISSIONED"
		event_type = "REGISTER_DECOMMISSIONED"
		label = _("Außer Betrieb")
		status_detail = (
			_(
				"Die Kasse ist außer Betrieb; die gesetzlich erforderlichen Abschlussnachweise sind vollständig."
			)
			if register_doc.decommission_status == "COMPLETE"
			else _(
				"Fiskaly hat die Kasse geschlossen; Schlussbeleg oder finale DEP7-Nachweise sind noch offen."
			)
		)
	else:
		return {"terminal": False}

	event = frappe.db.get_value(
		"Fiskaly Lifecycle Event",
		{"register": register_doc.name, "event_type": event_type},
		["name", "register", "event_time", "actor", "reason", "action_required_reason"],
		as_dict=True,
		order_by="event_time desc",
	)
	reason = None
	if event:
		reason = (event.reason or event.action_required_reason or "").strip() or None
	return {
		"terminal": True,
		"kind": kind,
		"label": label,
		"status_detail": status_detail,
		"reason": reason,
		"event": dict(event) if event else None,
		"decommission_status": register_doc.decommission_status,
	}


class FiskalyRegister(Document):
	def validate(self):
		connection = frappe.get_doc("Fiskaly API Connection", self.connection)
		if connection.company != self.company:
			frappe.throw(_("The register company must match the API connection company."))
		company_currency = frappe.db.get_value("Company", self.company, "default_currency")
		if company_currency != "EUR":
			frappe.throw(_("An Austrian RKSV register requires company default currency EUR."))
		profile_company = frappe.db.get_value("POS Profile", self.pos_profile, "company")
		if profile_company != self.company:
			frappe.throw(_("The POS Profile must belong to the register company."))
		if not self.location_address:
			frappe.throw(_("A company location address is required for statutory receipt snapshots."))
		address = frappe.get_doc("Address", self.location_address)
		if not address.has_link("Company", self.company):
			frappe.throw(
				_("The location address must be linked to register company {0}.").format(self.company)
			)

		self.provider = connection.provider
		self.environment = connection.environment
		if connection.provider == "SIGN_AT_V1":
			if self.scu_assignment_mode not in {"EXISTING", "NEW"}:
				frappe.throw(_("Choose whether an existing SCU is used or a new SCU is created."))
			if (
				self.scu_assignment_mode == "NEW"
				and self.signature_creation_unit_id
				and not self.provider_register_id
			):
				frappe.throw(_("A new SCU cannot be combined with an existing SCU selection."))
		if self.environment == "LIVE" and get_system_timezone() != "Europe/Vienna":
			frappe.throw(_("A LIVE Austrian RKSV register requires site timezone Europe/Vienna."))

		if self.is_new() and not self.vat_mappings:
			for rate, bucket in (
				(20, "standard"),
				(10, "reduced1"),
				(13, "reduced2"),
				(19, "special"),
				(4.9, "special"),
				(0, "zero"),
			):
				self.append("vat_mappings", {"tax_rate": rate, "v1_bucket": bucket})

		duplicate = frappe.db.exists(
			"Fiskaly Register",
			{
				"company": self.company,
				"pos_profile": self.pos_profile,
				"environment": self.environment,
				"active": 1,
				"name": ["!=", self.name],
			},
		)
		if duplicate and self.active:
			frappe.throw(
				_("An active register already exists for this company, POS Profile, and environment.")
			)

		rates = [round(flt(row.tax_rate), 6) for row in self.vat_mappings]
		if len(set(rates)) != len(rates):
			frappe.throw(_("Each VAT rate may only be mapped once."))
		for mapping in self.vat_mappings:
			rate = round(flt(mapping.tax_rate), 6)
			if connection.provider == "SIGN_AT_V1":
				official_bucket = {
					20.0: "standard",
					10.0: "reduced1",
					13.0: "reduced2",
					19.0: "special",
					4.9: "special",
					0.0: "zero",
				}.get(rate)
				if official_bucket and mapping.v1_bucket != official_bucket:
					frappe.throw(
						_("Tax rate {0}% must use the RKSV v1 bucket {1}.").format(
							mapping.tax_rate, official_bucket
						)
					)
				if mapping.is_exempt and mapping.v1_bucket != "zero":
					frappe.throw(_("Tax-exempt revenue must use the RKSV v1 zero bucket."))
			if mapping.tax_account:
				account = frappe.db.get_value(
					"Account", mapping.tax_account, ["company", "is_group"], as_dict=True
				)
				if not account or account.company != self.company or account.is_group:
					frappe.throw(
						_("VAT mapping account {0} must be a ledger account of company {1}.").format(
							mapping.tax_account, self.company
						)
					)

		if self.initialized and not self.provider_register_id:
			frappe.throw(_("An initialized register requires a provider register/system ID."))
		if not self.is_new():
			old = self.get_doc_before_save()
			if old and (old.initialized or old.provider_register_id or old.start_receipt):
				for fieldname in PROTECTED_INITIALIZED_FIELDS:
					field = self.meta.get_field(fieldname)
					fieldtype = field.fieldtype if field else None
					if _protected_field_value(fieldtype, old.get(fieldname)) != _protected_field_value(
						fieldtype, self.get(fieldname)
					):
						frappe.throw(
							_(
								"Initialized RKSV register field {0} is controlled by the fiscal lifecycle and cannot be changed directly."
							).format(self.meta.get_label(fieldname) or fieldname)
						)

	def on_trash(self):
		if (
			self.provider_register_id
			or self.start_receipt
			or frappe.db.exists("Fiskaly Receipt", {"register": self.name})
		):
			frappe.throw(
				_(
					"A provisioned RKSV register and its receipt chain cannot be deleted. "
					"Use the controlled decommissioning action."
				)
			)

	def _validate_austrian_runtime(self):
		if get_system_timezone() != "Europe/Vienna":
			frappe.throw(_("Provisioning requires site timezone Europe/Vienna."))
		if frappe.db.get_value("Company", self.company, "default_currency") != "EUR":
			frappe.throw(_("Provisioning requires company default currency EUR."))
		self._company_address_snapshot()

	def _provider(self):
		connection = frappe.get_doc("Fiskaly API Connection", self.connection)
		return connection, get_provider(connection)

	def _stable_provider_uuid(self, resource_type: str) -> str:
		"""Return a retry-stable v4-shaped ID without relying on an uncommitted random UUID."""
		site = getattr(frappe.local, "site", None) or "erpnext"
		seed = f"{site}:Fiskaly Register:{self.name}:{self.creation}:{resource_type}"
		digest = hashlib.sha256(seed.encode("utf-8")).digest()
		return str(uuid.UUID(bytes=digest[:16], version=4))

	def _company_address_snapshot(self) -> str:
		if not self.location_address:
			frappe.throw(_("The register has no location address for the statutory receipt."))
		address = frappe.get_doc("Address", self.location_address)
		if not address.has_link("Company", self.company):
			frappe.throw(
				_("The register location address is not linked to company {0}.").format(self.company)
			)
		return address.get_display()

	def _record_lifecycle_event(self, event_type: str, **values):
		response = values.pop("provider_response", None)
		doc = frappe.get_doc(
			{
				"doctype": "Fiskaly Lifecycle Event",
				"event_uuid": str(uuid.uuid4()),
				"register": self.name,
				"connection": self.connection,
				"provider": self.provider,
				"environment": self.environment,
				"event_type": event_type,
				"event_time": now_datetime(),
				"actor": frappe.session.user or "Administrator",
				"outage_scope": self.outage_scope,
				"outage_reference": self.outage_reference,
				"outage_started_at": self.outage_started_at,
				"deadline_at": self.outage_deadline_at,
				"provider_response": frappe.as_json(response) if response is not None else None,
				**values,
			}
		).insert(ignore_permissions=True)
		return doc.name

	def _store_provider_resource(self, connection, resource: dict):
		name = frappe.db.exists(
			"Fiskaly Provider Resource",
			{"connection": connection.name, "provider_resource_id": resource["id"]},
		)
		data = resource["data"]
		values = {
			"connection": connection.name,
			"register": self.name,
			"resource_type": resource["type"],
			"provider_resource_id": resource["id"],
			"state": data.get("state"),
			"mode": data.get("mode"),
			"provider_data": frappe.as_json(data),
		}
		if name:
			frappe.db.set_value("Fiskaly Provider Resource", name, values)
		else:
			frappe.get_doc({"doctype": "Fiskaly Provider Resource", **values}).insert(ignore_permissions=True)

	@staticmethod
	def _as_datetime(timestamp):
		return provider_datetime_for_db(timestamp)

	@staticmethod
	def _closing_period(data: dict) -> str | None:
		receipt_type = data.get("receipt_type")
		if receipt_type not in {"MONTHLY_CLOSE", "YEARLY_CLOSE"} or not data.get("time_signature"):
			return None
		signed_local = datetime.fromtimestamp(
			int(data["time_signature"]), tz=ZoneInfo("Europe/Vienna")
		) - timedelta(days=1)
		return signed_local.strftime("%Y-%m" if receipt_type == "MONTHLY_CLOSE" else "%Y")

	@staticmethod
	def _fon_evidence(data: dict) -> tuple[str, str | None, str | None]:
		validations = data.get("fon_validations") or []
		if not validations:
			status = "PENDING" if data.get("receipt_type") in FON_REQUIRED_RECEIPT_TYPES else "NOT_REQUIRED"
			return status, None, None
		validation = validations[-1]
		status = "SUCCESS" if validation.get("validation_result") == "SUCCESS" else "FAILED"
		return (
			status,
			FiskalyRegister._as_datetime(validation.get("time_validation")),
			frappe.as_json(validation),
		)

	def _store_provider_receipt(self, data: dict) -> str:
		from erpnext_fiskaly_sign_at.services.fiscalization import _outage_signature_is_valid

		receipt_uuid = data.get("_id")
		receipt_type = data.get("receipt_type")
		if not receipt_uuid or receipt_type not in RECEIPT_KIND_BY_PROVIDER_TYPE:
			frappe.throw(_("fiskaly returned an invalid automatic RKSV receipt."))
		kind = RECEIPT_KIND_BY_PROVIDER_TYPE[receipt_type]
		period = self._closing_period(data)
		fon_status, fon_at, fon_response = self._fon_evidence(data)
		qr_data = data.get("qr_code_data") or ""
		is_substitute_signed = data.get("signed") is False and _outage_signature_is_valid(
			qr_data, "Sicherheitseinrichtung ausgefallen"
		)
		response_payload = frappe.as_json(data)
		values = {
			"receipt_uuid": receipt_uuid,
			"company": self.company,
			"company_address_display": self._company_address_snapshot(),
			"register": self.name,
			"connection": self.connection,
			"provider": self.provider,
			"environment": self.environment,
			"receipt_type": receipt_type,
			"receipt_kind": kind,
			"fiscal_period": period,
			"status": "SIGNED"
			if data.get("signed") is True
			else "SUBSTITUTE_SIGNED"
			if is_substitute_signed
			else "ACTION_REQUIRED",
			"idempotency_key": receipt_uuid,
			"provider_receipt_id": receipt_uuid,
			"receipt_number": str(data.get("receipt_number", "")),
			"signed_at": self._as_datetime(data.get("time_signature")),
			"serial_number": data.get("cash_register_serial_number"),
			"qr_code_data": qr_data,
			"signature_value": qr_data.rsplit("_", 1)[-1] if qr_data else "",
			"signed": int(data.get("signed") is True),
			"hints": "\n".join(data.get("hints") or ()),
			"response_payload": response_payload,
			"response_payload_sha256": hashlib.sha256(response_payload.encode("utf-8")).hexdigest(),
			"fon_validation_status": fon_status,
			"fon_validation_at": fon_at,
			"fon_validation_response": fon_response,
		}
		if kind == "YEARLY" and period:
			values.update(
				{
					"annual_compliance_status": "PENDING",
					"verification_deadline": date(int(period) + 1, 2, 15),
					"verification_status": (
						"SUCCESS"
						if fon_status == "SUCCESS"
						else "FAILED"
						if fon_status == "FAILED"
						else "PENDING"
					),
					"verification_at": fon_at if fon_status == "SUCCESS" else None,
					"verification_note": (
						"FinanzOnline Webservice validation" if fon_status == "SUCCESS" else None
					),
				}
			)
		name = frappe.db.exists("Fiskaly Receipt", {"receipt_uuid": receipt_uuid})
		if name:
			existing = frappe.get_doc("Fiskaly Receipt", name)
			if existing.company:
				values["company"] = existing.company
			if existing.company_address_display:
				values["company_address_display"] = existing.company_address_display
			if existing.verification_by:
				for fieldname in (
					"verification_status",
					"verification_at",
					"verification_by",
					"verification_note",
				):
					values[fieldname] = existing.get(fieldname)
			if kind == "YEARLY":
				verified = values.get("verification_status") == "SUCCESS"
				values["annual_compliance_status"] = (
					"COMPLETE" if verified and existing.print_evidence_at else "PENDING"
				)
				if values["annual_compliance_status"] == "COMPLETE":
					values["annual_action_required_reason"] = None
			for fieldname in (
				"company",
				"company_address_display",
				"register",
				"provider_receipt_id",
				"receipt_type",
				"receipt_kind",
				"receipt_number",
				"serial_number",
				"qr_code_data",
			):
				old_value = existing.get(fieldname)
				new_value = values.get(fieldname)
				if old_value not in (None, "") and str(old_value) != str(new_value):
					frappe.throw(
						_("Provider receipt {0} conflicts with its immutable stored {1}.").format(
							receipt_uuid, fieldname
						)
					)
			if existing.status in {"SIGNED", "SUBSTITUTE_SIGNED"}:
				for fieldname in ("status", "signed_at", "signature_value", "signed", "hints"):
					old_value = existing.get(fieldname)
					new_value = values.get(fieldname)
					try:
						if fieldname == "signed_at":
							old_datetime = get_datetime(old_value) if old_value else None
							new_datetime = get_datetime(new_value) if new_value else None
							if old_datetime and old_datetime.tzinfo:
								old_datetime = old_datetime.astimezone(UTC).replace(tzinfo=None)
							if new_datetime and new_datetime.tzinfo:
								new_datetime = new_datetime.astimezone(UTC).replace(tzinfo=None)
							matches = old_datetime == new_datetime
						elif fieldname == "signed":
							matches = int(old_value or 0) == int(new_value or 0)
						else:
							matches = str(old_value or "") == str(new_value or "")
					except TypeError, ValueError:
						matches = False
					if not matches:
						frappe.throw(
							_("Provider receipt {0} conflicts with its immutable stored {1}.").format(
								receipt_uuid, fieldname
							)
						)
					# Preserve the stored representation byte-for-byte; later syncs may only
					# add FinanzOnline validation evidence around the terminal signature result.
					values[fieldname] = old_value
			frappe.db.set_value("Fiskaly Receipt", name, values, update_modified=False)
		else:
			name = (
				frappe.get_doc({"doctype": "Fiskaly Receipt", **values}).insert(ignore_permissions=True).name
			)
		return name

	def _store_start_receipt(self, data):
		name = self._store_provider_receipt(data)
		fon_status = frappe.db.get_value("Fiskaly Receipt", name, "fon_validation_status")
		if fon_status != "SUCCESS":
			frappe.throw(_("The initialization receipt has no successful FinanzOnline validation."))
		self.db_set({"start_receipt": name, "start_receipt_fon_status": fon_status})
		return name

	@frappe.whitelist(methods=["POST"])
	def provision_register(self):
		from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_api_connection.fiskaly_api_connection import (
			normalize_austrian_tax_id_number,
			normalize_austrian_vat_id_number,
			unified_austrian_vat_id_number,
			validate_fon_credential_format,
		)
		from erpnext_fiskaly_sign_at.services.compliance import require_canonical_pos_print_setup
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		frappe.only_for("System Manager")
		# Serialize the complete preflight and remote provisioning attempt. Reloading
		# after the row lock makes a concurrent request observe the committed lifecycle
		# state instead of provisioning the same deterministic provider IDs twice.
		_lock_register_row(self.name)
		self.reload()
		# The uninitialized register's company/POS Profile can still change while the
		# caller waits for the lock. Re-authorize the reloaded document before using it.
		self.check_permission("write")
		if self.initialized:
			frappe.throw(_("This register is already initialized."))
		if self.provider_state in {"DECOMMISSIONED", "DEFECTIVE"}:
			frappe.throw(_("A decommissioned or defective RKSV register cannot be recommissioned."))
		require_canonical_pos_print_setup(self.pos_profile)
		self._validate_austrian_runtime()
		connection, provider = self._provider()
		fon_labels = {
			"fon_participant_id": _("Teilnehmer-ID"),
			"fon_user_id": _("Benutzer-ID"),
			"fon_user_pin": _("PIN"),
		}
		fon_values = {
			"fon_participant_id": connection.fon_participant_id,
			"fon_user_id": connection.fon_user_id,
			"fon_user_pin": connection.get_password("fon_user_pin", raise_exception=False),
		}
		missing_fon = [fieldname for fieldname in fon_labels if not fon_values[fieldname]]
		if missing_fon:
			frappe.throw(
				_(
					"Für die österreichische Provisionierung über fiskaly werden FinanzOnline-Daten "
					"benötigt. In TEST genügen syntaktisch gültige Dummy-Daten. Fehlend: {0}."
				).format(", ".join(fon_labels[fieldname] for fieldname in missing_fon)),
				title=_("FinanzOnline-Daten unvollständig"),
			)
		validate_fon_credential_format(
			fon_values["fon_participant_id"],
			fon_values["fon_user_id"],
			fon_values["fon_user_pin"],
			provider=connection.provider,
		)
		connection.tax_id_number = normalize_austrian_tax_id_number(connection.tax_id_number)
		connection.vat_id_number = normalize_austrian_vat_id_number(connection.vat_id_number)
		if connection.provider == "SIGN_AT_UNIFIED":
			connection.vat_id_number = unified_austrian_vat_id_number(connection.vat_id_number)
		if not connection.tax_id_number and not connection.vat_id_number:
			frappe.throw(_("An Austrian tax number or VAT ID is required before provisioning."))
		if connection.provider == "SIGN_AT_V1":
			if self.scu_assignment_mode == "EXISTING" and not self.signature_creation_unit_id:
				frappe.throw(_("Select an existing SCU before provisioning."))
			if self.scu_assignment_mode not in {"EXISTING", "NEW"}:
				frappe.throw(_("Choose whether an existing SCU is used or a new SCU is created."))
			identifiers = {}
			if self.scu_assignment_mode == "NEW" and not self.signature_creation_unit_id:
				identifiers["signature_creation_unit_id"] = self._stable_provider_uuid("SCU")
			if not self.provider_register_id:
				identifiers["provider_register_id"] = self._stable_provider_uuid("CASH_REGISTER")
			if identifiers:
				self.db_set(identifiers)
		try:
			result = provider.provision_register(self)
		except PermanentFiskalyError as error:
			if connection.provider == "SIGN_AT_V1":
				_raise_actionable_scu_limit_error(connection, error)
			raise
		for resource in result.get("resources", []):
			self._store_provider_resource(connection, resource)

		start_receipt = None
		start_fon_status = None
		if connection.provider == "SIGN_AT_V1":
			if not result.get("start_receipt"):
				frappe.throw(_("SIGN AT v1 did not return a validated initialization receipt."))
			start_receipt = self._store_start_receipt(result["start_receipt"])
			start_fon_status = "SUCCESS"
		values = {
			"provider_register_id": result["provider_register_id"],
			"signature_creation_unit_id": result.get("signature_creation_unit_id")
			or self.signature_creation_unit_id,
			"taxpayer_id": result.get("taxpayer_id"),
			"location_id": result.get("location_id"),
			"serial_number": result.get("serial_number"),
			"provider_state": result.get("state"),
			"provider_mode": result.get("mode"),
			"provider_data": frappe.as_json(result.get("raw")),
			"initialized": 1,
			"start_receipt": start_receipt,
			"start_receipt_fon_status": start_fon_status,
			"last_lifecycle_sync_at": now_datetime(),
		}
		self.db_set(values)
		if connection.provider == "SIGN_AT_UNIFIED":
			provider_rates = {
				float(row["percentage"]): row["code"]
				for row in result.get("vat_rates", [])
				if row.get("percentage") is not None and row.get("code")
			}
			for mapping in self.vat_mappings:
				if code := provider_rates.get(float(mapping.tax_rate)):
					mapping.db_set("unified_code", code)
		self._record_lifecycle_event(
			"REGISTER_STATE_SYNCED",
			previous_state=None,
			requested_state="INITIALIZED",
			resulting_state=result.get("state"),
			fon_reporting_mode="FISKALY_AUTOMATIC"
			if connection.provider == "SIGN_AT_V1"
			else "NOT_APPLICABLE",
			fon_status="REPORTED" if start_fon_status == "SUCCESS" else "NOT_APPLICABLE",
			provider_response=result.get("raw"),
		)
		return result

	@frappe.whitelist(methods=["POST"])
	def refresh_provider_state(self):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		_lock_register_row(self.name)
		self.reload()
		connection, provider = self._provider()
		if not provider.capabilities.register_lifecycle:
			frappe.throw(
				_("Provider {0} has no released register lifecycle contract.").format(connection.provider)
			)
		previous_state = self.provider_state
		register_data = provider.retrieve_register(self)
		values = {
			"provider_state": register_data.get("state"),
			"serial_number": register_data.get("serial_number"),
			"provider_data": frappe.as_json(register_data),
			"last_lifecycle_sync_at": now_datetime(),
		}
		self.db_set(values)
		resources = []
		if self.signature_creation_unit_id and provider.capabilities.scu_lifecycle:
			scu = provider.retrieve_scu(self)
			resource = {"type": "SCU", "id": self.signature_creation_unit_id, "data": scu}
			self._store_provider_resource(connection, resource)
			resources.append(resource)
		self._record_lifecycle_event(
			"REGISTER_STATE_SYNCED",
			previous_state=previous_state,
			resulting_state=register_data.get("state"),
			fon_reporting_mode="NOT_APPLICABLE",
			fon_status="NOT_APPLICABLE",
			provider_response=register_data,
		)
		return {"register": register_data, "resources": resources}

	@frappe.whitelist(methods=["POST"])
	def start_outage(self, reason: str, outage_scope: str = "CASH_REGISTER"):
		"""Open an idempotent outage.

		An unreachable SIGN AT service is a cash-register outage because the provider
		owns the DEP, counter, fiscal receipt number and chain. SECURITY_SYSTEM remains
		available only for an explicitly documented local/TSP workflow; TSP substitute
		signatures themselves are normally handled automatically by SIGN AT.
		"""
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		reason = (reason or "").strip()
		if not reason:
			frappe.throw(_("An outage reason is required."))
		outage_scope = (outage_scope or "CASH_REGISTER").upper()
		if outage_scope not in {"SECURITY_SYSTEM", "CASH_REGISTER"}:
			frappe.throw(_("Outage scope must be SECURITY_SYSTEM or CASH_REGISTER."))
		_lock_register_row(self.name)
		self.reload()
		if self.outage_status in {"ACTIVE", "ACTION_REQUIRED"} and self.outage_reference:
			return {
				"outage_reference": self.outage_reference,
				"status": self.outage_status,
				"scope": self.outage_scope,
				"idempotent": True,
			}

		started_at = now_datetime()
		reference = str(uuid.uuid4())
		deadline = add_to_date(started_at, hours=48) if outage_scope == "CASH_REGISTER" else None
		self.db_set(
			{
				"outage_status": "ACTIVE",
				"outage_scope": outage_scope,
				"outage_reference": reference,
				"outage_started_at": started_at,
				"outage_deadline_at": deadline,
				"outage_ended_at": None,
				"outage_reason": reason,
				"fon_outage_begin_reference": None,
				"fon_outage_begin_reported_at": None,
				"fon_outage_end_reference": None,
				"fon_outage_end_reported_at": None,
				"fon_outage_status": "NOT_APPLICABLE"
				if outage_scope == "SECURITY_SYSTEM"
				else "ACTION_REQUIRED",
			}
		)
		if outage_scope == "SECURITY_SYSTEM":
			event = self._record_lifecycle_event(
				"OUTAGE_STARTED",
				previous_state=self.provider_state,
				resulting_state=self.provider_state,
				reason=reason,
				fon_reporting_mode="NOT_APPLICABLE",
				fon_status="NOT_APPLICABLE",
			)
			return {"outage_reference": reference, "status": "ACTIVE", "event": event}

		connection, provider = self._provider()
		if not provider.capabilities.register_lifecycle:
			return self._mark_outage_report_failed(
				reason,
				ProviderNotAvailableError(
					f"Provider {connection.provider} has no released cash-register outage contract"
				),
			)
		try:
			previous_state = self.provider_state
			if self.provider_state == "OUTAGE":
				result = provider.retrieve_register(self)
			else:
				result = provider.transition_register_state(self, "OUTAGE")
			self.db_set(
				{
					"provider_state": result.get("state"),
					"provider_data": frappe.as_json(result),
					"fon_outage_status": "REPORTED",
					"last_lifecycle_sync_at": now_datetime(),
				}
			)
			event = self._record_lifecycle_event(
				"OUTAGE_STARTED",
				previous_state=previous_state,
				requested_state="OUTAGE",
				resulting_state=result.get("state"),
				reason=reason,
				fon_reporting_mode="FISKALY_AUTOMATIC",
				fon_status="REPORTED",
				provider_response=result,
			)
			return {"outage_reference": reference, "status": "ACTIVE", "event": event}
		except Exception as exc:
			return self._mark_outage_report_failed(reason, exc)

	def _mark_outage_report_failed(self, reason: str, exc: Exception):
		message = str(exc)[:1000]
		self.db_set({"outage_status": "ACTION_REQUIRED", "fon_outage_status": "ACTION_REQUIRED"})
		event = self._record_lifecycle_event(
			"OUTAGE_REPORT_FAILED",
			previous_state=self.provider_state,
			requested_state="OUTAGE",
			resulting_state=self.provider_state,
			reason=reason,
			fon_reporting_mode="MANUAL_FINANZONLINE",
			fon_status="ACTION_REQUIRED",
			action_required=1,
			action_required_reason=message,
			provider_request_id=getattr(exc, "request_id", None),
			provider_response=getattr(exc, "response", None),
		)
		return {
			"outage_reference": self.outage_reference,
			"status": "ACTION_REQUIRED",
			"event": event,
			"error": message,
		}

	def record_api_outage(self, reason: str, request_id: str | None = None):
		"""Persist an API/Internet cash-register outage without performing another network call."""
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		reason = (reason or "SIGN_AT_API_UNREACHABLE").strip()
		_lock_register_row(self.name)
		self.reload()
		if self.outage_status in {"ACTIVE", "ACTION_REQUIRED"} and self.outage_reference:
			return {
				"outage_reference": self.outage_reference,
				"status": self.outage_status,
				"scope": self.outage_scope,
				"idempotent": True,
			}
		started_at = now_datetime()
		reference = str(uuid.uuid4())
		deadline = add_to_date(started_at, hours=48)
		self.db_set(
			{
				"outage_status": "ACTION_REQUIRED",
				"outage_scope": "CASH_REGISTER",
				"outage_reference": reference,
				"outage_started_at": started_at,
				"outage_deadline_at": deadline,
				"outage_ended_at": None,
				"outage_reason": reason,
				"fon_outage_begin_reference": None,
				"fon_outage_begin_reported_at": None,
				"fon_outage_end_reference": None,
				"fon_outage_end_reported_at": None,
				"fon_outage_status": "ACTION_REQUIRED",
			}
		)
		event = self._record_lifecycle_event(
			"OUTAGE_STARTED",
			previous_state=self.provider_state,
			requested_state="OUTAGE",
			resulting_state=self.provider_state,
			reason=reason,
			fon_reporting_mode="MANUAL_FINANZONLINE",
			fon_status="ACTION_REQUIRED",
			action_required=1,
			action_required_reason=(
				"SIGN AT was unreachable. No second network request was made; verify/report the outage workflow."
			),
			provider_request_id=request_id,
		)
		return {
			"outage_reference": reference,
			"status": "ACTION_REQUIRED",
			"scope": "CASH_REGISTER",
			"deadline": deadline,
			"event": event,
		}

	def record_api_recovery(self, reason: str | None = None):
		"""Trusted receipt-replay recovery path without end-user register write permission."""
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		_lock_register_row(self.name)
		self.reload()
		return FiskalyRegister._end_outage(self, reason or "SIGN_AT_API_RECOVERED")

	@frappe.whitelist(methods=["POST"])
	def end_outage(self, reason: str | None = None):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		_lock_register_row(self.name)
		self.reload()
		return FiskalyRegister._end_outage(self, reason)

	def _end_outage(self, reason: str | None = None):
		if self.outage_status not in {"ACTIVE", "ACTION_REQUIRED"} or not self.outage_reference:
			return {"status": self.outage_status or "NONE", "idempotent": True}
		if self.outage_ended_at:
			return {
				"status": self.outage_status,
				"outage_reference": self.outage_reference,
				"fon_status": self.fon_outage_status,
				"idempotent": True,
			}
		previous_state = self.provider_state
		result = None
		transitioned = False
		if self.outage_scope == "CASH_REGISTER":
			_connection, provider = self._provider()
			try:
				current = provider.retrieve_register(self)
				if current.get("state") == "OUTAGE":
					result = provider.transition_register_state(self, "INITIALIZED")
					transitioned = True
				else:
					result = current
				if result.get("state") != "INITIALIZED":
					frappe.throw(_("Cash register did not return to INITIALIZED."))
			except Exception as exc:
				return self._mark_outage_report_failed(reason or "Outage clearance failed", exc)

		ended_at = now_datetime()
		manual_begin_recorded = bool(getattr(self, "fon_outage_begin_reference", None))
		manual_end_recorded = bool(getattr(self, "fon_outage_end_reference", None))
		if manual_begin_recorded and manual_end_recorded:
			fon_status = "MANUALLY_REPORTED"
			fon_mode = "MANUAL_FINANZONLINE"
		elif manual_begin_recorded:
			# A manually reported outage requires a separate manual end report.
			fon_status = "BEGIN_REPORTED"
			fon_mode = "MANUAL_FINANZONLINE"
		elif transitioned:
			fon_status = "REPORTED"
			fon_mode = "FISKALY_AUTOMATIC"
		elif self.fon_outage_status == "REPORTED":
			fon_status = "REPORTED"
			fon_mode = "FISKALY_AUTOMATIC"
		elif self.fon_outage_status == "MANUALLY_REPORTED":
			fon_status = "MANUALLY_REPORTED"
			fon_mode = "MANUAL_FINANZONLINE"
		elif (
			self.outage_scope == "CASH_REGISTER"
			and self.outage_deadline_at
			and get_datetime(self.outage_deadline_at) <= ended_at
		):
			# fiskaly's v1 state endpoint cannot backdate an outage that happened while
			# the API itself was unreachable. After 48 hours, do not silently clear the
			# legal reporting evidence merely because replay succeeds.
			fon_status = "ACTION_REQUIRED"
			fon_mode = "MANUAL_FINANZONLINE"
		else:
			fon_status = "NOT_APPLICABLE"
			fon_mode = "NOT_APPLICABLE"
		outage_status = "ACTION_REQUIRED" if fon_status in {"ACTION_REQUIRED", "BEGIN_REPORTED"} else "ENDED"
		values = {
			"outage_status": outage_status,
			"outage_ended_at": ended_at,
			"fon_outage_status": fon_status,
			"last_lifecycle_sync_at": ended_at,
		}
		if result:
			values.update({"provider_state": result.get("state"), "provider_data": frappe.as_json(result)})
		self.db_set(values)
		event = self._record_lifecycle_event(
			"OUTAGE_RECOVERED_FON_PENDING"
			if fon_status in {"ACTION_REQUIRED", "BEGIN_REPORTED"}
			else "OUTAGE_ENDED",
			previous_state=previous_state,
			requested_state="INITIALIZED" if self.outage_scope == "CASH_REGISTER" else None,
			resulting_state=result.get("state") if result else previous_state,
			reason=reason,
			fon_reporting_mode=fon_mode,
			fon_status=fon_status,
			action_required=int(fon_status in {"ACTION_REQUIRED", "BEGIN_REPORTED"}),
			action_required_reason=(
				(
					"The outage begin was reported manually. Record the separate FinanzOnline end evidence."
					if fon_status == "BEGIN_REPORTED"
					else "The API outage exceeded 48 hours. Record the manual FinanzOnline begin and end evidence."
				)
				if fon_status in {"ACTION_REQUIRED", "BEGIN_REPORTED"}
				else None
			),
			provider_response=result,
		)
		try:
			savepoint = f"fiskaly_auto_receipts_{uuid.uuid4().hex}"
			frappe.db.savepoint(savepoint)
			FiskalyRegister._sync_automatic_receipts(self)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			self.reload()
			frappe.log_error(
				title=f"Automatic RKSV receipt sync after outage {self.name}",
				message=frappe.get_traceback(),
			)
		return {"status": outage_status, "event": event, "outage_reference": self.outage_reference}

	@frappe.whitelist(methods=["POST"])
	def check_outage_deadline(self):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		_lock_register_row(self.name)
		self.reload()
		if (
			self.outage_scope != "CASH_REGISTER"
			or self.outage_status not in {"ACTIVE", "ACTION_REQUIRED"}
			or not self.outage_deadline_at
			or self.fon_outage_status == "REPORTED"
		):
			return {"alert": False}
		if get_datetime(self.outage_deadline_at) > now_datetime():
			return {"alert": False, "deadline": self.outage_deadline_at}
		existing = frappe.db.exists(
			"Fiskaly Lifecycle Event",
			{
				"register": self.name,
				"outage_reference": self.outage_reference,
				"event_type": "OUTAGE_48H_ALERT",
			},
		)
		if existing:
			return {"alert": True, "event": existing, "idempotent": True}
		self.db_set({"outage_status": "ACTION_REQUIRED", "fon_outage_status": "ACTION_REQUIRED"})
		event = self._record_lifecycle_event(
			"OUTAGE_48H_ALERT",
			previous_state=self.provider_state,
			resulting_state=self.provider_state,
			reason=self.outage_reason,
			fon_reporting_mode="MANUAL_FINANZONLINE",
			fon_status="ACTION_REQUIRED",
			action_required=1,
			action_required_reason="Cash-register outage has reached the 48-hour reporting deadline.",
		)
		return {"alert": True, "event": event}

	@frappe.whitelist(methods=["POST"])
	def record_manual_fon_report(self, reference: str, phase: str | None = None, note: str | None = None):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		_lock_register_row(self.name)
		self.reload()
		if self.outage_scope != "CASH_REGISTER" or not self.outage_reference:
			frappe.throw(_("There is no cash-register outage requiring a manual FON report."))
		reference = (reference or "").strip()
		if not reference:
			frappe.throw(_("A FinanzOnline confirmation reference is required."))
		phase = (phase or ("END" if self.fon_outage_begin_reference else "BEGIN")).upper()
		if phase not in {"BEGIN", "END"}:
			frappe.throw(_("Manual FinanzOnline report phase must be BEGIN or END."))

		if phase == "BEGIN":
			if self.fon_outage_begin_reference:
				if self.fon_outage_begin_reference != reference:
					frappe.throw(_("The immutable FinanzOnline begin reference was already recorded."))
				return {
					"status": self.fon_outage_status,
					"phase": "BEGIN",
					"reference": self.fon_outage_begin_reference,
					"reported_at": self.fon_outage_begin_reported_at,
					"idempotent": True,
				}
			reported_at = now_datetime()
			self.db_set(
				{
					"fon_outage_begin_reference": reference,
					"fon_outage_begin_reported_at": reported_at,
					"fon_outage_status": "BEGIN_REPORTED",
					"outage_status": "ACTION_REQUIRED",
				}
			)
			event_type = "MANUAL_FON_BEGIN_REPORTED"
			fon_status = "BEGIN_REPORTED"
		else:
			if not self.fon_outage_begin_reference:
				frappe.throw(_("Record the FinanzOnline outage-begin confirmation before its end."))
			if not self.outage_ended_at:
				frappe.throw(_("End the technical cash-register outage before recording its FON end."))
			if self.fon_outage_end_reference:
				if self.fon_outage_end_reference != reference:
					frappe.throw(_("The immutable FinanzOnline end reference was already recorded."))
				return {
					"status": self.fon_outage_status,
					"phase": "END",
					"reference": self.fon_outage_end_reference,
					"reported_at": self.fon_outage_end_reported_at,
					"idempotent": True,
				}
			reported_at = now_datetime()
			self.db_set(
				{
					"fon_outage_end_reference": reference,
					"fon_outage_end_reported_at": reported_at,
					"fon_outage_status": "MANUALLY_REPORTED",
					"outage_status": "ENDED",
				}
			)
			event_type = "MANUAL_FON_END_REPORTED"
			fon_status = "MANUALLY_REPORTED"
		event = self._record_lifecycle_event(
			event_type,
			previous_state=self.provider_state,
			resulting_state=self.provider_state,
			reason=note,
			fon_reporting_mode="MANUAL_FINANZONLINE",
			fon_status=fon_status,
			fon_reference=reference,
		)
		return {
			"status": fon_status,
			"phase": phase,
			"reference": reference,
			"reported_at": reported_at,
			"event": event,
		}

	@frappe.whitelist(methods=["POST"])
	def decommission_register(self, reason: str):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		frappe.only_for("System Manager")
		reason = (reason or "").strip()
		if not reason:
			frappe.throw(_("A decommissioning reason is required."))
		_lock_register_row(self.name)
		self.reload()
		if self.decommission_status == "EVIDENCE_REQUIRED" and self.closing_receipt:
			return {
				"state": "DECOMMISSIONED",
				"receipt": self.closing_receipt,
				"status": "EVIDENCE_REQUIRED",
				"idempotent": True,
			}
		if self.decommission_status == "COMPLETE":
			return {
				"state": "DECOMMISSIONED",
				"receipt": self.closing_receipt,
				"status": "COMPLETE",
				"idempotent": True,
			}
		if self.outage_status in {"ACTIVE", "ACTION_REQUIRED"}:
			frappe.throw(_("Resolve the open cash-register/FinanzOnline outage before decommissioning."))
		pending_receipt = frappe.db.get_value(
			"Fiskaly Receipt",
			{"register": self.name, "status": ["in", OPEN_RECEIPT_STATUSES]},
			"name",
		)
		if pending_receipt:
			frappe.throw(
				_(
					"Register decommissioning is blocked by unresolved RKSV receipt {0}. "
					"Replay or resolve every receipt before closing the provider chain."
				).format(pending_receipt)
			)
		connection, provider = self._provider()
		if not provider.capabilities.register_lifecycle:
			frappe.throw(
				_("Provider {0} has no released decommissioning contract.").format(connection.provider)
			)
		current = provider.retrieve_register(self)
		result = (
			current
			if current.get("state") == "DECOMMISSIONED"
			else provider.transition_register_state(self, "DECOMMISSIONED")
		)
		receipt_id = result.get("decommission_receipt_id")
		receipt = None
		if receipt_id:
			receipt = provider.retrieve_receipt(self.provider_register_id, receipt_id)
		else:
			# A retry may observe an already decommissioned remote register after the
			# original request lost its local transaction. Recover the provider's
			# automatic closing receipt instead of issuing another transition.
			closing_receipts = provider.list_automatic_receipts(self, ("DECOMMISSION",))
			if closing_receipts:
				receipt = closing_receipts[0]
				receipt_id = receipt.get("_id")
		if not receipt_id or not receipt:
			frappe.throw(_("Decommissioned cash register has no retrievable closing receipt."))
		if not any(
			validation.get("validation_result") == "SUCCESS"
			for validation in receipt.get("fon_validations") or []
		):
			validation = provider.validate_receipt_with_fon(self, receipt_id)
			receipt["fon_validations"] = [*(receipt.get("fon_validations") or []), validation]
		if not any(
			validation.get("validation_result") == "SUCCESS"
			for validation in receipt.get("fon_validations") or []
		):
			frappe.throw(_("The closing receipt has no successful FinanzOnline validation."))
		closing_receipt = self._store_provider_receipt(receipt)
		closing_status = frappe.db.get_value("Fiskaly Receipt", closing_receipt, "status")
		if closing_status not in {"SIGNED", "SUBSTITUTE_SIGNED"}:
			frappe.throw(_("The closing receipt is incomplete and cannot close the local register."))
		self.db_set(
			{
				"provider_state": "DECOMMISSIONED",
				"provider_data": frappe.as_json(result),
				"active": 0,
				"decommission_status": "EVIDENCE_REQUIRED",
				"closing_receipt": closing_receipt,
				"last_lifecycle_sync_at": now_datetime(),
			}
		)
		event = self._record_lifecycle_event(
			"REGISTER_DECOMMISSIONED",
			previous_state=current.get("state"),
			requested_state="DECOMMISSIONED",
			resulting_state="DECOMMISSIONED",
			reason=reason,
			fon_reporting_mode="FISKALY_AUTOMATIC",
			fon_status="REPORTED",
			provider_response=result,
		)
		return {
			"state": "DECOMMISSIONED",
			"status": "EVIDENCE_REQUIRED",
			"receipt": closing_receipt,
			"event": event,
			"next_steps": ["ARCHIVE_CLOSING_RECEIPT", "CREATE_FINAL_DEP7", "CONFIRM_EXTERNAL_COPY"],
		}

	@frappe.whitelist(methods=["POST"])
	def decommission_and_archive_closing_receipt(self, reason: str | None = None):
		"""Close the provider register and retain its protected closing PDF in one step."""
		self.check_permission("write")
		frappe.only_for("System Manager")
		reason = (reason or "").strip()
		if self.provider_state != "DECOMMISSIONED" and not reason:
			frappe.throw(_("A decommissioning reason is required."))
		self.decommission_register(reason or "Archive existing provider closing receipt")
		self.reload()
		if not self.closing_receipt:
			frappe.throw(_("The provider decommissioning produced no closing receipt."))
		closing = frappe.get_doc("Fiskaly Receipt", self.closing_receipt)
		archive_warning = None
		try:
			closing.create_and_archive_print_evidence()
		except Exception as error:
			if not isinstance(error, frappe.ValidationError):
				frappe.log_error(
					title=f"Closing receipt archive failed for register {self.name}",
					message=frappe.get_traceback(),
				)
			archive_warning = _(
				"Die Kasse wurde bei Fiskaly erfolgreich geschlossen und der Schlussbeleg "
				"gespeichert. Nur die lokale PDF-Archivierung ist fehlgeschlagen. Öffnen Sie "
				"den Assistenten erneut und wählen Sie „Schlussbeleg automatisch archivieren“. "
				"Die Kasse wird dabei nicht erneut geschlossen."
			)
			frappe.msgprint(
				archive_warning,
				title=_("Außerbetriebnahme gespeichert - PDF noch offen"),
				indicator="orange",
			)
		self.reload()
		state = _decommission_workflow_state(self)
		if archive_warning:
			state["archive_warning"] = archive_warning
		return state

	def _final_dep7_document(self):
		self.reload()
		if not self.final_dep7_export:
			frappe.throw(_("Create the final DEP7 backup first."))
		export = frappe.get_doc("Fiskaly DEP7 Export", self.final_dep7_export)
		if export.register != self.name or export.purpose != "DECOMMISSION_FINAL":
			frappe.throw(_("The linked DEP7 export is not valid for this decommissioning."))
		return export

	@frappe.whitelist(methods=["POST"])
	def verify_final_dep7_integrity(self):
		self.check_permission("write")
		frappe.only_for("System Manager")
		export = self._final_dep7_document()
		export.verify_export_integrity()
		return _decommission_workflow_state(self)

	@frappe.whitelist(methods=["POST"])
	def confirm_final_dep7_external_copy(self, storage_reference: str):
		self.check_permission("write")
		frappe.only_for("System Manager")
		export = self._final_dep7_document()
		export.mark_external_copy_confirmed(storage_reference)
		return _decommission_workflow_state(self)

	@frappe.whitelist(methods=["POST"])
	def complete_decommission(self):
		from erpnext_fiskaly_sign_at.api.exports import _validate_final_decommission_export
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		frappe.only_for("System Manager")
		_lock_register_row(self.name)
		self.reload()
		pending_receipt = frappe.db.get_value(
			"Fiskaly Receipt",
			{"register": self.name, "status": ["in", OPEN_RECEIPT_STATUSES]},
			"name",
		)
		if pending_receipt:
			frappe.throw(
				_(
					"Register completion is blocked by unresolved RKSV receipt {0}. "
					"Replay or resolve every receipt before finalizing decommissioning."
				).format(pending_receipt)
			)
		if self.decommission_status == "COMPLETE":
			return {"status": "COMPLETE", "idempotent": True}
		if self.provider_state != "DECOMMISSIONED" or not self.closing_receipt:
			frappe.throw(_("The provider decommissioning and closing receipt are not complete."))
		export = frappe.db.get_value(
			"Fiskaly DEP7 Export",
			{
				"name": self.final_dep7_export,
				"register": self.name,
				"purpose": "DECOMMISSION_FINAL",
				"export_scope": "COMPLETE",
				"status": "READY",
			},
			[
				"name",
				"creation",
				"generated_at",
				"integrity_verified_at",
				"supplementary_integrity_verified_at",
				"external_copy_confirmed",
				"external_storage_reference",
			],
			as_dict=True,
		)
		if not export:
			frappe.throw(_("Create the complete final DEP7 export before closing the local register."))
		if not export.creation or not export.generated_at:
			frappe.throw(_("The final DEP7 has no auditable creation and generation timestamps."))
		preconditions = _validate_final_decommission_export(
			self,
			export_created_at=export.creation,
			export_generated_at=export.generated_at,
		)
		closing = preconditions["closing"]
		if not closing.print_evidence_at or not closing.print_evidence_file:
			frappe.throw(_("Archive the closing receipt in a private evidence file first."))
		if not (
			export.integrity_verified_at
			and export.supplementary_integrity_verified_at
			and export.external_copy_confirmed
			and export.external_storage_reference
		):
			frappe.throw(_("Verify both final DEP7 files and confirm their external copy first."))
		self.db_set({"initialized": 0, "decommission_status": "COMPLETE", "active": 0})
		event = self._record_lifecycle_event(
			"REGISTER_DECOMMISSION_COMPLETED",
			previous_state="DECOMMISSIONED",
			resulting_state="DECOMMISSIONED",
			reason="Closing receipt and final external DEP7 evidence completed",
			fon_reporting_mode="FISKALY_AUTOMATIC",
			fon_status="REPORTED",
			provider_response={
				"closing_receipt": self.closing_receipt,
				"final_dep7_export": export.name,
				"external_storage_reference": export.external_storage_reference,
			},
		)
		return {
			"status": "COMPLETE",
			"closing_receipt": self.closing_receipt,
			"final_dep7_export": export.name,
			"event": event,
		}

	@frappe.whitelist(methods=["POST"])
	def mark_register_defective(self, reason: str):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		frappe.only_for("System Manager")
		reason = (reason or "").strip()
		if not reason:
			frappe.throw(_("A defect reason is required."))
		_lock_register_row(self.name)
		self.reload()
		pending_receipt = frappe.db.get_value(
			"Fiskaly Receipt",
			{"register": self.name, "status": ["in", OPEN_RECEIPT_STATUSES]},
			"name",
		)
		if pending_receipt:
			frappe.throw(
				_(
					"Register defect marking is blocked by unresolved RKSV receipt {0}. "
					"Replay or resolve every receipt before ending the provider chain."
				).format(pending_receipt)
			)
		_connection, provider = self._provider()
		previous_state = self.provider_state
		result = provider.transition_register_state(self, "DEFECTIVE")
		self.db_set(
			{
				"provider_state": "DEFECTIVE",
				"provider_data": frappe.as_json(result),
				"initialized": 0,
				"active": 0,
				"last_lifecycle_sync_at": now_datetime(),
			}
		)
		event = self._record_lifecycle_event(
			"REGISTER_DEFECTIVE",
			previous_state=previous_state,
			requested_state="DEFECTIVE",
			resulting_state="DEFECTIVE",
			reason=reason,
			fon_reporting_mode="FISKALY_AUTOMATIC",
			fon_status="REPORTED",
			provider_response=result,
		)
		return {"state": "DEFECTIVE", "event": event}

	@frappe.whitelist(methods=["POST"])
	def decommission_scu(self, reason: str):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		frappe.only_for("System Manager")
		reason = (reason or "").strip()
		if not reason:
			frappe.throw(_("An SCU decommissioning reason is required."))
		_lock_register_row(self.name)
		self.reload()
		if self.active or self.initialized:
			frappe.throw(_("Decommission and complete this cash register before decommissioning its SCU."))
		other_register = frappe.db.exists(
			"Fiskaly Register",
			{
				"name": ["!=", self.name],
				"signature_creation_unit_id": self.signature_creation_unit_id,
				"active": 1,
				"initialized": 1,
			},
		)
		if other_register:
			frappe.throw(_("SCU is still used by active register {0}.").format(other_register))
		connection, provider = self._provider()
		if not provider.capabilities.scu_lifecycle:
			frappe.throw(
				_("Provider {0} has no released SCU lifecycle contract.").format(connection.provider)
			)
		current = provider.retrieve_scu(self)
		result = (
			current
			if current.get("state") == "DECOMMISSIONED"
			else provider.transition_scu_state(self, "DECOMMISSIONED")
		)
		self._store_provider_resource(
			connection, {"type": "SCU", "id": self.signature_creation_unit_id, "data": result}
		)
		event = self._record_lifecycle_event(
			"SCU_DECOMMISSIONED",
			previous_state=current.get("state"),
			requested_state="DECOMMISSIONED",
			resulting_state=result.get("state"),
			reason=reason,
			fon_reporting_mode="FISKALY_AUTOMATIC",
			fon_status="REPORTED",
			provider_response=result,
		)
		return {"state": result.get("state"), "event": event}

	@frappe.whitelist(methods=["POST"])
	def sync_automatic_receipts(self):
		from erpnext_fiskaly_sign_at.services.fiscalization import _lock_register_row

		self.check_permission("write")
		_lock_register_row(self.name)
		self.reload()
		return FiskalyRegister._sync_automatic_receipts(self)

	def _sync_automatic_receipts(self):
		connection, provider = self._provider()
		if not provider.capabilities.automatic_closing_receipts:
			frappe.throw(
				_("Provider {0} has no released Austrian automatic-receipt contract.").format(
					connection.provider
				)
			)
		receipts = provider.list_automatic_receipts(self, AUTOMATIC_RECEIPT_TYPES)
		stored = []
		for receipt in receipts:
			if receipt.get("receipt_type") in FON_REQUIRED_RECEIPT_TYPES and not any(
				validation.get("validation_result") == "SUCCESS"
				for validation in receipt.get("fon_validations") or []
			):
				try:
					validation = provider.validate_receipt_with_fon(self, receipt["_id"])
					receipt["fon_validations"] = [*(receipt.get("fon_validations") or []), validation]
				except FiskalyError as exc:
					receipt["fon_validations"] = [
						*(receipt.get("fon_validations") or []),
						{
							"validation_result": "ERROR_UNSPECIFIED",
							"time_validation": int(now_datetime().timestamp()),
							"error": str(exc),
						},
					]
			stored.append(self._store_provider_receipt(receipt))
		self.db_set("last_closing_sync_at", now_datetime())
		self._record_lifecycle_event(
			"AUTOMATIC_RECEIPTS_SYNCED",
			previous_state=self.provider_state,
			resulting_state=self.provider_state,
			fon_reporting_mode="FISKALY_AUTOMATIC",
			fon_status="NOT_APPLICABLE",
			provider_response={"received": len(receipts), "stored": stored},
		)
		return {"received": len(receipts), "stored": stored}

	@frappe.whitelist(methods=["POST"])
	def create_monthly_receipt(self, fiscal_period=None):
		"""Backward-compatible name: retrieve fiskaly's automatic close, never submit a fake NORMAL receipt."""
		period = fiscal_period or nowdate()[:7]
		self.sync_automatic_receipts()
		name = frappe.db.get_value(
			"Fiskaly Receipt",
			{"register": self.name, "receipt_kind": "MONTHLY", "fiscal_period": period},
			"name",
		)
		if not name:
			frappe.throw(
				_("fiskaly has not generated the monthly closing receipt for {0} yet.").format(period)
			)
		return {"receipt": name, "fiscal_period": period}

	@frappe.whitelist(methods=["POST"])
	def create_yearly_receipt(self, fiscal_year=None):
		"""Backward-compatible name: retrieve fiskaly's automatic annual receipt."""
		year = str(fiscal_year or nowdate()[:4])
		self.sync_automatic_receipts()
		name = frappe.db.get_value(
			"Fiskaly Receipt",
			{"register": self.name, "receipt_kind": "YEARLY", "fiscal_period": year},
			"name",
		)
		if not name:
			frappe.throw(_("fiskaly has not generated the yearly closing receipt for {0} yet.").format(year))
		return {"receipt": name, "fiscal_period": year}

	@frappe.whitelist(methods=["POST"])
	def create_manual_control_receipt(self, reference: str | None = None):
		self.check_permission("write")
		from erpnext_fiskaly_sign_at.services.fiscalization import create_zero_receipt

		if self.provider != "SIGN_AT_V1":
			frappe.throw(_("Manual control receipts are available only through the released SIGN AT v1 API."))
		control_reference = (reference or now_datetime().strftime("%Y%m%d-%H%M%S")).strip()
		return create_zero_receipt(self.name, "CONTROL", control_reference)


def check_all_outage_deadlines():
	for name in frappe.get_all(
		"Fiskaly Register",
		filters={"outage_scope": "CASH_REGISTER", "outage_status": ["in", ["ACTIVE", "ACTION_REQUIRED"]]},
		pluck="name",
	):
		savepoint = f"fiskaly_deadline_{uuid.uuid4().hex}"
		frappe.db.savepoint(savepoint)
		try:
			frappe.get_doc("Fiskaly Register", name).check_outage_deadline()
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(title=f"RKSV outage deadline check {name}", message=frappe.get_traceback())


def sync_all_automatic_receipts():
	for name in frappe.get_all(
		"Fiskaly Register",
		filters={"provider": "SIGN_AT_V1", "provider_register_id": ["is", "set"]},
		pluck="name",
	):
		savepoint = f"fiskaly_auto_sync_{uuid.uuid4().hex}"
		frappe.db.savepoint(savepoint)
		try:
			frappe.get_doc("Fiskaly Register", name).sync_automatic_receipts()
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(title=f"Automatic RKSV receipt sync {name}", message=frappe.get_traceback())
