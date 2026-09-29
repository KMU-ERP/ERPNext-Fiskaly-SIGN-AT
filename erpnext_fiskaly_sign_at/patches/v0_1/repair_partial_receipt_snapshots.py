from erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots import (
	execute as repair_snapshots,
)


def execute():
	"""Run the expanded idempotent repair on sites that executed the original patch."""
	repair_snapshots()
