from erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots import (
	execute as repair_verified_snapshots,
)


def execute():
	"""Re-run the fail-closed repair on sites that executed an earlier snapshot patch."""

	repair_verified_snapshots()
