import frappe

from erpnext_fiskaly_sign_at.print_utils import fiskaly_qr_svg
from erpnext_fiskaly_sign_at.services.fiscalization import _public_result, fiscalize_receipt


@frappe.whitelist(methods=["GET"])
def get_fiscalization_status(pos_invoice: str):
	"""Return fiscal status without causing a provider or database side effect."""

	frappe.get_doc("POS Invoice", pos_invoice).check_permission("read")
	receipt = frappe.db.get_value("Fiskaly Receipt", {"pos_invoice": pos_invoice}, "name")
	if not receipt:
		status = frappe.db.get_value("POS Invoice", pos_invoice, "fiskaly_status")
		return {"status": status or "NOT_CONFIGURED"}
	result = _public_result(frappe.get_doc("Fiskaly Receipt", receipt))
	qr_data = (
		result.get("offline_qr_code_data")
		if result.get("status") in {"SUBSTITUTE_SIGNED", "OFFLINE_PENDING"}
		else result.get("qr_code_data")
	)
	result["qr_svg"] = str(fiskaly_qr_svg(qr_data, scale=3)) if qr_data else ""
	return result


@frappe.whitelist(methods=["POST"])
def fiscalize_now(pos_invoice: str):
	# Retrying can write several fiscal records and trigger a remote PUT. Read or
	# share permission is therefore insufficient; this is deliberately separate
	# from the side-effect-free status endpoint above.
	frappe.get_doc("POS Invoice", pos_invoice).check_permission("submit")
	receipt = frappe.db.get_value("Fiskaly Receipt", {"pos_invoice": pos_invoice}, "name")
	if not receipt:
		status = frappe.db.get_value("POS Invoice", pos_invoice, "fiskaly_status")
		return {"status": status or "NOT_CONFIGURED"}
	return fiscalize_receipt(receipt)
