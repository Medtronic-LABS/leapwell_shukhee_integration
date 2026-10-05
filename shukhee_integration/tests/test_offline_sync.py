"""
Integration tests for shukhee_integration.api.offline_sync -- real DB, real
Household/Patient creation (unlike test_consent.py's pure-mock style), since
this module's actual value is its idempotency and savepoint-isolation
behaviour against a real database, not business logic that's meaningfully
testable in isolation from it.

inspect.unwrap reaches the raw function beneath frappe.whitelist's own
argument-typing wrapper and @whitelist(remote_auth=True)'s require_remote_auth
guard, same convention as test_consent.py / test_consultation.py.
"""

import inspect
import json
import unittest

import frappe

from shukhee_integration.api import offline_sync

create = inspect.unwrap(offline_sync.create)
status = inspect.unwrap(offline_sync.status)
fetch_synced_data = inspect.unwrap(offline_sync.fetch_synced_data)
member_assessment_history = inspect.unwrap(offline_sync.member_assessment_history)


class TestOfflineSyncCreate(unittest.TestCase):

	_PROVIDER = "lf_sk"

	def setUp(self):
		frappe.local.remote_user_id = self._PROVIDER
		self._created_households = []
		self._created_patients = []
		self._created_batches = []

	def tearDown(self):
		for name in self._created_households:
			if frappe.db.exists("Household", name):
				doc = frappe.get_doc("Household", name)
				doc.members = []
				doc.save(ignore_permissions=True)
		for name in self._created_patients:
			if frappe.db.exists("Patient", name):
				frappe.delete_doc("Patient", name, ignore_permissions=True, delete_permanently=True)
		for name in self._created_households:
			if frappe.db.exists("Household", name):
				frappe.delete_doc("Household", name, ignore_permissions=True, delete_permanently=True)
		for name in self._created_batches:
			if frappe.db.exists("Offline Sync Batch", name):
				frappe.delete_doc(
					"Offline Sync Batch", name, ignore_permissions=True, delete_permanently=True
				)
		frappe.db.commit()

	def _create(self, payload):
		result = create(payload=json.dumps(payload))
		self._created_batches.append(payload["requestId"])
		return result

	def _household_payload(self, request_id, *, household_ref, member_ref, device_id="device-1"):
		return {
			"requestId": request_id,
			"deviceId": device_id,
			"households": [
				{
					"referenceId": household_ref,
					"name": "Test Household",
					"villageId": 0,
					"householdMembers": [
						{
							"referenceId": member_ref,
							"name": "Jane Doe",
							"dateOfBirth": "1990-01-01",
							"gender": "Female",
							"isHouseholdHead": True,
						}
					],
				}
			],
		}

	def test_requires_request_id(self):
		with self.assertRaises(frappe.ValidationError):
			create(payload=json.dumps({"deviceId": "device-1", "households": []}))

	def test_household_and_nested_member_created(self):
		payload = self._household_payload("itc-req-1", household_ref=1, member_ref=2)
		result = self._create(payload)

		by_type = {item["type"]: item for item in result["entityList"]}
		self.assertEqual(by_type["Household"]["status"], "Success")
		self.assertEqual(by_type["HouseholdMember"]["status"], "Success")

		hh_name = by_type["Household"]["fhirId"]
		member_name = by_type["HouseholdMember"]["fhirId"]
		self._created_households.append(hh_name)
		self._created_patients.append(member_name)

		hh = frappe.get_doc("Household", hh_name)
		self.assertEqual(hh.display_title, "Test Household")
		self.assertEqual([r.patient for r in hh.members], [member_name])
		self.assertTrue(hh.members[0].is_head)

		member = frappe.get_doc("Patient", member_name)
		self.assertEqual(member.full_name, "Jane Doe")
		self.assertEqual(member.primary_household, hh_name)

	def test_same_request_id_replay_does_not_reprocess(self):
		payload = self._household_payload("itc-req-2", household_ref=10, member_ref=20)
		first = self._create(payload)
		hh_name = next(i["fhirId"] for i in first["entityList"] if i["type"] == "Household")
		member_name = next(i["fhirId"] for i in first["entityList"] if i["type"] == "HouseholdMember")
		self._created_households.append(hh_name)
		self._created_patients.append(member_name)

		# Re-POST the identical requestId -- create() itself short-circuits
		# before reprocessing, replaying the stored batch verbatim.
		second = create(payload=json.dumps(payload))
		self.assertEqual(second, first)

	def test_different_request_id_same_reference_id_replays_without_duplicating(self):
		device_id = "device-replay"
		first_payload = {
			"requestId": "itc-req-3a",
			"deviceId": device_id,
			"households": [{"referenceId": 30, "name": "Original Name", "householdMembers": []}],
		}
		first = self._create(first_payload)
		hh_name = first["entityList"][0]["fhirId"]
		self._created_households.append(hh_name)

		second_payload = {
			"requestId": "itc-req-3b",
			"deviceId": device_id,
			# Same device_id + referenceId, different requestId and a
			# changed name -- must replay the ORIGINAL result, not reprocess.
			"households": [{"referenceId": 30, "name": "Changed Name", "householdMembers": []}],
		}
		second = self._create(second_payload)

		self.assertEqual(second["entityList"][0]["fhirId"], hh_name)
		self.assertEqual(frappe.db.get_value("Household", hh_name, "display_title"), "Original Name")
		self.assertEqual(
			frappe.db.count("Household", {"client_uuid": hh_name}),
			1,
		)

	def test_unimplemented_entity_types_marked_failed_not_dropped(self):
		# NCD, the pregnancy-episode programmes, and CHILDHOOD_VISIT/ICCM/
		# EYE_CARE/CATARACT/FAMILY_PLANNING are all fully-translated now --
		# that's every type the migration plan scoped in. TB is the one
		# wire type uhis_lf_mobile's own mapper implements that the plan
		# explicitly scoped OUT, so it stays the not-yet-implemented stub
		# for this test.
		payload = {
			"requestId": "itc-req-4",
			"deviceId": "device-1",
			"assessments": [{"referenceId": 40, "assessmentType": "TB"}],
			"followUps": [{"referenceId": 41}],
		}
		result = self._create(payload)
		by_type = {item["type"]: item for item in result["entityList"]}
		self.assertEqual(by_type["Assessment"]["status"], "Failed")
		self.assertIsNotNone(by_type["Assessment"]["errorMessage"])
		self.assertEqual(by_type["FollowUp"]["status"], "Failed")

	def test_status_for_unknown_request_id_returns_empty(self):
		self.assertEqual(status(payload=json.dumps({"requestId": "no-such-batch"})), {"entityList": []})

	def test_status_matches_create_result(self):
		payload = self._household_payload("itc-req-5", household_ref=50, member_ref=51)
		created = self._create(payload)
		hh_name = next(i["fhirId"] for i in created["entityList"] if i["type"] == "Household")
		member_name = next(i["fhirId"] for i in created["entityList"] if i["type"] == "HouseholdMember")
		self._created_households.append(hh_name)
		self._created_patients.append(member_name)

		fetched = status(payload=json.dumps({"requestId": "itc-req-5"}))
		self.assertEqual(fetched, created)


