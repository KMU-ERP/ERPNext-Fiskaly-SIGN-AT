import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from erpnext_fiskaly_sign_at.providers.errors import RetryableFiskalyError
from erpnext_fiskaly_sign_at.providers.http import (
	API_LOG_FIELDS,
	API_LOG_JOB,
	FiskalyHttpClient,
	persist_api_log,
)


def make_client(api_key="api-key-value"):
	client = object.__new__(FiskalyHttpClient)
	client.connection = SimpleNamespace(name="TEST-CONNECTION", api_key=api_key)
	client.base_url = "https://rksv.fiskaly.com/api/v1"
	client.default_headers = {}
	client.timeout = (5, 20)
	return client


def json_response(body, status_code=200):
	response = Mock()
	response.headers = {"x-request-id": "request-123"}
	response.content = b"{}"
	response.status_code = status_code
	response.ok = 200 <= status_code < 300
	response.json.return_value = body
	return response


class TestFiskalyHttpLogging(TestCase):
	def test_v1_top_level_error_details_are_preserved(self):
		message, code = FiskalyHttpClient._error_details(
			{
				"code": "E_SCU_LIMIT_REACHED",
				"status_code": 400,
				"message": "Limit of 1 active signature creation units reached.",
				"error": "Bad Request",
			},
			400,
		)

		self.assertEqual(message, "Limit of 1 active signature creation units reached.")
		self.assertEqual(code, "E_SCU_LIMIT_REACHED")

	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.enqueue")
	@patch("erpnext_fiskaly_sign_at.providers.http.requests.request")
	def test_success_evidence_is_redacted_before_immediate_enqueue(self, request, enqueue):
		api_key = "api-key-value"
		api_secret = "api-secret-value"
		token = "bearer-token-value"
		request.return_value = json_response(
			{
				"access_token": token,
				"echo": f"credentials={api_key}:{api_secret}:{token}",
				"safe": "provider-evidence",
			}
		)
		client = make_client(api_key)

		result = client.request(
			"POST",
			f"/auth?access-token={token}",
			json_data={
				"content": {"type": "API_KEY", "key": api_key, "secret": api_secret},
				"echo": api_secret,
			},
			headers={"Authorization": f"Bearer {token}"},
		)

		self.assertEqual(result["safe"], "provider-evidence")
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args, (API_LOG_JOB,))
		self.assertEqual(enqueue.call_args.kwargs["queue"], "short")
		self.assertIs(enqueue.call_args.kwargs["is_async"], True)
		self.assertIs(enqueue.call_args.kwargs["enqueue_after_commit"], False)
		evidence = enqueue.call_args.kwargs["evidence"]
		queued_payload = json.dumps(enqueue.call_args.kwargs, ensure_ascii=False, default=str)
		for secret in (api_key, api_secret, token, f"Bearer {token}"):
			self.assertNotIn(secret, queued_payload)
		self.assertEqual(json.loads(evidence["request_payload"])["content"]["secret"], "***")
		self.assertEqual(json.loads(evidence["request_payload"])["content"]["key"], "***")
		self.assertEqual(json.loads(evidence["response_payload"])["access_token"], "***")
		self.assertEqual(evidence["success"], 1)
		self.assertNotIn("Authorization", queued_payload)

	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.enqueue")
	@patch("erpnext_fiskaly_sign_at.providers.http.requests.request")
	def test_transport_failure_enqueues_redacted_evidence_and_raises_safe_error(self, request, enqueue):
		api_secret = "api-secret-value"
		token = "bearer-token-value"
		request.side_effect = requests.Timeout(f"timeout while sending {api_secret} with Bearer {token}")
		client = make_client()

		with self.assertRaises(RetryableFiskalyError) as failure:
			client.request(
				"POST",
				"/auth",
				json_data={"api_secret": api_secret},
				headers={"Authorization": f"Bearer {token}"},
			)

		self.assertNotIn(api_secret, str(failure.exception))
		self.assertNotIn(token, str(failure.exception))
		evidence = enqueue.call_args.kwargs["evidence"]
		queued_payload = json.dumps(enqueue.call_args.kwargs, ensure_ascii=False, default=str)
		self.assertNotIn(api_secret, queued_payload)
		self.assertNotIn(token, queued_payload)
		self.assertEqual(evidence["success"], 0)
		self.assertIsNone(evidence["http_status"])
		self.assertEqual(evidence["response_payload"], "null")
		self.assertIn("***", evidence["error_message"])

	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.enqueue")
	@patch("erpnext_fiskaly_sign_at.providers.http.requests.request")
	def test_http_failure_is_queued_and_exception_evidence_stays_redacted(self, request, enqueue):
		api_secret = "api-secret-value"
		token = "bearer-token-value"
		request.return_value = json_response(
			{
				"error": {
					"code": "SERVICE_UNAVAILABLE",
					"message": f"provider echoed {api_secret} and {token}",
				},
				"access_token": token,
			},
			503,
		)
		client = make_client()

		with self.assertRaises(RetryableFiskalyError) as failure:
			client.request(
				"POST",
				"/receipts",
				json_data={"api_secret": api_secret},
				headers={"Authorization": f"Bearer {token}"},
			)

		self.assertEqual(failure.exception.status_code, 503)
		self.assertNotIn(api_secret, str(failure.exception))
		self.assertNotIn(token, str(failure.exception))
		self.assertNotIn(token, json.dumps(failure.exception.response))
		evidence = enqueue.call_args.kwargs["evidence"]
		self.assertEqual(evidence["http_status"], 503)
		self.assertEqual(evidence["success"], 0)
		queued_payload = json.dumps(enqueue.call_args.kwargs, ensure_ascii=False, default=str)
		self.assertNotIn(api_secret, queued_payload)
		self.assertNotIn(token, queued_payload)

	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.db.commit")
	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.get_doc")
	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.enqueue")
	def test_outer_rollback_boundary_uses_worker_transaction(self, enqueue, get_doc, commit):
		client = make_client()
		client._log(
			"GET",
			"https://rksv.fiskaly.com/api/v1/receipts/id",
			None,
			{"state": "SIGNED"},
			200,
			"receipt-id",
			None,
		)

		# The request transaction only publishes an immediate, non-after-commit job.
		# Therefore a later rollback cannot remove the already queued evidence.
		enqueue.assert_called_once()
		self.assertIs(enqueue.call_args.kwargs["enqueue_after_commit"], False)
		get_doc.assert_not_called()
		commit.assert_not_called()

		# Model the worker after that rollback. It inserts only whitelisted fields in
		# its own job transaction and deliberately does not commit business data here.
		evidence = dict(enqueue.call_args.kwargs["evidence"])
		evidence.update({"doctype": "User", "owner": "attacker"})
		stored_log = Mock()
		stored_log.name = "LOG-1"
		pending_log = Mock()
		pending_log.insert.return_value = stored_log
		get_doc.return_value = pending_log

		self.assertEqual(persist_api_log(evidence), "LOG-1")
		document = get_doc.call_args.args[0]
		self.assertEqual(set(document), {"doctype", *API_LOG_FIELDS})
		self.assertEqual(document["doctype"], "Fiskaly API Log")
		self.assertNotIn("owner", document)
		pending_log.insert.assert_called_once_with(ignore_permissions=True, ignore_links=True)
		commit.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.logger")
	@patch("erpnext_fiskaly_sign_at.providers.http.frappe.enqueue")
	@patch("erpnext_fiskaly_sign_at.providers.http.requests.request")
	def test_queue_failure_does_not_leak_payload_or_break_http_result(self, request, enqueue, logger):
		secret = "api-secret-value"
		request.return_value = json_response({"safe": "result", "echo": secret})
		enqueue.side_effect = RuntimeError("queue unavailable")
		client = make_client()

		result = client.request(
			"POST",
			"/auth",
			json_data={"api_secret": secret},
		)

		self.assertEqual(result["safe"], "result")
		logger.assert_called_once_with("fiskaly")
		logger.return_value.error.assert_called_once_with("Could not enqueue the redacted fiskaly API log")
		fallback_log = repr(logger.return_value.error.call_args)
		self.assertNotIn(secret, fallback_log)
		queued_payload = json.dumps(enqueue.call_args.kwargs, ensure_ascii=False, default=str)
		self.assertNotIn(secret, queued_payload)
