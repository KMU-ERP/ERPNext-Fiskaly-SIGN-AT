const FISKALY_CONNECTION_CRITICAL_FIELDS = [
	"active",
	"company",
	"provider",
	"environment",
	"api_key",
	"api_secret",
	"api_version",
	"scope_identifier",
	"base_url_override",
	"tax_id_number",
	"vat_id_number",
	"fon_participant_id",
	"fon_user_id",
	"fon_user_pin",
];

function fiskaly_connection_snapshot(frm) {
	return Object.fromEntries(
		FISKALY_CONNECTION_CRITICAL_FIELDS.map((fieldname) => [fieldname, frm.doc[fieldname]])
	);
}

function fiskaly_connection_changed_fields(frm) {
	const baseline = frm.__fiskaly_baseline || {};
	return FISKALY_CONNECTION_CRITICAL_FIELDS.filter(
		(fieldname) => baseline[fieldname] !== frm.doc[fieldname]
	);
}

function fiskaly_connection_changed_field_labels(frm, fieldnames) {
	return fieldnames.map((fieldname) => __(frm.get_docfield(fieldname).label)).join(", ");
}

function configure_provider_environment(frm) {
	const unified = frm.doc.provider === "SIGN_AT_UNIFIED";
	frm.set_df_property("environment", "options", unified ? "TEST" : "TEST\nLIVE");
	if (unified && frm.doc.environment === "LIVE") {
		frm.set_value("environment", "TEST");
		frappe.msgprint({
			title: __("Unified LIVE gesperrt"),
			indicator: "red",
			message: __(
				"Die neue Unified SIGN AT API ist noch nicht für den Produktivbetrieb freigegeben. Die Verbindung wurde auf TEST zurückgesetzt."
			),
		});
	}
}

function configure_provider_fields(frm) {
	const unified = frm.doc.provider === "SIGN_AT_UNIFIED";
	const is_system_manager = frappe.user.has_role("System Manager");
	frm.toggle_display("api_version", !unified);
	frm.toggle_display("base_url", !unified);
	frm.toggle_display("base_url_override", !unified && is_system_manager);
	// This confirmation is required server-side for both API generations.
	frm.toggle_display("critical_change_confirmation", true);
	frm.set_df_property("base_url_override", "read_only", !unified && is_system_manager ? 0 : 1);

	if (unified && frm.doc.scope_identifier) {
		frm.get_field("scope_identifier")?.set_data([
			{
				value: frm.doc.scope_identifier,
				label: frm.doc.scope_identifier,
				description: __("Aktuell gespeicherter Scope"),
			},
		]);
	}
}

function apply_unified_scope_options(frm, scopes) {
	const field = frm.get_field("scope_identifier");
	if (!field) return;
	const scope = Array.isArray(scopes) ? scopes.find((entry) => entry?.value) : null;
	if (!scope) {
		frappe.msgprint({
			title: __("Organisation nicht ermittelbar"),
			indicator: "red",
			message: __(
				"fiskaly hat im Token keine Organisation für diesen API-Key ausgewiesen. Der Scope wird aus Sicherheitsgründen nicht manuell gesetzt."
			),
		});
		return;
	}
	const option = {
		value: String(scope.value),
		label: scope.label || String(scope.value),
		description: scope.description || __("Ausschließlich die Organisation dieses API-Keys"),
	};
	field.set_data([option]);
	if (frm.doc.scope_identifier !== option.value) {
		frm.set_value("scope_identifier", option.value);
	}
	frappe.show_alert({
		message: __("Organisation des API-Keys übernommen. Bitte speichern."),
		indicator: "green",
	});
}

function load_unified_scopes(frm) {
	if (frm.doc.provider !== "SIGN_AT_UNIFIED") return;
	if (frm.is_new() || frm.is_dirty()) {
		frappe.msgprint({
			title: __("Verbindung zuerst speichern"),
			indicator: "blue",
			message: __(
				"Speichern Sie API-Key und API-Secret. Danach können die für diesen Zugang verfügbaren Organisationen sicher geladen werden."
			),
		});
		return;
	}
	frm.call("get_unified_scopes").then(({ message }) =>
		apply_unified_scope_options(frm, message || [])
	);
}

