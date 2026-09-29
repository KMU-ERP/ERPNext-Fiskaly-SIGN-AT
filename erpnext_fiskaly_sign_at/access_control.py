from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.permissions import setup_custom_perms

POS_CASHIER_ROLE = "POS Cashier"
POS_MANAGER_ROLE = "POS Manager"
RKSV_ADMIN_ROLE = "Fiskaly RKSV Administrator"

APP_NAME = "erpnext_fiskaly_sign_at"
APP_MODULE = "Erpnext Fiskaly Sign At"
FISKALY_ADMIN_DESKTOP_ICON = "Fiskaly RKSV"
GENERATED_AGGREGATE_DESKTOP_ICONS = (
	POS_CASHIER_ROLE,
	POS_MANAGER_ROLE,
	APP_MODULE,
)

ROLE_PROFILES = {
	POS_CASHIER_ROLE: (POS_CASHIER_ROLE, "Accounts User", "Sales User"),
	POS_MANAGER_ROLE: (
		POS_MANAGER_ROLE,
		"Accounts User",
		"Sales User",
		"Sales Manager",
	),
	RKSV_ADMIN_ROLE: (RKSV_ADMIN_ROLE, "Accounts User", "Accounts Manager"),
}

ROLE_HOME_PAGES = {
	POS_CASHIER_ROLE: "/desk",
	POS_MANAGER_ROLE: "/desk/pos-manager",
	RKSV_ADMIN_ROLE: "/desk/fiskaly-rksv",
}

POS_CASHIER_DESKTOP_ACTIONS = (
	"Point of Sale",
	"Open POS",
	"Close POS",
	"My POS Invoices",
	"POS Cashier Help",
)

POS_MANAGER_DESKTOP_ACTIONS = (
	"Point of Sale",
	"POS Openings",
	"POS Closings",
	"POS Invoices",
	"Receipt Status",
	"Fiscal Receipts",
	"POS Profiles",
	"Modes of Payment",
	"Customers",
	"POS Manager Help",
)

RKSV_ADMIN_DESKTOP_ACTIONS = (
	"RKSV Settings",
	"API Connections",
	"Cash Registers",
	"Provider Resources",
	"Receipt Status",
	"Fiscal Receipts",
	"Outage Lifecycle",
	"DEP7 Exports",
	"API Logs",
	"Fiskaly Administrator Help",
)

ROLE_DESKTOP_ACTIONS = {
	POS_CASHIER_ROLE: POS_CASHIER_DESKTOP_ACTIONS,
	POS_MANAGER_ROLE: POS_MANAGER_DESKTOP_ACTIONS,
	RKSV_ADMIN_ROLE: RKSV_ADMIN_DESKTOP_ACTIONS,
}

ROLE_WORKSPACES = {
	POS_CASHIER_ROLE: POS_CASHIER_ROLE,
	POS_MANAGER_ROLE: POS_MANAGER_ROLE,
	RKSV_ADMIN_ROLE: "Fiskaly RKSV",
}

POS_PROFILE_PRIORITY = (
	RKSV_ADMIN_ROLE,
	POS_MANAGER_ROLE,
	POS_CASHIER_ROLE,
)

POS_CASHIER_PERMISSIONS = {
	"POS Opening Entry": {"read", "write", "create", "submit", "print"},
	"POS Closing Entry": {"read", "write", "create", "submit", "print"},
}

PERMISSION_FIELDS = {
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
}

ELEVATED_POS_ROLES = {
	POS_MANAGER_ROLE,
	RKSV_ADMIN_ROLE,
	"Sales Manager",
	"Accounts Manager",
	"System Manager",
}


def setup_pos_access_control():
	"""Create the app-managed POS roles, profiles, navigation filters and permissions."""

	_setup_roles()
	_setup_role_profiles()
	_setup_module_profile_translation()
	_setup_module_profiles()
	_setup_cashier_docperms()
	_setup_point_of_sale_page_roles()
	_remove_generated_aggregate_desktop_icons()
	frappe.clear_cache()


def apply_pos_module_profile(doc, method=None):
	"""Keep the Module Profile aligned with an app-managed Role Profile."""

	del method
	if doc.name in {"Administrator", "Guest"}:
		return

	assigned_profiles = {
		row.role_profile for row in doc.get("role_profiles") or [] if row.role_profile
	}
	profile_name = next(
		(profile for profile in POS_PROFILE_PRIORITY if profile in assigned_profiles),
		None,
	)

	if profile_name:
		doc.module_profile = profile_name
	elif doc.module_profile in ROLE_PROFILES:
		doc.module_profile = None


