"""
Offline-sync wire-adapter endpoints -- the Frappe-side replacement for the
legacy Java offline-service's `/offline-sync/*` surface, scoped to exactly
one client (uhis_lf_mobile). This module is a thin wire adapter only (auth,
audit, idempotency bookkeeping against Offline Sync Batch/Offline Sync Item);
every actual household/member/patient read or write goes through
spice_next_core.api.mobile_sync, which owns the domain model.

  shukhee_integration.api.offline_sync.create                     push
  shukhee_integration.api.offline_sync.status                     poll
  shukhee_integration.api.offline_sync.fetch_synced_data           pull (stub)
  shukhee_integration.api.offline_sync.member_assessment_history   pull (stub)

`create` processes every item in the batch synchronously, in the request
itself -- there is no async queue and therefore no window for an item to get
permanently stuck the way the legacy offline-service's 3-retry-then-silent-
failure queue consumer can (see Offline Sync Batch's own doc comment). Each
item gets its own DB savepoint so one bad row never blocks the rest of the
batch, and its terminal Success/Failed status + assigned fhir_id are written
to an Offline Sync Item row before `create` returns -- `status` is therefore
a near-trivial re-read of those same rows, never a real poll.

Idempotency key is (device_id, entity_type, reference_id), not requestId
alone -- reference_id is the mobile's own local SQLite PK, unique only per
device, never globally (see Offline Sync Item's own doc comment).
"""

import frappe
from frappe import _

from shukhee_integration.api.consultation import _current_provider
from shukhee_integration.audit import audit_inbound
from spice_next_core.api import mobile_sync
from spice_next_core.auth.decorators import whitelist


def _resolve_env(payload=None):
	"""Same dual-mode parsing as shukhee_integration.api.consultation / .consent
	-- accepts either a legacy `payload` string or the raw JSON body merged
	into form_dict."""
	if payload:
		return frappe.parse_json(payload)
	return frappe.local.form_dict


@whitelist(methods=["POST"], remote_auth=True)
@audit_inbound
def create(payload=None):
	env = _resolve_env(payload)
	request_id = env.get("requestId")
	if not request_id:
		frappe.throw(_("requestId is required."), frappe.ValidationError)
	device_id = env.get("deviceId") or ""

	existing = frappe.db.exists("Offline Sync Batch", request_id)
	if existing:
		# Retried create (same requestId) -- replay the already-recorded
		# results, reprocess nothing.
		batch = frappe.get_doc("Offline Sync Batch", request_id)
		return {"entityList": [_item_wire(row) for row in batch.sync_items]}

	provider = _current_provider()
	batch = frappe.get_doc(
		{
			"doctype": "Offline Sync Batch",
			"request_id": request_id,
			"device_id": device_id,
			"provider": provider,
			"sync_mode": env.get("syncMode"),
			"app_version_name": env.get("appVersionName"),
			"app_version_code": env.get("appVersionCode"),
		}
	)
	batch.insert(ignore_permissions=True)

	for household in env.get("households") or []:
		_process_household(batch, household, device_id)
	for member in env.get("householdMembers") or []:
		_process_item(
			batch,
			"HouseholdMember",
			member.get("referenceId"),
			device_id,
			lambda m=member: mobile_sync.upsert_member(m, device_id),
		)
	# assessments: NCD (Phase 3) and the pregnancy-episode programmes (Phase 4
	# -- ANC/PWPROFILE/PNC_MOTHER/PNC_NEONATE/PREGNANCYOUTCOME) are the fully-
	# translated programmes so far (see the migration plan); every other
	# programme type still gets the not-yet-implemented stub below -- a mixed
	# batch must accept-and-store those too rather than failing the whole
	# batch.
	for assessment in env.get("assessments") or []:
		wire_type = (assessment.get("assessmentType") or "").upper()
		if wire_type == "NCD":
			_process_item(
				batch,
				"Assessment",
				assessment.get("referenceId"),
				device_id,
				lambda a=assessment: mobile_sync.process_ncd_assessment(a, device_id),
			)
		elif wire_type in mobile_sync.PREGNANCY_ASSESSMENT_TYPES:
			_process_item(
				batch,
				"Assessment",
				assessment.get("referenceId"),
				device_id,
				lambda a=assessment: mobile_sync.process_pregnancy_assessment(a, device_id),
			)
		else:
			_record_not_yet_implemented(
				batch, "Assessment", assessment.get("referenceId"), device_id
			)
	for follow_up in env.get("followUps") or []:
		_record_not_yet_implemented(batch, "FollowUp", follow_up.get("referenceId"), device_id)

	batch.save(ignore_permissions=True)
	frappe.db.commit()
	return {"entityList": [_item_wire(row) for row in batch.sync_items]}


