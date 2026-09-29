from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from frappe.utils import get_datetime, get_system_timezone

from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError


def provider_datetime_for_db(value: Any, *, fieldname: str = "provider timestamp") -> datetime | None:
	"""Convert a provider instant to Frappe's naive system-timezone representation."""
	if value in (None, ""):
		return None
	try:
		stamp = (
			datetime.fromtimestamp(value, tz=UTC)
			if isinstance(value, int | float) and not isinstance(value, bool)
			else get_datetime(value)
		)
	except OverflowError, TypeError, ValueError:
		stamp = None
	if not isinstance(stamp, datetime):
		raise PermanentFiskalyError(
			f"fiskaly returned an invalid {fieldname}",
			code="E_INVALID_PROVIDER_DATETIME",
			response={fieldname: value},
		)
	if stamp.tzinfo is not None:
		stamp = stamp.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
	return stamp
