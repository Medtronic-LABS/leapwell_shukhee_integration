"""Shukhee Consent Log (and its child doctype Shukhee Consent Log Item) is retired:
consent decisions are now recorded directly on the Call Logs row they belong to
(consent_version/consent_lng/consent_items/consent_filled_text, set once at booking time
by api.consultation.start_consultation) for an Agreed decision, or on the new, separate
Shukhee Consent Decline doctype for a Declined one. The old design recorded every decision
into its own doctype and then tried to link it back to Call Logs after the fact via a
shared encounter_id/visit_id -- that linking step was the source of several real bugs
(timing races between the lazy mobile upload and the synchronous booking call, a
version_id-only match that could cross-attach a different patient's decision) and is not
being carried forward in any form.

Standard Frappe pattern for retiring a doctype in the same release that deletes its
directory (mirrors frappe/patches/v14_0/delete_data_migration_tool.py) --
ignore_missing=True so this is a no-op on a site that never had it (e.g. a fresh install
created after this patch was added)."""

import frappe


def execute():
	frappe.delete_doc("DocType", "Shukhee Consent Log", ignore_missing=True, force=True)
	frappe.delete_doc("DocType", "Shukhee Consent Log Item", ignore_missing=True, force=True)