class TestOfflineSyncNcdAssessment(unittest.TestCase):
	"""Phase 3 of the migration plan: NCD is the one fully-translated
	programme -- create() routes assessmentType=NCD to spice_next_core.api.
	mobile_sync.process_ncd_assessment instead of the not-yet-implemented
	stub every other programme type still gets."""

	_PROVIDER = "lf_sk"

	def setUp(self):
		frappe.local.remote_user_id = self._PROVIDER
		self._docs = []  # [(doctype, name), ...] in creation order, deleted in reverse
		self._batches = []

	def tearDown(self):
		for doctype, name in reversed(self._docs):
			if not frappe.db.exists(doctype, name):
				continue
			if doctype == "Household":
				doc = frappe.get_doc(doctype, name)
				doc.members = []
				doc.save(ignore_permissions=True)
			if frappe.get_meta(doctype).is_submittable and frappe.db.get_value(doctype, name, "docstatus") == 1:
				frappe.get_doc(doctype, name).cancel()
			frappe.delete_doc(doctype, name, ignore_permissions=True, delete_permanently=True, force=True)
		for name in self._batches:
			if frappe.db.exists("Offline Sync Batch", name):
				frappe.delete_doc(
					"Offline Sync Batch", name, ignore_permissions=True, delete_permanently=True
				)
		frappe.db.commit()

	def _create(self, payload):
		result = create(payload=json.dumps(payload))
		self._batches.append(payload["requestId"])
		return result

	def _make_member(self, request_id, *, household_ref, member_ref, device_id="device-ncd"):
		payload = {
			"requestId": request_id,
			"deviceId": device_id,
			"households": [
				{
					"referenceId": household_ref,
					"name": "NCD Test Household",
					"villageId": 0,
					"householdMembers": [
						{
							"referenceId": member_ref,
							"name": "NCD Patient",
							"dateOfBirth": "1970-01-01",
							"gender": "Female",
						}
					],
				}
			],
		}
		result = self._create(payload)
		by_type = {item["type"]: item for item in result["entityList"]}
		hh_name = by_type["Household"]["fhirId"]
		member_name = by_type["HouseholdMember"]["fhirId"]
		self._docs.append(("Patient", member_name))
		self._docs.append(("Household", hh_name))
		return hh_name, member_name

	def _ncd_assessment(self, reference_id, member_name, household_name):
		return {
			"referenceId": reference_id,
			"assessmentType": "NCD",
			"assessmentDetails": {
				"ncd": {
					"bpLog": {
						"avgSystolic": 150,
						"avgDiastolic": 95,
						"bpLogDetails": [{"systolic": 150, "diastolic": 95}],
					},
					"glucoseLog": {"glucose": 7.2, "glucoseType": "fbs"},
					"biometric": {"height": 160.0, "weight": 55.0},
				}
			},
			"villageId": "0",
			"patientStatus": "Recovered",
			"encounter": {
				"householdId": household_name,
				"memberId": member_name,
				"patientId": member_name,
				"startTime": "2026-10-06 09:00:00",
				"endTime": "2026-10-06 09:30:00",
			},
		}

	def _track_ncd_side_effects(self, encounter_name, member_name):
		self._docs.append(("Mobile Encounter Context", encounter_name))
		self._docs.append(("Encounter", encounter_name))
		case_name = frappe.db.get_value("Case", {"patient": member_name}, "name")
		if case_name:
			self._docs.append(("Case", case_name))
		for obs_name in frappe.get_all("Observation", filters={"encounter": encounter_name}, pluck="name"):
			self._docs.append(("Observation", obs_name))

	def test_create_processes_ncd_assessment_end_to_end(self):
		hh_name, member_name = self._make_member("itc-ncd-req-1", household_ref=100, member_ref=101)
		payload = {
			"requestId": "itc-ncd-req-2",
			"deviceId": "device-ncd",
			"assessments": [self._ncd_assessment(200, member_name, hh_name)],
		}
		result = self._create(payload)

		item = next(i for i in result["entityList"] if i["type"] == "Assessment")
		self.assertEqual(item["status"], "Success")
		encounter_name = item["fhirId"]
		self._track_ncd_side_effects(encounter_name, member_name)

		encounter = frappe.get_doc("Encounter", encounter_name)
		self.assertEqual(encounter.docstatus, 1)
		self.assertEqual(encounter.patient, member_name)

	def test_mixed_batch_ncd_succeeds_other_programme_still_not_implemented(self):
		hh_name, member_name = self._make_member("itc-ncd-req-3", household_ref=110, member_ref=111)
		payload = {
			"requestId": "itc-ncd-req-4",
			"deviceId": "device-ncd",
			"assessments": [
				self._ncd_assessment(210, member_name, hh_name),
				{"referenceId": 211, "assessmentType": "TB"},
			],
		}
		result = self._create(payload)

		assessments = [i for i in result["entityList"] if i["type"] == "Assessment"]
		by_ref = {i["referenceId"]: i for i in assessments}
		self.assertEqual(by_ref["210"]["status"], "Success")
		self.assertEqual(by_ref["211"]["status"], "Failed")
		self._track_ncd_side_effects(by_ref["210"]["fhirId"], member_name)


