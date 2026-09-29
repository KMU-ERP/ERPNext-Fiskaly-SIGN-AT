from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from erpnext_fiskaly_sign_at.contracts import Environment, FiscalReceiptRequest, FiscalReceiptResult


@dataclass(frozen=True)
class ProviderCapabilities:
	sign_receipts: bool = True
	register_resources: bool = True
	export_dep7: bool = True
	register_lifecycle: bool = False
	scu_lifecycle: bool = False
	automatic_closing_receipts: bool = False
	fon_receipt_validation: bool = False
	live: bool = True


class SignAtProvider(ABC):
	code: str
	api_version: str
	capabilities = ProviderCapabilities()

	def __init__(self, connection):
		self.connection = connection

	@property
	def environment(self) -> Environment:
		return Environment(self.connection.environment)

	@abstractmethod
	def sign_receipt(self, register, request: FiscalReceiptRequest) -> FiscalReceiptResult:
		raise NotImplementedError

	@abstractmethod
	def test_connection(self) -> dict[str, Any]:
		raise NotImplementedError

	@abstractmethod
	def export_dep7(self, register, date_from=None, date_to=None) -> dict[str, Any]:
		raise NotImplementedError

	def provision_register(self, register) -> dict[str, Any]:
		raise NotImplementedError(f"{self.code} does not implement automatic register provisioning")

	def retrieve_register(self, register) -> dict[str, Any]:
		raise NotImplementedError(f"{self.code} does not implement cash-register retrieval")

	def transition_register_state(self, register, state: str) -> dict[str, Any]:
		raise NotImplementedError(f"{self.code} does not implement cash-register lifecycle transitions")

	def retrieve_scu(self, register) -> dict[str, Any]:
		raise NotImplementedError(f"{self.code} does not implement SCU retrieval")

	def transition_scu_state(self, register, state: str) -> dict[str, Any]:
		raise NotImplementedError(f"{self.code} does not implement SCU lifecycle transitions")

	def list_automatic_receipts(
		self, register, receipt_types: tuple[str, ...] | list[str]
	) -> list[dict[str, Any]]:
		raise NotImplementedError(f"{self.code} does not expose automatic closing receipts")

	def validate_receipt_with_fon(self, register, receipt_id: str) -> dict[str, Any]:
		raise NotImplementedError(f"{self.code} does not expose FinanzOnline receipt validation")
