# ERPNext Fiskaly SIGN AT – Operations Guide

This Wiki explains how to configure and operate the Austrian Fiskaly SIGN AT integration for ERPNext 16. It is intended for POS cashiers, POS managers, Fiskaly administrators, and accounting staff.

> **Public information:** The GitHub repository and its Wiki are public. Never add API keys, API secrets, FinanzOnline credentials, customer data, receipt data, screenshots containing personal data, or production identifiers to this Wiki.

## Read the guide

- [Administrator Guide](Administrator-Guide) — prerequisites, TEST setup, connections, registers, tax mappings, and LIVE activation.
- [POS Operations](POS-Operations) — opening a shift, completing sales, printing receipts, returns, and closing a shift.
- [Outages, Exports, and Decommissioning](Outages-Exports-and-Decommissioning) — incident handling, receipt status, DEP7 evidence, and register closure.

## Before using the system

1. Complete the setup and acceptance checks in TEST.
2. Keep TEST and LIVE credentials, API connections, and registers separate.
3. Use the built-in **Fiskaly RKSV** workspace and role-specific help pages for current in-app guidance.
4. Do not alter fiscal receipt data or lifecycle records manually. Escalate unresolved issues to the Fiskaly administrator.

The app currently supports **SIGN AT v1** for production operation. **SIGN AT Unified** is available for TEST integration only; its LIVE operation is disabled in the app.

This guide describes the app’s operational workflow. It is not tax or legal advice. Have the responsible accounting or tax adviser confirm company-specific settings and procedures.

