import hashlib
import hmac

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now_datetime
from frappe.utils.file_manager import save_file

from erpnext_fiskaly_sign_at.install import RKSV_CLOSING_PRINT_FORMAT, SPECIAL_RECEIPT_HTML


def _check_evidence_write_permission(receipt):
	"""Authorize immutable evidence writes through the owning register.

	Fiskaly Receipt deliberately has no generic write permission because its fiscal
	payload is immutable. Evidence actions therefore require an explicit compliance
	role plus write permission on the receipt's owning register.
	"""

	receipt.check_permission("read")
	frappe.only_for(("System Manager", "Accounts Manager"))
	register = getattr(receipt, "register", None)
	if not register:
		frappe.throw(_("Receipt evidence cannot be changed without an owning Fiskaly register."))
	frappe.get_doc("Fiskaly Register", register).check_permission("write")


def _attach_private_evidence_file(receipt, file_url: str) -> str:
	"""Bind an owned, unattached private upload to its immutable receipt evidence."""

	file_name = frappe.db.get_value("File", {"file_url": file_url, "is_private": 1}, "name")
	if not file_name:
		frappe.throw(_("Print evidence must reference an existing private File."))
	frappe.db.sql("select name from `tabFile` where name = %s for update", (file_name,))
	file_doc = frappe.get_doc("File", file_name)
	if not file_doc.is_private or file_doc.file_url != file_url:
		frappe.throw(_("Print evidence must reference an existing private File."))

	if file_doc.attached_to_doctype == "Fiskaly Receipt" and file_doc.attached_to_name == receipt.name:
		return file_doc.name
	if file_doc.attached_to_doctype or file_doc.attached_to_name:
		frappe.throw(_("The private evidence File is already attached to another document."))

	# Dialog Attach fields create an owned but unattached private File because the
	# immutable receipt grants no generic write permission. Verify File ownership
	# through Frappe's permission engine, then bind it under the same transaction.
	file_doc.check_permission("write")
	frappe.db.set_value(
		"File",
		file_doc.name,
		{
			"attached_to_doctype": "Fiskaly Receipt",
			"attached_to_name": receipt.name,
		},
		update_modified=False,
	)
	return file_doc.name