@whitelist(methods=["POST"], remote_auth=True)
def status(payload=None):
	"""Near-trivial re-read -- `create` already resolved every item's
	terminal status synchronously before returning, so this never has real
	work to do; it exists only because the mobile client's push/poll
	contract always calls it after every create."""
	env = _resolve_env(payload)
	request_id = env.get("requestId")
	if not request_id or not frappe.db.exists("Offline Sync Batch", request_id):
		return {"entityList": []}
	batch = frappe.get_doc("Offline Sync Batch", request_id)
	return {"entityList": [_item_wire(row) for row in batch.sync_items]}


@whitelist(methods=["POST"], remote_auth=True)
def fetch_synced_data(payload=None):
	"""Pull (cold + delta). Phase 3 scope was households/householdMembers only
	(enough to render a worklist); Phase 4 adds pregnancyInfos/
	treatmentDetails (presence/LMP/gravida/parity, enough for the client's
	own pregnancy-risk cohort rules) -- patients/followUps/immunisations/
	assessmentHistory remain later phases (see the migration plan)."""
	env = _resolve_env(payload)
	village_ids = env.get("villageIds") or []
	households, members = mobile_sync.fetch_households_and_members(village_ids)
	pregnancy_infos, treatment_details = mobile_sync.fetch_pregnancy_infos_and_treatment_details(
		village_ids
	)
	return {
		"households": households,
		"householdMembers": members,
		"pregnancyInfos": pregnancy_infos,
		"treatmentDetails": treatment_details,
	}


@whitelist(methods=["POST"], remote_auth=True)
def member_assessment_history(payload=None):
	"""Phase 3 scope was NCD fields only in the `observations` map; Phase 4
	adds the pregnancy-episode programmes. Other programme types still
	appear in the list (serviceProvided/referralStatus/etc.), just with an
	empty observations map until their own translation phase lands."""
	env = _resolve_env(payload)
	village_ids = env.get("villageIds") or []
	items = mobile_sync.member_assessment_history(village_ids)
	return {"entityList": items}


def _process_household(batch, household_payload, device_id):
	"""A household's own nested `householdMembers[]` are processed as
	independent entities (each gets its own Offline Sync Item row/status/
	fhir_id), matching the flat entityList shape `status` returns -- not
	bundled under the household's own entry."""
	household_name_holder = {}

	def _do():
		name = mobile_sync.upsert_household(household_payload, device_id)
		household_name_holder["name"] = name
		for member in household_payload.get("householdMembers") or []:
			_process_item(
				batch,
				"HouseholdMember",
				member.get("referenceId"),
				device_id,
				lambda m=member: mobile_sync.upsert_member(
					m, device_id, household_client_uuid=household_name_holder["name"]
				),
			)
		return name

	_process_item(batch, "Household", household_payload.get("referenceId"), device_id, _do)


def _process_item(batch, entity_type, reference_id, device_id, fn):
	"""Processes one entity synchronously under its own savepoint (so one bad
	row never blocks the rest of the batch), or replays a prior result for
	the same (device_id, entity_type, reference_id) without reprocessing."""
	if reference_id is None:
		return
	reference_id = str(reference_id)

	prior = frappe.get_all(
		"Offline Sync Item",
		filters={"device_id": device_id, "entity_type": entity_type, "reference_id": reference_id},
		fields=["status", "fhir_id", "error_message"],
		order_by="creation desc",
		limit=1,
	)
	if prior:
		row = prior[0]
		batch.append(
			"sync_items",
			{
				"device_id": device_id,
				"entity_type": entity_type,
				"reference_id": reference_id,
				"status": row["status"],
				"fhir_id": row["fhir_id"],
				"error_message": row["error_message"],
			},
		)
		return

	# Prefixed so it's never a bare, digit-leading token -- Postgres parses an
	# unquoted SAVEPOINT name as an identifier, and one starting with a digit
	# (frappe.generate_hash's output sometimes does) is rejected as invalid
	# numeric-literal syntax.
	savepoint = f"sp_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		fhir_id = fn()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			frappe.get_traceback(), f"offline_sync.create: {entity_type} {reference_id} failed"
		)
		batch.append(
			"sync_items",
			{
				"device_id": device_id,
				"entity_type": entity_type,
				"reference_id": reference_id,
				"status": "Failed",
				"error_message": frappe.get_traceback()[-140:],
			},
		)
	else:
		frappe.db.release_savepoint(savepoint)
		batch.append(
			"sync_items",
			{
				"device_id": device_id,
				"entity_type": entity_type,
				"reference_id": reference_id,
				"status": "Success",
				"fhir_id": fhir_id,
			},
		)


def _record_not_yet_implemented(batch, entity_type, reference_id, device_id):
	if reference_id is None:
		return
	batch.append(
		"sync_items",
		{
			"device_id": device_id,
			"entity_type": entity_type,
			"reference_id": str(reference_id),
			"status": "Failed",
			"error_message": f"{entity_type} sync not yet implemented on this backend.",
		},
	)


def _item_wire(row):
	return {
		"type": row.entity_type,
		"status": row.status,
		"referenceId": row.reference_id,
		"fhirId": row.fhir_id,
		"errorMessage": row.error_message,
	}