class TestOfflineSyncFetchAndHistory(unittest.TestCase):
	"""Thin wire-adapter smoke tests -- the domain logic these two endpoints
	call into (mobile_sync.fetch_households_and_members /
	mobile_sync.member_assessment_history) has its own full coverage in
	spice_next_core/tests/test_mobile_sync.py."""

	_PROVIDER = "lf_sk"

	def setUp(self):
		frappe.local.remote_user_id = self._PROVIDER

	def test_fetch_synced_data_returns_households_and_members_shape(self):
		result = fetch_synced_data(payload=json.dumps({"villageIds": []}))
		self.assertEqual(
			result,
			{
				"households": [],
				"householdMembers": [],
				"pregnancyInfos": [],
				"treatmentDetails": [],
			},
		)

	def test_member_assessment_history_returns_entity_list_shape(self):
		result = member_assessment_history(payload=json.dumps({"villageIds": ["0"]}))
		self.assertIn("entityList", result)
		self.assertIsInstance(result["entityList"], list)


class TestOfflineSyncPregnancyAssessment(unittest.TestCase):
	"""Phase 4 of the migration plan: ANC/PWPROFILE/PNC_MOTHER/PNC_NEONATE/
	PREGNANCYOUTCOME -- create() routes these to spice_next_core.api.
	mobile_sync.process_pregnancy_assessment, sharing one Case per
	pregnancyEpisodeId across the whole episode's visits."""

	_PROVIDER = "lf_sk"

	def setUp(self):
		frappe.local.remote_user_id = self._PROVIDER
		self._docs = []
		self._batches = []

	def tearDown(self):
		for doctype, name in reversed(self._docs):
			if not frappe.db.exists(doctype, name):
				continue
			if doctype == "Household":
				doc = frappe.get_doc(doctype, name)
				doc.members = []
				doc.save(ignore_permissions=True)
			if frappe.get_meta(doctype).is_submittable and frappe.db.get_value(doctype, name, "docstatus") == 1:
				frappe.get_doc(doctype, name).cancel()
			frappe.delete_doc(doctype, name, ignore_permissions=True, delete_permanently=True, force=True)
		for name in self._batches:
			if frappe.db.exists("Offline Sync Batch", name):
				frappe.delete_doc(
					"Offline Sync Batch", name, ignore_permissions=True, delete_permanently=True
				)
		frappe.db.commit()

	def _create(self, payload):
		result = create(payload=json.dumps(payload))
		self._batches.append(payload["requestId"])
		return result

	def _make_member(self, request_id, *, household_ref, member_ref, device_id="device-preg"):
		payload = {
			"requestId": request_id,
			"deviceId": device_id,
			"households": [
				{
					"referenceId": household_ref,
					"name": "Pregnancy Test Household",
					"villageId": 0,
					"householdMembers": [
						{
							"referenceId": member_ref,
							"name": "Pregnancy Patient",
							"dateOfBirth": "1995-01-01",
							"gender": "Female",
						}
					],
				}
			],
		}
		result = self._create(payload)
		by_type = {item["type"]: item for item in result["entityList"]}
		hh_name = by_type["Household"]["fhirId"]
		member_name = by_type["HouseholdMember"]["fhirId"]
		self._docs.append(("Patient", member_name))
		self._docs.append(("Household", hh_name))
		return hh_name, member_name

	def _anc_assessment(self, reference_id, member_name, pregnancy_episode_id):
		return {
			"referenceId": reference_id,
			"assessmentType": "ANC",
			"assessmentDetails": {
				"anc": {
					"medicalHistoryPhysicalExamination": {"systolic": "130", "diastolic": "85"},
				}
			},
			"villageId": "0",
			"patientStatus": "Recovered",
			"encounter": {
				"memberId": member_name,
				"pregnancyEpisodeId": pregnancy_episode_id,
				"startTime": "2026-10-06 09:00:00",
				"endTime": "2026-10-06 09:30:00",
				"visitNumber": 1,
			},
		}

	def _track_side_effects(self, encounter_name, pregnancy_episode_id):
		self._docs.append(("Mobile Encounter Context", encounter_name))
		self._docs.append(("Encounter", encounter_name))
		case_name = f"case-preg-{pregnancy_episode_id}"
		if frappe.db.exists("Case", case_name):
			self._docs.append(("Case", case_name))
		for obs_name in frappe.get_all("Observation", filters={"encounter": encounter_name}, pluck="name"):
			self._docs.append(("Observation", obs_name))

	def test_create_processes_anc_assessment_end_to_end(self):
		_, member_name = self._make_member("itc-preg-req-1", household_ref=300, member_ref=301)
		episode_id = frappe.generate_hash(length=12)
		payload = {
			"requestId": "itc-preg-req-2",
			"deviceId": "device-preg",
			"assessments": [self._anc_assessment(400, member_name, episode_id)],
		}
		result = self._create(payload)

		item = next(i for i in result["entityList"] if i["type"] == "Assessment")
		self.assertEqual(item["status"], "Success")
		encounter_name = item["fhirId"]
		self._track_side_effects(encounter_name, episode_id)

		encounter = frappe.get_doc("Encounter", encounter_name)
		self.assertEqual(encounter.docstatus, 1)
		ctx = frappe.get_doc("Mobile Encounter Context", encounter_name)
		self.assertEqual(ctx.pregnancy_episode_id, episode_id)

	def test_two_anc_visits_same_batch_share_one_case(self):
		_, member_name = self._make_member("itc-preg-req-3", household_ref=310, member_ref=311)
		episode_id = frappe.generate_hash(length=12)
		payload = {
			"requestId": "itc-preg-req-4",
			"deviceId": "device-preg",
			"assessments": [
				self._anc_assessment(410, member_name, episode_id),
				self._anc_assessment(411, member_name, episode_id),
			],
		}
		result = self._create(payload)

		assessments = [i for i in result["entityList"] if i["type"] == "Assessment"]
		self.assertEqual(len(assessments), 2)
		self.assertTrue(all(a["status"] == "Success" for a in assessments))
		encounter_names = [a["fhirId"] for a in assessments]
		self.assertNotEqual(encounter_names[0], encounter_names[1])
		for enc_name in encounter_names:
			self._track_side_effects(enc_name, episode_id)
		encounters = [frappe.get_doc("Encounter", n) for n in encounter_names]
		self.assertEqual(encounters[0].case, encounters[1].case)


