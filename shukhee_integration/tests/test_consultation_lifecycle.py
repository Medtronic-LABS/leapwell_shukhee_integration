"""
Lifecycle tests for the Shukhee teleconsultation flow: start_consultation ->
get_consultation_status (polled across a status change) -> get_prescription ->
download_document, exercised end to end against REAL Frappe DB records (a real
Provider, UHIS Shukhee User, and Call Logs row persist and evolve across the test),
mocking only the true external boundary -- the Shukhee (SSK) vendor's own HTTP
responses (requests.request / requests.get).

This is deliberately a different layer of coverage from test_consultation.py and
test_shukhee_client.py, which unit-test each function in isolation with everything
mocked. Here the point is the *sequence*: does the Call Logs record actually
transition pending -> accepted -> completed correctly across repeated polls, does
the vendor Bearer token really get reused instead of re-logging in on every call,
does a later step see exactly what an earlier step wrote, and are the permission/
state guards (wrong provider, premature prescription fetch) enforced across that
same real data.

Isolation, since this runs against the same live dev site a human may be poking at
via Desk in parallel:
  - Uses its own uniquely-named Provider / UHIS Shukhee User -- never touches a
    real operator's records (e.g. the "kakina_sk" UHIS Shukhee User used for
    manual testing).
  - Never reads or writes the global "Shukhee Settings" singleton -- every outbound
    call is mocked at the requests layer, so the configured base_url is never
    actually dialed and doesn't need to be touched.
  - start_consultation/get_consultation_status call frappe.db.commit() internally,
    so tearDown deletes everything it created for real (force=True + commit)
    rather than relying on the test-runner's rollback, which committed rows would
    survive.
"""

import base64
import json
import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration.api import consultation

start_consultation = None  # resolved in setUpModule, after remote-auth guard removal
get_consultation_status = None
get_prescription = None
download_document = None


def setUpModule():
	import inspect

	global start_consultation, get_consultation_status, get_prescription, download_document
	start_consultation = inspect.unwrap(consultation.start_consultation)
	get_consultation_status = inspect.unwrap(consultation.get_consultation_status)
	get_prescription = inspect.unwrap(consultation.get_prescription)
	download_document = inspect.unwrap(consultation.download_document)


class _LocalAttr:
	"""Sets a real attribute on frappe.local for the duration of a `with` block,
	then restores it. Not unittest.mock.patch("frappe.local", ...) -- that trips a
	Python 3.14 unittest.mock regression against Frappe's LocalProxy (see
	test_consultation.py's own copy of this helper for the full explanation)."""

	_MISSING = object()

	def __init__(self, name, value):
		self.name = name
		self.value = value

	def __enter__(self):
		self._previous = getattr(frappe.local, self.name, self._MISSING)
		setattr(frappe.local, self.name, self.value)
		return self.value

	def __exit__(self, *exc_info):
		if self._previous is self._MISSING:
			try:
				delattr(frappe.local, self.name)
			except AttributeError:
				pass
		else:
			setattr(frappe.local, self.name, self._previous)


