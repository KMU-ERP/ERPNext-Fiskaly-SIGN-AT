from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

PROVIDER_DEFAULTS = {
	"SIGN_AT_V1": {
		"api_version": "v1",
		"TEST": "https://rksv.fiskaly.com/api/v1",
		"LIVE": "https://rksv.fiskaly.com/api/v1",
	},
	"SIGN_AT_UNIFIED": {
		"api_version": "2026-06-01",
		"TEST": "https://test.api.fiskaly.com",
		"LIVE": "https://live.api.fiskaly.com",
	},
}
LOCAL_TEST_HOSTS = {"localhost", "127.0.0.1", "::1"}
AUDITED_FIELDS = (
	"active",
	"company",
	"provider",
	"environment",
	"api_key",
	"api_secret",
	"api_version",
	"scope_identifier",
	"base_url_override",
	"tax_id_number",
	"vat_id_number",
	"fon_participant_id",
	"fon_user_id",
	"fon_user_pin",
)


def validate_fon_credential_format(
	participant_id: str | None,
	user_id: str | None,
	pin: str | None,
	*,
	provider: str = "SIGN_AT_UNIFIED",
) -> None:
	"""Validate the public FinanzOnline limits before a provider request is sent."""
	errors = []
	minimum_length = 8 if provider == "SIGN_AT_UNIFIED" else 5
	if participant_id and not re.fullmatch(r"[0-9A-Za-z]{8,12}", str(participant_id)):
		errors.append(
			_(
				"Die Teilnehmer-ID muss aus 8 bis 12 Buchstaben oder Ziffern bestehen "
				"(aktuell: {0} Zeichen)."
			).format(len(str(participant_id)))
		)
	if user_id and not minimum_length <= len(str(user_id)) <= 12:
		errors.append(
			_("Die Benutzer-ID muss {0} bis 12 Zeichen lang sein (aktuell: {1} Zeichen).").format(
				minimum_length, len(str(user_id))
			)
		)
	if pin and not minimum_length <= len(str(pin)) <= 128:
		errors.append(
			_("Die PIN muss {0} bis 128 Zeichen lang sein (aktuell: {1} Zeichen).").format(
				minimum_length, len(str(pin))
			)
		)
	if errors:
		frappe.throw("<br>".join(errors), title=_("FinanzOnline-Daten ungültig"))


def normalize_austrian_tax_id_number(value: str | None) -> str | None:
	"""Format compact Austrian tax numbers for the Unified API."""
	if not value:
		return None
	compact = re.sub(r"[-/\s]", "", str(value).strip())
	if re.fullmatch(r"[0-9]{9}", compact):
		return f"{compact[:2]}-{compact[2:5]}/{compact[5:]}"
	if re.fullmatch(r"[0-9]{7}", compact):
		return f"{compact[:3]}/{compact[3:]}"
	frappe.throw(
		_(
			"Die österreichische Steuernummer muss 7 oder 9 Ziffern enthalten, z. B. "
			"543762173 (wird als 54-376/2173 formatiert)."
		),
		title=_("Steuernummer ungültig"),
	)


def normalize_austrian_vat_id_number(value: str | None) -> str | None:
	"""Keep the Austrian UID in its customary ATU12345678 representation."""
	if not value:
		return None
	normalized = re.sub(r"\s+", "", str(value)).upper()
	if re.fullmatch(r"ATU[0-9]{8}", normalized):
		return normalized
	if re.fullmatch(r"U[0-9]{8}", normalized):
		return f"AT{normalized}"
	frappe.throw(
		_("Die österreichische UID muss aus ATU und 8 Ziffern bestehen, z. B. ATU77315745."),
		title=_("UID-Nummer ungültig"),
	)


def unified_austrian_vat_id_number(value: str | None) -> str | None:
	"""Return fiskaly Unified's Austrian UID representation without the AT prefix."""
	normalized = normalize_austrian_vat_id_number(value)
	return normalized[2:] if normalized else None


