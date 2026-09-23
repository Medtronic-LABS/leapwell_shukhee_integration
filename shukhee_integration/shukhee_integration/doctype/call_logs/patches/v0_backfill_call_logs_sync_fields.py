"""Backfill patient/geography_node/sync_seq onto Call Logs rows created before
this rollout. Best-effort only: encounter_id has always been a free-text,
non-FK field (see its own DocField description), so there is no reliable
local mapping from an old encounter_id string back to a Patient record --
these rows are left with patient/geography_node unset (null), not guessed.
sync_seq is left at 0/unset rather than back-assigned a sequence, so these
rows simply do not appear in any sync.pull until they are next touched
(matching how a genuinely untouched record should behave under a
sync_seq > cursor cursor query) -- assigning them a synthetic historical
sequence would be indistinguishable from real activity to any pulling
client and is unnecessary since nothing consumed Call Logs via sync before
this rollout existed.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Call Logs"):
		return
	# Nothing to resolve patient/geography_node from (no reliable encounter_id
	# -> Patient mapping exists) -- this patch is a documented no-op placeholder
	# so `bench migrate` history records that this gap was considered, not
	# missed. If a reliable mapping ever exists, extend this function; do not
	# guess a patient/geography_node from encounter_id string matching.
	pass
