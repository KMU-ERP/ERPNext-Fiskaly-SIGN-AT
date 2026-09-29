# Outages, Exports, and Decommissioning

## During a connection or provider outage

1. Keep the original POS invoice and fiscal receipt record. Never create a replacement invoice with a new fiscal receipt identity to get around a pending request.
2. Note when the issue began, which register is affected, and whether it is a Fiskaly/security-system outage or a cash-register outage.
3. Use **Receipt Status**, **Fiscal Receipts**, **Cash Registers**, and **Cash Register and Outage Lifecycle** to review the affected records and follow the available lifecycle actions.
4. Follow the organization’s FinanzOnline reporting procedure. The app records outage state, a 48-hour threshold, reporting status, and reference evidence; operators remain responsible for ensuring that required reporting is completed.
5. When service returns, monitor retries and automatic receipts. Confirm that affected receipts are synchronized and that no action-required items remain. Escalate unresolved errors; do not edit records directly in the database.

Do not run outage simulations on LIVE. Test recovery procedures only in an approved TEST environment.

## Receipt and year-end follow-up

- Review the **Fiskaly Receipt Status** report regularly, including automatic monthly, yearly, recovery, and closing receipts where applicable.
- For a yearly receipt, follow the app’s verification deadline and record the verification status and evidence on the receipt. Keep the receipt print/archive evidence with the relevant accounting records.
- If the receipt shows `ACTION_REQUIRED`, a failed FinanzOnline validation, or an overdue verification, assign it to the responsible administrator/accounting contact and document the resolution.

## Create and archive a DEP7 export

Use the **DEP7 Exports** entry in the **Fiskaly RKSV** workspace.

1. Create an export for the correct register. Select the purpose (for example, quarterly backup or audit) and the required scope/date range.
2. Wait until the export status is **READY**. If it is **FAILED** or **ACTION_REQUIRED**, review the displayed reason and escalate if necessary.
3. Download the DEP7 file and its supplementary file, when provided. Keep both together.
4. Run the app’s integrity verification and confirm each downloaded file matches its recorded SHA-256 value.
5. Copy both files to the organization’s approved external archive, separate from the ERPNext server.
6. Record the external storage reference and confirm the external copy in the export record. Respect the displayed retention date and the organization’s retention policy.

Do not put DEP7 files, hashes tied to production records, provider download links, or customer data in this public Wiki or in public issue attachments.

## Decommission a register

Only start when the register is permanently being closed. Resolve open receipts and outages first.

Follow the guided decommission workflow on the register:

1. Close the register and create the closing receipt.
2. Verify and privately archive the closing receipt PDF.
3. Create the final complete DEP7 export and supplementary file.
4. Verify both SHA-256 hashes.
5. Copy both files to external storage and record the storage reference.
6. Complete the final decommission step only after all evidence checks pass.

Do not decommission an SCU that is still used by another active register. Keep the final receipt, exports, verification, and external-copy evidence for the applicable retention period.

## Troubleshooting guide

| Symptom | First checks |
| --- | --- |
| Connection test fails | Confirm provider, company, TEST/LIVE environment, credential pair, network access, and any configured endpoint. Save the connection before testing. |
| FinanzOnline authentication fails | Check that the correct environment is selected. TEST uses dummy values; LIVE uses the authorized real web-service user details. Do not copy credentials into logs or tickets. |
| Receipt stays pending/retrying | Keep the original invoice. Review **Receipt Status**, the next retry, and the linked API log. Escalate if it does not recover or needs action. |
| Receipt is `ACTION_REQUIRED` | Read the recorded error and ask the Fiskaly administrator to check the connection, VAT mapping, provider state, and lifecycle records. Do not change the receipt payload. |
| QR code or print is missing | Check the POS Profile’s **POS Invoice RKSV** print format and use the protected print action. Do not issue a second invoice as a printing workaround. |
| DEP7 export is not ready | Check its status, last error, polling attempts, and provider connection. Retry through the export workflow only when the app offers that action. |

API logs are intended for troubleshooting and are redacted by the app, but they can still contain operational context. Share them only through approved private support channels after reviewing them.