def _is_system_manager() -> bool:
	user = getattr(getattr(frappe, "session", None), "user", None)
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def validate_base_url_override(
	url: str | None,
	environment: str,
	*,
	developer_mode: bool | None = None,
) -> str | None:
	"""Validate and normalize a privileged provider endpoint override.

	Production sites are restricted to HTTPS under fiskaly.com. Loopback hosts are
	accepted only for TEST; developer_mode is the explicit escape hatch for local
	integration servers. Credentials, query parameters and fragments are never
	valid in an API base URL.
	"""

	if not url:
		return None
	value = str(url).strip().rstrip("/")
	if any(character.isspace() for character in value) or "\\" in value:
		frappe.throw(_("The API endpoint must not contain whitespace or backslashes."))
	try:
		parts = urlsplit(value)
		# Accessing port also validates malformed/non-numeric port declarations.
		parts.port
	except ValueError:
		frappe.throw(_("The API endpoint is not a valid URL."))
	if not parts.scheme or not parts.netloc or not parts.hostname:
		frappe.throw(_("The API endpoint must be an absolute URL."))
	if parts.username or parts.password:
		frappe.throw(_("Credentials are not permitted in the API endpoint URL."))
	if parts.query or parts.fragment:
		frappe.throw(_("Query strings and fragments are not permitted in the API endpoint URL."))

	host = parts.hostname.rstrip(".").lower()
	scheme = parts.scheme.lower()
	local_test = host in LOCAL_TEST_HOSTS
	if developer_mode is None:
		developer_mode = bool(cint(getattr(frappe.conf, "developer_mode", 0)))

	if environment == "LIVE":
		if scheme != "https" or (host != "fiskaly.com" and not host.endswith(".fiskaly.com")):
			frappe.throw(_("LIVE API endpoints must use HTTPS and be hosted below fiskaly.com."))
	elif developer_mode:
		if scheme not in {"http", "https"}:
			frappe.throw(_("Developer API endpoints must use HTTP or HTTPS."))
	elif local_test:
		if environment != "TEST":
			frappe.throw(_("A localhost API endpoint is permitted only in TEST."))
		if scheme not in {"http", "https"}:
			frappe.throw(_("A localhost TEST endpoint must use HTTP or HTTPS."))
	else:
		if scheme != "https":
			frappe.throw(_("Non-local Fiskaly API endpoints must use HTTPS."))
		if host != "fiskaly.com" and not host.endswith(".fiskaly.com"):
			frappe.throw(_("Outside developer_mode, API endpoints must be hosted below fiskaly.com."))

	return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class FiskalyAPIConnection(Document):
	def validate(self):
		if self.provider not in PROVIDER_DEFAULTS:
			frappe.throw(_("Unsupported Fiskaly provider adapter: {0}").format(self.provider))
		if self.environment not in {"TEST", "LIVE"}:
			frappe.throw(_("The environment must be TEST or LIVE."))
		if self.provider == "SIGN_AT_UNIFIED" and self.environment == "LIVE":
			frappe.throw(
				_(
					"Unified SIGN AT is not released for LIVE use. Create a SIGN AT v1 LIVE "
					"connection or keep this connection in TEST."
				),
				title=_("Unified LIVE blocked"),
			)
		validate_fon_credential_format(
			self.fon_participant_id,
			self.fon_user_id,
			self.get_password("fon_user_pin", raise_exception=False),
			provider=self.provider,
		)
		self.tax_id_number = normalize_austrian_tax_id_number(self.tax_id_number)
		self.vat_id_number = normalize_austrian_vat_id_number(self.vat_id_number)
		self._validate_unified_tenant_binding()

		self._changed_fields = self._critical_changes()
		self._validate_critical_confirmation()
		self._validate_locked_adapter()
		self._validate_override_permission()
		self.base_url_override = validate_base_url_override(self.base_url_override, self.environment)
		if not self.api_version:
			self.api_version = PROVIDER_DEFAULTS[self.provider]["api_version"]
		self.base_url = self._base_url()
		if self._changed_fields and not self.is_new():
			self.status = "UNTESTED"
			self.last_tested_at = None
			self.last_error = None
		self.critical_change_confirmation = 0

	def _validate_unified_tenant_binding(self):
		if self.provider != "SIGN_AT_UNIFIED":
			return
		for fieldname, value in (
			("api_key", self.api_key),
			("scope_identifier", self.scope_identifier),
		):
			if not value:
				continue
			conflict = frappe.db.get_value(
				"Fiskaly API Connection",
				{
					"provider": "SIGN_AT_UNIFIED",
					fieldname: value,
					"company": ["!=", self.company],
					"name": ["!=", self.name],
				},
				["name", "company"],
				as_dict=True,
			)
			if conflict:
				frappe.throw(
					_(
						"These Unified credentials are already assigned to company {0} through "
						"connection {1}. Use an API key from the customer's own fiskaly organization."
					).format(frappe.bold(conflict.company), frappe.bold(conflict.name)),
					title=_("Unified organization already assigned"),
				)

	def on_update(self):
		changes = getattr(self, "_changed_fields", ())
		if not changes:
			return
		frappe.cache.delete_value(
			[f"fiskaly:v1:token:{self.name}", f"fiskaly:unified:token:{self.name}"],
			shared=True,
		)
		message = _("Critical Fiskaly API connection change confirmed by {0}: {1}").format(
			frappe.session.user, ", ".join(changes)
		)
		frappe.logger("erpnext_fiskaly_sign_at.audit", allow_site=True).warning(message)
		self.add_comment("Info", message)

	def _critical_changes(self) -> tuple[str, ...]:
		old = self.get_doc_before_save()
		if not old:
			return tuple(
				fieldname for fieldname in AUDITED_FIELDS if self.get(fieldname) not in (None, "", 0)
			)
		return tuple(fieldname for fieldname in AUDITED_FIELDS if old.get(fieldname) != self.get(fieldname))

	def _validate_critical_confirmation(self):
		if not self._changed_fields:
			return
		if not cint(self.critical_change_confirmation):
			frappe.throw(
				_(
					"Confirm this critical API connection change explicitly. Provider, environment, "
					"credentials and endpoints directly affect fiscal records."
				),
				title=_("Confirmation required"),
			)
		if self.environment == "LIVE" and not _is_system_manager():
			frappe.throw(
				_("Only a System Manager may create or change a LIVE Fiskaly connection."),
				title=_("System Manager required"),
			)

	def _validate_locked_adapter(self):
		if self.is_new():
			return
		old = self.get_doc_before_save()
		if old and (old.provider != self.provider or old.environment != self.environment):
			linked = frappe.db.exists("Fiskaly Register", {"connection": self.name})
			if linked:
				frappe.throw(
					_("Provider and environment cannot be changed after a register has been linked.")
				)

	def _validate_override_permission(self):
		old = self.get_doc_before_save()
		old_value = old.base_url_override if old else None
		if old_value == self.base_url_override:
			return
		if not _is_system_manager():
			frappe.throw(
				_("Only a System Manager may set or change an API endpoint override."),
				title=_("System Manager required"),
			)

	def _base_url(self):
		if self.base_url_override:
			return self.base_url_override
		return PROVIDER_DEFAULTS[self.provider][self.environment]

	@frappe.whitelist(methods=["POST"])
	def test_connection(self):
		self.check_permission("write")
		from erpnext_fiskaly_sign_at.providers import get_provider

		try:
			result = get_provider(self).test_connection()
		except Exception as exc:
			error = str(exc)
			tested_at = frappe.utils.now()
			self.db_set({"status": "ERROR", "last_error": error, "last_tested_at": tested_at})
			# Returning normally lets Frappe commit the status update at request end.
			# Re-raising here would roll the same request transaction back.
			return {
				"success": False,
				"status": "ERROR",
				"last_tested_at": tested_at,
				"result": None,
				"error": error,
			}

		tested_at = frappe.utils.now()
		self.db_set({"status": "CONNECTED", "last_error": None, "last_tested_at": tested_at})
		return {
			"success": True,
			"status": "CONNECTED",
			"last_tested_at": tested_at,
			"result": result,
			"error": None,
		}

	@frappe.whitelist(methods=["POST"])
	def get_unified_scopes(self):
		"""Return the organizations selectable for this Unified API credential."""
		self.check_permission("write")
		if self.provider != "SIGN_AT_UNIFIED":
			frappe.throw(_("Organization scopes are available only for SIGN AT Unified."))
		from erpnext_fiskaly_sign_at.providers import get_provider

		return get_provider(self).list_scopes()

	@frappe.whitelist(methods=["POST"])
	def authenticate_fon(self):
		self.check_permission("write")
		frappe.only_for("System Manager")
		if self.provider != "SIGN_AT_V1":
			frappe.throw(
				_("FinanzOnline authentication is part of taxpayer provisioning in Unified SIGN AT.")
			)
		from erpnext_fiskaly_sign_at.providers import get_provider

		return get_provider(self).authenticate_fon()