class FiskalyReceipt(Document):
	IMMUTABLE_FIELDS = (
		"receipt_uuid",
		"pos_invoice",
		"company",
		"company_address_display",
		"register",
		"connection",
		"provider",
		"environment",
		"receipt_type",
		"receipt_kind",
		"fiscal_period",
		"idempotency_key",
		"payload_sha256",
		"request_payload",
		"provider_request_payload",
		"provider_request_payload_sha256",
		"offline_issued_at",
		"offline_qr_code_data",
		"offline_receipt_snapshot",
		"offline_snapshot_sha256",
		"status",
		"provider_receipt_id",
		"intention_id",
		"receipt_number",
		"signed_at",
		"serial_number",
		"qr_code_data",
		"signature_value",
		"signed",
		"hints",
		"response_payload",
		"response_payload_sha256",
		"fon_validation_status",
		"fon_validation_at",
		"fon_validation_response",
		"annual_compliance_status",
		"annual_action_required_reason",
		"verification_deadline",
		"verification_status",
		"verification_at",
		"verification_by",
		"verification_note",
		"print_evidence_at",
		"print_evidence_by",
		"print_evidence_file",
		"attempt_count",
		"next_retry_at",
		"last_attempt_at",
		"last_error",
		"request_id",
	)

	def validate(self):
		if self.is_new():
			return
		old = self.get_doc_before_save()
		if not old:
			return
		changed = [
			fieldname for fieldname in self.IMMUTABLE_FIELDS if old.get(fieldname) != self.get(fieldname)
		]
		if changed:
			frappe.throw(
				_("Immutable fiscal receipt evidence cannot be changed: {0}").format(", ".join(changed))
			)

	def before_rename(self, old_name, new_name, merge=False):
		raise PermissionError("Fiskaly receipt identifiers are immutable")

	def on_trash(self):
		if not getattr(frappe.flags, "in_uninstall", False):
			frappe.throw(_("Fiskaly fiscal receipts cannot be deleted."))

	def before_print(self, print_settings=None):
		if self.receipt_kind not in {
			"START",
			"MONTHLY",
			"YEARLY",
			"MANUAL_ZERO",
			"RECOVERY",
			"CLOSING",
			"CONTROL",
		}:
			return
		form_dict = getattr(frappe.local, "form_dict", None) or {}
		requested_format = form_dict.get("print_format") or form_dict.get("format")
		if requested_format != RKSV_CLOSING_PRINT_FORMAT:
			frappe.throw(_("RKSV special receipts may only use the protected closing-receipt print format."))
		definition = frappe.db.get_value(
			"Print Format",
			RKSV_CLOSING_PRINT_FORMAT,
			["doc_type", "print_format_type", "custom_format", "disabled", "html"],
			as_dict=True,
		)
		if not definition or (
			definition.doc_type != "Fiskaly Receipt"
			or definition.print_format_type != "Jinja"
			or not cint(definition.custom_format)
			or cint(definition.disabled)
			or definition.html != SPECIAL_RECEIPT_HTML
		):
			frappe.throw(_("The protected closing-receipt print format failed its integrity check."))
		if self.status not in {"SIGNED", "SUBSTITUTE_SIGNED"}:
			frappe.throw(
				_("This RKSV special receipt cannot be printed while its status is {0}.").format(
					frappe.bold(self.status or _("not set"))
				)
			)
		required = (
			"provider_receipt_id",
			"receipt_number",
			"signed_at",
			"serial_number",
			"qr_code_data",
			"signature_value",
			"response_payload",
			"response_payload_sha256",
		)
		missing = [fieldname for fieldname in required if not self.get(fieldname)]
		response_hash_valid = bool(
			self.response_payload and self.response_payload_sha256
		) and hmac.compare_digest(
			hashlib.sha256(self.response_payload.encode("utf-8")).hexdigest(),
			self.response_payload_sha256,
		)
		if missing or not response_hash_valid:
			frappe.throw(_("The immutable provider evidence for this RKSV special receipt is incomplete."))
		if self.status == "SIGNED" and not self.signed:
			frappe.throw(_("A normally signed RKSV special receipt must contain a provider signature."))
		if self.receipt_kind == "START" and self.fon_validation_status != "SUCCESS":
			frappe.throw(
				_("The initialization receipt cannot be printed without successful FinanzOnline validation.")
			)

	@frappe.whitelist(methods=["POST"])
	def create_and_archive_print_evidence(self):
		"""Render the protected RKSV receipt and retain it as private evidence."""
		self.check_permission("print")
		_check_evidence_write_permission(self)
		if self.receipt_kind not in {"YEARLY", "CLOSING"}:
			frappe.throw(_("Automatic print evidence is available only for annual or closing receipts."))
		if self.print_evidence_at:
			if not self.print_evidence_file:
				frappe.throw(_("The existing print evidence has no retained archive file."))
			return {
				"print_evidence_at": self.print_evidence_at,
				"print_evidence_file": self.print_evidence_file,
				"idempotent": True,
			}
		if self.status not in {"SIGNED", "SUBSTITUTE_SIGNED"}:
			frappe.throw(_("Only a completed RKSV receipt can be archived."))

		try:
			pdf = frappe.get_print(
				self.doctype,
				self.name,
				RKSV_CLOSING_PRINT_FORMAT,
				doc=self,
				as_pdf=True,
				no_letterhead=1,
				pdf_generator="wkhtmltopdf",
			)
		except Exception:
			frappe.log_error(
				title=f"RKSV archive PDF generation failed for {self.name}",
				message=frappe.get_traceback(),
			)
			raise frappe.ValidationError(
				_(
					"PDF-Archivierung vorübergehend nicht möglich: Der geschützte RKSV-Beleg "
					"konnte vom lokalen PDF-Dienst nicht erzeugt werden. Die Fiskaly-Belegdaten "
					"bleiben erhalten. Bitte versuchen Sie die PDF-Archivierung erneut."
				)
			) from None
		if not isinstance(pdf, bytes) or not pdf:
			frappe.throw(_("The protected RKSV receipt PDF could not be generated."))
		label = "Schlussbeleg" if self.receipt_kind == "CLOSING" else "Jahresbeleg"
		filename = f"{label}-{frappe.scrub(self.register or self.name)}-{frappe.scrub(self.name)}.pdf"
		file_doc = save_file(filename, pdf, self.doctype, self.name, is_private=1)
		result = self.mark_print_evidence(file_doc.file_url)
		return {**result, "idempotent": False}

	@frappe.whitelist(methods=["POST"])
	def mark_print_evidence(self, file_url: str | None = None):
		self.check_permission("print")
		_check_evidence_write_permission(self)
		if self.receipt_kind not in {"YEARLY", "CLOSING"}:
			frappe.throw(_("Print evidence can be recorded only for annual or closing RKSV receipts."))
		if self.receipt_kind == "CLOSING" and not file_url:
			frappe.throw(_("A private archive file is mandatory for the closing receipt."))
		if file_url:
			_attach_private_evidence_file(self, file_url)
		if self.print_evidence_at:
			if (self.print_evidence_file or None) != (file_url or None):
				frappe.throw(_("Print/archive evidence is immutable and was already recorded."))
			return {
				"print_evidence_at": self.print_evidence_at,
				"print_evidence_file": self.print_evidence_file,
			}
		values = {
			"print_evidence_at": now_datetime(),
			"print_evidence_by": frappe.session.user,
			"print_evidence_file": file_url,
		}
		if self.receipt_kind == "YEARLY":
			values.update(
				{
					"annual_compliance_status": (
						"COMPLETE"
						if self.fon_validation_status == "SUCCESS"
						or (self.verification_by and self.verification_at)
						else "PENDING"
					),
					"annual_action_required_reason": None,
				}
			)
		self.db_set(values)
		return {"print_evidence_at": self.print_evidence_at, "print_evidence_file": file_url}

	@frappe.whitelist(methods=["POST"])
	def record_annual_verification(self, status: str, note: str | None = None):
		_check_evidence_write_permission(self)
		if self.receipt_kind != "YEARLY":
			frappe.throw(_("BMF annual verification can only be recorded for a yearly receipt."))
		status = (status or "").upper()
		if status not in {"SUCCESS", "FAILED"}:
			frappe.throw(_("Annual verification status must be SUCCESS or FAILED."))
		note = (note or "").strip() or None
		if self.verification_at:
			if self.verification_status != status or (self.verification_note or None) != note:
				frappe.throw(_("Annual verification evidence is immutable and was already recorded."))
			return {"verification_status": self.verification_status, "verification_at": self.verification_at}
		self.db_set(
			{
				"verification_status": status,
				"verification_at": now_datetime(),
				"verification_by": frappe.session.user,
				"verification_note": note,
				"annual_compliance_status": (
					"COMPLETE" if status == "SUCCESS" and self.print_evidence_at else "PENDING"
				),
				"annual_action_required_reason": None,
			}
		)
		return {"verification_status": status, "verification_at": self.verification_at}


