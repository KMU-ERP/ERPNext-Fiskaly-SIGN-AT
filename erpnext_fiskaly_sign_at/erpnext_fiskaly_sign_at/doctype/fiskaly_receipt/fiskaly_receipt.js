function ensure_receipt_workflow_styles() {
	if (document.getElementById("fiskaly-receipt-workflow-styles")) return;
	$("<style>", { id: "fiskaly-receipt-workflow-styles" })
		.text(
			`
			.fiskaly-receipt-step { display: grid; grid-template-columns: 42px 1fr; gap: 12px; min-height: 96px; }
			.fiskaly-receipt-marker { position: relative; display: flex; justify-content: center; }
			.fiskaly-receipt-marker span { z-index: 1; display: flex; width: 32px; height: 32px; align-items: center; justify-content: center; border: 2px solid var(--gray-400); border-radius: 50%; background: var(--card-bg); font-weight: 700; }
			.fiskaly-receipt-step:not(:last-child) .fiskaly-receipt-marker::after { content: ""; position: absolute; top: 32px; bottom: 0; width: 2px; background: var(--gray-300); }
			.fiskaly-receipt-step.complete .fiskaly-receipt-marker span { border-color: var(--green-500); background: var(--green-500); color: white; }
			.fiskaly-receipt-step.complete .fiskaly-receipt-marker::after { background: var(--green-400); }
			.fiskaly-receipt-step.active .fiskaly-receipt-marker span { border-color: var(--blue-500); color: var(--blue-600); box-shadow: 0 0 0 4px var(--blue-100); }
			.fiskaly-receipt-content { padding: 3px 0 20px; }
			.fiskaly-receipt-content h5 { margin: 0 0 5px; font-size: 14px; }
			.fiskaly-receipt-content p { margin: 0 0 9px; color: var(--text-muted); }
		`
		)
		.appendTo("head");
}

function open_yearly_receipt_workflow(frm) {
	ensure_receipt_workflow_styles();
	const dialog = new frappe.ui.Dialog({
		title: __("Jahresbeleg-Assistent – {0}", [frm.doc.name]),
		size: "large",
		fields: [{ fieldname: "workflow", fieldtype: "HTML" }],
	});

	const render = () => {
		const verified =
			frm.doc.fon_validation_status === "SUCCESS" ||
			frm.doc.verification_status === "SUCCESS";
		const archived = Boolean(frm.doc.print_evidence_at && frm.doc.print_evidence_file);
		const steps = [
			{
				title: __("BMF-Prüfung bestätigen"),
				done: verified,
				description:
					frm.doc.fon_validation_status === "SUCCESS"
						? __("Der Jahresbeleg wurde automatisch erfolgreich geprüft.")
						: verified
						? __("Die manuelle BMF-Prüfung wurde erfolgreich dokumentiert.")
						: __("Erfassen Sie das Ergebnis der manuellen BMF-Prüfung."),
				action: "verify",
				action_label: __("Prüfergebnis erfassen"),
			},
			{
				title: __("Jahresbeleg automatisch archivieren"),
				done: archived,
				description: archived
					? __("Der geschützte Jahresbeleg ist als private PDF archiviert.")
					: __("Erzeugt den geschützten RKSV-Druck und legt ihn privat am Beleg ab."),
				action: "archive",
				action_label: __("PDF erstellen und ablegen"),
			},
		];
		const current = steps.findIndex((step) => !step.done);
		const escape = (value) => frappe.utils.escape_html(String(value || ""));
		dialog.get_field("workflow").$wrapper.html(
			steps
				.map((step, index) => {
					const blocked = current !== -1 && index > current;
					const action =
						!step.done && !blocked
							? `<button class="btn btn-primary btn-sm" data-receipt-action="${escape(
									step.action
							  )}">${escape(step.action_label)}</button>`
							: "";
					return `<div class="fiskaly-receipt-step ${
						step.done ? "complete" : index === current ? "active" : "pending"
					}">
						<div class="fiskaly-receipt-marker"><span>${step.done ? "✓" : index + 1}</span></div>
						<div class="fiskaly-receipt-content"><h5>${escape(step.title)}</h5><p>${escape(
						step.description
					)}</p>${action}</div>
					</div>`;
				})
				.join("")
		);
		bind_actions();
	};

	const run = async (label, method, values = {}) => {
		frappe.dom.freeze(label);
		try {
			await frm.call(method, values);
			await frm.reload_doc();
			render();
		} finally {
			frappe.dom.unfreeze();
		}
	};

	const bind_actions = () => {
		const body = dialog.get_field("workflow").$wrapper;
		body.off(".fiskaly_receipt");
		body.on("click.fiskaly_receipt", "[data-receipt-action]", function () {
			if (this.dataset.receiptAction === "archive") {
				run(
					__("Jahresbeleg wird als private PDF archiviert …"),
					"create_and_archive_print_evidence"
				);
				return;
			}
			frappe.prompt(
				[
					{
						fieldname: "status",
						label: __("Prüfstatus"),
						fieldtype: "Select",
						options: "SUCCESS\nFAILED",
						reqd: 1,
					},
					{
						fieldname: "note",
						label: __("Nachweis / Anmerkung"),
						fieldtype: "Small Text",
						reqd: 1,
					},
				],
				(values) =>
					run(
						__("Prüfnachweis wird gespeichert …"),
						"record_annual_verification",
						values
					),
				__("Manuelle Jahresbelegprüfung"),
				__("Speichern")
			);
		});
	};

	dialog.show();
	render();
}

frappe.ui.form.on("Fiskaly Receipt", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.receipt_kind === "CLOSING") {
			frm.set_intro(
				frm.doc.print_evidence_at
					? __("Schlussbeleg wurde automatisch als private PDF archiviert.")
					: __(
							"Der Schlussbeleg kann jetzt automatisch als private PDF erstellt und archiviert werden."
					  ),
				frm.doc.print_evidence_at ? "green" : "orange"
			);
			if (!frm.doc.print_evidence_at) {
				frm.add_custom_button(__("PDF erstellen und automatisch archivieren"), () => {
					frappe.dom.freeze(__("Schlussbeleg wird archiviert …"));
					frm.call("create_and_archive_print_evidence")
						.then(() => frm.reload_doc())
						.finally(() => frappe.dom.unfreeze());
				});
			}
			return;
		}
		if (frm.doc.receipt_kind !== "YEARLY") return;
		const complete = frm.doc.annual_compliance_status === "COMPLETE";
		frm.set_intro(
			complete
				? __("Jahresbeleg vollständig geprüft und automatisch archiviert.")
				: __("Der Jahresbeleg-Assistent führt durch Prüfung und Archivierung."),
			complete ? "green" : "orange"
		);
		frm.add_custom_button(__("Jahresbeleg-Assistent"), () =>
			open_yearly_receipt_workflow(frm)
		);
	},
});
