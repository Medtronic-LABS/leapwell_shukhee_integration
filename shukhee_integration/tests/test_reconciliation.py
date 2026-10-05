"""
Integration tests for shukhee_integration.reconciliation -- real DB, since
the behaviour under test (replaying a stored payload against the real
mobile_sync processing functions, including the savepoint isolation) only
exists at the level of real rows.
"""

import json
import unittest

import frappe

from shukhee_integration import reconciliation


class TestReconcileFailedItems(unittest.TestCase):

	def setUp(self):
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

	def _make_batch_with_item(self, *, entity_type, reference_id, device_id, payload, status="Failed", creation=None):
		request_id = frappe.generate_hash(length=10)
		batch = frappe.get_doc(
			{
				"doctype": "Offline Sync Batch",
				"request_id": request_id,
				"device_id": device_id,
			}
		)
		batch.append(
			"sync_items",
			{
				"device_id": device_id,
				"entity_type": entity_type,
				"reference_id": str(reference_id),
				"status": status,
				"payload_json": json.dumps(payload),
			},
		)
		batch.insert(ignore_permissions=True)
		self._batches.append(request_id)
		item_name = batch.sync_items[0].name
		if creation is not None:
			frappe.db.set_value("Offline Sync Item", item_name, "creation", creation, update_modified=False)
		return item_name

	def test_reconciles_a_household_that_now_succeeds(self):
		item_name = self._make_batch_with_item(
			entity_type="Household",
			reference_id=900,
			device_id="device-recon",
			payload={"referenceId": 900, "name": "Reconciled Household", "villageId": 0},
		)

		reconciliation.reconcile_failed_items()

		item = frappe.get_doc("Offline Sync Item", item_name)
		self.assertEqual(item.status, "Success")
		self.assertIsNotNone(item.fhir_id)
		self._docs.append(("Household", item.fhir_id))

	def test_reconciles_a_nested_household_member_using_stored_household_client_uuid(self):
		household = frappe.get_doc(
			{"doctype": "Household", "client_uuid": "lf-hh-device-recon-901", "display_title": "HH"}
		)
		household.insert(ignore_permissions=True)
		self._docs.append(("Household", household.name))

		item_name = self._make_batch_with_item(
			entity_type="HouseholdMember",
			reference_id=902,
			device_id="device-recon",
			payload={
				"member": {"referenceId": 902, "name": "Reconciled Member", "gender": "Female"},
				"householdClientUuid": household.name,
			},
		)

		reconciliation.reconcile_failed_items()

		item = frappe.get_doc("Offline Sync Item", item_name)
		self.assertEqual(item.status, "Success")
		self._docs.append(("Patient", item.fhir_id))
		patient = frappe.get_doc("Patient", item.fhir_id)
		self.assertEqual(patient.primary_household, household.name)

	def test_reconciles_an_ncd_assessment_once_the_patient_now_exists(self):
		patient = frappe.get_doc(
			{"doctype": "Patient", "client_uuid": frappe.generate_hash(length=16), "full_name": "Recon Patient"}
		)
		patient.insert(ignore_permissions=True)
		self._docs.append(("Patient", patient.name))

		item_name = self._make_batch_with_item(
			entity_type="Assessment",
			reference_id=903,
			device_id="device-recon",
			payload={
				"referenceId": 903,
				"assessmentType": "NCD",
				"assessmentDetails": {"ncd": {"biometric": {"weight": 60.0}}},
				"villageId": "0",
				"patientStatus": "Recovered",
				"encounter": {"memberId": patient.name, "startTime": "2026-10-06 09:00:00"},
			},
		)

		reconciliation.reconcile_failed_items()

		item = frappe.get_doc("Offline Sync Item", item_name)
		self.assertEqual(item.status, "Success")
		encounter_name = item.fhir_id
		self._docs.append(("Mobile Encounter Context", encounter_name))
		self._docs.append(("Encounter", encounter_name))
		for obs_name in frappe.get_all("Observation", filters={"encounter": encounter_name}, pluck="name"):
			self._docs.append(("Observation", obs_name))
		case_name = frappe.db.get_value("Case", {"patient": patient.name}, "name")
		if case_name:
			self._docs.append(("Case", case_name))

	def test_still_failing_item_keeps_failed_status_and_updates_error_message(self):
		item_name = self._make_batch_with_item(
			entity_type="Assessment",
			reference_id=904,
			device_id="device-recon",
			payload={
				"referenceId": 904,
				"assessmentType": "NCD",
				"assessmentDetails": {"ncd": {}},
				"villageId": "0",
				"patientStatus": "Recovered",
				"encounter": {"memberId": "Patient-does-not-exist"},
			},
		)

		reconciliation.reconcile_failed_items()

		item = frappe.get_doc("Offline Sync Item", item_name)
		self.assertEqual(item.status, "Failed")
		self.assertIsNotNone(item.error_message)

	def test_old_failed_item_outside_window_is_not_touched(self):
		old_creation = frappe.utils.add_days(frappe.utils.now_datetime(), -30)
		item_name = self._make_batch_with_item(
			entity_type="Household",
			reference_id=905,
			device_id="device-recon",
			payload={"referenceId": 905, "name": "Too Old"},
			creation=old_creation,
		)

		reconciliation.reconcile_failed_items()

		item = frappe.get_doc("Offline Sync Item", item_name)
		self.assertEqual(item.status, "Failed")
		self.assertIsNone(item.fhir_id)

	def test_item_with_no_stored_payload_is_left_untouched(self):
		request_id = frappe.generate_hash(length=10)
		batch = frappe.get_doc(
			{"doctype": "Offline Sync Batch", "request_id": request_id, "device_id": "device-recon"}
		)
		batch.append(
			"sync_items",
			{
				"device_id": "device-recon",
				"entity_type": "FollowUp",
				"reference_id": "906",
				"status": "Failed",
				"error_message": "FollowUp sync not yet implemented on this backend.",
			},
		)
		batch.insert(ignore_permissions=True)
		self._batches.append(request_id)
		item_name = batch.sync_items[0].name

		reconciliation.reconcile_failed_items()

		item = frappe.get_doc("Offline Sync Item", item_name)
		self.assertEqual(item.status, "Failed")

	def test_successful_item_is_not_reprocessed(self):
		item_name = self._make_batch_with_item(
			entity_type="Household",
			reference_id=907,
			device_id="device-recon",
			payload={"referenceId": 907, "name": "Already Succeeded"},
			status="Success",
		)

		reconciliation.reconcile_failed_items()

		item = frappe.get_doc("Offline Sync Item", item_name)
		self.assertEqual(item.status, "Success")
		self.assertIsNone(item.fhir_id)  # never actually ran -- confirms it was skipped, not processed


if __name__ == "__main__":
	unittest.main()
