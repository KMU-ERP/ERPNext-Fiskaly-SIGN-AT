const V1_BUCKET_LABELS = {
	standard: "20 % – Normalsteuersatz",
	reduced1: "10 % – Ermäßigter Steuersatz",
	reduced2: "13 % – Ermäßigter Steuersatz",
	special: "19 % / 4,9 % – Besonderer Steuersatz",
	zero: "0 % / steuerfrei – Nullsatz",
};

const SCU_ASSIGNMENT_OPTIONS = [
	{ value: "EXISTING", label: "Bestehende SCU auswählen" },
	{ value: "NEW", label: "Neue SCU beim Provisionieren erstellen" },
];

const SCU_STATE_LABELS = {
	PENDING: "Wird angelegt",
	CREATED: "Angelegt",
	INITIALIZED: "Initialisiert",
};

function scu_assignment_is_locked(frm) {
	return Boolean(frm.doc.initialized || frm.doc.provider_register_id || frm.doc.start_receipt);
}

function set_scu_options(frm, scus = []) {
	const current = frm.doc.signature_creation_unit_id;
	const options = [{ value: "", label: __("Bitte SCU auswählen") }];
	for (const scu of scus) {
		options.push({
			value: scu.id,
			label: __("{0} · {1} · {2}", [
				scu.legal_entity_name,
				__(SCU_STATE_LABELS[scu.state] || scu.state),
				scu.id,
			]),
		});
	}
	if (current && !scus.some((scu) => scu.id === current)) {
		options.push({
			value: current,
			label: scu_assignment_is_locked(frm)
				? __("Zugeordnete SCU · {0}", [current])
				: __("Nicht mehr auswählbar · {0}", [current]),
			disabled: !scu_assignment_is_locked(frm),
			selected: true,
		});
	}
	frm.set_df_property("signature_creation_unit_id", "options", options);
}

function configure_scu_assignment(frm, force_reload = false) {
	frm.set_df_property("scu_assignment_mode", "options", [
		...SCU_ASSIGNMENT_OPTIONS.map((option) => ({
			value: option.value,
			label: __(option.label),
		})),
	]);

	if (frm.doc.provider !== "SIGN_AT_V1") return Promise.resolve();
	const locked = scu_assignment_is_locked(frm);
	frm.set_df_property("scu_assignment_mode", "read_only", locked);
	frm.set_df_property("signature_creation_unit_id", "read_only", locked);

	if (locked || frm.doc.scu_assignment_mode !== "EXISTING") {
		set_scu_options(frm);
		return Promise.resolve();
	}
	if (!frm.doc.connection || !frm.doc.company) {
		set_scu_options(frm);
		return Promise.resolve();
	}

	const request_key = `${frm.doc.connection}:${frm.doc.company}`;
	if (!force_reload && frm._loaded_scu_request_key === request_key) {
		set_scu_options(frm, frm._available_scus || []);
		return Promise.resolve();
	}
	frm.set_df_property(
		"signature_creation_unit_id",
		"description",
		__("Passende SCUs werden direkt von Fiskaly geladen …")
	);
	return frappe
		.call({
			method: "erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register.get_available_scus",
			type: "GET",
			args: {
				connection: frm.doc.connection,
				company: frm.doc.company,
			},
		})
		.then(({ message }) => {
			if (`${frm.doc.connection}:${frm.doc.company}` !== request_key) return;
			frm._loaded_scu_request_key = request_key;
			frm._available_scus = message || [];
			set_scu_options(frm, frm._available_scus);
			frm.set_df_property(
				"signature_creation_unit_id",
				"description",
				frm._available_scus.length
					? __(
							"Nur SCUs derselben Umgebung und UID beziehungsweise Steuernummer werden angezeigt."
					  )
					: __(
							"Keine passende bestehende SCU gefunden. Wählen Sie „Neue SCU beim Provisionieren erstellen“."
					  )
			);
		});
}

function sync_connection_metadata(frm) {
	if (!frm.doc.connection) return Promise.resolve();
	return frappe.db
		.get_value("Fiskaly API Connection", frm.doc.connection, ["provider", "environment"])
		.then(({ message }) =>
			frm.set_value({
				provider: message?.provider,
				environment: message?.environment,
			})
		);
}