class TestOfflineSyncOtherAssessment(unittest.TestCase):
	"""Phase 5 of the migration plan: CHILDHOOD_VISIT/ICCM/EYE_CARE/CATARACT/
	FAMILY_PLANNING -- create() routes these to spice_next_core.api.
	mobile_sync.process_other_assessment."""

	_PROVIDER = "lf_sk"

	def setUp(self):
		frappe.local.remote_user_id = self._PROVIDER
		self._docs = []
		self._batches = []

	def tearDown(self):
		for doctype, name in reversed(self._docs):
			if not frappe.db.exists(doctype, name):
				continue
			if doctype == "Household":
				doc = frappe.get_doc(doctype, name)
				doc.members = []
				doc.save(ignore_permissions=True)
			if frappe.get_meta(doctype).is_submittable and frappe.db.get_value(doctype, name, "docstatus") == 1:
				frappe.get_doc(doctype, name).cancel()
			frappe.delete_doc(doctype, name, ignore_permissions=True, delete_permanently=True, force=True)
		for name in self._batches:
			if frappe.db.exists("Offline Sync Batch", name):
				frappe.delete_doc(
					"Offline Sync Batch", name, ignore_permissions=True, delete_permanently=True
				)
		frappe.db.commit()

	def _create(self, payload):
		result = create(payload=json.dumps(payload))
		self._batches.append(payload["requestId"])
		return result

	def _make_member(self, request_id, *, household_ref, member_ref, device_id="device-other"):
		payload = {
			"requestId": request_id,
			"deviceId": device_id,
			"households": [
				{
					"referenceId": household_ref,
					"name": "Other Programme Household",
					"villageId": 0,
					"householdMembers": [
						{
							"referenceId": member_ref,
							"name": "Other Programme Patient",
							"dateOfBirth": "1990-01-01",
							"gender": "Male",
						}
					],
				}
			],
		}
		result = self._create(payload)
		by_type = {item["type"]: item for item in result["entityList"]}
		hh_name = by_type["Household"]["fhirId"]
		member_name = by_type["HouseholdMember"]["fhirId"]
		self._docs.append(("Patient", member_name))
		self._docs.append(("Household", hh_name))
		return hh_name, member_name

	def _track_side_effects(self, encounter_name, member_name):
		self._docs.append(("Mobile Encounter Context", encounter_name))
		self._docs.append(("Encounter", encounter_name))
		case_name = frappe.db.get_value("Case", {"patient": member_name}, "name")
		if case_name:
			self._docs.append(("Case", case_name))
		for obs_name in frappe.get_all("Observation", filters={"encounter": encounter_name}, pluck="name"):
			self._docs.append(("Observation", obs_name))

	def test_create_processes_iccm_assessment_end_to_end(self):
		_, member_name = self._make_member("itc-other-req-1", household_ref=500, member_ref=501)
		payload = {
			"requestId": "itc-other-req-2",
			"deviceId": "device-other",
			"assessments": [
				{
					"referenceId": 600,
					"assessmentType": "ICCM",
					"assessmentDetails": {"iccm": {"iccmClassification": "Pneumonia"}},
					"villageId": "0",
					"patientStatus": "Recovered",
					"encounter": {"memberId": member_name, "startTime": "2026-10-06 09:00:00"},
				}
			],
		}
		result = self._create(payload)

		item = next(i for i in result["entityList"] if i["type"] == "Assessment")
		self.assertEqual(item["status"], "Success")
		encounter_name = item["fhirId"]
		self._track_side_effects(encounter_name, member_name)

		encounter = frappe.get_doc("Encounter", encounter_name)
		self.assertEqual(encounter.docstatus, 1)


if __name__ == "__main__":
	unittest.main()
