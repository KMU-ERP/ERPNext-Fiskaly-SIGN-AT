import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from erpnext_fiskaly_sign_at.api.exports import (
	_check_export_creation_permission,
	create_final_decommission_export,
	create_quarterly_backup,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_receipt.fiskaly_receipt import (
	FiskalyReceipt,
	_attach_private_evidence_file,
	_check_evidence_write_permission,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype.fiskaly_register.fiskaly_register import (
	FiskalyRegister,
)
from erpnext_fiskaly_sign_at.services.compliance import (
	RKSV_PRINT_FORMAT,
	require_canonical_pos_print_setup,
)


class TestWhitelistedEndpointSecurity(TestCase):
	def test_every_app_whitelist_declares_a_safe_http_method(self):
		app_root = Path(frappe.get_app_path("erpnext_fiskaly_sign_at"))
		endpoints = []
		for path in app_root.rglob("*.py"):
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef):
					continue
				for decorator in node.decorator_list:
					if not isinstance(decorator, ast.Call):
						continue
					function = decorator.func
					if not (
						isinstance(function, ast.Attribute)
						and isinstance(function.value, ast.Name)
						and function.value.id == "frappe"
						and function.attr == "whitelist"
					):
						continue
					keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
					self.assertIn("methods", keywords, f"{path}:{node.lineno} must declare HTTP methods")
					methods = ast.literal_eval(keywords["methods"])
					allow_guest = ast.literal_eval(keywords.get("allow_guest", ast.Constant(False)))
					self.assertFalse(allow_guest, f"{path}:{node.lineno} must require authentication")
					endpoints.append((path.relative_to(app_root).as_posix(), node.name, methods))

		self.assertGreater(len(endpoints), 20)
		read_only_endpoints = {
			("api/pos.py", "get_fiscalization_status"),
			(
				"erpnext_fiskaly_sign_at/doctype/fiskaly_register/fiskaly_register.py",
				"get_available_scus",
			),
			(
				"erpnext_fiskaly_sign_at/doctype/fiskaly_register/fiskaly_register.py",
				"get_decommission_workflow",
			),
			(
				"erpnext_fiskaly_sign_at/doctype/fiskaly_register/fiskaly_register.py",
				"get_terminal_status_summary",
			),
		}
		for relative_path, name, methods in endpoints:
			expected = ["GET"] if (relative_path, name) in read_only_endpoints else ["POST"]
			self.assertEqual(methods, expected, f"unsafe HTTP method on {relative_path}:{name}")

	@patch("erpnext_fiskaly_sign_at.api.exports.frappe.get_doc")
	@patch("erpnext_fiskaly_sign_at.api.exports.frappe.has_permission")
	def test_dep7_creation_permission_checks_export_and_register(self, has_permission, get_doc):
		register = SimpleNamespace(check_permission=Mock())
		get_doc.return_value = register

		self.assertIs(_check_export_creation_permission("REGISTER-1"), register)

		has_permission.assert_called_once_with("Fiskaly DEP7 Export", "create", throw=True)
		get_doc.assert_called_once_with("Fiskaly Register", "REGISTER-1")
		register.check_permission.assert_called_once_with("read")

	@patch("erpnext_fiskaly_sign_at.api.exports.frappe.db.get_value")
	@patch(
		"erpnext_fiskaly_sign_at.api.exports._check_export_creation_permission",
		side_effect=frappe.PermissionError,
	)
	def test_quarterly_export_checks_permission_before_existing_name(self, check_permission, get_value):
		with self.assertRaises(frappe.PermissionError):
			create_quarterly_backup("REGISTER-1", 2026, 2)

		check_permission.assert_called_once_with("REGISTER-1")
		get_value.assert_not_called()

	@patch(
		"erpnext_fiskaly_sign_at.api.exports._check_export_creation_permission",
		side_effect=frappe.PermissionError,
	)
	def test_final_export_requires_register_write_permission(self, check_permission):
		with self.assertRaises(frappe.PermissionError):
			create_final_decommission_export("REGISTER-1")

		check_permission.assert_called_once_with("REGISTER-1", register_permission="write")

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.get_doc"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.only_for"
	)
	def test_receipt_evidence_write_requires_role_and_owning_register_write(self, only_for, get_doc):
		receipt = SimpleNamespace(register="REGISTER-1", check_permission=Mock())
		register = SimpleNamespace(check_permission=Mock())
		get_doc.return_value = register

		_check_evidence_write_permission(receipt)

		receipt.check_permission.assert_called_once_with("read")
		only_for.assert_called_once_with(("System Manager", "Accounts Manager"))
		get_doc.assert_called_once_with("Fiskaly Register", "REGISTER-1")
		register.check_permission.assert_called_once_with("write")

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.db.set_value"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.get_doc"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.db.sql"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.db.get_value",
		return_value="FILE-1",
	)
	def test_unattached_owned_private_evidence_file_is_bound_atomically(
		self, _get_value, sql, get_doc, set_value
	):
		file_doc = SimpleNamespace(
			name="FILE-1",
			is_private=1,
			file_url="/private/files/closing.pdf",
			attached_to_doctype=None,
			attached_to_name=None,
			check_permission=Mock(),
		)
		get_doc.return_value = file_doc
		receipt = SimpleNamespace(name="RECEIPT-1")

		self.assertEqual(
			_attach_private_evidence_file(receipt, "/private/files/closing.pdf"),
			"FILE-1",
		)

		self.assertIn("for update", sql.call_args.args[0].lower())
		file_doc.check_permission.assert_called_once_with("write")
		set_value.assert_called_once_with(
			"File",
			"FILE-1",
			{
				"attached_to_doctype": "Fiskaly Receipt",
				"attached_to_name": "RECEIPT-1",
			},
			update_modified=False,
		)

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.db.set_value"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.get_doc"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.db.sql"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt.frappe.db.get_value",
		return_value="FILE-OTHER",
	)
	def test_evidence_file_attached_elsewhere_is_rejected(self, _get_value, _sql, get_doc, set_value):
		get_doc.return_value = SimpleNamespace(
			name="FILE-OTHER",
			is_private=1,
			file_url="/private/files/closing.pdf",
			attached_to_doctype="Fiskaly Receipt",
			attached_to_name="OTHER-RECEIPT",
			check_permission=Mock(),
		)

		with self.assertRaises(frappe.ValidationError):
			_attach_private_evidence_file(SimpleNamespace(name="RECEIPT-1"), "/private/files/closing.pdf")
		set_value.assert_not_called()

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt._attach_private_evidence_file"
	)
	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt._check_evidence_write_permission",
		side_effect=frappe.PermissionError,
	)
	def test_print_evidence_permission_fails_before_file_or_audit_write(self, _permission, attach_file):
		receipt = SimpleNamespace(check_permission=Mock(), db_set=Mock())
		with self.assertRaises(frappe.PermissionError):
			FiskalyReceipt.mark_print_evidence(receipt, "/private/files/closing.pdf")

		receipt.check_permission.assert_called_once_with("print")
		attach_file.assert_not_called()
		receipt.db_set.assert_not_called()

	@patch(
		"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
		"fiskaly_receipt.fiskaly_receipt._check_evidence_write_permission",
		side_effect=frappe.PermissionError,
	)
	def test_annual_verification_permission_fails_before_audit_write(self, _permission):
		receipt = SimpleNamespace(check_permission=Mock(), db_set=Mock())
		with self.assertRaises(frappe.PermissionError):
			FiskalyReceipt.record_annual_verification(receipt, "SUCCESS", "verified")
		receipt.db_set.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.services.compliance.frappe.db.get_value", return_value="Standard")
	@patch("erpnext_fiskaly_sign_at.services.compliance._require_canonical_print_format")
	def test_provisioning_print_preflight_rejects_wrong_pos_format(self, require_format, get_value):
		with self.assertRaises(frappe.ValidationError):
			require_canonical_pos_print_setup("POS-PROFILE-1")

		require_format.assert_called_once_with()
		get_value.assert_called_once_with("POS Profile", "POS-PROFILE-1", "print_format")

	@patch("erpnext_fiskaly_sign_at.services.compliance.frappe.db.get_value", return_value=RKSV_PRINT_FORMAT)
	@patch("erpnext_fiskaly_sign_at.services.compliance._require_canonical_print_format")
	def test_provisioning_print_preflight_accepts_canonical_setup(self, require_format, _get_value):
		require_canonical_pos_print_setup("POS-PROFILE-1")
		require_format.assert_called_once_with()

	@patch(
		"erpnext_fiskaly_sign_at.services.compliance.require_canonical_pos_print_setup",
		side_effect=frappe.ValidationError,
	)
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_provision_locks_and_reloads_before_print_preflight(self, lock_row, preflight):
		register = SimpleNamespace(
			name="REGISTER-1",
			pos_profile="POS-PROFILE-1",
			initialized=0,
			provider_state="NEW",
			check_permission=Mock(),
			reload=Mock(),
			_validate_austrian_runtime=Mock(),
			_provider=Mock(),
		)

		with (
			patch(
				"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
				"fiskaly_register.fiskaly_register.frappe.only_for"
			),
			self.assertRaises(frappe.ValidationError),
		):
			FiskalyRegister.provision_register(register)

		self.assertEqual(
			[entry.args for entry in register.check_permission.call_args_list],
			[("write",), ("write",)],
		)
		lock_row.assert_called_once_with("REGISTER-1")
		register.reload.assert_called_once_with()
		preflight.assert_called_once_with("POS-PROFILE-1")
		register._provider.assert_not_called()

	@patch("erpnext_fiskaly_sign_at.services.compliance.require_canonical_pos_print_setup")
	@patch("erpnext_fiskaly_sign_at.services.fiscalization._lock_register_row")
	def test_provision_replay_observes_initialized_state_after_lock(self, lock_row, preflight):
		register = SimpleNamespace(
			name="REGISTER-1",
			pos_profile="POS-PROFILE-1",
			initialized=0,
			provider_state="NEW",
			check_permission=Mock(),
			_provider=Mock(),
		)
		register.reload = Mock(side_effect=lambda: setattr(register, "initialized", 1))

		with (
			patch(
				"erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.doctype."
				"fiskaly_register.fiskaly_register.frappe.only_for"
			),
			self.assertRaises(frappe.ValidationError),
		):
			FiskalyRegister.provision_register(register)

		lock_row.assert_called_once_with("REGISTER-1")
		register.reload.assert_called_once_with()
		self.assertEqual(register.check_permission.call_count, 2)
		preflight.assert_not_called()
		register._provider.assert_not_called()
