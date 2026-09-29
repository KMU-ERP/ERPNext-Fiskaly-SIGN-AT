frappe.pages["pos-cashier-help"].on_page_load = function (wrapper) {
	frappe.require(
		[
			"/assets/erpnext_fiskaly_sign_at/js/role_help.js",
			"/assets/erpnext_fiskaly_sign_at/css/role_help.css",
		],
		() => {
			erpnext_fiskaly_sign_at.render_role_help(wrapper, {
				page_title: __("POS Cashier Help"),
				eyebrow: __("POS CASHIER"),
				title: __("Your daily POS workflow"),
				intro: __("Open your session, complete sales, review your invoices, and close the POS safely."),
				workspace_note: __("Only your five daily actions are displayed. Your POS documents are limited to your own user."),
				actions: [
					{ key: "pos", label: __("Open Point of Sale"), route: ["point-of-sale"] },
					{ key: "opening", label: __("Open POS session"), route: ["List", "POS Opening Entry"] },
					{ key: "closing", label: __("Close POS session"), route: ["List", "POS Closing Entry"] },
				],
				sections: [
					{
						title: __("Start your shift"),
						description: __("Create one opening entry before the first sale."),
						items: [
							__("Select your POS Profile and enter the actual opening cash balance."),
							__("Do not open a second session while your own session is still open."),
						],
					},
					{
						title: __("Complete a sale"),
						description: __("Use Point of Sale for every cashier transaction."),
						items: [
							__("Add items, select the correct payment method, and verify the received amount."),
							__("Print the protected RKSV receipt after the invoice is completed."),
						],
					},
					{
						title: __("Review your invoices"),
						description: __("My POS Invoices contains only invoices assigned to your user."),
						items: [
							__("Check the fiscalization status before retrying or reporting a problem."),
							__("Create returns through the approved POS return workflow; do not edit fiscal data."),
						],
					},
					{
						title: __("Close your shift"),
						description: __("Count the actual amounts and submit one closing entry."),
						items: [
							__("Enter counted cash and other payment totals accurately."),
							__("If a receipt remains pending or an amount differs, contact the POS Manager before closing."),
						],
					},
				],
			});
		},
	);
};
