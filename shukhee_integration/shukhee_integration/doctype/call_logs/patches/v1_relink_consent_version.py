"""Call Logs.consent_version used to Link to Shukhee Consent (added, then superseded,
in the same release); it now Links to Shukhee Consent Version instead, so existing rows
need their stored value rewritten from a Shukhee Consent name to that row's
current_version. Must run after shukhee_consent.patches.v0_backfill_consent_versions
(see patches.txt ordering), which guarantees current_version is set on every Shukhee
Consent row before this patch reads it.

Best-effort only: a row whose stored value doesn't resolve to a known Shukhee Consent
is left untouched rather than guessed -- same posture as every other backfill patch in
this app (see call_logs.patches.v0_backfill_call_logs_sync_fields).
"""

import frappe


def execute():
	if not frappe.db.table_exists("Call Logs") or not frappe.db.table_exists("Shukhee Consent"):
		return

	rows = frappe.get_all(
		"Call Logs", filters={"consent_version": ("not in", ("", None))}, fields=["name", "consent_version"]
	)
	for row in rows:
		current_version = frappe.db.get_value("Shukhee Consent", row["consent_version"], "current_version")
		if current_version:
			frappe.db.set_value("Call Logs", row["name"], "consent_version", current_version)

	if rows:
		frappe.db.commit()
