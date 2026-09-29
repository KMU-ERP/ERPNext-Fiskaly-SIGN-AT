frappe.pages["pos-manager-help"].on_page_load = function (wrapper) {
	frappe.require(
		[
			"/assets/erpnext_fiskaly_sign_at/js/role_help.js",
			"/assets/erpnext_fiskaly_sign_at/css/role_help.css",
		],
		() => {
			erpnext_fiskaly_sign_at.render_role_help(wrapper, {
				page_title: __("POS Manager Help"),
				eyebrow: __("POS MANAGER"),
				title: __("Manage daily POS operations"),
				intro: __("Monitor sessions and fiscal receipts, resolve operational issues, and maintain POS master data."),
				workspace_note: __("Only POS management actions are displayed; unrelated ERPNext modules are hidden."),
				actions: [
					{ key: "status", label: __("Open Receipt Status"), route: ["query-report", "Fiskaly Receipt Status"] },
					{ key: "closings", label: __("Review POS Closings"), route: ["List", "POS Closing Entry"] },
					{ key: "profiles", label: __("Manage POS Profiles"), route: ["List", "POS Profile"] },
				],
				sections: [
					{
						title: __("Monitor sessions"),
						description: __("Review openings and closings across the POS team."),
						items: [
							__("Identify sessions that remain open unexpectedly and contact the responsible cashier."),
							__("Compare closing totals with the recorded payment methods and investigate differences."),
						],
					},
					{
						title: __("Review invoices and returns"),
						description: __("Use POS Invoices and Fiscal Receipts for operational follow-up."),
						items: [
							__("Check invoice status, payment allocation, returns, and the linked fiscal receipt."),
							__("Never repair signature fields or QR data manually; escalate technical failures."),
						],
					},
					{
						title: __("Watch fiscalization"),
						description: __("Receipt Status is the central operational exception list."),
						items: [
							__("Review pending, retrying, failed, and action-required receipts regularly."),
							__("Escalate outages and configuration errors to the Fiskaly Administrator."),
						],
					},
					{
						title: __("Maintain POS master data"),
						description: __("Keep profiles, payment methods, and customers ready for cashiers."),
						items: [
							__("Assign the correct users, warehouse, price list, payment methods, and RKSV print format."),
							__("Coordinate fiscal register or tax mapping changes with the Fiskaly Administrator."),
						],
					},
				],
			});
		},
	);
};
