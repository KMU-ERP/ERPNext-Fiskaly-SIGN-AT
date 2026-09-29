function ensure_dep7_workflow_styles() {
	if (document.getElementById("fiskaly-dep7-workflow-styles")) return;
	$("<style>", { id: "fiskaly-dep7-workflow-styles" })
		.text(
			`
			.fiskaly-dep7-summary { padding: 12px 14px; margin-bottom: 14px; border-radius: 8px; background: var(--control-bg); }
			.fiskaly-dep7-step { display: grid; grid-template-columns: 42px 1fr; gap: 12px; min-height: 100px; }
			.fiskaly-dep7-marker { position: relative; display: flex; justify-content: center; }
			.fiskaly-dep7-marker span { z-index: 1; display: flex; width: 32px; height: 32px; align-items: center; justify-content: center; border: 2px solid var(--gray-400); border-radius: 50%; background: var(--card-bg); font-weight: 700; }
			.fiskaly-dep7-step:not(:last-child) .fiskaly-dep7-marker::after { content: ""; position: absolute; top: 32px; bottom: 0; width: 2px; background: var(--gray-300); }
			.fiskaly-dep7-step.complete .fiskaly-dep7-marker span { border-color: var(--green-500); background: var(--green-500); color: white; }
			.fiskaly-dep7-step.complete .fiskaly-dep7-marker::after { background: var(--green-400); }
			.fiskaly-dep7-step.active .fiskaly-dep7-marker span { border-color: var(--blue-500); color: var(--blue-600); box-shadow: 0 0 0 4px var(--blue-100); }
			.fiskaly-dep7-step.error .fiskaly-dep7-marker span { border-color: var(--red-500); color: var(--red-600); }
			.fiskaly-dep7-content { padding: 3px 0 20px; }
			.fiskaly-dep7-content h5 { margin: 0 0 5px; font-size: 14px; }
			.fiskaly-dep7-content p { margin: 0 0 9px; color: var(--text-muted); }
			.fiskaly-dep7-files a { display: inline-block; margin: 0 12px 8px 0; }
		`
		)
		.appendTo("head");
}