def check_annual_receipt_requirements(as_of=None) -> list[dict]:
	"""Escalate annual receipts missing validation or print/archive evidence by 15 February."""
	today = frappe.utils.getdate(as_of or frappe.utils.nowdate())
	issues = []
	for row in frappe.get_all(
		"Fiskaly Receipt",
		filters={"receipt_kind": "YEARLY", "verification_deadline": ["<=", today]},
		fields=[
			"name",
			"fon_validation_status",
			"verification_status",
			"verification_by",
			"print_evidence_at",
			"annual_compliance_status",
		],
	):
		verified = row.fon_validation_status == "SUCCESS" or (
			row.verification_status == "SUCCESS" and row.verification_by
		)
		reasons = []
		if not verified:
			reasons.append(_("annual receipt validation is not successful"))
		if not row.print_evidence_at:
			reasons.append(_("print/archive evidence is missing"))
		if not reasons:
			if row.annual_compliance_status != "COMPLETE":
				frappe.db.set_value(
					"Fiskaly Receipt",
					row.name,
					{"annual_compliance_status": "COMPLETE", "annual_action_required_reason": None},
					update_modified=False,
				)
			continue
		reason = "; ".join(reasons)
		if row.annual_compliance_status != "ACTION_REQUIRED":
			frappe.db.set_value(
				"Fiskaly Receipt",
				row.name,
				{
					"annual_compliance_status": "ACTION_REQUIRED",
					"annual_action_required_reason": reason,
				},
				update_modified=False,
			)
		issues.append({"receipt": row.name, "reason": reason})
	return issues
