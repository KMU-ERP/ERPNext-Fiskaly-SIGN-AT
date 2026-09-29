import frappe
from frappe import _
from frappe.model.document import Document


class FiskalyLifecycleEvent(Document):
	def validate(self):
		if not self.is_new():
			frappe.throw(_("Fiskaly lifecycle events are append-only."))

	def before_rename(self, old_name, new_name, merge=False):
		frappe.throw(_("Fiskaly lifecycle event identifiers are immutable."))

	def on_trash(self):
		if not getattr(frappe.flags, "in_uninstall", False):
			frappe.throw(_("Fiskaly lifecycle events cannot be deleted."))
