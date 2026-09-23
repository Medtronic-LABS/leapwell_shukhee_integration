"""
Tests for Call Logs' participation in spice_next_core.api.sync.pull: sync_seq
stamping on insert/update, geography_node denormalization from the linked
Patient's household (drives catchment filtering), catchment filtering itself,
and that clinical_data/medias serialize correctly through the generic
_serialize_change path (doc.as_dict()) -- exercised against REAL Frappe DB
records, the same "real DB, not mocks" layer as test_consultation_lifecycle.py,
since this is exactly the kind of cross-cutting (doctype metadata + hooks +
Postgres JSON auto-deserialize) behavior a mocked unit test would not catch.

Call Logs has no offline-authored push path (booking always requires a live
vendor round trip) -- push() never consults _SYNCABLE_DOCTYPES at all (confirmed
by reading sync.py), so there is deliberately no push-side test here.
"""

import json
import unittest
import uuid

import frappe

from spice_next_core.api.sync import _changes_since, _serialize_change


class TestCallLogsSync(unittest.TestCase):
	def setUp(self):
		suffix = uuid.uuid4().hex[:8]

		self.provider = frappe.get_doc(
			{
				"doctype": "Provider",
				"username": f"test.synclogs.provider.{suffix}@shukhee.test",
				"full_name": "Sync Test Provider",
			}
		).insert(ignore_permissions=True)

		self.geo_in = frappe.get_doc(
			{"doctype": "Geography Node", "name": f"Sync Test Ward In {suffix}", "label": f"Sync Test Ward In {suffix}"}
		).insert(ignore_permissions=True)
		self.geo_out = frappe.get_doc(
			{"doctype": "Geography Node", "name": f"Sync Test Ward Out {suffix}", "label": f"Sync Test Ward Out {suffix}"}
		).insert(ignore_permissions=True)

		self.household_in = frappe.get_doc(
			{
				"doctype": "Household",
				"client_uuid": f"hh-sync-in-{suffix}",
				"geography_node": self.geo_in.name,
			}
		).insert(ignore_permissions=True)
		self.household_out = frappe.get_doc(
			{
				"doctype": "Household",
				"client_uuid": f"hh-sync-out-{suffix}",
				"geography_node": self.geo_out.name,
			}
		).insert(ignore_permissions=True)

		self.patient_in = frappe.get_doc(
			{
				"doctype": "Patient",
				"client_uuid": f"pt-sync-in-{suffix}",
				"full_name": "Sync Test Patient In",
				"primary_household": self.household_in.name,
			}
		).insert(ignore_permissions=True)
		self.patient_out = frappe.get_doc(
			{
				"doctype": "Patient",
				"client_uuid": f"pt-sync-out-{suffix}",
				"full_name": "Sync Test Patient Out",
				"primary_household": self.household_out.name,
			}
		).insert(ignore_permissions=True)

		frappe.db.commit()
		self._call_logs = []

	def tearDown(self):
		for name in self._call_logs:
			frappe.delete_doc("Call Logs", name, force=True, ignore_permissions=True)
		frappe.delete_doc("Patient", self.patient_in.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Patient", self.patient_out.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Household", self.household_in.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Household", self.household_out.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Geography Node", self.geo_in.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Geography Node", self.geo_out.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Provider", self.provider.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _make_call_log(self, patient=None, **overrides):
		fields = {
			"doctype": "Call Logs",
			"uhis_user": self.provider.name,
			"contact_number": "01710000099",
			"reason": "Sync test call",
			"requested_speciality": "General Medicine",
			"call_type": "video",
			"status": "pending",
		}
		if patient:
			fields["patient"] = patient
		fields.update(overrides)
		doc = frappe.get_doc(fields).insert(ignore_permissions=True)
		frappe.db.commit()
		self._call_logs.append(doc.name)
		return doc

	def test_sync_seq_stamped_on_insert_and_advances_on_update(self):
		doc = self._make_call_log()
		self.assertIsInstance(doc.sync_seq, int)
		self.assertGreater(doc.sync_seq, 0)

		first_seq = doc.sync_seq
		doc.status = "accepted"
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		self.assertGreater(doc.sync_seq, first_seq)

	def test_call_log_appears_in_changes_since_and_cursor_excludes_it_after(self):
		doc = self._make_call_log()

		rows = _changes_since(cursor=doc.sync_seq - 1, catchment=None, limit=100)
		names = [r["name"] for r in rows if r["doctype"] == "Call Logs"]
		self.assertIn(doc.name, names)

		rows_after = _changes_since(cursor=doc.sync_seq, catchment=None, limit=100)
		names_after = [r["name"] for r in rows_after if r["doctype"] == "Call Logs"]
		self.assertNotIn(doc.name, names_after)

	def test_geography_node_denormalized_from_patient_household(self):
		doc = self._make_call_log(patient=self.patient_in.name)
		self.assertEqual(doc.geography_node, self.geo_in.name)

	def test_no_patient_means_no_geography_node(self):
		doc = self._make_call_log()
		self.assertFalse(doc.geography_node)

	def test_catchment_filtering_excludes_out_of_catchment_rows(self):
		doc_in = self._make_call_log(patient=self.patient_in.name)
		doc_out = self._make_call_log(patient=self.patient_out.name)

		cursor = min(doc_in.sync_seq, doc_out.sync_seq) - 1
		rows = _changes_since(cursor=cursor, catchment={self.geo_in.name}, limit=100)
		names = [r["name"] for r in rows if r["doctype"] == "Call Logs"]

		self.assertIn(doc_in.name, names)
		self.assertNotIn(doc_out.name, names)

	def test_clinical_data_always_serializes_as_a_json_string_via_as_dict(self):
		"""Real, verified behavior (not an assumption): frappe.get_doc(...).clinical_data
		is a dict, thanks to Postgres/psycopg2 auto-deserializing the `json`-typed
		column on SELECT -- but BaseDocument.as_dict() (which _serialize_change calls)
		has its own explicit normalization for fieldtype == "JSON"
		(frappe/model/base_document.py: `elif fieldtype == "JSON" and isinstance(value,
		dict): value = json.dumps(value, ...)`), which re-encodes it back into a
		compact JSON string every time -- deliberately papering over the exact
		cross-backend inconsistency this app's own clinical_data field description
		warns about (MariaDB would never auto-deserialize in the first place). Net
		effect: the wire contract for Call Logs.clinical_data through sync.pull is
		ALWAYS a JSON string, never a bare nested object -- mobile must jsonDecode()
		it exactly once, matching the write-json.dumps/read-verbatim asymmetry
		established elsewhere in this codebase. This test guards that contract: if a
		future Frappe upgrade or a well-meaning "fix" to _serialize_change ever makes
		this come back as a bare dict instead, this test will catch the contract
		change rather than mobile silently receiving a shape it doesn't expect."""
		doc = self._make_call_log()
		clinical_data = {"chiefComplaints": ["Fever"], "medicine": [{"name": "Paracetamol"}]}
		# Write side: json.dumps() via frappe.db.set_value, matching the
		# established asymmetric write/read contract (see call_logs.json's
		# clinical_data field description).
		frappe.db.set_value("Call Logs", doc.name, "clinical_data", json.dumps(clinical_data))
		frappe.db.commit()

		change = _serialize_change({"doctype": "Call Logs", "name": doc.name, "sync_seq": doc.sync_seq})

		self.assertIsInstance(change["doc"]["clinical_data"], str)
		self.assertEqual(json.loads(change["doc"]["clinical_data"]), clinical_data)

	def test_medias_child_rows_appear_in_serialized_change(self):
		doc = self._make_call_log()
		doc.append("medias", {"type": "prescription", "file": "/files/test.pdf", "shukhee_document_id": "doc-1"})
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		change = _serialize_change({"doctype": "Call Logs", "name": doc.name, "sync_seq": doc.sync_seq})

		medias = change["doc"]["medias"]
		self.assertEqual(len(medias), 1)
		self.assertEqual(medias[0]["type"], "prescription")
		self.assertEqual(medias[0]["file"], "/files/test.pdf")
