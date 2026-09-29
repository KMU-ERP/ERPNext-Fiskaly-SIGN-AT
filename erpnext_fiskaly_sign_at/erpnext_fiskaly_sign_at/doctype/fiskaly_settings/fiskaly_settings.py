from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_system_timezone

from erpnext_fiskaly_sign_at.install import (
	RKSV_OFFLINE_NOTICE,
	RKSV_PRINT_FORMAT,
	get_pos_profile_print_format_mismatches,
	setup_pos_profile_print_formats,
)

PENDING_RECEIPT_STATUSES = (
	"PREPARED",
	"SIGNING",
	"OFFLINE_PENDING",
	"RETRYING",
	"ACTION_REQUIRED",
)
AUDITED_FIELDS = (
	"enabled",
	"operating_environment",
	"enforce_pos_only_cash_receipts",
)


def _is_system_manager() -> bool:
	user = getattr(getattr(frappe, "session", None), "user", None)
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


class FiskalySettings(Document):
	def validate(self):
		self._confirmed_changes = self._critical_changes()
		self._validate_critical_confirmation()
		self._validate_values()
		self._block_switch_with_pending_receipts()
		if cint(self.enabled) or self.operating_environment == "LIVE":
			self._validate_enabled_operation()
		self.offline_notice = RKSV_OFFLINE_NOTICE
		self.allow_unified_live = 0
		# A confirmation is a one-shot acknowledgement, never a persistent bypass.
		self.critical_change_confirmation = 0

	def on_update(self):
		changes = getattr(self, "_confirmed_changes", ())
		if not changes:
			return
		message = _("Critical Fiskaly RKSV settings change confirmed by {0}: {1}").format(
			frappe.session.user, ", ".join(changes)
		)
		frappe.logger("erpnext_fiskaly_sign_at.audit", allow_site=True).warning(message)
		self.add_comment("Info", message)

	def _critical_changes(self) -> tuple[str, ...]:
		old = self.get_doc_before_save()
		if not old:
			changes = []
			if cint(self.enabled):
				changes.append("enabled")
			if self.operating_environment == "LIVE":
				changes.append("operating_environment")
			return tuple(changes)

		changes = [fieldname for fieldname in AUDITED_FIELDS if old.get(fieldname) != self.get(fieldname)]
		return tuple(changes)

	def _validate_critical_confirmation(self):
		if not self._confirmed_changes:
			return
		if not _is_system_manager():
			frappe.throw(
				_("Only a System Manager may apply critical Fiskaly RKSV settings changes."),
				title=_("System Manager required"),
			)
		if not cint(self.critical_change_confirmation):
			frappe.throw(
				_(
					"Confirm the critical change explicitly. Activation, environment switches, "
					"LIVE deactivation and changes to the cash-bypass protection affect the "
					"statutory receipt chain."
				),
				title=_("Confirmation required"),
			)

	def _validate_values(self):
		if self.operating_environment not in {"TEST", "LIVE"}:
			frappe.throw(_("The active environment must be TEST or LIVE."))
		for fieldname, minimum, maximum in (
			("connect_timeout_seconds", 1, 60),
			("read_timeout_seconds", 1, 180),
			("retry_interval_minutes", 1, 1440),
			("max_retries", 1, 1000000),
			("log_retention_days", 1, 3650),
		):
			value = cint(self.get(fieldname))
			if not minimum <= value <= maximum:
				frappe.throw(
					_("{0} must be between {1} and {2}.").format(
						self.meta.get_label(fieldname), minimum, maximum
					)
				)
			self.set(fieldname, value)
		if cint(self.allow_unified_live):
			frappe.throw(
				_("Unified SIGN AT is not released for LIVE use and cannot be enabled."),
				title=_("Unified LIVE blocked"),
			)

	def _block_switch_with_pending_receipts(self):
		old = self.get_doc_before_save()
		if not old:
			return
		environment_changed = old.operating_environment != self.operating_environment
		disabling = cint(old.enabled) and not cint(self.enabled)
		if not (environment_changed or disabling):
			return
		pending = frappe.get_all(
			"Fiskaly Receipt",
			filters={
				"environment": old.operating_environment,
				"status": ["in", PENDING_RECEIPT_STATUSES],
			},
			pluck="name",
			limit=6,
		)
		if pending:
			preview = ", ".join(pending[:5])
			if len(pending) > 5:
				preview += ", …"
			frappe.throw(
				_(
					"Environment change or deactivation is blocked while unresolved RKSV receipts "
					"exist in {0}: {1}. Resolve or document these receipts first."
				).format(old.operating_environment, preview),
				title=_("Open RKSV receipts"),
			)

	def _validate_enabled_operation(self):
		if not cint(self.enforce_pos_only_cash_receipts):
			frappe.throw(
				_(
					"The current integration fiscalizes POS Invoice only. Keep the protection against "
					"direct cash Payment Entries and paid Sales Invoices enabled while RKSV is active."
				),
				title=_("Cash-bypass protection required"),
			)
		if get_system_timezone() != "Europe/Vienna":
			frappe.throw(
				_(
					"Set the system time zone to Europe/Vienna before enabling Austrian RKSV. "
					"Receipt date and time must follow Austrian local time."
				),
				title=_("Invalid system time zone"),
			)

		registers = frappe.get_all(
			"Fiskaly Register",
			filters={"active": 1, "environment": self.operating_environment},
			fields=[
				"name",
				"company",
				"provider",
				"initialized",
				"pos_profile",
				"connection",
				"location_address",
			],
		)
		if not registers:
			frappe.throw(
				_("At least one active Fiskaly register is required in environment {0}.").format(
					self.operating_environment
				)
			)
		invalid = [
			row.name
			for row in registers
			if not cint(row.initialized)
			or not row.location_address
			or (self.operating_environment == "LIVE" and row.provider != "SIGN_AT_V1")
		]
		if invalid:
			frappe.throw(
				_(
					"Every active register must be initialized and have a company-linked location "
					"address; LIVE additionally requires SIGN AT v1. Check: {0}."
				).format(", ".join(invalid)),
				title=_("RKSV register not ready"),
			)

		invalid_connections = []
		for register in registers:
			connection = frappe.get_doc("Fiskaly API Connection", register.connection)
			if (
				not cint(connection.active)
				or connection.environment != self.operating_environment
				or connection.company != register.company
				or connection.provider != register.provider
			):
				invalid_connections.append(register.name)
		if invalid_connections:
			frappe.throw(
				_(
					"Every active register must use an active API connection for the same company, "
					"provider and environment. Check: {0}."
				).format(", ".join(invalid_connections)),
				title=_("RKSV connection not ready"),
			)

		mismatches = get_pos_profile_print_format_mismatches()
		if mismatches:
			frappe.throw(
				_(
					"Active RKSV POS Profiles must use the print format {0}. Repair these profiles "
					"from the affected POS Profile first: {1}."
				).format(
					RKSV_PRINT_FORMAT,
					", ".join(row["pos_profile"] for row in mismatches),
				),
				title=_("RKSV print format required"),
			)


@frappe.whitelist(methods=["POST"])
def check_pos_print_formats(repair: int | str = 0, pos_profile: str | None = None) -> dict:
	"""Check or repair the RKSV print format from the affected POS Profile."""

	frappe.only_for(("System Manager", "Accounts Manager"))
	if pos_profile:
		frappe.get_doc("POS Profile", pos_profile).check_permission("write" if cint(repair) else "read")
	before = get_pos_profile_print_format_mismatches(pos_profile=pos_profile)
	updated = []
	if cint(repair):
		if not _is_system_manager():
			frappe.throw(_("Only a System Manager may change POS Profile print formats."))
		updated = setup_pos_profile_print_formats(pos_profile=pos_profile)
	return {
		"required_print_format": RKSV_PRINT_FORMAT,
		"mismatches": before,
		"updated": updated,
		"valid": not get_pos_profile_print_format_mismatches(pos_profile=pos_profile),
	}
