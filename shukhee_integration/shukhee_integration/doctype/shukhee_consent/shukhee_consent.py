import frappe
from frappe.model.document import Document


class ShukheeConsent(Document):
	def on_update(self):
		"""Keeps a full history of every edit to this row's consent copy, since this
		doctype itself only ever holds the CURRENT content per language (edited in
		place -- see its own `consent`/`version` field doc comments). Fires on the
		first save and on every save that actually changes the content, even if an
		editor forgets to bump `version` -- history must never silently miss an edit.

		Each snapshot is immutable (see Shukhee Consent Version's own doc comment);
		this row's `current_version` always points at the latest one, which is what
		Call Logs.consent_version / Shukhee Consent Log.consent_version actually link
		to, not this row directly."""
		is_new = bool(self.flags.get("in_insert"))
		changed = is_new or self.has_value_changed("consent") or self.has_value_changed("version")
		if not changed:
			return

		snapshot = frappe.get_doc(
			{
				"doctype": "Shukhee Consent Version",
				"shukhee_consent": self.name,
				"lng": self.lng,
				"version": self.version,
				"consent": self.consent,
			}
		).insert(ignore_permissions=True)
		self.db_set("current_version", snapshot.name, update_modified=False)
