frappe.pages["fiskaly-administrator-help"].on_page_load = function (wrapper) {
	frappe.require(
		[
			"/assets/erpnext_fiskaly_sign_at/js/role_help.js",
			"/assets/erpnext_fiskaly_sign_at/css/role_help.css",
		],
		() => {
			erpnext_fiskaly_sign_at.render_role_help(wrapper, {
				page_title: __("Fiskaly Administrator Help"),
				eyebrow: __("FISKALY RKSV ADMINISTRATOR"),
				title: __("Configure and safeguard RKSV operation"),
				intro: __("Manage Fiskaly connections, cash registers, outages, evidence, exports, and controlled decommissioning."),
				workspace_note: __("Only Fiskaly administration and compliance actions are displayed; unrelated ERPNext modules are hidden."),
				actions: [
					{ key: "registers", label: __("Open Cash Registers"), route: ["List", "Fiskaly Register"] },
					{ key: "connections", label: __("Open API Connections"), route: ["List", "Fiskaly API Connection"] },
					{ key: "settings", label: __("Open RKSV Settings"), route: ["Form", "Fiskaly Settings", "Fiskaly Settings"] },
				],
				sections: [
					{
						title: __("Configure the environment"),
						description: __("Prepare TEST completely before creating a separate LIVE setup."),
						items: [
							__("Create separate API connections and cash registers for TEST and LIVE; never reuse credentials across environments."),
							__("Validate company identity, address, POS Profile, payment classification, and tax mappings before provisioning."),
						],
					},
					{
						title: __("Provision and activate"),
						description: __("Provision resources deliberately and verify the start receipt."),
						items: [
							__("Select or create the correct SCU, initialize the register, and confirm successful FinanzOnline validation."),
							__("Activate RKSV only after the linked register and connection pass all checks."),
						],
					},
					{
						title: __("Operate and handle outages"),
						description: __("Monitor receipts and preserve the original transaction identity during failures."),
						items: [
							__("Review Receipt Status, Fiscal Receipts, Outage Lifecycle, and redacted API Logs."),
							__("Record required FON outage evidence and synchronize automatic receipts after recovery."),
						],
					},
					{
						title: __("Archive and decommission"),
						description: __("Keep complete evidence and use irreversible actions only for permanent closure."),
						items: [
							__("Create quarterly backups and complete DEP7 audit exports with external-copy evidence."),
							__("Before decommissioning, resolve open receipts and outages, create the closing receipt, and verify the final export."),
						],
					},
				],
			});
		},
	);
};