function open_dep7_workflow(frm) {
	ensure_dep7_workflow_styles();
	const dialog = new frappe.ui.Dialog({
		title: __("Sicherungs-Assistent – {0}", [frm.doc.name]),
		size: "large",
		fields: [{ fieldname: "workflow", fieldtype: "HTML" }],
	});
	let timer = null;
	let closed = false;
	let refreshing = false;
	let action_running = false;
	let poll_promise = null;

	const render = () => {
		const ready = frm.doc.status === "READY";
		const processing = ["QUEUED", "PROCESSING"].includes(frm.doc.status);
		const failed = ["FAILED", "ACTION_REQUIRED"].includes(frm.doc.status);
		const integrity = Boolean(
			frm.doc.integrity_verified_at && frm.doc.supplementary_integrity_verified_at
		);
		const external = Boolean(
			frm.doc.external_copy_confirmed && frm.doc.external_storage_reference
		);
		const requires_external = ["QUARTERLY_BACKUP", "DECOMMISSION_FINAL"].includes(
			frm.doc.purpose
		);
		const steps = [
			{
				title: __("Sicherungsdateien erstellen"),
				done: ready,
				description: ready
					? __("DEP7 und ergänzende Belegdaten wurden privat abgelegt.")
					: processing
					? __("Die Dateien werden im Hintergrund erstellt.")
					: frm.doc.last_error ||
					  frm.doc.action_required_reason ||
					  __("Die Sicherung ist noch nicht bereit."),
				error: failed,
				action: failed ? "restart" : null,
				action_label: failed ? __("Neue Sicherung erstellen") : null,
			},
			{
				title: __("SHA-256-Integrität prüfen"),
				done: integrity,
				description: integrity
					? __("Beide Dateien wurden erfolgreich geprüft.")
					: __("Vergleicht beide Dateien mit ihren gespeicherten SHA-256-Werten."),
				action: "integrity",
				action_label: __("SHA-256 jetzt prüfen"),
			},
		];
		if (requires_external) {
			steps.push({
				title: __("Externe Kopie bestätigen"),
				done: external,
				description: external
					? __("Externe Sicherung bestätigt: {0}", [frm.doc.external_storage_reference])
					: __(
							"Kopieren Sie beide Dateien auf ein externes Medium und erfassen Sie die Referenz."
					  ),
				action: "external",
				action_label: __("Externe Kopie erfassen"),
			});
		}
		const current = steps.findIndex((step) => !step.done);
		const escape = (value) => frappe.utils.escape_html(String(value || ""));
		const files = ready
			? `<div class="fiskaly-dep7-files">
				<a href="${escape(frm.doc.export_file)}" target="_blank">${escape(__("DEP7 herunterladen"))}</a>
				<a href="${escape(frm.doc.supplementary_export_file)}" target="_blank">${escape(
					__("Zusatzdatei herunterladen")
			  )}</a>
			</div>`
			: "";
		dialog.get_field("workflow").$wrapper.html(`
			<div class="fiskaly-dep7-summary"><strong>${escape(
				__("Geführter Sicherungsnachweis")
			)}</strong><br>${files}</div>
			${steps
				.map((step, index) => {
					const blocked = current !== -1 && index > current;
					const action =
						step.action && !step.done && !blocked
							? `<button class="btn btn-primary btn-sm" data-dep7-action="${escape(
									step.action
							  )}">${escape(step.action_label)}</button>`
							: "";
					const status_class = step.done
						? "complete"
						: step.error && index === current
						? "error"
						: index === current
						? "active"
						: "pending";
					return `<div class="fiskaly-dep7-step ${status_class}">
						<div class="fiskaly-dep7-marker"><span>${step.done ? "✓" : index + 1}</span></div>
						<div class="fiskaly-dep7-content"><h5>${escape(step.title)}</h5><p>${escape(
						step.description
					)}</p>${action}</div>
					</div>`;
				})
				.join("")}
		`);
		bind_actions();
	};

	const run = async (label, method, args = {}) => {
		if (action_running) return;
		action_running = true;
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
		frappe.dom.freeze(label);
		try {
			if (refreshing && poll_promise) await poll_promise;
			await frm.call(method, args);
			await frm.reload_doc();
			render();
		} finally {
			frappe.dom.unfreeze();
			action_running = false;
		}
	};

	const restart_export = async () => {
		if (action_running) return;
		action_running = true;
		frappe.dom.freeze(__("Neue Sicherung wird gestartet …"));
		try {
			if (refreshing && poll_promise) await poll_promise;
			let method = "erpnext_fiskaly_sign_at.api.exports.create_dep7_export";
			let args = {
				register: frm.doc.register,
				date_from: frm.doc.date_from,
				date_to: frm.doc.date_to,
				purpose: frm.doc.purpose,
			};
			if (frm.doc.purpose === "QUARTERLY_BACKUP") {
				method = "erpnext_fiskaly_sign_at.api.exports.create_quarterly_backup";
				args = {
					register: frm.doc.register,
					fiscal_year: frm.doc.fiscal_year,
					fiscal_quarter: frm.doc.fiscal_quarter,
				};
			} else if (frm.doc.purpose === "DECOMMISSION_FINAL") {
				method = "erpnext_fiskaly_sign_at.api.exports.create_final_decommission_export";
				args = { register: frm.doc.register };
			}
			const { message } = await frappe.call({ method, args });
			dialog.hide();
			frappe.set_route("Form", "Fiskaly DEP7 Export", message);
		} finally {
			frappe.dom.unfreeze();
			action_running = false;
		}
	};

	const bind_actions = () => {
		const body = dialog.get_field("workflow").$wrapper;
		body.off(".fiskaly_dep7");
		body.on("click.fiskaly_dep7", "[data-dep7-action]", function () {
			if (this.dataset.dep7Action === "restart") {
				restart_export();
				return;
			}
			if (this.dataset.dep7Action === "integrity") {
				run(__("SHA-256-Integrität wird geprüft …"), "verify_export_integrity");
				return;
			}
			frappe.prompt(
				[
					{
						fieldname: "storage_reference",
						label: __("Referenz des externen Speichermediums"),
						fieldtype: "Small Text",
						description: __(
							"Bestätigen Sie erst, nachdem DEP7 und Zusatzdatei tatsächlich extern kopiert wurden."
						),
						reqd: 1,
					},
				],
				(values) =>
					run(
						__("Externer Sicherungsnachweis wird gespeichert …"),
						"mark_external_copy_confirmed",
						values
					),
				__("Externe Sicherung bestätigen"),
				__("Bestätigen")
			);
		});
	};

	const poll = () => {
		if (closed || action_running) return Promise.resolve();
		if (refreshing) return poll_promise;
		refreshing = true;
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
		poll_promise = (async () => {
			let should_poll = false;
			try {
				await frm.reload_doc();
				if (closed) return;
				render();
				should_poll = ["QUEUED", "PROCESSING"].includes(frm.doc.status);
			} finally {
				refreshing = false;
				poll_promise = null;
				if (!closed && !action_running && should_poll) timer = setTimeout(poll, 2500);
			}
		})();
		return poll_promise;
	};

	dialog.set_primary_action(__("Status aktualisieren"), poll);
	dialog.$wrapper.on("hidden.bs.modal", () => {
		closed = true;
		if (timer) clearTimeout(timer);
	});
	dialog.show();
	render();
	if (["QUEUED", "PROCESSING"].includes(frm.doc.status)) timer = setTimeout(poll, 2500);
}

frappe.ui.form.on("Fiskaly DEP7 Export", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Sicherungs-Assistent"), () => open_dep7_workflow(frm));
	},
});
