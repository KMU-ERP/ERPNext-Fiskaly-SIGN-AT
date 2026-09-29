import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


class FiskalyDEP7Export(Document):
	IMMUTABLE_EVIDENCE_FIELDS = (
		"creation",
		"register",
		"connection",
		"purpose",
		"export_scope",
		"fiscal_year",
		"fiscal_quarter",
		"date_from",
		"date_to",
		"generated_at",
		"retention_until",
		"provider_file_id",
		"download_url",
		"provider_response",
		"export_file",
		"file_hash",
		"file_size",
		"integrity_verified_at",
		"supplementary_export_file",
		"supplementary_file_hash",
		"supplementary_file_size",
		"supplementary_integrity_verified_at",
		"external_copy_confirmed",
		"external_copy_confirmed_at",
		"external_copy_confirmed_by",
		"external_storage_reference",
	)

	def validate(self):
		if bool(self.date_from) != bool(self.date_to):
			frappe.throw(_("DEP7 period exports require both start and end."))
		if self.date_from and self.date_to and get_datetime(self.date_from) > get_datetime(self.date_to):
			frappe.throw(_("DEP7 export start must not be after its end."))
		if self.purpose in {"QUARTERLY_BACKUP", "DECOMMISSION_FINAL"}:
			if self.date_from or self.date_to:
				frappe.throw(_("Compliance DEP7 backups must be complete snapshots, not period exports."))
			if self.purpose == "QUARTERLY_BACKUP" and (
				not self.fiscal_year or int(self.fiscal_quarter or 0) not in {1, 2, 3, 4}
			):
				frappe.throw(_("Quarterly DEP7 backups require a fiscal year and quarter 1-4."))
			self.export_scope = "COMPLETE"
		else:
			self.export_scope = "PERIOD" if self.date_from and self.date_to else "COMPLETE"
		if self.is_new() and self.purpose == "DECOMMISSION_FINAL":
			from erpnext_fiskaly_sign_at.api.exports import _validate_final_decommission_export

			_validate_final_decommission_export(self.register, export_created_at=self.creation)
			if not getattr(frappe.flags, "in_final_decommission_export_creation", False):
				frappe.throw(
					_("Create a final decommissioning DEP7 only through the controlled register action.")
				)
		if not self.is_new():
			old = self.get_doc_before_save()
			if old:
				changed = [
					fieldname
					for fieldname in self.IMMUTABLE_EVIDENCE_FIELDS
					if old.get(fieldname) != self.get(fieldname)
				]
				if changed:
					frappe.throw(
						_("Immutable DEP7 evidence cannot be changed: {0}").format(", ".join(changed))
					)

	def on_trash(self):
		if not getattr(frappe.flags, "in_uninstall", False):
			frappe.throw(_("DEP7 export records are retained as fiscal evidence and cannot be deleted."))

	@frappe.whitelist(methods=["POST"])
	def verify_export_integrity(self):
		# The hash calculation itself is harmless, but this method records a durable
		# audit timestamp. A read-only share must not be able to write that evidence.
		self.check_permission("write")
		if (
			self.status != "READY"
			or not self.export_file
			or not self.file_hash
			or not self.supplementary_export_file
			or not self.supplementary_file_hash
		):
			frappe.throw(_("No completed DEP7 file is available for verification."))
		actual_hash = _file_sha256(self.export_file)
		supplementary_hash = _file_sha256(self.supplementary_export_file)
		if actual_hash != self.file_hash or supplementary_hash != self.supplementary_file_hash:
			frappe.throw(_("DEP7 integrity check failed: SHA-256 does not match."))
		verified_at = self.integrity_verified_at or self.supplementary_integrity_verified_at or now_datetime()
		missing_values = {}
		if not self.integrity_verified_at:
			missing_values["integrity_verified_at"] = verified_at
		if not self.supplementary_integrity_verified_at:
			missing_values["supplementary_integrity_verified_at"] = verified_at
		if missing_values:
			self.db_set(missing_values)
		return {
			"valid": True,
			"sha256": actual_hash,
			"supplementary_sha256": supplementary_hash,
		}

	@frappe.whitelist(methods=["POST"])
	def mark_external_copy_confirmed(self, storage_reference: str):
		self.check_permission("write")
		if self.status != "READY" or not self.export_file or not self.file_hash:
			frappe.throw(_("Complete and verify the DEP7 export before confirming an external copy."))
		storage_reference = (storage_reference or "").strip()
		if not storage_reference:
			frappe.throw(_("An external storage reference is required."))
		if self.external_copy_confirmed:
			if self.external_storage_reference != storage_reference:
				frappe.throw(
					_("The immutable external-copy evidence was already recorded with another reference.")
				)
			return {
				"external_copy_confirmed": 1,
				"external_copy_confirmed_at": self.external_copy_confirmed_at,
				"external_copy_confirmed_by": self.external_copy_confirmed_by,
				"external_storage_reference": self.external_storage_reference,
				"action_required_reason": self.action_required_reason,
			}
		self.verify_export_integrity()
		values = {
			"external_copy_confirmed": 1,
			"external_copy_confirmed_at": now_datetime(),
			"external_copy_confirmed_by": frappe.session.user,
			"external_storage_reference": storage_reference,
			"action_required_reason": None,
		}
		self.db_set(values)
		return values


def prevent_compliance_file_deletion(doc, method=None):
	"""Keep files referenced by immutable RKSV evidence records from being deleted."""
	if getattr(frappe.flags, "in_uninstall", False) or not doc.file_url:
		return

	dep7_export = frappe.db.exists("Fiskaly DEP7 Export", {"export_file": doc.file_url})
	if dep7_export:
		frappe.throw(
			_("File {0} is retained by DEP7 export {1} and cannot be deleted.").format(
				doc.file_url, dep7_export
			)
		)

	supplementary_export = frappe.db.exists(
		"Fiskaly DEP7 Export", {"supplementary_export_file": doc.file_url}
	)
	if supplementary_export:
		frappe.throw(
			_("File {0} is retained by DEP7 export {1} and cannot be deleted.").format(
				doc.file_url, supplementary_export
			)
		)

	yearly_receipt = frappe.db.exists(
		"Fiskaly Receipt",
		{
			"receipt_kind": ["in", ["YEARLY", "CLOSING"]],
			"print_evidence_file": doc.file_url,
		},
	)
	if yearly_receipt:
		frappe.throw(
			_("File {0} is retained as print evidence for annual receipt {1} and cannot be deleted.").format(
				doc.file_url, yearly_receipt
			)
		)


def _file_sha256(file_url: str) -> str:
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode("utf-8")
	return hashlib.sha256(content).hexdigest()
