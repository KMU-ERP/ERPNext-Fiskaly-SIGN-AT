import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.translate import get_translations_from_csv

from erpnext_fiskaly_sign_at.access_control import (
	APP_NAME,
	APP_MODULE,
	POS_CASHIER_DESKTOP_ACTIONS,
	POS_CASHIER_ROLE,
	POS_MANAGER_DESKTOP_ACTIONS,
	POS_MANAGER_ROLE,
	RKSV_ADMIN_DESKTOP_ACTIONS,
	RKSV_ADMIN_ROLE,
	ROLE_DESKTOP_ACTIONS,
	ROLE_HOME_PAGES,
	ROLE_WORKSPACES,
	_get_restricted_pos_profile,
	apply_pos_module_profile,
	configure_pos_boot,
	get_pos_user_permission_query,
	has_pos_user_permission,
)
from erpnext_fiskaly_sign_at.erpnext_fiskaly_sign_at.report.fiskaly_receipt_status import (
	fiskaly_receipt_status,
)


def _app_json(*parts: str) -> dict:
	path = Path(frappe.get_app_path("erpnext_fiskaly_sign_at", *parts))
	return json.loads(path.read_text(encoding="utf-8"))


class TestAccessControls(IntegrationTestCase):
	def test_system_administrator_sees_only_aggregate_fiskaly_icon(self):
		direct_actions = tuple(
			dict.fromkeys(action for actions in ROLE_DESKTOP_ACTIONS.values() for action in actions)
		)
		bootinfo = frappe._dict(
			desktop_icons=[
				*(frappe._dict(label=label) for label in direct_actions),
				frappe._dict(label="Fiskaly RKSV"),
				frappe._dict(label="Accounting"),
				frappe._dict(label="Buying"),
			],
			workspace_sidebar_item={
				**{label.lower(): frappe._dict(label=label) for label in direct_actions},
				"fiskaly rksv": frappe._dict(label="Fiskaly RKSV"),
				"accounting": frappe._dict(label="Accounting"),
			},
		)

		with (
			patch(
				"erpnext_fiskaly_sign_at.access_control._get_restricted_pos_profile",
				return_value=None,
			),
			patch(
				"erpnext_fiskaly_sign_at.access_control._is_system_administrator",
				return_value=True,
			),
		):
			configure_pos_boot(bootinfo)

		self.assertEqual(
			[icon.label for icon in bootinfo.desktop_icons],
			["Fiskaly RKSV", "Accounting", "Buying"],
		)
		self.assertEqual(
			set(bootinfo.workspace_sidebar_item),
			{"fiskaly rksv", "accounting"},
		)

	def test_each_operational_role_boot_contains_only_its_navigation(self):
		all_actions = tuple(
			dict.fromkeys(action for actions in ROLE_DESKTOP_ACTIONS.values() for action in actions)
		)
		for profile_name, expected_actions in ROLE_DESKTOP_ACTIONS.items():
			workspace_name = ROLE_WORKSPACES[profile_name]
			bootinfo = frappe._dict(
				desktop_icons=[
					frappe._dict(label=label)
					for label in (*all_actions, "Accounting", "Buying", "Company")
				],
				workspace_sidebar_item={
					label.lower(): frappe._dict(label=label)
					for label in (
						*all_actions,
						*ROLE_WORKSPACES.values(),
						"Accounting",
						"Buying",
						"Company",
					)
				},
				workspaces=frappe._dict(
					pages=[
						*(frappe._dict(name=name) for name in ROLE_WORKSPACES.values()),
						frappe._dict(name="Accounting"),
					]
				),
				module_wise_workspaces={
					APP_MODULE: list(ROLE_WORKSPACES.values()),
					"Accounts": ["Accounting"],
				},
				app_data=[
					frappe._dict(app_name="erpnext", workspaces=["Accounting"]),
					frappe._dict(app_name=APP_NAME, workspaces=list(ROLE_WORKSPACES.values())),
				],
			)

			with (
				self.subTest(profile_name=profile_name),
				patch(
					"erpnext_fiskaly_sign_at.access_control._get_restricted_pos_profile",
					return_value=profile_name,
				),
			):
				configure_pos_boot(bootinfo)
				self.assertEqual(
					[icon.label for icon in bootinfo.desktop_icons],
					[action for action in all_actions if action in expected_actions],
				)
				self.assertEqual(
					set(bootinfo.workspace_sidebar_item),
					{
						*(label.lower() for label in expected_actions),
						workspace_name.lower(),
					},
				)
				self.assertEqual(
					[workspace.name for workspace in bootinfo.workspaces.pages],
					[workspace_name],
				)
				self.assertEqual(
					bootinfo.module_wise_workspaces,
					{APP_MODULE: [workspace_name]},
				)
				self.assertEqual([app.app_name for app in bootinfo.app_data], [APP_NAME])
				self.assertEqual(bootinfo.app_data[0].app_route, ROLE_HOME_PAGES[profile_name])

	@patch("erpnext_fiskaly_sign_at.access_control.frappe.get_roles")
	def test_navigation_profile_uses_highest_app_role_but_never_restricts_system_manager(self, get_roles):
		for roles, expected_profile in (
			([POS_CASHIER_ROLE, "Accounts User", "Sales User"], POS_CASHIER_ROLE),
			([POS_MANAGER_ROLE, "Accounts User", "Sales User", "Sales Manager"], POS_MANAGER_ROLE),
			([RKSV_ADMIN_ROLE, "Accounts User", "Accounts Manager"], RKSV_ADMIN_ROLE),
			([POS_CASHIER_ROLE, "Accounts Manager"], None),
			([RKSV_ADMIN_ROLE, "System Manager"], None),
		):
			with self.subTest(roles=roles):
				get_roles.return_value = roles
				self.assertEqual(_get_restricted_pos_profile("user@example.com"), expected_profile)

	def test_pos_role_profile_assigns_matching_module_profile(self):
		cashier = frappe._dict(
			name="cashier@example.com",
			role_profiles=[frappe._dict(role_profile=POS_CASHIER_ROLE)],
			module_profile=None,
		)
		apply_pos_module_profile(cashier)
		self.assertEqual(cashier.module_profile, POS_CASHIER_ROLE)

		manager = frappe._dict(
			name="manager@example.com",
			role_profiles=[
				frappe._dict(role_profile=POS_CASHIER_ROLE),
				frappe._dict(role_profile=POS_MANAGER_ROLE),
			],
			module_profile=POS_CASHIER_ROLE,
		)
		apply_pos_module_profile(manager)
		self.assertEqual(manager.module_profile, POS_MANAGER_ROLE)

	def test_role_desktop_icons_have_app_owned_svg_assets(self):
		all_actions = set().union(*map(set, ROLE_DESKTOP_ACTIONS.values()))
		for variant in ("subtle", "solid"):
			for label in all_actions:
				path = Path(
					frappe.get_app_path(
						APP_NAME,
						"public",
						"icons",
						"desktop_icons",
						variant,
						f"{frappe.scrub(label)}.svg",
					)
				)
				self.assertTrue(path.is_file(), path)

	def test_german_translations_cover_pos_navigation(self):
		translations = get_translations_from_csv("de", "erpnext_fiskaly_sign_at")
		self.assertEqual(
			{
				key: translations.get(key)
				for key in (
					"POS Cashier",
					"POS Manager",
					"POS Profiles",
					"Point of Sale",
					"Open POS",
					"Close POS",
					"My POS Invoices",
					"POS Cashier Help",
					"POS Manager Help",
					"Fiskaly Administrator Help",
					"Fiskaly RKSV Help",
				)
			},
			{
				"POS Cashier": "POS-Kassier",
				"POS Manager": "POS-Kassenleitung",
				"POS Profiles": "POS-Profile",
				"Point of Sale": "Verkaufsstelle",
				"Open POS": "Kasse öffnen",
				"Close POS": "Kasse abschließen",
				"My POS Invoices": "Meine POS-Rechnungen",
				"POS Cashier Help": "POS-Kassier-Hilfe",
				"POS Manager Help": "POS-Kassenleitung-Hilfe",
				"Fiskaly Administrator Help": "Fiskaly-Administrator-Hilfe",
				"Fiskaly RKSV Help": "Fiskaly-RKSV-Hilfe",
			},
		)

	@patch("frappe.get_list", return_value=["Allowed GmbH"])
	def test_allowed_companies_use_permission_checked_company_query(self, get_list):
		self.assertEqual(
			fiskaly_receipt_status._get_allowed_companies("restricted@example.com"),
			("Allowed GmbH",),
		)
		get_list.assert_called_once_with(
			"Company",
			pluck="name",
			user="restricted@example.com",
			limit_page_length=0,
			order_by="name",
			reference_doctype="Fiskaly Receipt",
		)

	def test_provider_resource_requires_backend_insert_and_is_immutable(self):
		doc = frappe.new_doc("Fiskaly Provider Resource")
		doc.update(
			{
				"connection": "CONNECTION-1",
				"register": "REGISTER-1",
				"resource_type": "SCU",
				"provider_resource_id": "RESOURCE-1",
				"state": "INITIALIZED",
				"mode": "TEST",
				"provider_data": "{}",
			}
		)

		with self.assertRaises(frappe.PermissionError):
			doc.before_insert()

		doc.flags.ignore_permissions = True
		doc.before_insert()
		doc.set("__islocal", False)
		doc._doc_before_save = frappe._dict(doc.as_dict())
		doc.state = "DECOMMISSIONED"
		with self.assertRaises(frappe.PermissionError):
			doc.validate()

	def test_provider_resource_cannot_be_deleted_or_renamed(self):
		doc = frappe.new_doc("Fiskaly Provider Resource")
		old_in_uninstall = getattr(frappe.flags, "in_uninstall", False)
		frappe.flags.in_uninstall = False
		try:
			with self.assertRaises(frappe.PermissionError):
				doc.on_trash()
			with self.assertRaises(frappe.PermissionError):
				doc.before_rename("old", "new")
		finally:
			frappe.flags.in_uninstall = old_in_uninstall

	@patch.object(fiskaly_receipt_status, "_get_allowed_companies", return_value=("Allowed GmbH",))
	@patch("frappe.db.sql", return_value=[])
	def test_receipt_status_sql_is_limited_to_allowed_companies(self, sql, _allowed_companies):
		fiskaly_receipt_status.execute({"from_date": "2026-08-01", "to_date": "2026-08-31"})

		query, params = sql.call_args.args[:2]
		self.assertIn("register.company in %(allowed_companies)s", query)
		self.assertEqual(params["allowed_companies"], ("Allowed GmbH",))

	@patch.object(fiskaly_receipt_status, "_get_allowed_companies", return_value=("Allowed GmbH",))
	@patch("frappe.db.sql")
	def test_receipt_status_rejects_disallowed_company_filter(self, sql, _allowed_companies):
		with self.assertRaises(frappe.PermissionError):
			fiskaly_receipt_status.execute(
				{
					"from_date": "2026-08-01",
					"to_date": "2026-08-31",
					"company": "Blocked GmbH",
				}
			)
		sql.assert_not_called()

	def test_standard_desk_and_report_roles_match_document_access(self):
		desktop_icon = _app_json("desktop_icon", "fiskaly_rksv.json")
		workspace = _app_json("erpnext_fiskaly_sign_at", "workspace", "fiskaly_rksv", "fiskaly_rksv.json")
		report = _app_json(
			"erpnext_fiskaly_sign_at",
			"report",
			"fiskaly_receipt_status",
			"fiskaly_receipt_status.json",
		)
		self.assertEqual({row["role"] for row in desktop_icon["roles"]}, {"System Manager"})
		self.assertEqual(
			{row["role"] for row in workspace["roles"]},
			{"System Manager", RKSV_ADMIN_ROLE},
		)
		self.assertEqual(desktop_icon["icon"], "shield-check")
		self.assertEqual(workspace["icon"], "shield-check")
		self.assertEqual(
			{row["role"] for row in report["roles"]},
			{"System Manager", "Accounts Manager", POS_MANAGER_ROLE},
		)

	def test_pos_roles_profiles_navigation_and_cashier_permissions_are_installed(self):
		self.assertTrue(frappe.get_meta("Module Profile").translated_doctype)

		role_profiles = {
			POS_CASHIER_ROLE: {POS_CASHIER_ROLE, "Accounts User", "Sales User"},
			POS_MANAGER_ROLE: {POS_MANAGER_ROLE, "Accounts User", "Sales User", "Sales Manager"},
			RKSV_ADMIN_ROLE: {RKSV_ADMIN_ROLE, "Accounts User", "Accounts Manager"},
		}
		for profile_name, expected_roles in role_profiles.items():
			self.assertTrue(frappe.db.exists("Role", profile_name))
			self.assertEqual(
				set(
					frappe.get_all(
						"Has Role",
						filters={"parent": profile_name, "parenttype": "Role Profile"},
						pluck="role",
					)
				),
				expected_roles,
			)
			blocked_modules = set(
				frappe.get_all("Block Module", filters={"parent": profile_name}, pluck="module")
			)
			self.assertNotIn(APP_MODULE, blocked_modules)
			self.assertIn("Accounts", blocked_modules)

		for doctype in ("POS Opening Entry", "POS Closing Entry"):
			permission = frappe.db.get_value(
				"Custom DocPerm",
				{"parent": doctype, "role": POS_CASHIER_ROLE},
				["read", "write", "create", "submit", "delete", "cancel"],
				as_dict=True,
			)
			self.assertEqual(
				permission,
				frappe._dict(read=1, write=1, create=1, submit=1, delete=0, cancel=0),
			)

		for workspace, role, icon in (
			(POS_CASHIER_ROLE, POS_CASHIER_ROLE, "shopping-cart"),
			(POS_MANAGER_ROLE, POS_MANAGER_ROLE, "monitor-check"),
		):
			self.assertEqual(frappe.db.get_value("Workspace", workspace, "icon"), icon)
			self.assertEqual(frappe.db.get_value("Workspace Sidebar", workspace, "header_icon"), icon)
			self.assertEqual(
				set(
					frappe.get_all(
						"Has Role",
						filters={"parent": workspace, "parenttype": "Workspace"},
						pluck="role",
					)
				),
				{role},
			)

		self.assertEqual(frappe.db.get_value("Role", POS_CASHIER_ROLE, "home_page"), "/desk")
		self.assertFalse(frappe.db.exists("Desktop Icon", POS_CASHIER_ROLE))
		self.assertFalse(frappe.db.exists("Desktop Icon", POS_MANAGER_ROLE))
		self.assertFalse(frappe.db.exists("Desktop Icon", APP_MODULE))
		self.assertFalse(frappe.db.exists("Desktop Icon", "RKSV Help"))
		self.assertEqual(
			frappe.db.get_value("Desktop Icon", "Fiskaly RKSV", "logo_url"),
			"/assets/erpnext_fiskaly_sign_at/images/kmu_erp_logo.svg",
		)

		actions = {
			"Point of Sale": ("shopping-cart", "Page", "point-of-sale", {POS_CASHIER_ROLE, POS_MANAGER_ROLE}),
			"Open POS": ("log-in", "DocType", "POS Opening Entry", {POS_CASHIER_ROLE}),
			"Close POS": ("log-out", "DocType", "POS Closing Entry", {POS_CASHIER_ROLE}),
			"My POS Invoices": ("receipt-text", "DocType", "POS Invoice", {POS_CASHIER_ROLE}),
			"POS Cashier Help": ("circle-help", "Page", "pos-cashier-help", {POS_CASHIER_ROLE}),
			"POS Openings": ("log-in", "DocType", "POS Opening Entry", {POS_MANAGER_ROLE}),
			"POS Closings": ("log-out", "DocType", "POS Closing Entry", {POS_MANAGER_ROLE}),
			"POS Invoices": ("receipt-text", "DocType", "POS Invoice", {POS_MANAGER_ROLE}),
			"Receipt Status": (
				"chart-no-axes-column",
				"Report",
				"Fiskaly Receipt Status",
				{POS_MANAGER_ROLE, RKSV_ADMIN_ROLE},
			),
			"Fiscal Receipts": (
				"shield-check",
				"DocType",
				"Fiskaly Receipt",
				{POS_MANAGER_ROLE, RKSV_ADMIN_ROLE},
			),
			"POS Profiles": ("settings-2", "DocType", "POS Profile", {POS_MANAGER_ROLE}),
			"Modes of Payment": ("credit-card", "DocType", "Mode of Payment", {POS_MANAGER_ROLE}),
			"Customers": ("users", "DocType", "Customer", {POS_MANAGER_ROLE}),
			"POS Manager Help": ("circle-help", "Page", "pos-manager-help", {POS_MANAGER_ROLE}),
			"RKSV Settings": ("settings", "DocType", "Fiskaly Settings", {RKSV_ADMIN_ROLE}),
			"API Connections": ("plug", "DocType", "Fiskaly API Connection", {RKSV_ADMIN_ROLE}),
			"Cash Registers": ("monitor-smartphone", "DocType", "Fiskaly Register", {RKSV_ADMIN_ROLE}),
			"Provider Resources": ("database", "DocType", "Fiskaly Provider Resource", {RKSV_ADMIN_ROLE}),
			"Outage Lifecycle": ("history", "DocType", "Fiskaly Lifecycle Event", {RKSV_ADMIN_ROLE}),
			"DEP7 Exports": ("download", "DocType", "Fiskaly DEP7 Export", {RKSV_ADMIN_ROLE}),
			"API Logs": ("logs", "DocType", "Fiskaly API Log", {RKSV_ADMIN_ROLE}),
			"Fiskaly Administrator Help": (
				"circle-help",
				"Page",
				"fiskaly-administrator-help",
				{RKSV_ADMIN_ROLE},
			),
		}
		self.assertEqual(set(actions), set().union(*map(set, ROLE_DESKTOP_ACTIONS.values())))
		for label, (icon, link_type, link_to, expected_roles) in actions.items():
			self.assertEqual(frappe.db.get_value("Desktop Icon", label, "icon"), icon)
			self.assertEqual(frappe.db.get_value("Workspace Sidebar", label, "header_icon"), icon)
			self.assertEqual(
				set(
					frappe.get_all(
						"Has Role",
						filters={"parent": label, "parenttype": "Desktop Icon"},
						pluck="role",
					)
				),
				expected_roles,
			)
			first_item = frappe.get_doc("Workspace Sidebar", label).items[0]
			self.assertEqual((first_item.link_type, first_item.link_to), (link_type, link_to))

		for page_name, expected_roles in {
			"pos-cashier-help": {POS_CASHIER_ROLE},
			"pos-manager-help": {POS_MANAGER_ROLE},
			"fiskaly-administrator-help": {RKSV_ADMIN_ROLE},
			"fiskaly-rksv-help": {RKSV_ADMIN_ROLE, "System Manager"},
		}.items():
			self.assertTrue(frappe.db.exists("Page", page_name))
			self.assertEqual(
				set(
					frappe.get_all(
						"Has Role",
						filters={"parent": page_name, "parenttype": "Page"},
						pluck="role",
					)
				),
				expected_roles,
			)
		self.assertFalse(frappe.db.exists("Page", "fiskaly-rksv-anleitung"))
		fiskaly_workspace = frappe.get_doc("Workspace", "Fiskaly RKSV")
		self.assertIn(
			("Fiskaly RKSV Help", "fiskaly-rksv-help"),
			{(link.label, link.link_to) for link in fiskaly_workspace.links if link.link_to},
		)
		self.assertIn(
			("Fiskaly RKSV Help", "fiskaly-rksv-help"),
			{(shortcut.label, shortcut.link_to) for shortcut in fiskaly_workspace.shortcuts},
		)
		self.assertIn(
			("Fiskaly RKSV Help", "fiskaly-rksv-help"),
			{
				(item.label, item.link_to)
				for item in frappe.get_doc("Workspace Sidebar", "Fiskaly RKSV").items
			},
		)
		manager_receipt_permission = frappe.get_all(
			"DocPerm",
			filters={"parent": "Fiskaly Receipt", "role": POS_MANAGER_ROLE},
			fields=["read", "report", "write", "export"],
		)
		self.assertEqual(
			manager_receipt_permission,
			[{"read": 1, "report": 1, "write": 0, "export": 0}],
		)

	@patch("erpnext_fiskaly_sign_at.access_control.frappe.db.escape", return_value="'cashier@example.com'")
	@patch(
		"erpnext_fiskaly_sign_at.access_control.frappe.get_roles",
		return_value=[POS_CASHIER_ROLE],
	)
	def test_cashier_queries_and_document_access_are_limited_to_own_pos_records(self, _roles, _escape):
		self.assertEqual(
			get_pos_user_permission_query("cashier@example.com", "POS Invoice"),
			"`tabPOS Invoice`.`owner` = 'cashier@example.com'",
		)
		self.assertEqual(
			get_pos_user_permission_query("cashier@example.com", "POS Opening Entry"),
			"`tabPOS Opening Entry`.`user` = 'cashier@example.com'",
		)
		own_opening = frappe._dict(doctype="POS Opening Entry", user="cashier@example.com")
		other_opening = frappe._dict(doctype="POS Opening Entry", user="other@example.com")
		self.assertTrue(has_pos_user_permission(own_opening, user="cashier@example.com"))
		self.assertFalse(has_pos_user_permission(other_opening, user="cashier@example.com"))

	def test_provider_resource_metadata_is_read_only(self):
		metadata = _app_json(
			"erpnext_fiskaly_sign_at",
			"doctype",
			"fiskaly_provider_resource",
			"fiskaly_provider_resource.json",
		)
		for field in metadata["fields"]:
			self.assertEqual(field.get("read_only"), 1, field["fieldname"])
		for permission in metadata["permissions"]:
			self.assertFalse(permission.get("create"), permission["role"])
			self.assertFalse(permission.get("write"), permission["role"])
			self.assertFalse(permission.get("delete"), permission["role"])
