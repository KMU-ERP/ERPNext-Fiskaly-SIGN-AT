frappe.listview_settings["Fiskaly Register"] = {
	add_fields: [
		"provider_state",
		"decommission_status",
		"outage_status",
		"outage_ended_at",
		"active",
		"initialized",
	],
	get_indicator(doc) {
		if (doc.provider_state === "DEFECTIVE") {
			return [__("Defekt"), "red", "provider_state,=,DEFECTIVE"];
		}
		if (
			doc.provider_state === "DECOMMISSIONED" ||
			["EVIDENCE_REQUIRED", "COMPLETE"].includes(doc.decommission_status)
		) {
			if (doc.decommission_status === "EVIDENCE_REQUIRED") {
				return [
					__("Außer Betrieb · Abschluss offen"),
					"orange",
					"decommission_status,=,EVIDENCE_REQUIRED",
				];
			}
			return [__("Außer Betrieb"), "orange", "provider_state,=,DECOMMISSIONED"];
		}
		if (
			doc.provider_state === "OUTAGE" ||
			(["ACTIVE", "ACTION_REQUIRED"].includes(doc.outage_status) && !doc.outage_ended_at)
		) {
			return [__("Ausgefallen"), "yellow", "outage_status,in,ACTIVE|ACTION_REQUIRED"];
		}
		if (doc.active && doc.initialized && doc.provider_state === "INITIALIZED") {
			return [__("In Betrieb"), "green", "provider_state,=,INITIALIZED"];
		}
	},
};
