frappe.query_reports["Fiskaly Receipt Status"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("Von"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("Bis"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "company",
			label: __("Unternehmen"),
			fieldtype: "Link",
			options: "Company",
		},
		{
			fieldname: "register",
			label: __("Registrierkasse"),
			fieldtype: "Link",
			options: "Fiskaly Register",
		},
		{
			fieldname: "environment",
			label: __("Betriebsumgebung"),
			fieldtype: "Select",
			options: "\nTEST\nLIVE",
		},
		{
			fieldname: "status",
			label: __("Fiskalisierungsstatus"),
			fieldtype: "Select",
			options:
				"\nPREPARED\nSIGNING\nSIGNED\nSUBSTITUTE_SIGNED\nOFFLINE_PENDING\nRETRYING\nACTION_REQUIRED",
		},
	],
};
