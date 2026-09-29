import frappe

from erpnext_fiskaly_sign_at.access_control import (
	POS_CASHIER_ROLE,
	POS_MANAGER_ROLE,
	RKSV_ADMIN_ROLE,
)


def has_app_permission():
	return bool(
		{
			"System Manager",
			POS_CASHIER_ROLE,
			POS_MANAGER_ROLE,
			RKSV_ADMIN_ROLE,
		}
		& set(frappe.get_roles())
	)