def configure_pos_boot(bootinfo):
	"""Expose only role-specific POS navigation to an app-managed operational user.

	Frappe's Module Profile filters workspaces, but auto-generated module sidebars
	are permission based and can still create Accounting, Buying, or Company desktop
	icons. A strict allowlist closes that navigation gap without removing the
	underlying ERPNext permissions required by Point of Sale.
	"""

	profile_name = _get_restricted_pos_profile(frappe.session.user)
	if not profile_name:
		if _is_system_administrator(frappe.session.user):
			_hide_direct_pos_actions_for_system_administrator(bootinfo)
		return

	workspace_name = ROLE_WORKSPACES[profile_name]
	allowed_icons = set(ROLE_DESKTOP_ACTIONS[profile_name])
	bootinfo.desktop_icons = [
		icon for icon in bootinfo.get("desktop_icons") or [] if icon.get("label") in allowed_icons
	]

	allowed_sidebars = {
		*(label.lower() for label in allowed_icons),
		workspace_name.lower(),
	}
	bootinfo.workspace_sidebar_item = {
		key: sidebar
		for key, sidebar in (bootinfo.get("workspace_sidebar_item") or {}).items()
		if str(key).lower() in allowed_sidebars
	}

	workspaces = bootinfo.get("workspaces")
	if workspaces and workspaces.get("pages") is not None:
		workspaces["pages"] = [
			workspace
			for workspace in workspaces["pages"]
			if workspace.get("name") == workspace_name
		]

	module_wise_workspaces = bootinfo.get("module_wise_workspaces")
	if module_wise_workspaces:
		bootinfo.module_wise_workspaces = {
			module: [workspace for workspace in module_workspaces if workspace == workspace_name]
			for module, module_workspaces in module_wise_workspaces.items()
			if workspace_name in module_workspaces
		}

	bootinfo.app_data = [
		app for app in bootinfo.get("app_data") or [] if app.get("app_name") == APP_NAME
	]
	for app in bootinfo.app_data:
		app["app_route"] = ROLE_HOME_PAGES[profile_name]
		app["modules"] = [APP_MODULE]
		app["workspaces"] = [workspace_name]


def _hide_direct_pos_actions_for_system_administrator(bootinfo):
	"""Keep the app's aggregate icon while preserving every unrelated admin icon."""

	direct_actions = set().union(*map(set, ROLE_DESKTOP_ACTIONS.values()))
	bootinfo.desktop_icons = [
		icon
		for icon in bootinfo.get("desktop_icons") or []
		if icon.get("label") == FISKALY_ADMIN_DESKTOP_ICON
		or icon.get("label") not in direct_actions
	]

	direct_sidebars = {label.lower() for label in direct_actions}
	bootinfo.workspace_sidebar_item = {
		key: sidebar
		for key, sidebar in (bootinfo.get("workspace_sidebar_item") or {}).items()
		if str(key).lower() not in direct_sidebars
	}


def _remove_generated_aggregate_desktop_icons():
	"""Remove aggregate icons generated by Frappe for app-managed workspaces.

	Frappe creates these after the regular ``after_install`` hook. Operational
	users use the direct action icons, while System Manager uses Fiskaly RKSV.
	"""

	for icon_name in GENERATED_AGGREGATE_DESKTOP_ICONS:
		if frappe.db.exists("Desktop Icon", icon_name):
			frappe.delete_doc("Desktop Icon", icon_name, force=True, ignore_permissions=True)


def _setup_roles():
	for role_name, home_page in ROLE_HOME_PAGES.items():
		if frappe.db.exists("Role", role_name):
			role = frappe.get_doc("Role", role_name)
		else:
			role = frappe.new_doc("Role")
			role.role_name = role_name

		changed = False
		for fieldname, value in {
			"desk_access": 1,
			"disabled": 0,
			"is_custom": 0,
			"home_page": home_page,
		}.items():
			if role.get(fieldname) != value:
				role.set(fieldname, value)
				changed = True

		if role.is_new():
			role.insert(ignore_permissions=True)
		elif changed:
			role.save(ignore_permissions=True)


def _setup_role_profiles():
	for profile_name, roles in ROLE_PROFILES.items():
		if frappe.db.exists("Role Profile", profile_name):
			profile = frappe.get_doc("Role Profile", profile_name)
		else:
			profile = frappe.new_doc("Role Profile")
			profile.role_profile = profile_name

		if [row.role for row in profile.roles] != list(roles):
			profile.set("roles", [])
			for role in roles:
				profile.append("roles", {"role": role})

			if profile.is_new():
				profile.insert(ignore_permissions=True)
			else:
				profile.save(ignore_permissions=True)
		elif profile.is_new():
			profile.insert(ignore_permissions=True)