function ensure_fiskaly_workflow_styles() {
	if (document.getElementById("fiskaly-workflow-styles")) return;
	$("<style>", { id: "fiskaly-workflow-styles" })
		.text(
			`
			.fiskaly-workflow-summary { padding: 12px 14px; margin-bottom: 14px; border-radius: 8px; background: var(--control-bg); }
			.fiskaly-workflow-step { display: grid; grid-template-columns: 42px 1fr; gap: 12px; min-height: 104px; }
			.fiskaly-workflow-marker { position: relative; display: flex; justify-content: center; }
			.fiskaly-workflow-marker span { z-index: 1; display: flex; width: 32px; height: 32px; align-items: center; justify-content: center; border-radius: 50%; border: 2px solid var(--gray-400); background: var(--card-bg); font-weight: 700; }
			.fiskaly-workflow-step:not(:last-child) .fiskaly-workflow-marker::after { content: ""; position: absolute; top: 32px; bottom: 0; width: 2px; background: var(--gray-300); }
			.fiskaly-workflow-step.complete .fiskaly-workflow-marker span { border-color: var(--green-500); background: var(--green-500); color: white; }
			.fiskaly-workflow-step.complete .fiskaly-workflow-marker::after { background: var(--green-400); }
			.fiskaly-workflow-step.active .fiskaly-workflow-marker span { border-color: var(--blue-500); color: var(--blue-600); box-shadow: 0 0 0 4px var(--blue-100); }
			.fiskaly-workflow-step.error .fiskaly-workflow-marker span { border-color: var(--red-500); color: var(--red-600); }
			.fiskaly-workflow-content { padding: 3px 0 22px; }
			.fiskaly-workflow-content h5 { margin: 0 0 5px; font-size: 14px; }
			.fiskaly-workflow-content p { margin: 0 0 9px; color: var(--text-muted); }
			.fiskaly-workflow-content .btn { margin-right: 8px; }
			.fiskaly-workflow-link { font-size: 12px; }
		`
		)
		.appendTo("head");
}

function decommission_workflow_steps(state) {
	const closing = state.closing_receipt || {};
	const dep7 = state.final_dep7_export || {};
	const export_processing = ["QUEUED", "PROCESSING"].includes(dep7.status);
	const export_error = ["FAILED", "ACTION_REQUIRED"].includes(dep7.status);
	return [
		{
			key: "closing",
			title: __("Schlussbeleg erstellen und archivieren"),
			done: state.closing_archived,
			description: state.closing_archived
				? __("Der FON-geprüfte Schlussbeleg ist als private PDF archiviert.")
				: state.closing_ready
				? __("Der Schlussbeleg liegt vor und wird jetzt automatisch als PDF archiviert.")
				: __(
						"Die Kasse wird bei Fiskaly geschlossen, der Schlussbeleg geprüft und privat archiviert."
				  ),
			action: "closing",
			action_label: state.closing_ready
				? __("Schlussbeleg automatisch archivieren")
				: __("Kasse schließen und Beleg ablegen"),
			link: closing.name
				? {
						doctype: "Fiskaly Receipt",
						name: closing.name,
						label: __("Schlussbeleg öffnen"),
				  }
				: null,
		},
		{
			key: "export",
			title: __("Finale DEP7-Sicherung erstellen"),
			done: state.export_ready,
			working: export_processing,
			error: export_error,
			description: state.export_ready
				? __("Vollständige DEP7- und Zusatzdatei wurden privat erzeugt.")
				: export_processing
				? __(
						"Die Sicherung wird im Hintergrund erstellt. Der Status wird automatisch aktualisiert."
				  )
				: dep7.last_error ||
				  dep7.action_required_reason ||
				  __("Erstellt eine vollständige finale DEP7-Sicherung."),
			action: "export",
			action_label: export_error
				? __("Neue Sicherung starten")
				: __("DEP7-Sicherung erstellen"),
			link: dep7.name
				? { doctype: "Fiskaly DEP7 Export", name: dep7.name, label: __("DEP7 öffnen") }
				: null,
		},
		{
			key: "integrity",
			title: __("SHA-256-Integrität prüfen"),
			done: state.integrity_verified,
			description: state.integrity_verified
				? __("Beide Dateien stimmen mit ihren gespeicherten SHA-256-Werten überein.")
				: __("Prüft DEP7 und Zusatzdatei vollständig gegen ihre gespeicherten Hashwerte."),
			action: "integrity",
			action_label: __("SHA-256 jetzt prüfen"),
		},
		{
			key: "external",
			title: __("Externe Kopie bestätigen"),
			done: state.external_copy_confirmed,
			description: state.external_copy_confirmed
				? __("Externe Sicherung bestätigt: {0}", [dep7.external_storage_reference])
				: __(
						"Kopieren Sie beide Dateien auf ein externes Medium und erfassen Sie dessen Referenz."
				  ),
			action: "external",
			action_label: __("Externe Kopie erfassen"),
		},
		{
			key: "complete",
			title: __("Außerbetriebnahme abschließen"),
			done: state.complete,
			description: state.complete
				? __("Die Kasse ist vollständig und revisionssicher abgeschlossen.")
				: __("Prüft alle Nachweise und schließt den lokalen Kassenlebenszyklus ab."),
			action: "complete",
			action_label: __("Abschluss finalisieren"),
		},
	];
}

