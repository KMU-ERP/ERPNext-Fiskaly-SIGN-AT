from __future__ import annotations

from typing import Any


class FiskalyError(Exception):
	def __init__(
		self,
		message: str,
		*,
		status_code: int | None = None,
		code: str | None = None,
		request_id: str | None = None,
		response: Any = None,
	):
		super().__init__(message)
		self.status_code = status_code
		self.code = code
		self.request_id = request_id
		self.response = response


class RetryableFiskalyError(FiskalyError):
	"""A transient network, rate-limit, provider, or FinanzOnline error."""


class PermanentFiskalyError(FiskalyError):
	"""An invalid payload, credential, state, or configuration error."""


class ProviderNotAvailableError(PermanentFiskalyError):
	"""A provider capability is intentionally guarded or unavailable."""
