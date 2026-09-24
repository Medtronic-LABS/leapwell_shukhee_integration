"""
Implementation of doc_events handlers wired in hooks.py.

  set_call_logs_geography_node — on Call Logs.before_insert, denormalize
  geography_node from the linked Patient's household, mirroring
  spice_next_core.hooks_impl.set_case_geography_node's exact pattern.
"""

import frappe


def set_call_logs_geography_node(doc, event):
	"""Denormalize geography_node onto Call Logs from Patient -> Household at
	insert time -- drives spice_next_core.api.sync.pull's catchment filter
	(generic: any doctype with a geography_node/care_team field gets
	filtered, no doctype-specific change needed there). Never client-supplied
	-- doc.patient itself is only ever set server-side in
	shukhee_integration.api.consultation.start_consultation, soft-degrading
	to null (and thus no geography_node, and thus unsynced) rather than
	failing the booking when the caller didn't send a resolvable patient id."""
	if doc.geography_node:
		return  # already set
	if not doc.patient:
		return
	household = frappe.db.get_value("Patient", doc.patient, "primary_household")
	if household:
		doc.geography_node = frappe.db.get_value("Household", household, "geography_node")