function format_austrian_tax_id_number(value) {
	if (!value) return value;
	const compact = String(value).trim().replace(/[-/\s]/g, "");
	if (/^[0-9]{9}$/.test(compact)) {
		return `${compact.slice(0, 2)}-${compact.slice(2, 5)}/${compact.slice(5)}`;
	}
	if (/^[0-9]{7}$/.test(compact)) {
		return `${compact.slice(0, 3)}/${compact.slice(3)}`;
	}
	return value;
}

function format_austrian_vat_id_number(value) {
	if (!value) return value;
	const normalized = String(value).replace(/\s/g, "").toUpperCase();
	if (/^ATU[0-9]{8}$/.test(normalized)) return normalized;
	if (/^U[0-9]{8}$/.test(normalized)) return `AT${normalized}`;
	return value;
}

function configure_fon_fields(frm) {
	const test = frm.doc.environment !== "LIVE";
	const unified = frm.doc.provider === "SIGN_AT_UNIFIED";
	const help = test
		? unified
			? __(
					'<div class="alert alert-info"><strong>Unified TEST</strong><br>Beim Anlegen des österreichischen Abgabepflichtigen übergibt ERPNext diese Daten an fiskaly. Verwenden Sie syntaktisch gültige Dummy-Daten; echte FinanzOnline-Zugangsdaten sind in TEST nicht erforderlich.</div>'
			  )
			: __(
				'<div class="alert alert-info"><strong>TEST-Umgebung</strong><br>fiskaly simuliert die Kommunikation mit FinanzOnline. Verwenden Sie ausschließlich syntaktisch gültige Dummy-Daten und keine produktiven Zugangsdaten.</div>'
			  )
		: __(
				'<div class="alert alert-warning"><strong>LIVE-Umgebung</strong><br>Verwenden Sie die echten Daten eines eigens angelegten FinanzOnline-Registrierkassen-Webservice-Benutzers. fiskaly benötigt diese grundsätzlich nur für die erstmalige Authentifizierung.</div>'
		  );

	frm.set_df_property("fon_help_html", "options", help);
	frm.set_df_property(
		"fon_participant_id",
		"description",
		test
			? __("TEST: Dummywert mit 8 bis 12 Buchstaben oder Ziffern, z. B. TESTAT01.")
			: __(
					"LIVE: Echte Teilnehmer-Identifikation des FinanzOnline-Webservice-Benutzers; 8 bis 12 Buchstaben oder Ziffern."
			  )
	);
	frm.set_df_property(
		"fon_user_id",
		"description",
		test
			? unified
				? __("Unified TEST: Dummywert mit 8 bis 12 Zeichen, z. B. TESTUSER.")
				: __("SIGN AT v1 TEST: Dummywert mit 5 bis 12 Zeichen, z. B. TEST01.")
			: __(
					"LIVE: Echte Benutzer-Identifikation des FinanzOnline-Webservice-Benutzers; 5 bis 12 Zeichen."
			  )
	);
	frm.set_df_property(
		"fon_user_pin",
		"description",
		test
			? unified
				? __("Unified TEST: Dummy-PIN mit 8 bis 128 Zeichen, z. B. TESTPIN1. Keine echte PIN verwenden.")
				: __("SIGN AT v1 TEST: Dummy-PIN mit 5 bis 128 Zeichen, z. B. TEST01. Keine echte PIN verwenden.")
			: __("LIVE: Echte PIN mit 5 bis 128 Zeichen; sie wird verschlüsselt gespeichert.")
	);
}

