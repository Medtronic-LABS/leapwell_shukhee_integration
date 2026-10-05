"""Backfill a Shukhee Consent Version snapshot (and current_version link) for every
Shukhee Consent row that predates this doctype -- ShukheeConsent.on_update only
captures a snapshot going forward, on each future edit; older wording was never
retained anywhere and cannot be recovered, so this patch snapshots whatever content is
live right now, as the best available history. Must run before
call_logs.patches.v1_relink_consent_version and
shukhee_consent_log.patches.v0_relink_consent_version (see patches.txt ordering),
both of which depend on every Shukhee Consent row already having a current_version to
relink to.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Shukhee Consent"):
		return

	rows = frappe.get_all(
		"Shukhee Consent",
		filters={"current_version": ("in", ("", None))},
		fields=["name", "lng", "version", "consent"],
	)
	for row in rows:
		snapshot = frappe.get_doc(
			{
				"doctype": "Shukhee Consent Version",
				"shukhee_consent": row["name"],
				"lng": row["lng"],
				"version": row["version"],
				"consent": row["consent"],
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Shukhee Consent", row["name"], "current_version", snapshot.name)

	if rows:
		frappe.db.commit()
