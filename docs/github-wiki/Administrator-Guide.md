# 🛠️ Administrator Guide

[🇬🇧 English](Administrator-Guide) · [🇩🇪 Deutsch](Administrationshandbuch) · [Home](Home)

Use this process for each company and environment. Complete TEST and document acceptance before preparing LIVE.

## 1 · Prepare ERPNext

Before creating Fiskaly resources, confirm the following:

| Check | What to confirm |
| --- | --- |
| Company | Currency is **EUR**, time zone is **Europe/Vienna**, Austrian tax identifiers are correct, and the operating address is complete. |
| POS Profile | Users, warehouse, price list, and payment methods are correct for the register. |
| Payment methods | Each method has the correct RKSV classification: cash equivalent or non-cash. Include mixed payments in acceptance checks. |
| Receipt print | The POS Profile uses the protected **POS Invoice RKSV** print format. |
| VAT mappings | Every tax rate used by the POS has a mapping. Confirm each mapping with accounting; do not map a taxable rate to zero to bypass a validation error. |

The register form describes the assignment as **one cash register per POS Profile and environment**. Plan a separate register for each relevant profile in TEST and LIVE.

## 2 · Create a TEST API connection

Open **Fiskaly RKSV → API Connections**, or search for **Fiskaly API Connection** in ERPNext.

1. Create a connection for the intended company and select **TEST**.
2. Select the provider:
   - **SIGN_AT_V1** for the current production path.
   - **SIGN_AT_UNIFIED** for TEST integration only; the app disables Unified LIVE.
3. Enter the TEST API credentials in ERPNext. Never put credentials in source files, Git, the Wiki, tickets, or screenshots.
4. In TEST, enter only syntactically valid dummy FinanzOnline values where requested. Never use real LIVE credentials in TEST.
5. Save the connection, then click **Verbindung testen** (Test Connection). Resolve errors before continuing.
6. For SIGN_AT_V1, click **FinanzOnline authentifizieren** after saving. This action is not available for Unified.
7. For Unified, load the organization/scope after saving the credentials, then verify that it belongs to the API key.

Keep connections separate by company, provider, and environment. Do not repurpose a provisioned connection to move a register between environments. Critical changes require confirmation, and register assignments are locked after initialization.

## 3 · Create and provision the TEST register

Open **Fiskaly RKSV → Cash Registers** and create a register.

1. Set the register name, Company, POS Profile, API Connection, and operating address.
2. Confirm the displayed provider and environment match the connection.
3. For SIGN_AT_V1, choose a matching existing SCU or select the option to create one during provisioning. An existing SCU must belong to the same environment and company identity.
4. Add the VAT mappings required by the POS Profile.
5. Review every assignment, then click **Provision and initialize** once.
6. Verify the provider register ID, status, serial number, linked start receipt, and—on SIGN_AT_V1—the start receipt’s FinanzOnline validation status.

> 🔒 **After initialization:** Key assignments are locked after initialization or creation of the start receipt. If an assignment is wrong, stop and contact the Fiskaly administrator before processing sales.

## 4 · Enable and accept TEST

Open **Fiskaly RKSV → RKSV Settings**.

1. Set the active environment to **TEST**.
2. Enable RKSV only after the TEST connection and register are ready.
3. Confirm the critical change if prompted.
4. Run a controlled acceptance checklist:
   - cash-equivalent payment;
   - non-cash payment;
   - mixed payment;
   - relevant tax rates and rounding;
   - receipt print and QR code;
   - return linked to the original invoice;
   - receipt-status monitoring;
   - recovery from a simulated TEST interruption, if included in your test plan.
5. Confirm invoice totals and fiscal receipt data agree. Resolve any unexplained pending, failed, or action-required receipt before acceptance.

Never simulate an outage by disconnecting a LIVE register.

## 5 · Prepare LIVE

After TEST acceptance, create a **separate LIVE API connection and a separate LIVE register**. Do not convert the TEST register.

1. Create a SIGN_AT_V1 connection for the correct company and select **LIVE**.
2. Enter production Fiskaly credentials and the authorized real FinanzOnline registrierkassen web-service user details.
3. Test the connection and complete the required FinanzOnline authentication.
4. Create a separate LIVE register linked to the correct LIVE POS Profile, connection, address, SCU, and VAT mappings.
5. Provision once and verify the LIVE start receipt and its FinanzOnline status.
6. Only after these checks pass, set the global **RKSV Settings** environment to **LIVE**, confirm the critical change, and verify the active connection and register are also LIVE.
7. Supervise the first sale and verify its receipt, QR code, and status.

> ⛔ **Unified LIVE is unavailable.** Do not use the `Allow Unified LIVE` setting as a workaround; the provider adapter has a hard LIVE guard.

## Security and access

- Grant access to API connections, settings, register provisioning, outages, and exports only to roles that need them.
- Never share credentials in this public Wiki. Use your approved private secret-sharing method.
- Do not edit signatures, QR data, receipt identifiers, provider snapshots, or lifecycle evidence in the database.
- Treat fiscal receipts, DEP7 files, API logs, FinanzOnline references, and customer details as confidential operational data.
- Before changing an environment or credentials, verify the company, provider, and register assignment, then complete the app’s critical-change confirmation.

