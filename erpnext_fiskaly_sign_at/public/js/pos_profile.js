function check_rksv_print_format(frm, repair = false) {
	return frappe
		.call({
			method: "erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_settings.fiskaly_settings.check_pos_print_formats",
			args: {
				pos_profile: frm.doc.name,
				repair: repair ? 1 : 0,
			},
			freeze: repair,
			freeze_message: __("Setting up the RKSV print format …"),
		})
		.then(({ message }) => {
			if (message.valid) {
				frappe.show_alert({
					message: repair
						? __("The RKSV print format was configured.")
						: __("This POS Profile uses the required RKSV print format."),
					indicator: "green",
				});
				frm.reload_doc();
				return;
			}

			if (frappe.user.has_role("System Manager")) {
				frappe.confirm(
					__(
						"This active RKSV POS Profile must use print format {0}. Correct it now?",
						[message.required_print_format],
					),
					() => check_rksv_print_format(frm, true),
				);
				return;
			}

			frappe.msgprint({
				title: __("RKSV Print Format Required"),
				indicator: "orange",
				message: __(
					"Ask a System Manager to set the print format to {0}.",
					[message.required_print_format],
				),
			});
		});
}

function disable_default_payment_prefill_for_manual_cash(frm) {
	if (!frm.doc.fiskaly_manual_cash_entry || !frm.doc.set_grand_total_to_default_mop) {
		return Promise.resolve();
	}

	return frm.set_value("set_grand_total_to_default_mop", 0).then(() => {
		frappe.show_alert({
			message: __(
				"Automatic prefill of the default payment method was disabled for manual cash entry.",
			),
			indicator: "blue",
		});
	});
}

frappe.ui.form.on("POS Profile", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frappe.user.has_role("System Manager")) {
			frm.add_custom_button(
				__("Fiskaly RKSV Help"),
				() => frappe.set_route("fiskaly-rksv-help"),
				__("Fiskaly RKSV"),
			);
		}

		frappe.db
			.exists("Fiskaly Register", {
				pos_profile: frm.doc.name,
				active: 1,
			})
			.then((is_rksv_profile) => {
				if (!is_rksv_profile) return;
				frm.add_custom_button(
					__("Check RKSV Print Format"),
					() => check_rksv_print_format(frm),
					__("Fiskaly RKSV"),
				);
			});
	},

	fiskaly_manual_cash_entry(frm) {
		return disable_default_payment_prefill_for_manual_cash(frm);
	},

	validate(frm) {
		if (frm.doc.fiskaly_manual_cash_entry && frm.doc.set_grand_total_to_default_mop) {
			frappe.throw(
				__(
					"Disable Set Grand Total to Default Payment Method before enabling manual cash entry.",
				),
			);
		}
	},
});