class _FakeShukheeApi:
	"""Stands in for the real Shukhee (SSK) sandbox. Dispatches by URL suffix,
	matching the exact endpoints shukhee_client.py calls. Tracks every call so
	tests can assert on call counts (e.g. the vendor token is fetched once and
	reused, not re-fetched on every poll)."""

	def __init__(self):
		self.calls = []
		self.poll_count = 0

	@staticmethod
	def _resp(status_code, json_data):
		resp = MagicMock()
		resp.status_code = status_code
		resp.json.return_value = json_data
		if status_code >= 400:
			import requests

			resp.raise_for_status = MagicMock(side_effect=requests.HTTPError(response=resp))
		else:
			resp.raise_for_status = MagicMock()
		return resp

	def request(self, method, url, **kwargs):
		self.calls.append((method, url))

		if url.endswith("/ssk/login-v2"):
			return self._resp(
				200, {"success": True, "data": {"accessToken": "tok-access-1", "refreshToken": "tok-refresh-1"}}
			)
		if url.endswith("/ssk/patient-list"):
			return self._resp(200, {"data": []})
		if url.endswith("/v2/patient/person-patient-create"):
			return self._resp(200, {"data": {"id": "sk-patient-lifecycle-1"}})
		if url.endswith("/patient/emergency-request-specialities"):
			return self._resp(200, {"data": [{"specialityId": "spec-1", "title": "General Medicine"}]})
		if url.endswith("/ssk/emergency-request/v3"):
			return self._resp(200, {"success": True, "data": {"transactionId": "txn-lifecycle-1"}})
		if url.endswith("/ssk/all-emergency-request"):
			return self._resp(
				200,
				{
					"data": {
						"data": [{"id": "req-lifecycle-1", "createdAt": "2026-09-11T00:00:00Z"}],
						"pagination": {"totalItems": 1},
					}
				},
			)
		if "/patient/emergency-request/" in url:
			self.poll_count += 1
			doctor = {
				"name": "Dr. Lifecycle",
				"specialty": {"title": "General Medicine"},
				"working_at": "Test Clinic",
			}
			if self.poll_count == 1:
				return self._resp(200, {"data": {"status": "accepted", "doctor": doctor}})
			return self._resp(
				200,
				{
					"data": {
						"status": "completed",
						"doctor": doctor,
						"appointment": {
							"prescriptionLink": "https://vendor.example/presigned/rx.pdf",
							"invoiceLink": "https://vendor.example/presigned/inv.pdf",
						},
					}
				},
			)
		raise AssertionError(f"Unexpected Shukhee API call: {method} {url}")

	@staticmethod
	def _minimal_pdf(marker):
		# A genuinely parseable one-page PDF (not just bytes starting with the
		# magic number) -- Frappe's File.before_insert runs real PDF structure
		# validation (pypdf, scanning for embedded JS) on save, which rejects
		# anything pypdf can't parse -- including a missing/wrong xref table,
		# so the byte offsets below are computed exactly, not guessed. `marker`
		# is embedded as a PDF comment (ignored by parsers, preserved verbatim
		# in the bytes) so prescription vs invoice downloads stay distinguishable.
		header = f"%PDF-1.4\n%{marker}\n".encode("ascii")
		objects = [
			b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
			b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
			b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n",
		]
		body = bytearray(header)
		offsets = []
		for obj in objects:
			offsets.append(len(body))
			body += obj
		xref_start = len(body)
		xref = bytearray(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
		for offset in offsets:
			xref += f"{offset:010d} 00000 n \n".encode("ascii")
		trailer = (
			f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_start}\n%%EOF\n"
		).encode("ascii")
		return bytes(body) + bytes(xref) + trailer

	def get(self, url, **kwargs):
		self.calls.append(("GET-DOC", url))
		resp = MagicMock()
		resp.raise_for_status = MagicMock()
		resp.content = self._minimal_pdf("PRESCRIPTION" if "rx.pdf" in url else "INVOICE")
		return resp

	def login_call_count(self):
		return len([c for c in self.calls if c[1].endswith("/ssk/login-v2")])


