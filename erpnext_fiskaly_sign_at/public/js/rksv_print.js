(() => {
	const RKSV_PRINT_FORMAT = "POS Invoice RKSV";
	const CONTROLLED_PRINT_ENDPOINT =
		"/api/method/erpnext_fiskaly_sign_at.api.printing.render_pos_receipt";
	const FISCAL_STATUSES = new Set([
		"SIGNED",
		"SUBSTITUTE_SIGNED",
		"OFFLINE_PENDING",
		"PREPARED",
		"SIGNING",
		"RETRYING",
		"ACTION_REQUIRED",
	]);

	function requires_controlled_print(doc, print_format) {
		return Boolean(
			doc &&
			doc.doctype === "POS Invoice" &&
			(print_format === RKSV_PRINT_FORMAT ||
				doc.fiskaly_receipt ||
				FISCAL_STATUSES.has(doc.fiskaly_status)),
		);
	}

	function open_controlled_print(pos_invoice, output = "html", auto_print = true) {
		return window.open_url_post(
			CONTROLLED_PRINT_ENDPOINT,
			{
				pos_invoice,
				output,
				auto_print: auto_print ? 1 : 0,
			},
			true,
		);
	}

	function install_pos_print_patch() {
		if (!frappe.utils || !frappe.utils.print || frappe.utils.print.__rksv_controlled) {
			return;
		}
		const core_print = frappe.utils.print;
		const controlled_print = function (doctype, docname, print_format) {
			const doc = frappe.get_doc(doctype, docname);
			if (requires_controlled_print(doc, print_format)) {
				return open_controlled_print(docname, "html", true);
			}
			return core_print.apply(this, arguments);
		};
		controlled_print.__rksv_controlled = true;
		controlled_print.__rksv_core_print = core_print;
		frappe.utils.print = controlled_print;
	}

	function install_print_view_patch() {
		const PrintView = frappe.ui && frappe.ui.form && frappe.ui.form.PrintView;
		if (!PrintView || PrintView.prototype.__rksv_controlled) {
			return;
		}
		const prototype = PrintView.prototype;
		const core_printit = prototype.printit;
		const core_render_page = prototype.render_page;
		const core_render_pdf = prototype.render_pdf;
		const core_print_by_server = prototype.print_by_server;

		prototype.printit = function () {
			const doc = this.frm && this.frm.doc;
			const print_format = this.selected_format && this.selected_format();
			if (requires_controlled_print(doc, print_format)) {
				return open_controlled_print(doc.name, "html", true);
			}
			return core_printit.apply(this, arguments);
		};

		prototype.render_page = function (method, printit, pdf_generator) {
			const doc = this.frm && this.frm.doc;
			const print_format = this.selected_format && this.selected_format();
			if (requires_controlled_print(doc, print_format)) {
				const output = method && method.includes("download_pdf") ? "pdf" : "html";
				return open_controlled_print(doc.name, output, Boolean(printit));
			}
			return core_render_page.call(this, method, printit, pdf_generator);
		};

		prototype.render_pdf = function () {
			const doc = this.frm && this.frm.doc;
			const print_format = this.selected_format && this.selected_format();
			if (requires_controlled_print(doc, print_format)) {
				return open_controlled_print(doc.name, "pdf", false);
			}
			return core_render_pdf.apply(this, arguments);
		};

		prototype.print_by_server = function () {
			const doc = this.frm && this.frm.doc;
			const print_format = this.selected_format && this.selected_format();
			if (requires_controlled_print(doc, print_format)) {
				frappe.show_alert({
					message: __(
						"RKSV receipts use the controlled browser print because the generic print server cannot preserve the Original/DUPLIKAT claim.",
					),
					indicator: "orange",
				});
				return open_controlled_print(doc.name, "html", true);
			}
			return core_print_by_server.apply(this, arguments);
		};

		prototype.__rksv_controlled = true;
	}

	function manual_cash_entry_enabled() {
		const value = window.cur_pos?.settings?.fiskaly_manual_cash_entry;
		return value === true || Number(value) === 1;
	}

	function selected_payment_row(payment) {
		const frm = payment.events?.get_frm?.();
		const label = payment.selected_mode?._label || payment.selected_mode?.df?.label;
		return (frm?.doc?.payments || []).find((row) => row.mode_of_payment === label);
	}

	function install_manual_cash_entry_patch() {
		const Payment = window.erpnext?.PointOfSale?.Payment;
		const prototype = Payment?.prototype;
		if (!prototype || prototype.__fiskaly_manual_cash_entry) {
			return Boolean(prototype);
		}

		const core_auto_set_remaining_amount = prototype.auto_set_remaining_amount;
		if (typeof core_auto_set_remaining_amount !== "function") {
			return false;
		}

		prototype.auto_set_remaining_amount = function () {
			const row = selected_payment_row(this);
			if (manual_cash_entry_enabled() && String(row?.type || "").toLowerCase() === "cash") {
				// Keep an existing manually entered value intact when the operator reselects
				// Cash. New payment rows start at zero because the conflicting ERPNext
				// default-payment prefill is disabled when this option is saved.
				this.numpad_value = "";
				return;
			}
			return core_auto_set_remaining_amount.apply(this, arguments);
		};
		prototype.auto_set_remaining_amount.__fiskaly_core = core_auto_set_remaining_amount;
		prototype.__fiskaly_manual_cash_entry = true;
		return true;
	}

	function wait_for_pos_payment_class() {
		if (install_manual_cash_entry_patch()) return;

		let attempts = 0;
		const timer = window.setInterval(() => {
			attempts += 1;
			if (install_manual_cash_entry_patch() || attempts >= 200) {
				window.clearInterval(timer);
			}
		}, 50);
	}

	install_pos_print_patch();
	install_print_view_patch();
	wait_for_pos_payment_class();
})();
