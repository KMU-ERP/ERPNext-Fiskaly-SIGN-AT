import frappe
from frappe import _


def _get_allowed_companies(user: str | None = None) -> tuple[str, ...]:
	"""Return every Company visible to the report user, including User Permission filtering."""

	return tuple(
		frappe.get_list(
			"Company",
			pluck="name",
			user=user or frappe.session.user,
			limit_page_length=0,
			order_by="name",
			reference_doctype="Fiskaly Receipt",
		)
	)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.from_date or not filters.to_date:
		frappe.throw(_("From Date and To Date are required."))
	allowed_companies = _get_allowed_companies()
	if filters.company and filters.company not in allowed_companies:
		frappe.throw(
			_("You do not have permission to access Company {0}.").format(frappe.bold(filters.company)),
			frappe.PermissionError,
		)
	query_params = dict(filters)
	conditions = ["date(receipt.creation) between %(from_date)s and %(to_date)s"]
	if allowed_companies:
		conditions.append("register.company in %(allowed_companies)s")
		query_params["allowed_companies"] = allowed_companies
	else:
		# A report user without a readable Company must never receive cross-company rows.
		conditions.append("1 = 0")
	for fieldname in ("register", "environment", "status"):
		if filters.get(fieldname):
			conditions.append(f"receipt.`{fieldname}` = %({fieldname})s")
	if filters.company:
		conditions.append("register.company = %(company)s")
	data = frappe.db.sql(
		f"""
		select receipt.creation, receipt.name as receipt, receipt.pos_invoice, register.company,
			receipt.register, receipt.receipt_kind, receipt.provider, receipt.environment,
			receipt.status, receipt.receipt_number, receipt.fon_validation_status,
			receipt.annual_compliance_status, receipt.print_evidence_at,
			receipt.attempt_count, receipt.last_error
		from `tabFiskaly Receipt` receipt
		join `tabFiskaly Register` register on register.name = receipt.register
		where {" and ".join(conditions)}
		order by receipt.creation desc
		""",
		query_params,
		as_dict=True,
	)
	columns = [
		{"fieldname": "creation", "label": _("Erstellt"), "fieldtype": "Datetime", "width": 150},
		{
			"fieldname": "receipt",
			"label": _("Fiskalbeleg"),
			"fieldtype": "Link",
			"options": "Fiskaly Receipt",
			"width": 180,
		},
		{
			"fieldname": "pos_invoice",
			"label": _("POS-Beleg"),
			"fieldtype": "Link",
			"options": "POS Invoice",
			"width": 170,
		},
		{
			"fieldname": "company",
			"label": _("Unternehmen"),
			"fieldtype": "Link",
			"options": "Company",
			"width": 160,
		},
		{
			"fieldname": "register",
			"label": _("Registrierkasse"),
			"fieldtype": "Link",
			"options": "Fiskaly Register",
			"width": 150,
		},
		{"fieldname": "receipt_kind", "label": _("Belegart"), "fieldtype": "Data", "width": 100},
		{"fieldname": "provider", "label": _("Provider"), "fieldtype": "Data", "width": 130},
		{"fieldname": "environment", "label": _("Umgebung"), "fieldtype": "Data", "width": 90},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 140},
		{"fieldname": "receipt_number", "label": _("Belegnummer"), "fieldtype": "Data", "width": 120},
		{"fieldname": "fon_validation_status", "label": _("FON-Prüfung"), "fieldtype": "Data", "width": 110},
		{
			"fieldname": "annual_compliance_status",
			"label": _("Jahresbeleg"),
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"fieldname": "print_evidence_at",
			"label": _("Drucknachweis"),
			"fieldtype": "Datetime",
			"width": 150,
		},
		{"fieldname": "attempt_count", "label": _("Versuche"), "fieldtype": "Int", "width": 80},
		{"fieldname": "last_error", "label": _("Letzter Fehler"), "fieldtype": "Small Text", "width": 300},
	]
	return columns, data
