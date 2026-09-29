from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
import requests

from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError, RetryableFiskalyError

SENSITIVE_KEYS = {
	"api_key",
	"api_secret",
	"client_secret",
	"cookie",
	"secret",
	"pin",
	"fon_user_pin",
	"access_token",
	"refresh_token",
	"token",
	"bearer",
	"authorization",
	"authorization_code",
	"password",
	"private_key",
	"session_id",
	"set_cookie",
}
COMPACT_SENSITIVE_KEYS = frozenset(item.replace("_", "") for item in SENSITIVE_KEYS)
REDACTED = "***"
API_LOG_JOB = "erpnext_fiskaly_sign_at.providers.http.persist_api_log"
API_LOG_FIELDS = (
	"connection",
	"receipt",
	"method",
	"url",
	"http_status",
	"success",
	"request_id",
	"request_payload",
	"response_payload",
	"error_message",
)
RETRYABLE_CODES = {408, 425, 429, 500, 502, 503, 504}
RETRYABLE_PROVIDER_CODES = {
	"E_FON_REQUEST_TIMEOUT",
	"FON_REQUEST_TIMEOUT",
	"TEMPORARILY_UNAVAILABLE",
	"SERVICE_UNAVAILABLE",
}


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
	if isinstance(value, dict):
		return {
			key: REDACTED if _is_sensitive_key(key) else redact(item, secrets) for key, item in value.items()
		}
	if isinstance(value, list):
		return [redact(item, secrets) for item in value]
	if isinstance(value, tuple):
		return tuple(redact(item, secrets) for item in value)
	if isinstance(value, str):
		return _redact_text(value, secrets)
	return value


def _is_sensitive_key(key: object) -> bool:
	name = str(key).casefold().replace("-", "_").replace(" ", "_")
	compact_name = name.replace("_", "")
	return (
		name in SENSITIVE_KEYS
		or compact_name in COMPACT_SENSITIVE_KEYS
		or compact_name.endswith(("apikey", "secret", "token", "password", "pin", "authorization"))
	)


def _redact_text(value: object, secrets: tuple[str, ...]) -> str:
	text = str(value)
	for secret in secrets:
		text = text.replace(secret, REDACTED)
	return text


def _add_secret(secrets: set[str], value: object):
	if value is None or isinstance(value, bool):
		return
	if isinstance(value, bytes):
		value = value.decode(errors="replace")
	if not isinstance(value, (str, int, float)):
		return
	secret = str(value)
	if not secret or secret == REDACTED:
		return
	secrets.add(secret)
	# Authorization headers commonly contain a scheme and the actual credential.
	# Both variants must be removed if either one is echoed elsewhere.
	if " " in secret:
		scheme, credential = secret.split(" ", 1)
		if scheme.casefold() in {"basic", "bearer", "token"} and credential:
			secrets.add(credential)


def _collect_sensitive_values(value: Any, secrets: set[str], sensitive: bool = False):
	if isinstance(value, dict):
		for key, item in value.items():
			_collect_sensitive_values(item, secrets, sensitive or _is_sensitive_key(key))
		return
	if isinstance(value, (list, tuple, set)):
		for item in value:
			_collect_sensitive_values(item, secrets, sensitive)
		return
	if sensitive:
		_add_secret(secrets, value)


def _collect_url_secrets(url: str, secrets: set[str]):
	try:
		query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
	except ValueError:
		return
	for key, value in query:
		if _is_sensitive_key(key):
			_add_secret(secrets, value)


def _known_secrets(
	*,
	connection_api_key: object,
	request: Any,
	response: Any,
	headers: dict[str, str] | None,
	url: str,
) -> tuple[str, ...]:
	secrets: set[str] = set()
	_add_secret(secrets, connection_api_key)
	_collect_sensitive_values(request, secrets)
	_collect_sensitive_values(response, secrets)
	_collect_sensitive_values(headers, secrets)
	_collect_url_secrets(url, secrets)
	# Replace longer credentials first so a short credential cannot leave a suffix
	# from a longer one behind in the queued evidence.
	return tuple(sorted(secrets, key=lambda item: (-len(item), item)))


def _redact_url(url: str, secrets: tuple[str, ...]) -> str:
	redacted_url = _redact_text(url, secrets)
	try:
		parts = urlsplit(redacted_url)
		query = urlencode(
			[
				(key, REDACTED if _is_sensitive_key(key) else value)
				for key, value in parse_qsl(parts.query, keep_blank_values=True)
			]
		)
		return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
	except ValueError:
		return redacted_url


def _redact_for_json(value: Any, secrets: tuple[str, ...]) -> Any:
	if isinstance(value, dict):
		return {
			_redact_text(key, secrets): (
				REDACTED if _is_sensitive_key(key) else _redact_for_json(item, secrets)
			)
			for key, item in value.items()
		}
	if isinstance(value, (list, tuple, set)):
		return [_redact_for_json(item, secrets) for item in value]
	if value is None or isinstance(value, (bool, int, float)):
		return value
	return _redact_text(value, secrets)


def _redacted_json(value: Any, secrets: tuple[str, ...]) -> str:
	# Convert custom values before serialization. Redacting the serialized string
	# itself could corrupt JSON syntax when a credential contains quotes or slashes.
	return json.dumps(_redact_for_json(value, secrets), ensure_ascii=False)