class TestConsultationLifecycle(unittest.TestCase):
	PROVIDER_USERNAME = "test.lifecycle.provider@shukhee.test"
	OTHER_PROVIDER_USERNAME = "test.lifecycle.other-provider@shukhee.test"

	def setUp(self):
		self.provider = frappe.get_doc(
			{
				"doctype": "Provider",
				"username": self.PROVIDER_USERNAME,
				"full_name": "Lifecycle Test Provider",
			}
		).insert(ignore_permissions=True)

		self.shukhee_user = frappe.get_doc(
			{
				"doctype": "UHIS Shukhee User",
				"provider": self.provider.name,
				"shukhee_username": "lifecycle-test@shukhee.test",
				"shukhee_password": "not-a-real-password",
				"status": "Active",
			}
		).insert(ignore_permissions=True)

		self.other_provider = frappe.get_doc(
			{
				"doctype": "Provider",
				"username": self.OTHER_PROVIDER_USERNAME,
				"full_name": "Lifecycle Test Other Provider",
			}
		).insert(ignore_permissions=True)

		frappe.db.commit()

		self._api = _FakeShukheeApi()
		self._patchers = [
			patch("requests.request", side_effect=self._api.request),
			patch("requests.get", side_effect=self._api.get),
		]
		for p in self._patchers:
			p.start()

	def tearDown(self):
		for p in self._patchers:
			p.stop()

		frappe.cache().delete_value(f"shukhee_token:{self.shukhee_user.name}")

		call_log_names = frappe.get_all(
			"Call Logs", filters={"uhis_user": self.provider.name}, pluck="name"
		)
		for name in call_log_names:
			for file_name in frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Call Logs", "attached_to_name": name},
				pluck="name",
			):
				frappe.delete_doc("File", file_name, force=True, ignore_permissions=True)
			frappe.delete_doc("Call Logs", name, force=True, ignore_permissions=True)

		frappe.delete_doc("UHIS Shukhee User", self.shukhee_user.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Provider", self.provider.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Provider", self.other_provider.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _start_consultation(self, **form_overrides):
		form = {
			"contact_number": "01710000001",
			"reason": "Fever and headache",
			"requested_speciality": "General Medicine",
			# No local Patient record exists for these synthetic test numbers, and
			# the fake Shukhee patient-list always reports "not found" -- explicit
			# demographics are required in that case (see find_or_create_shukhee_patient).
			"patient_name": "Lifecycle Test Patient",
			"patient_dob": "1995-01-01",
			"patient_gender": "Female",
		}
		form.update(form_overrides)
		with (
			_LocalAttr("remote_user_id", self.PROVIDER_USERNAME),
			# frappe.local.form_dict must support attribute access (frappe._dict,
			# not a plain dict) -- unrelated core code elsewhere reads
			# frappe.local.form_dict.cmd during frappe.new_doc()'s default-value step.
			_LocalAttr("form_dict", frappe._dict(form)),
			_LocalAttr("request", MagicMock(files=None)),
		):
			return start_consultation()

	def _poll(self, call_log_name, as_provider=None):
		with _LocalAttr("remote_user_id", as_provider or self.PROVIDER_USERNAME):
			return get_consultation_status(payload=json.dumps({"call_log": call_log_name}))

	def test_full_lifecycle_booking_through_document_download(self):
		# ── Step 1: book ────────────────────────────────────────────────────
		booking = self._start_consultation()
		self.assertEqual(booking["status"], "pending")
		self.assertTrue(booking["call_url"].startswith("https://"))
		call_log_name = booking["call_log"]

		call_log = frappe.get_doc("Call Logs", call_log_name)
		self.assertEqual(call_log.status, "pending")
		self.assertEqual(call_log.patient_id, "sk-patient-lifecycle-1")
		self.assertEqual(call_log.request_id, "req-lifecycle-1")
		self.assertEqual(call_log.transaction_id, "txn-lifecycle-1")

		# ── Step 2: first poll -- doctor assigned before completion ────────
		status_1 = self._poll(call_log_name)
		self.assertEqual(status_1["status"], "accepted")
		self.assertEqual(status_1["doctor_name"], "Dr. Lifecycle")
		self.assertIsNone(status_1["prescription_link"])

		# ── Step 3: second poll -- completed, documents captured ───────────
		status_2 = self._poll(call_log_name)
		self.assertEqual(status_2["status"], "completed")
		self.assertIsNotNone(status_2["prescription_link"])
		self.assertIsNotNone(status_2["invoice_link"])
		self.assertNotIn("vendor.example", status_2["prescription_link"])  # re-hosted, not the vendor's own link

		# ── Step 4: further polls short-circuit -- no additional vendor call
		polls_so_far = self._api.poll_count
		status_3 = self._poll(call_log_name)
		self.assertEqual(self._api.poll_count, polls_so_far)
		self.assertEqual(status_3["status"], "completed")
		self.assertEqual(status_3["prescription_link"], status_2["prescription_link"])

		# ── Step 5: get_prescription reads back the same cached links ──────
		with _LocalAttr("remote_user_id", self.PROVIDER_USERNAME):
			prescription = get_prescription(payload=json.dumps({"call_log": call_log_name}))
		self.assertEqual(prescription["prescription_link"], status_2["prescription_link"])
		self.assertEqual(prescription["doctor_name"], "Dr. Lifecycle")

		# ── Step 6: download_document returns the real captured bytes ──────
		with _LocalAttr("remote_user_id", self.PROVIDER_USERNAME):
			doc = download_document(
				payload=json.dumps({"call_log": call_log_name, "doc_type": "prescription"})
			)
		self.assertIn(b"%PRESCRIPTION", base64.b64decode(doc["content_base64"]))

		with _LocalAttr("remote_user_id", self.PROVIDER_USERNAME):
			invoice_doc = download_document(
				payload=json.dumps({"call_log": call_log_name, "doc_type": "invoice"})
			)
		self.assertIn(b"%INVOICE", base64.b64decode(invoice_doc["content_base64"]))

		# ── Step 7: the vendor Bearer token was fetched once and reused ─────
		# across booking + 3 polls + prescription + 2 downloads (7 authenticated
		# calls in total), not re-logged-in on every request.
		self.assertEqual(self._api.login_call_count(), 1)

	def test_get_prescription_before_completion_raises(self):
		booking = self._start_consultation(contact_number="01710000002")
		with self.assertRaises(frappe.ValidationError):
			with _LocalAttr("remote_user_id", self.PROVIDER_USERNAME):
				get_prescription(payload=json.dumps({"call_log": booking["call_log"]}))

	def test_download_document_denies_a_different_provider(self):
		booking = self._start_consultation(contact_number="01710000003")
		call_log_name = booking["call_log"]
		self._poll(call_log_name)  # -> accepted
		self._poll(call_log_name)  # -> completed, prescription captured

		with self.assertRaises(frappe.PermissionError):
			with _LocalAttr("remote_user_id", self.OTHER_PROVIDER_USERNAME):
				download_document(
					payload=json.dumps({"call_log": call_log_name, "doc_type": "prescription"})
				)

	def test_terminal_status_is_never_re_polled_even_across_repeated_calls(self):
		booking = self._start_consultation(contact_number="01710000004")
		call_log_name = booking["call_log"]
		self._poll(call_log_name)  # -> accepted (poll_count=1)
		self._poll(call_log_name)  # -> completed (poll_count=2)

		for _ in range(5):
			self._poll(call_log_name)

		self.assertEqual(self._api.poll_count, 2)


if __name__ == "__main__":
	unittest.main()
