from erpnext_fiskaly_sign_at.patches.v0_1.backfill_provider_request_hashes import (
	execute as verify_and_backfill_hashes,
)


def execute():
	"""Re-audit sites that may have run the original permissive hash backfill."""

	verify_and_backfill_hashes()
