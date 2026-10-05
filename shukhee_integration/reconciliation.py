"""
Daily reconciliation sweep for Offline Sync Item rows stuck Failed -- the
cross-cutting design decision from the "replace offline-service" migration
plan ("add what the legacy system never had: a scheduled daily job retrying
any Failed Offline Sync Item younger than N days"), built now rather than
left perpetually deferred. The legacy Java offline-service's queue consumer
had a hard 3-attempt retry cap and NO sweep job at all -- a row that
exhausted its retries went permanently Failed with nothing ever revisiting
it, which is the exact bug this whole migration started from (see Offline
Sync Batch's own doc comment).

Replays the SAME per-item processing create() uses (same dispatch-by-
entity_type/assessmentType rules as api/offline_sync.py, same savepoint
isolation) against the payload create() stored on the Failed row
(Offline Sync Item.payload_json) -- a reconciled item's terminal status/
fhir_id ends up identical to what a normal create() call would have
produced, because it runs the same translation functions, just re-triggered
server-side instead of by the mobile app's own retry.

Rows with no stored payload_json (synced before this field existed, or a
still-not-yet-implemented programme type with no processor to run) are left
untouched -- there is nothing to replay them with.
"""

import frappe

from spice_next_core.api import mobile_sync

RECONCILE_WINDOW_DAYS = 7


def reconcile_failed_items():
	"""Scheduled daily (see hooks.py's scheduler_events)."""
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -RECONCILE_WINDOW_DAYS)
	failed = frappe.get_all(
		"Offline Sync Item",
		filters={"status": "Failed", "creation": [">=", cutoff]},
		fields=["name", "device_id", "entity_type", "payload_json"],
	)
	for row in failed:
		_reconcile_one(row)
	frappe.db.commit()


def _reconcile_one(row):
	if not row.payload_json:
		return
	payload = frappe.parse_json(row.payload_json)
	fn = _resolve_processor(row.entity_type, payload, row.device_id)
	if fn is None:
		return

	savepoint = f"sp_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		fhir_id = fn()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			frappe.get_traceback(), f"reconcile_failed_items: {row.entity_type} {row.name} still failing"
		)
		frappe.db.set_value(
			"Offline Sync Item", row.name, "error_message", frappe.get_traceback()[-140:]
		)
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.set_value(
			"Offline Sync Item",
			row.name,
			{"status": "Success", "fhir_id": fhir_id, "error_message": None},
		)


def _resolve_processor(entity_type, payload, device_id):
	"""Mirrors api/offline_sync.create's own dispatch rules -- kept as a
	second copy rather than imported from there because create's version is
	expressed as inline lambdas inside a request-handling loop, not a
	reusable function; if the two drift, this module's own tests catch it,
	same discipline as the rest of this migration's "verify against the real
	behaviour" rule."""
	if entity_type == "Household":
		return lambda: mobile_sync.upsert_household(payload, device_id)
	if entity_type == "HouseholdMember":
		if isinstance(payload, dict) and "member" in payload and "householdClientUuid" in payload:
			member = payload["member"]
			household_client_uuid = payload.get("householdClientUuid")
			return lambda: mobile_sync.upsert_member(
				member, device_id, household_client_uuid=household_client_uuid
			)
		return lambda: mobile_sync.upsert_member(payload, device_id)
	if entity_type == "Assessment":
		wire_type = (payload.get("assessmentType") or "").upper()
		if wire_type == "NCD":
			return lambda: mobile_sync.process_ncd_assessment(payload, device_id)
		if wire_type in mobile_sync.PREGNANCY_ASSESSMENT_TYPES:
			return lambda: mobile_sync.process_pregnancy_assessment(payload, device_id)
		if wire_type in mobile_sync.OTHER_ASSESSMENT_TYPES:
			return lambda: mobile_sync.process_other_assessment(payload, device_id)
	return None
