import frappe
from frappe.utils import flt


def execute():
	"""Add Austria's 4.9% special bucket without changing an existing manual mapping."""
	for register_name in frappe.get_all("Fiskaly Register", pluck="name"):
		register = frappe.get_doc("Fiskaly Register", register_name)
		if any(abs(flt(row.tax_rate) - 4.9) < 0.000001 for row in register.vat_mappings):
			continue
		# Insert only the child row. Running the full Register validator here would
		# make an upgrade depend on completing newly introduced address/configuration
		# fields before `bench migrate` can finish.
		frappe.get_doc(
			{
				"doctype": "Fiskaly VAT Mapping",
				"parent": register_name,
				"parenttype": "Fiskaly Register",
				"parentfield": "vat_mappings",
				"tax_rate": 4.9,
				"v1_bucket": "special",
			}
		).insert(ignore_permissions=True)
