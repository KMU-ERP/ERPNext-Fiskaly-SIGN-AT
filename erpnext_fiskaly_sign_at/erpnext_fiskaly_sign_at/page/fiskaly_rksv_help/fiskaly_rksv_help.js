frappe.pages["fiskaly-rksv-help"].on_page_load = function (wrapper) {
	frappe.require(
		[
			"/assets/erpnext_fiskaly_sign_at/js/role_help.js",
			"/assets/erpnext_fiskaly_sign_at/css/role_help.css",
		],
		() => {
			erpnext_fiskaly_sign_at.render_role_help(wrapper, {
				page_title: __("Fiskaly RKSV Help"),
				eyebrow: __("SYSTEM ADMINISTRATION"),
				title: __("Set up and operate Fiskaly RKSV"),
				intro: __("General step-by-step guidance from TEST setup to controlled decommissioning."),
				workspace_note: __("This general guide covers configuration, POS operation, compliance evidence, outages, exports, and lifecycle tasks."),
				actions: [
					{ key: "registers", label: __("Open Cash Registers"), route: ["List", "Fiskaly Register"] },
					{ key: "connections", label: __("Open API Connections"), route: ["List", "Fiskaly API Connection"] },
					{ key: "status", label: __("Open Receipt Status"), route: ["query-report", "Fiskaly Receipt Status"] },
				],
				sections: [
					{
						title: __("Prepare ERPNext"),
						description: __("Complete the required master data before creating provider resources."),
						items: [
							__("Use Europe/Vienna, EUR, the Austrian tax identity, and a complete company operating address."),
							__("Configure the POS Profile with users, warehouse, price list, payment methods, and the protected RKSV print format."),
							__("Classify every payment method correctly as RKSV cash equivalent or non-cash."),
						],
					},
					{
						title: __("Create the TEST connection"),
						description: __("Validate credentials and FinanzOnline communication in TEST first."),
						items: [
							__("Keep TEST and LIVE API credentials, organizations, and cash registers strictly separate."),
							__("Test the API connection and authenticate FinanzOnline before provisioning a register."),
							__("Use syntactically valid dummy FinanzOnline values only in TEST; LIVE requires real credentials."),
						],
					},
					{
						title: __("Provision the cash register"),
						description: __("Create the register deliberately and verify every immutable assignment."),
						items: [
							__("Assign the company, POS Profile, connection, operating address, and the correct existing or new SCU."),
							__("Map every tax rate actually used by the POS to the correct RKSV tax group."),
							__("Provision once, then verify the provider status, register ID, start receipt, and successful FON validation."),
						],
					},
					{
						title: __("Activate and accept TEST"),
						description: __("Enable fiscalization only after the complete TEST setup passes all checks."),
						items: [
							__("Select TEST in RKSV Settings and confirm the critical configuration change."),
							__("Test cash, card, non-cash, mixed payment, return, receipt printing, and the RKSV QR code."),
							__("Verify that fiscal item totals, taxes, and the POS invoice grand total match exactly."),
						],
					},
					{
						title: __("Run daily operations"),
						description: __("Monitor fiscalization without modifying protected fiscal data."),
						items: [
							__("Review Receipt Status regularly for pending, retrying, failed, or action-required receipts."),
							__("Use the protected print action; duplicate prints and evidence are recorded."),
							__("Never edit receipt UUIDs, signatures, QR data, start receipts, or closing receipts directly."),
						],
					},
					{
						title: __("Handle outages"),
						description: __("Preserve the original transaction and follow the documented outage lifecycle."),
						items: [
							__("Do not create a replacement register or a replacement receipt with a new UUID."),
							__("Record outage start, the 48-hour threshold, required FON evidence, recovery, and outage end."),
							__("Synchronize automatic receipts after recovery until no action remains open."),
						],
					},
					{
						title: __("Create evidence and DEP7 backups"),
						description: __("Keep complete, verified, and externally copied compliance evidence."),
						items: [
							__("Create a complete DEP7 backup after every completed quarter and for external audits."),
							__("Verify both private export files against their SHA-256 values."),
							__("Copy both files to external storage and record an unambiguous storage reference."),
						],
					},
					{
						title: __("Move from TEST to LIVE"),
						description: __("Build LIVE separately instead of converting an existing TEST setup."),
						items: [
							__("Create a separate LIVE connection with productive Fiskaly and FinanzOnline credentials."),
							__("Create and provision a separate LIVE register and validate its start receipt."),
							__("Switch the global environment to LIVE only after documented technical and operational acceptance."),
						],
					},
					{
						title: __("Decommission safely"),
						description: __("Use irreversible lifecycle actions only when the register is permanently closed."),
						items: [
							__("Resolve all open receipts and outages before starting controlled decommissioning."),
							__("Create and archive the closing receipt, final DEP7 export, hash verification, and external-copy evidence."),
							__("Decommission a shared SCU only when no other active register still uses it."),
						],
					},
					{
						title: __("Troubleshoot without losing evidence"),
						description: __("Retry idempotently and inspect redacted technical information."),
						items: [
							__("After an interrupted provider call, continue with the same ERPNext register and the same identifiers."),
							__("Check Receipt Status, provider synchronization, tax mappings, and redacted API Logs."),
							__("Do not use database edits as a shortcut for correcting fiscal records or lifecycle evidence."),
						],
					},
				],
			});
		},
	);
};
