# Administrator Guide

Use this procedure for each company and environment. Finish TEST and document acceptance before preparing LIVE.

## 1. Prepare ERPNext master data

Before creating Fiskaly resources, confirm the following:

- The Company uses **EUR** and the **Europe/Vienna** time zone.
- The company’s Austrian tax identifiers and complete operating address are correct.
- The POS Profile has the intended users, warehouse, price list, and payment methods.
- Every payment method has the correct RKSV classification: cash equivalent or non-cash. Check mixed payments as well as single-method payments.
- The POS Profile uses the app’s protected RKSV print format, shown in ERPNext as **POS Invoice RKSV**.
- Every tax rate used by the POS has an explicit Fiskaly VAT mapping. Ask the responsible accounting or tax adviser to confirm the mapping. Do not map a taxable rate to zero merely to bypass a validation error.

The app’s register form describes its core assignment as **one cash register per POS Profile and environment**. Plan a separate register for each relevant POS Profile in TEST and LIVE.

## 2. Create and test a TEST API connection

Open the **Fiskaly RKSV** workspace and select **API Connections**. You can also find the **Fiskaly API Connection** DocType with ERPNext’s search.

1. Create a connection for the intended company.
2. Select the provider and **TEST** environment.
   - Choose **SIGN_AT_V1** when preparing the currently supported production path.
   - **SIGN_AT_UNIFIED** is TEST-only. The app does not permit it to run in LIVE.
3. Enter the TEST API credentials in ERPNext. Do not place credentials in source files, Git, Wiki pages, tickets, or screenshots.
4. For TEST, use only syntactically valid dummy FinanzOnline values where requested. Never enter real LIVE FinanzOnline credentials into TEST.
5. Save the connection, then use **Verbindung testen** (Test Connection). Resolve any error before proceeding.
6. For SIGN_AT_V1, use **FinanzOnline authentifizieren** after saving the connection. This action is not shown for Unified.
7. For Unified, load the organization/scope only after saving the API credentials, then verify the returned organization before continuing.

Keep connections separate by company, provider, and environment. Do not change a provisioned connection’s provider, credentials, scope, or environment as a shortcut for moving a register between environments. The app requires confirmation for critical changes and locks register assignments after initialization.

## 3. Create the TEST cash register

Open **Cash Registers** in the **Fiskaly RKSV** workspace and create a register.

1. Set the register name, Company, POS Profile, API Connection, and operating address.
2. Check that the provider and environment displayed on the register match the selected connection.
3. For SIGN_AT_V1, select a matching existing SCU or choose to create a new one during provisioning. Only choose an existing SCU after confirming it belongs to the same environment and company identity.
4. Add the VAT mappings required by the POS Profile. Check every tax account/rate combination with accounting.
5. Review all assignments before selecting **Provision and initialize**. Provisioning creates provider-side resources and should not be repeated to correct a simple configuration mistake.
6. After provisioning, verify the provider register ID, register status, serial number, linked start receipt, and start-receipt FinanzOnline validation status.

The app locks key assignments after initialization or creation of the start receipt. If an assignment is wrong, stop and contact the Fiskaly administrator before processing sales.

## 4. Enable and accept TEST

Open **RKSV Settings** from the workspace.

1. Set the active environment to **TEST**.
2. Enable RKSV only after the TEST connection and register are ready.
3. Confirm the critical change when prompted.
4. Run a controlled acceptance checklist:
   - cash-equivalent payment;
   - non-cash payment;
   - mixed payment;
   - relevant tax rates and rounding;
   - receipt print and QR code;
   - return linked to the original invoice;
   - receipt-status monitoring;
   - recovery from a simulated TEST connection interruption, if this is part of the organization’s test plan.
5. Confirm that invoice totals and the fiscal receipt agree and that no receipt remains in an unexplained pending, failed, or action-required state.

Do not simulate outages by disconnecting a LIVE register. Use an approved TEST scenario only.

## 5. Prepare LIVE

After TEST acceptance, create a **separate LIVE API connection and a separate LIVE register**. Do not convert the TEST register.

1. Create a SIGN_AT_V1 connection for the correct company and select **LIVE**.
2. Enter the production Fiskaly credentials and the real FinanzOnline registrierkassen web-service user details. Limit access to authorized administrators.
3. Test the connection and complete the required FinanzOnline authentication.
4. Create a separate LIVE register linked to the correct LIVE POS Profile, connection, address, SCU, and VAT mappings.
5. Provision it once and verify the LIVE start receipt and its FinanzOnline status.
6. Only after the checks pass, set the global **RKSV Settings** environment to **LIVE**, confirm the critical change, and verify that the active connection and register are also LIVE.
7. Complete a supervised first sale and verify its receipt, QR code, and status.

**Unified LIVE is not available.** Do not enable it or use the `Allow Unified LIVE` setting as a workaround; the provider adapter has a hard LIVE guard.

## Security and access

- Give API connections, settings, register provisioning, outage handling, and exports only to the roles that need them.
- Never share credentials in this public Wiki or in issue reports. Use your approved secret-sharing method.
- Do not edit signature values, QR data, receipt identifiers, provider snapshots, or lifecycle evidence in the database.
- Treat fiscal receipts, DEP7 files, API logs, FinanzOnline references, and customer details as confidential operational data.
- Before changing an environment or credentials, verify the company, provider, and register assignment, then complete the app’s critical-change confirmation.

