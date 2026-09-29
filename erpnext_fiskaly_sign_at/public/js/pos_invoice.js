const FISKALY_STATUS_PRESENTATION = {
	SIGNED: {
		label: __("Signed and in the fiscal chain"),
		indicator: "green",
		color: "#137333",
		background: "#e6f4ea",
	},
	SUBSTITUTE_SIGNED: {
		label: __("Signed with statutory substitute signature"),
		indicator: "orange",
		color: "#8a4b08",
		background: "#fef3e2",
	},
	OFFLINE_PENDING: {
		label: __("Emergency receipt – synchronization pending"),
		indicator: "orange",
		color: "#8a4b08",
		background: "#fef3e2",
	},
	NOT_REQUIRED: {
		label: __("No RKSV cash-equivalent payment"),
		indicator: "blue",
		color: "#175cd3",
		background: "#eff8ff",
	},
	ACTION_REQUIRED: {
		label: __("Manual action required"),
		indicator: "red",
		color: "#b42318",
		background: "#fef3f2",
	},
	PREPARED: {
		label: __("Prepared for fiscalization"),
		indicator: "blue",
		color: "#175cd3",
		background: "#eff8ff",
	},
	SIGNING: {
		label: __("Signature is being created"),
		indicator: "blue",
		color: "#175cd3",
		background: "#eff8ff",
	},
	RETRYING: {
		label: __("Provider request is being retried"),
		indicator: "orange",
		color: "#8a4b08",
		background: "#fef3e2",
	},
	NOT_CONFIGURED: {
		label: __("Not fiscalized yet"),
		indicator: "gray",
		color: "#475467",
		background: "#f2f4f7",
	},
};

function fiskaly_escape(value) {
	return $("<div>").text(value == null ? "" : String(value)).html();
}

function fiskaly_render_status(frm, message = {}) {
	const field = frm.fields_dict.fiskaly_status_summary;
	if (!field) return;
	const status = message.status || frm.doc.fiskaly_status || "NOT_CONFIGURED";
	const presentation = FISKALY_STATUS_PRESENTATION[status] || {
		label: status,
		indicator: "gray",
		color: "#475467",
		background: "#f2f4f7",
	};
	const provider = frm.doc.fiskaly_provider || message.snapshot?.fiskaly_provider;
	const environment = frm.doc.fiskaly_environment || message.snapshot?.fiskaly_environment;
	const receipt = frm.doc.fiskaly_receipt || message.receipt;
	const details = [provider, environment, receipt].filter(Boolean).map(fiskaly_escape).join(" · ");
	field.$wrapper.html(`
		<div style="border:1px solid var(--border-color);border-left:4px solid ${presentation.color};
			border-radius:8px;padding:14px 16px;margin-bottom:12px;background:${presentation.background}">
			<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
				<span class="indicator-pill ${presentation.indicator}">${fiskaly_escape(status)}</span>
				<strong style="color:${presentation.color}">${fiskaly_escape(presentation.label)}</strong>
			</div>
			${details ? `<div class="text-muted small" style="margin-top:7px">${details}</div>` : ""}
		</div>
	`);
}

function fiskaly_render_qr(frm, message = {}) {
	const field = frm.fields_dict.fiskaly_qr_preview;
	if (!field) return;
	if (!message.qr_svg) {
		const text = frm.doc.fiskaly_status
			? __("No QR-code preview is available for the current fiscalization status.")
			: __("The QR-code preview appears after fiscalization.");
		field.$wrapper.html(`<div class="text-muted small" style="padding:12px 0">${fiskaly_escape(text)}</div>`);
		return;
	}
	field.$wrapper.html(`
		<div style="display:inline-block;padding:12px;background:#fff;border:1px solid var(--border-color);
			border-radius:8px;text-align:center">
			<style>.fiskaly-pos-qr-preview svg{display:block;width:min(240px,100%);height:auto}</style>
			<div class="fiskaly-pos-qr-preview">${message.qr_svg}</div>
			<div class="text-muted small" style="margin-top:8px;max-width:240px">
				${fiskaly_escape(__("Data-record preview – the protected RKSV print process remains unchanged."))}
			</div>
		</div>
	`);
}

function fiskaly_apply_result(frm, message) {
	if (!message) return;
	if (message.snapshot) {
		Object.assign(frm.doc, message.snapshot);
	} else {
		Object.assign(frm.doc, {
			fiskaly_receipt: message.receipt,
			fiskaly_status: message.status,
			fiskaly_qr_code_data: message.qr_code_data,
			fiskaly_receipt_number: message.receipt_number,
			fiskaly_signed_at: message.signed_at,
			fiskaly_serial_number: message.serial_number,
			fiskaly_signature_value: message.signature_value,
			fiskaly_offline_qr_data: message.offline_qr_code_data,
			fiskaly_cash_amount: message.cash_amount,
			fiskaly_vat_breakdown: message.vat_breakdown,
			fiskaly_provider_hints: message.provider_hints,
			fiskaly_print_lines: message.print_lines,
		});
	}
	frm.refresh_fields();
	fiskaly_render_status(frm, message);
	fiskaly_render_qr(frm, message);
}

function fiskaly_notify_result(message) {
	if (message.status === "SIGNED") {
		frappe.show_alert({ message: __("RKSV receipt signed"), indicator: "green" });
	} else if (message.status === "SUBSTITUTE_SIGNED") {
		frappe.msgprint({
			title: __("RKSV substitute signature"),
			indicator: "orange",
			message: __(
				"fiskaly recorded a valid receipt with the statutory substitute signature. " +
					"Print it now; SIGN AT creates the recovery zero receipt automatically."
			),
		});
	} else if (message.status === "OFFLINE_PENDING") {
		frappe.msgprint({
			title: __("SIGN AT API outage – emergency receipt"),
			indicator: "orange",
			message: __(
				"Print the non-fiscal emergency receipt now. It contains no fiskaly receipt number or " +
					"cash-register ID. The immutable original request is queued and will be replayed " +
					"automatically with the same UUID."
			),
		});
	} else if (message.status === "NOT_REQUIRED") {
		frappe.show_alert({
			message: __("No RKSV cash-equivalent payment on this invoice"),
			indicator: "blue",
		});
	} else if (message.status !== "NOT_CONFIGURED") {
		frappe.msgprint({
			title: __("RKSV action required"),
			indicator: "red",
			message: __(
				"The receipt may not be printed yet because fiscalization did not complete. " +
					"Open the linked Fiskaly Receipt and correct the reported configuration or payload error."
			),
		});
	}
}

function fiskaly_load_status(frm, notify = false) {
	if (frm.is_new()) return Promise.resolve();
	return frappe
		.call({
			method: "erpnext_fiskaly_sign_at.api.pos.get_fiscalization_status",
			type: "GET",
			args: { pos_invoice: frm.doc.name },
		})
		.then(({ message }) => {
			if (!message) return;
			fiskaly_apply_result(frm, message);
			if (notify) fiskaly_notify_result(message);
		});
}

frappe.ui.form.on("POS Invoice", {
	refresh(frm) {
		fiskaly_render_status(frm);
		fiskaly_render_qr(frm);
		return fiskaly_load_status(frm);
	},

	on_submit(frm) {
		if (!frm.doc.is_pos) return;
		return fiskaly_load_status(frm, true);
	},
});
