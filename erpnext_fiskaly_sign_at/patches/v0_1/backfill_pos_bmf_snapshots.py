from erpnext_fiskaly_sign_at.patches.v0_1.backfill_receipt_company_snapshots import (
	execute as repair_verified_snapshots,
)


def execute():
	"""Populate the dedicated POS Invoice BMF fields from verified fiscal evidence."""

	repair_verified_snapshots()