def persist_api_log(evidence: dict[str, Any]) -> str:
	"""Persist already-redacted API evidence in the background-job transaction."""

	if not isinstance(evidence, dict):
		raise TypeError("Fiskaly API log evidence must be a dictionary")
	document = {
		"doctype": "Fiskaly API Log",
		**{fieldname: evidence.get(fieldname) for fieldname in API_LOG_FIELDS},
	}
	log = frappe.get_doc(document).insert(ignore_permissions=True, ignore_links=True)
	return log.name


class FiskalyHttpClient:
	def __init__(self, connection, base_url: str, default_headers: dict[str, str] | None = None):
		self.connection = connection
		self.base_url = base_url.rstrip("/")
		self.default_headers = default_headers or {}
		settings = frappe.get_cached_doc("Fiskaly Settings")
		self.timeout = (
			int(settings.connect_timeout_seconds or 5),
			int(settings.read_timeout_seconds or 20),
		)

	def request(
		self,
		method: str,
		path: str,
		*,
		json_data: dict[str, Any] | None = None,
		headers: dict[str, str] | None = None,
		receipt: str | None = None,
		stream: bool = False,
	) -> Any:
		url = f"{self.base_url}/{path.lstrip('/')}"
		request_headers = {"Accept": "application/json", **self.default_headers, **(headers or {})}
		try:
			response = requests.request(
				method,
				url,
				json=json_data,
				headers=request_headers,
				timeout=self.timeout,
				stream=stream,
			)
		except (requests.Timeout, requests.ConnectionError) as exc:
			secrets = _known_secrets(
				connection_api_key=getattr(self.connection, "api_key", None),
				request=json_data,
				response=None,
				headers=request_headers,
				url=url,
			)
			safe_error = _redact_text(exc, secrets)
			self._log(
				method,
				url,
				json_data,
				None,
				None,
				receipt,
				safe_error,
				headers=request_headers,
			)
			raise RetryableFiskalyError(safe_error) from None

		request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
		try:
			body = response.json() if response.content else {}
		except ValueError:
			body = {"text": response.text[:4000]}
		self._log(
			method,
			url,
			json_data,
			body,
			response.status_code,
			receipt,
			None,
			request_id,
			headers=request_headers,
		)
		if not response.ok:
			message, code = self._error_details(body, response.status_code)
			secrets = _known_secrets(
				connection_api_key=getattr(self.connection, "api_key", None),
				request=json_data,
				response=body,
				headers=request_headers,
				url=url,
			)
			retryable_provider_error = bool(
				code
				and (
					code.upper() in RETRYABLE_PROVIDER_CODES
					or "TIMEOUT" in code.upper()
					or "PROCESSING" in code.upper()
				)
			)
			error_class = (
				RetryableFiskalyError
				if response.status_code in RETRYABLE_CODES or retryable_provider_error
				else PermanentFiskalyError
			)
			raise error_class(
				_redact_text(message, secrets),
				status_code=response.status_code,
				code=_redact_text(code, secrets) if code is not None else None,
				request_id=_redact_text(request_id, secrets) if request_id is not None else None,
				response=redact(body, secrets),
			)
		return response if stream else body

	@staticmethod
	def _error_details(body: Any, status_code: int) -> tuple[str, str | None]:
		if isinstance(body, dict):
			# SIGN AT v1 returns code/message at the top level while ``error`` is
			# merely the HTTP reason string (for example ``Bad Request``). Other
			# fiskaly APIs nest the same information below error or content.
			for error in (body.get("error"), body.get("content"), body):
				if isinstance(error, dict):
					code = error.get("code") or error.get("type")
					message = error.get("message") or error.get("detail") or error.get("description")
					if message:
						return str(message), str(code) if code else None
		return f"fiskaly returned HTTP {status_code}", None

	def _log(
		self,
		method: str,
		url: str,
		request: Any,
		response: Any,
		status_code: int | None,
		receipt: str | None,
		error: str | None,
		request_id: str | None = None,
		*,
		headers: dict[str, str] | None = None,
	):
		try:
			secrets = _known_secrets(
				connection_api_key=getattr(self.connection, "api_key", None),
				request=request,
				response=response,
				headers=headers,
				url=url,
			)
			evidence = {
				"connection": _redact_text(self.connection.name, secrets),
				"receipt": _redact_text(receipt, secrets) if receipt is not None else None,
				"method": _redact_text(method.upper(), secrets),
				"url": _redact_url(url, secrets),
				"http_status": status_code,
				"success": int(bool(status_code and 200 <= status_code < 300)),
				"request_id": _redact_text(request_id, secrets) if request_id is not None else None,
				"request_payload": _redacted_json(request, secrets),
				"response_payload": _redacted_json(response, secrets),
				"error_message": _redact_text(error, secrets) if error is not None else None,
			}
			# This must be enqueued immediately: after-commit jobs are discarded by an
			# outer business rollback. The worker owns the independent log transaction;
			# the request thread neither inserts nor commits database records here.
			frappe.enqueue(
				API_LOG_JOB,
				queue="short",
				is_async=True,
				enqueue_after_commit=False,
				evidence=evidence,
			)
		except Exception:
			# Avoid exception details here: a queue/serialization error can contain the
			# job arguments. The arguments are redacted, but this keeps the fallback log
			# on the same strict no-payload boundary.
			frappe.logger("fiskaly").error("Could not enqueue the redacted fiskaly API log")
