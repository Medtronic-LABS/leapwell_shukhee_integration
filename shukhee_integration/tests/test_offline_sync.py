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
		payload = {
			"requestId": "itc-req-4",
			"deviceId": "device-1",
			"assessments": [{"referenceId": 40, "assessmentType": "NCD"}],
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


if __name__ == "__main__":
	unittest.main()
