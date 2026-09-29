import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from erpnext_fiskaly_sign_at.services.compliance import (
	CONTROLLED_PRINT_CLAIM_FLAG,
	FISCAL_RECEIPT_STATUSES,
	RKSV_PRINT_FORMAT,
)

CONTROLLED_PRINT_OUTPUTS = {"html", "pdf"}


def _lock_pos_print_row(pos_invoice: str):
	rows = frappe.db.sql(
		"""select name, fiskaly_print_count, fiskaly_first_printed_at
		from `tabPOS Invoice` where name = %s for update""",
		(pos_invoice,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("The POS invoice no longer exists."), frappe.DoesNotExistError)
	return rows[0]


def _render_claimed_pos_invoice(doc, output: str, ordinal: int, auto_print: bool):
	previous_claim = getattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, None)
	form_dict = getattr(frappe.local, "form_dict", None)
	if form_dict is None:
		form_dict = frappe._dict()
		frappe.local.form_dict = form_dict
	had_trigger_print = "trigger_print" in form_dict
	previous_trigger_print = form_dict.get("trigger_print")

	setattr(
		frappe.flags,
		CONTROLLED_PRINT_CLAIM_FLAG,
		frappe._dict(pos_invoice=doc.name, ordinal=ordinal),
	)
	# Preserve Frappe's normal auto-print behaviour, but only when this endpoint
	# explicitly requested it. Ignore any unrelated request parameter.
	frappe.local.form_dict.trigger_print = int(output == "html" and auto_print)
	try:
		return frappe.get_print(
			"POS Invoice",
			doc.name,
			RKSV_PRINT_FORMAT,
			doc=doc,
			as_pdf=output == "pdf",
			no_letterhead=1,
		)
	finally:
		if previous_claim is None:
			frappe.flags.pop(CONTROLLED_PRINT_CLAIM_FLAG, None)
		else:
			setattr(frappe.flags, CONTROLLED_PRINT_CLAIM_FLAG, previous_claim)
		if had_trigger_print:
			frappe.local.form_dict.trigger_print = previous_trigger_print
		else:
			frappe.local.form_dict.pop("trigger_print", None)


def _set_inline_print_response(pos_invoice: str, output: str, content):
	filename = str(pos_invoice).replace("/", "-").replace(" ", "-")
	if output == "pdf":
		frappe.local.response.update(
			{
				"type": "pdf",
				"filename": f"{filename}.pdf",
				"filecontent": content,
			}
		)
		return
	frappe.local.response.update(
		{
			"type": "download",
			"filename": f"{filename}.html",
			"filecontent": content,
			"content_type": "text/html; charset=utf-8",
			"display_content_as": "inline",
		}
	)


@frappe.whitelist(methods=["POST"])
def render_pos_receipt(pos_invoice: str, output: str = "html", auto_print: int = 1):
	"""Atomically classify and render one server-generated RKSV receipt copy."""

	output = (output or "html").strip().lower()
	if output not in CONTROLLED_PRINT_OUTPUTS:
		frappe.throw(_("RKSV print output must be HTML or PDF."))

	# Frappe's core print helper accepts read permission as sufficient. A fiscal
	# output is deliberately stricter and requires the explicit Print permission.
	frappe.has_permission("POS Invoice", ptype="print", throw=True)
	locked = _lock_pos_print_row(pos_invoice)
	doc = frappe.get_doc("POS Invoice", pos_invoice)
	doc.check_permission("print")
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Only submitted POS invoices can be printed as RKSV receipts."))

	is_fiscal = doc.get("fiskaly_status") in FISCAL_RECEIPT_STATUSES
	count = cint(locked.fiskaly_print_count)
	ordinal = count + 1 if is_fiscal else 0
	content = _render_claimed_pos_invoice(doc, output, ordinal, bool(cint(auto_print)))

	if is_fiscal:
		values = {"fiskaly_print_count": ordinal}
		if not locked.fiskaly_first_printed_at:
			values["fiskaly_first_printed_at"] = now_datetime()
		frappe.db.set_value("POS Invoice", doc.name, values, update_modified=False)

	_set_inline_print_response(doc.name, output, content)