frappe.ui.form.on("Fiskaly API Connection", {
	onload(frm) {
		frm.__fiskaly_baseline = fiskaly_connection_snapshot(frm);
	},

	refresh(frm) {
		configure_provider_environment(frm);
		configure_provider_fields(frm);
		configure_fon_fields(frm);
		const formatted_tax_id = format_austrian_tax_id_number(frm.doc.tax_id_number);
		if (formatted_tax_id !== frm.doc.tax_id_number) {
			frm.set_value("tax_id_number", formatted_tax_id);
		}
		const formatted_vat_id = format_austrian_vat_id_number(frm.doc.vat_id_number);
		if (formatted_vat_id !== frm.doc.vat_id_number) {
			frm.set_value("vat_id_number", formatted_vat_id);
		}

		const live = frm.doc.environment === "LIVE";
		const unified = frm.doc.provider === "SIGN_AT_UNIFIED";
		frm.set_intro(
			unified
				? __("Unified SIGN AT: technische Vorabintegration, ausschließlich TEST.")
				: live
				? __(
						"SIGN AT v1 LIVE: produktive Zugangsdaten und FinanzOnline-Webservice-Benutzer verwenden."
				  )
				: __(
						"SIGN AT v1 TEST: zuerst Authentifizierung, Provisionierung und Startbeleg vollständig testen."
				  ),
			unified ? "orange" : live ? "red" : "blue"
		);

		if (!frm.is_new()) {
			frm.add_custom_button(
				__("Verbindung testen"),
				() => {
					if (frm.is_dirty()) {
						frappe.msgprint(
							__("Speichern Sie die Verbindung vor dem Verbindungstest.")
						);
						return;
					}
					frm.call("test_connection").then(async ({ message }) => {
						await frm.reload_doc();
						if (message && message.success) {
							if (frm.doc.provider === "SIGN_AT_UNIFIED") {
								apply_unified_scope_options(frm, message.result?.scopes || []);
							}
							frappe.show_alert({
								message: __("Verbindung erfolgreich"),
								indicator: "green",
							});
							return;
						}
						frappe.msgprint({
							title: __("Verbindung fehlgeschlagen"),
							indicator: "red",
							message:
								(message && message.error) ||
								__("Der Provider konnte nicht erfolgreich geprüft werden."),
						});
					});
				},
				__("Aktionen")
			);

			if (frm.doc.provider === "SIGN_AT_V1") {
				frm.add_custom_button(
					__("FinanzOnline authentifizieren"),
					() => {
						if (frm.is_dirty()) {
							frappe.msgprint(
								__(
									"Speichern Sie die Verbindung vor der FinanzOnline-Authentifizierung."
								)
							);
							return;
						}
						frm.call("authenticate_fon").then(() => {
							frappe.show_alert({
								message: __("FinanzOnline erfolgreich authentifiziert"),
								indicator: "green",
							});
						});
					},
					__("Aktionen")
				);
			}
		}
	},

	provider(frm) {
		configure_provider_environment(frm);
		configure_provider_fields(frm);
		configure_fon_fields(frm);
		if (frm.doc.provider === "SIGN_AT_V1") {
			frm.set_value("api_version", "v1");
		} else if (!frm.doc.api_version || frm.doc.api_version === "v1") {
			frm.set_value("api_version", "2026-06-01");
		}
	},

	tax_id_number(frm) {
		const formatted = format_austrian_tax_id_number(frm.doc.tax_id_number);
		if (formatted !== frm.doc.tax_id_number) {
			frm.set_value("tax_id_number", formatted);
		}
	},

	vat_id_number(frm) {
		const formatted = format_austrian_vat_id_number(frm.doc.vat_id_number);
		if (formatted !== frm.doc.vat_id_number) {
			frm.set_value("vat_id_number", formatted);
		}
	},

	environment(frm) {
		configure_provider_environment(frm);
		configure_provider_fields(frm);
		configure_fon_fields(frm);
		if (frm.doc.environment === "LIVE") {
			frappe.msgprint({
				title: __("Produktive Verbindung"),
				indicator: "red",
				message: __(
					"LIVE-Verbindungen dürfen nur von System Managern und erst nach erfolgreichem TEST-Lauf eingerichtet werden."
				),
			});
		}
	},

	load_unified_scopes(frm) {
		load_unified_scopes(frm);
	},

	before_save(frm) {
		const changed = fiskaly_connection_changed_fields(frm);
		if (!changed.length || frm.doc.critical_change_confirmation || frm.__fiskaly_confirmed) {
			return;
		}
		frappe.validated = false;
		frappe.confirm(
			__(
				"Provider, Umgebung, Zugangsdaten oder Endpunkt wurden geändert ({0}). Sind Mandant und Zielumgebung eindeutig geprüft?",
				[fiskaly_connection_changed_field_labels(frm, changed)]
			),
			() => {
				frm.__fiskaly_confirmed = true;
				frm.set_value("critical_change_confirmation", 1).then(() => frm.save());
			}
		);
	},

	after_save(frm) {
		frm.__fiskaly_confirmed = false;
		frm.__fiskaly_baseline = fiskaly_connection_snapshot(frm);
	},
});
