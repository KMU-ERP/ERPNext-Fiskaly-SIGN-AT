const RKSV_CRITICAL_FIELDS = [
	"enabled",
	"operating_environment",
	"enforce_pos_only_cash_receipts",
];

function rksv_settings_snapshot(frm) {
	return Object.fromEntries(
		RKSV_CRITICAL_FIELDS.map((fieldname) => [fieldname, frm.doc[fieldname]])
	);
}

function rksv_changed_fields(frm) {
	const baseline = frm.__rksv_baseline || {};
	return RKSV_CRITICAL_FIELDS.filter((fieldname) => baseline[fieldname] !== frm.doc[fieldname]);
}

function rksv_changed_field_labels(frm, fieldnames) {
	return fieldnames.map((fieldname) => __(frm.get_docfield(fieldname).label)).join(", ");
}

frappe.ui.form.on("Fiskaly Settings", {
	onload(frm) {
		frm.__rksv_baseline = rksv_settings_snapshot(frm);
	},

	refresh(frm) {
		const live = frm.doc.operating_environment === "LIVE";
		frm.set_intro(
			live
				? __(
						"LIVE-Betrieb: Änderungen beeinflussen die gesetzliche Belegkette. Prüfen Sie offene Belege und Kassenstatus vor dem Speichern."
				  )
				: __(
						"Beginnen Sie in TEST. Unified SIGN AT ist derzeit ausschließlich für Tests freigegeben."
				  ),
			live ? "red" : "blue"
		);
		frm.dashboard.set_headline_alert(
			live
				? __("Produktive RKSV-Umgebung")
				: __("TEST-Umgebung – keine produktiven Fiskalbelege"),
			live ? "red" : "blue"
		);
		frm.set_df_property("allow_unified_live", "hidden", 1);
	},

	operating_environment(frm) {
		if (frm.doc.operating_environment === "LIVE") {
			frappe.msgprint({
				title: __("Produktivumgebung gewählt"),
				indicator: "red",
				message: __(
					"LIVE wird serverseitig nur mit Zeitzone Europe/Vienna, einer aktiven initialisierten SIGN AT v1-Kasse und dem RKSV-Druckformat zugelassen."
				),
			});
		}
	},

	before_save(frm) {
		const changed = rksv_changed_fields(frm);
		if (!changed.length || frm.doc.critical_change_confirmation || frm.__rksv_confirmed) {
			return;
		}

		frappe.validated = false;
		frappe.confirm(
			__(
				"Diese Änderung kann die gesetzliche RKSV-Belegkette beeinflussen ({0}). Haben Sie offene Belege, Kassenstatus und Druckformat geprüft?",
				[rksv_changed_field_labels(frm, changed)]
			),
			() => {
				frm.__rksv_confirmed = true;
				frm.set_value("critical_change_confirmation", 1).then(() => frm.save());
			}
		);
	},

	after_save(frm) {
		frm.__rksv_confirmed = false;
		frm.__rksv_baseline = rksv_settings_snapshot(frm);
	},
});