function render_decommission_workflow(state) {
	const steps = decommission_workflow_steps(state);
	const current = steps.findIndex((step) => !step.done);
	const completed = steps.filter((step) => step.done).length;
	const escape = (value) => frappe.utils.escape_html(String(value || ""));
	return `
		<div class="fiskaly-workflow-summary">
			<strong>${escape(__("Geführte Außerbetriebnahme"))}</strong><br>
			<span class="text-muted">${escape(
				__("{0} von {1} Schritten abgeschlossen", [completed, steps.length])
			)}</span>
		</div>
		<div class="fiskaly-workflow">
			${steps
				.map((step, index) => {
					const blocked = current !== -1 && index > current;
					const status_class = step.done
						? "complete"
						: step.error && index === current
						? "error"
						: index === current
						? "active"
						: "pending";
					const action =
						!step.done && !blocked && !step.working
							? `<button class="btn btn-primary btn-sm" data-workflow-action="${escape(
									step.action
							  )}">${escape(step.action_label)}</button>`
							: step.working
							? `<span class="indicator-pill orange">${escape(
									__("In Bearbeitung")
							  )}</span>`
							: "";
					const link = step.link
						? `<a href="#" class="fiskaly-workflow-link" data-workflow-doctype="${escape(
								step.link.doctype
						  )}" data-workflow-name="${escape(step.link.name)}">${escape(
								step.link.label
						  )}</a>`
						: "";
					return `<div class="fiskaly-workflow-step ${status_class}">
						<div class="fiskaly-workflow-marker"><span>${step.done ? "✓" : index + 1}</span></div>
						<div class="fiskaly-workflow-content">
							<h5>${escape(step.title)}</h5><p>${escape(step.description)}</p>${action}${link}
						</div>
					</div>`;
				})
				.join("")}
		</div>`;
}

function open_decommission_workflow(frm) {
	ensure_fiskaly_workflow_styles();
	const dialog = new frappe.ui.Dialog({
		title: __("Geführte Außerbetriebnahme – {0}", [frm.doc.name]),
		size: "large",
		fields: [{ fieldname: "workflow", fieldtype: "HTML" }],
	});
	let workflow_state = null;
	let refresh_timer = null;
	let closed = false;
	let loading = false;
	let action_running = false;
	let load_promise = null;

	const run = async (message, action) => {
		if (action_running) return;
		action_running = true;
		if (refresh_timer) {
			clearTimeout(refresh_timer);
			refresh_timer = null;
		}
		frappe.dom.freeze(message);
		try {
			if (loading && load_promise) await load_promise;
			await action();
			await load();
		} finally {
			frappe.dom.unfreeze();
			action_running = false;
			if (
				!closed &&
				!refresh_timer &&
				["QUEUED", "PROCESSING"].includes(workflow_state?.final_dep7_export?.status)
			) {
				refresh_timer = setTimeout(load, 2500);
			}
		}
	};

	const bind_actions = () => {
		const body = dialog.get_field("workflow").$wrapper;
		body.off(".fiskaly_workflow");
		body.on("click.fiskaly_workflow", "[data-workflow-doctype]", function (event) {
			event.preventDefault();
			frappe.set_route("Form", this.dataset.workflowDoctype, this.dataset.workflowName);
		});
		body.on("click.fiskaly_workflow", "[data-workflow-action]", function () {
			const action = this.dataset.workflowAction;
			if (action === "closing") {
				const close_and_archive = (reason) =>
					run(__("Schlussbeleg wird erstellt und archiviert …"), () =>
						frm.call("decommission_and_archive_closing_receipt", { reason })
					);
				if (workflow_state.provider_state === "DECOMMISSIONED") {
					close_and_archive(__("Vorhandenen Schlussbeleg archivieren"));
					return;
				}
				frappe.prompt(
					[
						{
							fieldname: "reason",
							label: __("Begründung für die Außerbetriebnahme"),
							fieldtype: "Small Text",
							reqd: 1,
						},
					],
					(values) =>
						frappe.confirm(
							__(
								"Die Kasse jetzt unwiderruflich bei Fiskaly schließen und den Schlussbeleg automatisch archivieren?"
							),
							() => close_and_archive(values.reason)
						),
					__("Schritt 1: Kasse schließen"),
					__("Weiter")
				);
				return;
			}
			if (action === "export") {
				run(__("Finale DEP7-Sicherung wird gestartet …"), () =>
					frappe.call({
						method: "erpnext_fiskaly_sign_at.api.exports.create_final_decommission_export",
						args: { register: frm.doc.name },
					})
				);
				return;
			}
			if (action === "integrity") {
				run(__("SHA-256-Integrität wird geprüft …"), () =>
					frm.call("verify_final_dep7_integrity")
				);
				return;
			}
			if (action === "external") {
				frappe.prompt(
					[
						{
							fieldname: "storage_reference",
							label: __("Referenz des externen Speichermediums"),
							fieldtype: "Small Text",
							description: __(
								"Beispiel: WORM-Archiv 2026/08 oder Tresor-USB-02. Bestätigen Sie erst nach der tatsächlichen Kopie beider Dateien."
							),
							reqd: 1,
						},
					],
					(values) =>
						run(__("Externer Sicherungsnachweis wird gespeichert …"), () =>
							frm.call("confirm_final_dep7_external_copy", values)
						),
					__("Schritt 4: Externe Kopie"),
					__("Bestätigen")
				);
				return;
			}
			if (action === "complete") {
				frappe.confirm(
					__(
						"Alle Nachweise nochmals serverseitig prüfen und die Außerbetriebnahme endgültig abschließen?"
					),
					() =>
						run(__("Außerbetriebnahme wird abgeschlossen …"), async () => {
							await frm.call("complete_decommission");
							await frm.reload_doc();
						})
				);
			}
		});
	};

	const load = () => {
		if (closed) return Promise.resolve();
		if (loading) return load_promise;
		loading = true;
		if (refresh_timer) {
			clearTimeout(refresh_timer);
			refresh_timer = null;
		}
		load_promise = (async () => {
			let should_poll = false;
			try {
				const { message } = await frappe.call({
					method: "erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register.get_decommission_workflow",
					type: "GET",
					args: { register: frm.doc.name },
				});
				if (closed) return;
				workflow_state = message;
				dialog.get_field("workflow").$wrapper.html(render_decommission_workflow(message));
				bind_actions();
				should_poll = ["QUEUED", "PROCESSING"].includes(message.final_dep7_export?.status);
			} finally {
				loading = false;
				load_promise = null;
				if (!closed && !action_running && should_poll) {
					refresh_timer = setTimeout(load, 2500);
				}
			}
		})();
		return load_promise;
	};

	dialog.set_primary_action(__("Status aktualisieren"), load);
	dialog.$wrapper.on("hidden.bs.modal", () => {
		closed = true;
		if (refresh_timer) clearTimeout(refresh_timer);
		frm.reload_doc();
	});
	dialog.show();
	load();
}

function ensure_register_status_styles() {
	if (document.getElementById("fiskaly-register-status-styles")) return;
	$("<style>", { id: "fiskaly-register-status-styles" })
		.text(
			`
			.fiskaly-register-status { border: 1px solid var(--border-color); border-left-width: 5px; border-radius: 8px; margin-bottom: 16px; padding: 14px 16px; }
			.fiskaly-register-status.decommissioned { border-left-color: var(--orange-500); background: var(--orange-50, #fff8e1); }
			.fiskaly-register-status.defective { border-left-color: var(--red-500); background: var(--red-50, #fff5f5); }
			.fiskaly-register-status-header { align-items: center; display: flex; gap: 10px; margin-bottom: 10px; }
			.fiskaly-register-status-header h4 { font-size: 15px; margin: 0; }
			.fiskaly-register-status-grid { display: grid; gap: 8px 20px; grid-template-columns: minmax(120px, 0.25fr) 1fr; }
			.fiskaly-register-status-label { color: var(--text-muted); font-size: 12px; font-weight: 600; }
			.fiskaly-register-status-value { overflow-wrap: anywhere; }
			.fiskaly-register-status-link { margin-top: 12px; }
		`
		)
		.appendTo("head");
}

function render_register_terminal_summary(frm) {
	const field = frm.get_field("intro_html");
	if (!field) return;
	const wrapper = field.$wrapper;
	const is_defective = frm.doc.provider_state === "DEFECTIVE";
	const is_decommissioned =
		frm.doc.provider_state === "DECOMMISSIONED" ||
		["EVIDENCE_REQUIRED", "COMPLETE"].includes(frm.doc.decommission_status);
	if (!is_defective && !is_decommissioned) {
		wrapper.html(field.df.options || "");
		const is_outage =
			frm.doc.provider_state === "OUTAGE" ||
			(["ACTIVE", "ACTION_REQUIRED"].includes(frm.doc.outage_status) &&
				!frm.doc.outage_ended_at);
		if (is_outage) {
			frm.page.set_indicator(__("Ausgefallen"), "yellow");
		} else if (
			frm.doc.active &&
			frm.doc.initialized &&
			frm.doc.provider_state === "INITIALIZED"
		) {
			frm.page.set_indicator(__("In Betrieb"), "green");
		}
		return;
	}

	ensure_register_status_styles();
	const color = is_defective ? "red" : "orange";
	const label = is_defective ? __("Defekt") : __("Außer Betrieb");
	frm.page.set_indicator(label, color);
	wrapper.html(
		`<div class="fiskaly-register-status ${
			is_defective ? "defective" : "decommissioned"
		}"><span class="text-muted">${__("Statuszusammenfassung wird geladen …")}</span></div>`
	);

	const request_key = `${frm.doc.name}:${frm.doc.modified}`;
	frm._terminal_status_request_key = request_key;
	frappe
		.call({
			method: "erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register.get_terminal_status_summary",
			type: "GET",
			args: { register: frm.doc.name },
		})
		.then(({ message }) => {
			if (
				frm._terminal_status_request_key !== request_key ||
				!message?.terminal ||
				(message.event?.register && frm.doc.name !== message.event.register)
			) {
				if (!message?.terminal) wrapper.html(field.df.options || "");
				return;
			}
			const escape = (value) => frappe.utils.escape_html(String(value || ""));
			const event = message.event || {};
			const reason = message.reason || __("Keine Begründung im Kassenprotokoll gefunden.");
			const occurred_at = event.event_time
				? frappe.datetime.str_to_user(event.event_time)
				: __("Nicht dokumentiert");
			const actor = event.actor || __("Nicht dokumentiert");
			const status_class = message.kind === "DEFECTIVE" ? "defective" : "decommissioned";
			const indicator_color = message.kind === "DEFECTIVE" ? "red" : "orange";
			wrapper.html(`
				<div class="fiskaly-register-status ${status_class}">
					<div class="fiskaly-register-status-header">
						<span class="indicator-pill ${indicator_color}">${escape(message.label)}</span>
						<h4>${escape(message.status_detail)}</h4>
					</div>
					<div class="fiskaly-register-status-grid">
						<div class="fiskaly-register-status-label">${escape(__("Begründung"))}</div>
						<div class="fiskaly-register-status-value">${escape(reason)}</div>
						<div class="fiskaly-register-status-label">${escape(__("Dokumentiert am"))}</div>
						<div class="fiskaly-register-status-value">${escape(occurred_at)}</div>
						<div class="fiskaly-register-status-label">${escape(__("Dokumentiert von"))}</div>
						<div class="fiskaly-register-status-value">${escape(actor)}</div>
					</div>
					${
						event.name
							? `<button class="btn btn-default btn-xs fiskaly-register-status-link" data-lifecycle-event="${escape(
									event.name
							  )}">${escape(__("Protokolleintrag öffnen"))}</button>`
							: ""
					}
				</div>
			`);
			wrapper
				.off("click.fiskaly_terminal_status")
				.on("click.fiskaly_terminal_status", "[data-lifecycle-event]", function () {
					frappe.set_route(
						"Form",
						"Fiskaly Lifecycle Event",
						this.dataset.lifecycleEvent
					);
				});
		});
}

function configure_v1_bucket_field(frm) {
	const grid = frm.get_field("vat_mappings")?.grid;
	if (!grid) return;

	const options = Object.entries(V1_BUCKET_LABELS).map(([value, label]) => ({
		value,
		label: __(label),
	}));
	grid.update_docfield_property("v1_bucket", "options", options);

	// Select formatters normally show the stored value in an editable grid.
	// Keep the technical API value hidden there as well.
	const meta_field = frappe.meta.docfield_map["Fiskaly VAT Mapping"]?.v1_bucket;
	if (meta_field) {
		meta_field.formatter = (value) => __(V1_BUCKET_LABELS[value] || value);
	}
}

frappe.ui.form.on("Fiskaly Register", {
	setup(frm) {
		configure_v1_bucket_field(frm);
	},

	refresh(frm) {
		configure_v1_bucket_field(frm);
		configure_scu_assignment(frm);
		if (frm.is_new()) return;
		render_register_terminal_summary(frm);
		if (frm.doc.decommission_status === "EVIDENCE_REQUIRED") {
			frm.add_custom_button(
				__("Außerbetriebnahme-Assistent fortsetzen"),
				() => open_decommission_workflow(frm),
				__("RKSV-Lebenszyklus")
			);
			frm.add_custom_button(
				__("Kassen- und Ausfallprotokoll"),
				() =>
					frappe.set_route("List", "Fiskaly Lifecycle Event", {
						register: frm.doc.name,
					}),
				__("Anzeigen")
			);
			return;
		}
		if (frm.doc.decommission_status === "COMPLETE") {
			frm.add_custom_button(
				__("Abgeschlossene Außerbetriebnahme anzeigen"),
				() => open_decommission_workflow(frm),
				__("RKSV-Lebenszyklus")
			);
		}

		if (!frm.doc.initialized) {
			if (!["DECOMMISSIONED", "DEFECTIVE"].includes(frm.doc.provider_state)) {
				frm.add_custom_button(__("Provisionieren und initialisieren"), () => {
					if (frm.is_dirty()) {
						frappe.msgprint(
							__(
								"Speichern Sie zuerst die SCU-Bereitstellung und alle Kasseneinstellungen."
							)
						);
						return;
					}
					frappe.confirm(
						__(
							"Fiskalressourcen anlegen, registrieren und den Startbeleg verbindlich über FinanzOnline prüfen?"
						),
						() => frm.call("provision_register").then(() => frm.reload_doc())
					);
				});
			}
			if (
				frm.doc.provider === "SIGN_AT_V1" &&
				(frm.doc.provider_state === "DEFECTIVE" ||
					(frm.doc.provider_state === "DECOMMISSIONED" &&
						frm.doc.decommission_status === "COMPLETE")) &&
				frm.doc.signature_creation_unit_id &&
				frappe.user.has_role("System Manager")
			) {
				frm.add_custom_button(
					__("SCU separat außer Betrieb nehmen"),
					() => {
						frappe.prompt(
							[
								{
									fieldname: "reason",
									label: __("Begründung für die SCU-Außerbetriebnahme"),
									fieldtype: "Small Text",
									reqd: 1,
								},
							],
							(values) =>
								frappe.confirm(
									__(
										"Die Signaturerstellungseinheit endgültig außer Betrieb nehmen? Der Server blockiert dies, solange sie noch von einer aktiven Kasse verwendet wird."
									),
									() =>
										frm
											.call("decommission_scu", values)
											.then(() => frm.reload_doc())
								),
							__("SCU endgültig außer Betrieb nehmen")
						);
					},
					__("RKSV-Lebenszyklus")
				);
			}
			frm.add_custom_button(
				__("Kassen- und Ausfallprotokoll"),
				() => {
					frappe.set_route("List", "Fiskaly Lifecycle Event", {
						register: frm.doc.name,
					});
				},
				__("Anzeigen")
			);
			return;
		}

		if (frm.doc.provider === "SIGN_AT_V1") {
			frm.add_custom_button(
				__("Provider-Status aktualisieren"),
				() => {
					frm.call("refresh_provider_state").then(() => frm.reload_doc());
				},
				__("RKSV-Lebenszyklus")
			);

			frm.add_custom_button(
				__("Automatische Belege synchronisieren"),
				() => {
					frm.call("sync_automatic_receipts").then(() => frm.reload_doc());
				},
				__("Abschlussbelege")
			);

			frm.add_custom_button(
				__("Kontroll-Nullbeleg erstellen"),
				() => {
					frappe.prompt(
						[
							{
								fieldname: "reference",
								label: __("Kontrollreferenz"),
								fieldtype: "Data",
							},
						],
						(values) =>
							frm
								.call("create_manual_control_receipt", values)
								.then(({ message }) => {
									if (!message || !message.receipt) return;
									if (
										!["SIGNED", "SUBSTITUTE_SIGNED"].includes(message.status)
									) {
										frappe.msgprint({
											title: __("Kontrollbeleg noch nicht druckbereit"),
											indicator: "orange",
											message: __(
												"Der unveränderliche Kontrollbeleg-Vorgang wurde gespeichert und wird mit derselben UUID wiederholt. Status: {0}",
												[message.status]
											),
										});
									}
									frappe.set_route("Form", "Fiskaly Receipt", message.receipt);
								}),
						__("Manueller RKSV-Kontrollbeleg")
					);
				},
				__("Abschlussbelege")
			);

			if (!["ACTIVE", "ACTION_REQUIRED"].includes(frm.doc.outage_status)) {
				frm.add_custom_button(
					__("Kassenausfall melden"),
					() => {
						frappe.prompt(
							[
								{
									fieldname: "reason",
									label: __("Begründung"),
									fieldtype: "Small Text",
									reqd: 1,
								},
							],
							(values) =>
								frm
									.call("start_outage", {
										...values,
										outage_scope: "CASH_REGISTER",
									})
									.then(() => frm.reload_doc()),
							__("Kassenausfall / FinanzOnline-Meldung")
						);
					},
					__("RKSV-Lebenszyklus")
				);
			} else {
				frm.add_custom_button(
					__("Ausfall beenden"),
					() => {
						frappe.prompt(
							[
								{
									fieldname: "reason",
									label: __("Wiederanlauf-Notiz"),
									fieldtype: "Small Text",
								},
							],
							(values) =>
								frm.call("end_outage", values).then(() => frm.reload_doc()),
							__("RKSV-Betrieb wiederaufnehmen")
						);
					},
					__("RKSV-Lebenszyklus")
				);
				if (
					frm.doc.outage_scope === "CASH_REGISTER" &&
					["ACTION_REQUIRED", "BEGIN_REPORTED"].includes(frm.doc.fon_outage_status) &&
					!frm.doc.fon_outage_begin_reference
				) {
					frm.add_custom_button(
						__("FON-Nachweis für Ausfallbeginn erfassen"),
						() => {
							frappe.prompt(
								[
									{
										fieldname: "reference",
										label: __(
											"FON-Bestätigungsreferenz für den Ausfallbeginn"
										),
										fieldtype: "Data",
										description: __(
											"Hier ausschließlich die Referenz der BEGIN-Meldung eintragen. Die END-Meldung wird nach dem Wiederanlauf getrennt nachgewiesen."
										),
										reqd: 1,
									},
									{
										fieldname: "note",
										label: __("Anmerkung"),
										fieldtype: "Small Text",
									},
								],
								(values) =>
									frm
										.call("record_manual_fon_report", {
											...values,
											phase: "BEGIN",
										})
										.then(() => frm.reload_doc()),
								__("FinanzOnline: Ausfallbeginn separat nachweisen")
							);
						},
						__("RKSV-Lebenszyklus")
					);
				}
				if (
					frm.doc.outage_scope === "CASH_REGISTER" &&
					frm.doc.fon_outage_begin_reference &&
					frm.doc.outage_ended_at &&
					!frm.doc.fon_outage_end_reference
				) {
					frm.add_custom_button(
						__("FON-Nachweis für Ausfallende erfassen"),
						() => {
							frappe.prompt(
								[
									{
										fieldname: "reference",
										label: __("FON-Bestätigungsreferenz für das Ausfallende"),
										fieldtype: "Data",
										description: __(
											"Hier ausschließlich die Referenz der END-Meldung eintragen. Der bereits erfasste BEGIN-Nachweis bleibt unverändert."
										),
										reqd: 1,
									},
									{
										fieldname: "note",
										label: __("Anmerkung"),
										fieldtype: "Small Text",
									},
								],
								(values) =>
									frm
										.call("record_manual_fon_report", {
											...values,
											phase: "END",
										})
										.then(() => frm.reload_doc()),
								__("FinanzOnline: Ausfallende separat nachweisen")
							);
						},
						__("RKSV-Lebenszyklus")
					);
				}
			}

			frm.add_custom_button(
				__("Vierteljährliche DEP7-Sicherung erstellen"),
				() => {
					const today = frappe.datetime.str_to_obj(frappe.datetime.get_today());
					let quarter = Math.floor(today.getMonth() / 3);
					let year = today.getFullYear();
					if (quarter === 0) {
						quarter = 4;
						year -= 1;
					}
					frappe.prompt(
						[
							{
								fieldname: "fiscal_year",
								label: __("Geschäftsjahr"),
								fieldtype: "Int",
								default: year,
								reqd: 1,
							},
							{
								fieldname: "fiscal_quarter",
								label: __("Quartal"),
								fieldtype: "Select",
								options: "1\n2\n3\n4",
								default: String(quarter),
								reqd: 1,
							},
						],
						(values) =>
							frappe
								.call({
									method: "erpnext_fiskaly_sign_at.api.exports.create_quarterly_backup",
									args: { register: frm.doc.name, ...values },
								})
								.then(({ message }) =>
									frappe.set_route("Form", "Fiskaly DEP7 Export", message)
								),
						__("Vollständige vierteljährliche DEP7-Sicherung")
					);
				},
				__("DEP7")
			);

			frm.add_custom_button(
				__("Vollständigen DEP7-Prüfexport erstellen"),
				() => {
					frappe
						.call({
							method: "erpnext_fiskaly_sign_at.api.exports.create_complete_dep7_export",
							args: { register: frm.doc.name, purpose: "AUDIT" },
						})
						.then(({ message }) =>
							frappe.set_route("Form", "Fiskaly DEP7 Export", message)
						);
				},
				__("DEP7")
			);

			frm.add_custom_button(
				__("Geführte Außerbetriebnahme"),
				() => open_decommission_workflow(frm),
				__("RKSV-Lebenszyklus")
			);

			frm.add_custom_button(
				__("Registrierkasse als defekt melden"),
				() => {
					frappe.prompt(
						[
							{
								fieldname: "reason",
								label: __("Nicht reparierbarer Defekt"),
								fieldtype: "Small Text",
								reqd: 1,
							},
						],
						(values) =>
							frappe.confirm(
								__(
									"Diese Registrierkasse unwiderruflich als dauerhaft defekt melden?"
								),
								() =>
									frm
										.call("mark_register_defective", values)
										.then(() => frm.reload_doc())
							),
						__("Endgültige Defektmeldung")
					);
				},
				__("RKSV-Lebenszyklus")
			);
		}

		frm.add_custom_button(
			__("Kassen- und Ausfallprotokoll"),
			() => {
				frappe.set_route("List", "Fiskaly Lifecycle Event", { register: frm.doc.name });
			},
			__("Anzeigen")
		);
	},

	connection(frm) {
		if (!scu_assignment_is_locked(frm)) {
			frm._loaded_scu_request_key = null;
			frm._available_scus = [];
			frm.set_value("signature_creation_unit_id", null);
		}
		sync_connection_metadata(frm).then(() => configure_scu_assignment(frm, true));
	},

	company(frm) {
		if (!scu_assignment_is_locked(frm)) {
			frm._loaded_scu_request_key = null;
			frm._available_scus = [];
			frm.set_value("signature_creation_unit_id", null);
		}
		configure_scu_assignment(frm, true);
	},

	scu_assignment_mode(frm) {
		if (
			!scu_assignment_is_locked(frm) &&
			frm.doc.scu_assignment_mode === "NEW" &&
			frm.doc.signature_creation_unit_id
		) {
			frm.set_value("signature_creation_unit_id", null).then(() =>
				configure_scu_assignment(frm)
			);
			return;
		}
		configure_scu_assignment(frm);
	},
});
