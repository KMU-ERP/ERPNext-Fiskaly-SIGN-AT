import frappe
from frappe import _
from frappe.model.document import Document


class FiskalyProviderResource(Document):
	IMMUTABLE_FIELDS = (
		"connection",
		"register",
		"resource_type",
		"provider_resource_id",
		"state",
		"mode",
		"provider_data",
	)

	def before_insert(self):
		# Provider synchronization is the sole writer and deliberately inserts with
		# ignore_permissions=True.  Human/API document creation must fail even for
		# Administrator, who otherwise bypasses DocType permissions.
		if not self.flags.ignore_permissions:
			frappe.throw(
				_("Fiskaly provider resources can only be created by the provider synchronization."),
				frappe.PermissionError,
			)

	def validate(self):
		if self.is_new():
			return
		old = self.get_doc_before_save()
		if not old:
			frappe.throw(
				_("Fiskaly provider resources cannot be updated outside provider synchronization."),
				frappe.PermissionError,
			)
		changed = [
			fieldname for fieldname in self.IMMUTABLE_FIELDS if old.get(fieldname) != self.get(fieldname)
		]
		if changed:
			frappe.throw(
				_("Provider-managed Fiskaly resource fields cannot be changed: {0}").format(
					", ".join(changed)
				),
				frappe.PermissionError,
			)

	def before_rename(self, old_name, new_name, merge=False):
		frappe.throw(_("Fiskaly provider resource identifiers are immutable."), frappe.PermissionError)

	def on_trash(self):
		if not getattr(frappe.flags, "in_uninstall", False):
			frappe.throw(
				_("Fiskaly provider resources are retained as provider evidence and cannot be deleted."),
				frappe.PermissionError,
			)
