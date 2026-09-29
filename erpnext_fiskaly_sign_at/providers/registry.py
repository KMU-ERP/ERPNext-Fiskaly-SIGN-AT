from __future__ import annotations

from erpnext_fiskaly_sign_at.contracts import ProviderCode
from erpnext_fiskaly_sign_at.providers.base import SignAtProvider
from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError


def provider_classes() -> dict[str, type[SignAtProvider]]:
	# Lazy imports keep future provider adapters isolated from one another.
	from erpnext_fiskaly_sign_at.providers.sign_at_unified import SignAtUnifiedProvider
	from erpnext_fiskaly_sign_at.providers.sign_at_v1 import SignAtV1Provider

	return {
		ProviderCode.SIGN_AT_V1.value: SignAtV1Provider,
		ProviderCode.SIGN_AT_UNIFIED.value: SignAtUnifiedProvider,
	}


def get_provider(connection) -> SignAtProvider:
	try:
		provider_class = provider_classes()[connection.provider]
	except KeyError as exc:
		raise PermanentFiskalyError(f"Unknown fiskaly provider: {connection.provider}") from exc
	return provider_class(connection)
