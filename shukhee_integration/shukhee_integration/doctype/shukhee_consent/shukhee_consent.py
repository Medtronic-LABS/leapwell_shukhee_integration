import frappe
from frappe.model.document import Document


def _items_signature(rows):
	"""Normalized (description, mandatory) tuples for change-detection -- `rows` is a list of
	child Document instances (or plain dicts). Table fields can't use has_value_changed() for
	this: Frappe compares the previous/current child-Document lists by identity, not content,
	so it would evaluate True on every single save (see test_resaving_with_no_content_change_
	does_not_create_another_snapshot) regardless of whether any row actually changed."""
	return [(row.get("description"), bool(row.get("mandatory"))) for row in (rows or [])]


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
		before = self.get_doc_before_save()
		items_changed = not is_new and _items_signature(
			before and before.get("consent_items")
		) != _items_signature(self.get("consent_items"))
		changed = (
			is_new
			or self.has_value_changed("consent")
			or self.has_value_changed("version")
			or items_changed
		)
		if not changed:
			return

		snapshot = frappe.get_doc(
			{
				"doctype": "Shukhee Consent Version",
				"shukhee_consent": self.name,
				"lng": self.lng,
				"version": self.version,
				"consent": self.consent,
				# Plain dicts, not self.get("consent_items")'s live child Document objects --
				# those still carry this row's own name/parent/parenttype/idx, which must not
				# leak into the snapshot's own independent child rows.
				"consent_items": [
					{"description": row.description, "mandatory": row.mandatory}
					for row in (self.get("consent_items") or [])
				],
			}
		).insert(ignore_permissions=True)
		self.db_set("current_version", snapshot.name, update_modified=False)
