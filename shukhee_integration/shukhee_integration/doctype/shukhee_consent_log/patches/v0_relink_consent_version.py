"""Shukhee Consent Log.consent_version used to be a plain version-label string; it now
Links to Shukhee Consent Version instead. Existing rows only ever held that raw label
(never a doctype reference, since this is the first time the field becomes a Link), so
there is no stored id to rewrite -- the best available relink is matching each row's
(lng, consent_version) against a backfilled snapshot's (lng, version). Must run after
shukhee_consent.patches.v0_backfill_consent_versions (see patches.txt ordering).

Best-effort only: a row with no matching snapshot is left untouched rather than
guessed. If more than one snapshot matches (version labels aren't guaranteed unique
over time), the earliest-created one is used -- there is no way to know which specific
edit a given log row actually saw beyond the label it recorded.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Shukhee Consent Log"):
		return

	rows = frappe.get_all(
		"Shukhee Consent Log",
		filters={"consent_version": ("not in", ("", None))},
		fields=["name", "lng", "consent_version"],
	)
	for row in rows:
		match = frappe.get_all(
			"Shukhee Consent Version",
			filters={"lng": row["lng"], "version": row["consent_version"]},
			fields=["name"],
			order_by="creation asc",
			limit=1,
		)
		if match:
			frappe.db.set_value("Shukhee Consent Log", row["name"], "consent_version", match[0]["name"])

	if rows:
		frappe.db.commit()