def _setup_module_profile_translation():
	"""Translate Module Profile document names in links and list views."""

	if frappe.get_meta("Module Profile").translated_doctype:
		return

	make_property_setter(
		"Module Profile",
		None,
		"translated_doctype",
		1,
		"Check",
		for_doctype=True,
	)


def _setup_module_profiles():
	"""Hide unrelated Desk modules when the matching module profile is assigned."""

	blocked_modules = sorted(
		module
		for module in frappe.get_all("Module Def", pluck="name")
		if module != APP_MODULE
	)

	for profile_name in ROLE_PROFILES:
		if frappe.db.exists("Module Profile", profile_name):
			profile = frappe.get_doc("Module Profile", profile_name)
		else:
			profile = frappe.new_doc("Module Profile")
			profile.module_profile_name = profile_name

		if [row.module for row in profile.block_modules] != blocked_modules:
			profile.set("block_modules", [])
			for module in blocked_modules:
				profile.append("block_modules", {"module": module})

			if profile.is_new():
				profile.insert(ignore_permissions=True)
			else:
				profile.save(ignore_permissions=True)
		elif profile.is_new():
			profile.insert(ignore_permissions=True)


def _setup_cashier_docperms():
	for doctype, enabled_permissions in POS_CASHIER_PERMISSIONS.items():
		# Custom DocPerm replaces the complete standard permission table for a DocType.
		# Frappe's helper copies the standard rows before our additional row is inserted.
		setup_custom_perms(doctype)

		name = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": doctype, "role": POS_CASHIER_ROLE, "permlevel": 0, "if_owner": 0},
			"name",
		)
		if name:
			permission = frappe.get_doc("Custom DocPerm", name)
		else:
			permission = frappe.new_doc("Custom DocPerm")
			permission.update(
				{
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": POS_CASHIER_ROLE,
					"permlevel": 0,
					"if_owner": 0,
				}
			)

		changed = False
		for fieldname in PERMISSION_FIELDS:
			value = int(fieldname in enabled_permissions)
			if permission.get(fieldname) != value:
				permission.set(fieldname, value)
				changed = True

		if permission.is_new():
			permission.insert(ignore_permissions=True)
		elif changed:
			permission.save(ignore_permissions=True)


def _setup_point_of_sale_page_roles():
	page_name = "point-of-sale"
	if not frappe.db.exists("Page", page_name):
		return

	standard_roles = frappe.get_all(
		"Has Role",
		filters={"parent": page_name, "parenttype": "Page"},
		pluck="role",
	)
	roles = list(dict.fromkeys([*standard_roles, POS_CASHIER_ROLE, POS_MANAGER_ROLE]))
	name = frappe.db.get_value("Custom Role", {"page": page_name}, "name")
	if name:
		custom_role = frappe.get_doc("Custom Role", name)
	else:
		custom_role = frappe.new_doc("Custom Role")
		custom_role.page = page_name

	if [row.role for row in custom_role.roles] == roles:
		return

	custom_role.set("roles", [])
	for role in roles:
		custom_role.append("roles", {"role": role})

	if custom_role.is_new():
		custom_role.insert(ignore_permissions=True)
	else:
		custom_role.save(ignore_permissions=True)


def get_pos_user_permission_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if not _is_restricted_cashier(user) or not doctype:
		return ""

	fieldname = "owner" if doctype == "POS Invoice" else "user"
	return f"`tab{doctype}`.`{fieldname}` = {frappe.db.escape(user, percent=False)}"


def has_pos_user_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	del debug
	user = user or frappe.session.user
	if not _is_restricted_cashier(user):
		return True

	fieldname = "owner" if doc.doctype == "POS Invoice" else "user"
	assigned_user = doc.get(fieldname)
	if ptype == "create" and not assigned_user:
		return True

	return assigned_user == user


def _is_restricted_cashier(user: str) -> bool:
	if not user or user == "Administrator":
		return False

	roles = set(frappe.get_roles(user))
	return POS_CASHIER_ROLE in roles and not roles.intersection(ELEVATED_POS_ROLES)


def _is_system_administrator(user: str) -> bool:
	return bool(user) and (user == "Administrator" or "System Manager" in frappe.get_roles(user))


def _get_restricted_pos_profile(user: str) -> str | None:
	if not user or user == "Administrator":
		return None

	roles = set(frappe.get_roles(user))
	if "System Manager" in roles:
		return None
	if RKSV_ADMIN_ROLE in roles:
		return RKSV_ADMIN_ROLE
	if POS_MANAGER_ROLE in roles:
		return POS_MANAGER_ROLE
	if _is_restricted_cashier(user):
		return POS_CASHIER_ROLE
	return None
