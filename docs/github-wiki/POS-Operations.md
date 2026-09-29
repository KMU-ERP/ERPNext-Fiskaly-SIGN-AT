# POS Operations

This page covers the cashier and POS manager workflow. Follow the organization’s cash-handling procedures alongside these steps.

## Cashier: start a shift

1. Open **Open POS** (POS Opening Entry) from the **POS Cashier** workspace.
2. Select the assigned POS Profile and enter the actual opening cash balance.
3. Submit one opening entry before the first sale. Do not create another opening while your session is still open.
4. Open **Point of Sale** using the same POS Profile.

## Cashier: complete a sale

1. Add the items and confirm quantities, customer, and tax treatment.
2. Select the correct payment method. For mixed payments, enter each amount accurately and ensure the total equals the invoice total.
3. Submit the POS invoice through Point of Sale. Do not record a cash sale through an unrelated non-POS workflow.
4. Print the fiscal receipt using the app’s protected RKSV print action. Check that the receipt and QR code are legible before handing it to the customer.
5. If ERPNext reports that the receipt is pending, retrying, failed, or requires action, do not create a replacement invoice or alter fiscal fields. Tell the POS Manager and check the status before taking further action.

Use the original print for the customer. A later copy must be printed through the controlled action; copies are marked as duplicates. Desk previews are not a substitute for the customer receipt.

## Cashier: returns and corrections

- Create a return using the approved ERPNext POS return workflow and reference the original invoice.
- Do not directly cancel, delete, or edit a fiscalized invoice to reverse a sale.
- If the original invoice or its receipt cannot be found, stop and ask the POS Manager to investigate.

## Cashier: close a shift

1. Open **Close POS** (POS Closing Entry) and select your open session.
2. Count the actual cash and other payment totals and enter them accurately.
3. Compare the counts with the recorded payment methods and submit the closing entry.
4. If a fiscal receipt is still pending or the totals do not match, contact the POS Manager before closing.

## POS Manager: daily checks

Review these areas during the shift and at close:

- **POS Openings** and **POS Closings** — sessions left open and differences between counted and recorded amounts.
- **POS Invoices** and **Fiscal Receipts** — invoice/return relationship and linked fiscal receipt.
- **Receipt Status** — receipts pending, retrying, failed, or marked as action required.
- **POS Profiles** and **Modes of Payment** — users, warehouse, price list, payment methods, and RKSV classification.

Coordinate tax mapping, register, connection, and environment changes with the Fiskaly administrator. Never repair signature data or QR content manually.

## Receipt statuses

The exact wording may differ slightly by screen, but the app uses these main states:

| Status | Operator action |
| --- | --- |
| `SIGNED` | Provider returned a signed receipt. Check the receipt and print evidence as required. |
| `OFFLINE_PENDING` / `RETRYING` | The app is waiting to synchronize or retry. Preserve the original invoice and monitor **Receipt Status**. |
| `SUBSTITUTE_SIGNED` | A substitute/offline signing path was used. Follow the outage and recovery procedure and confirm the follow-up status. |
| `ACTION_REQUIRED` | A person must investigate. Escalate to the POS Manager or Fiskaly administrator; do not create a duplicate sale. |
| `PREPARED` / `SIGNING` | Processing is in progress. Refresh the status; escalate if it remains stuck or an error is shown. |

