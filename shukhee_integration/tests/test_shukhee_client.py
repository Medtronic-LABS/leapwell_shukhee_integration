"""
Unit tests for shukhee_integration.shukhee_client — the low-level HTTP client for the
external Shukhee (SSK) teleconsultation API. All external calls are mocked; nothing here
hits a real Shukhee sandbox or requires network.

Also guards the spice_next_core rename: this module's own dependency surface is limited to
frappe/requests, but shukhee_client and api/consultation together are this app's only real
integration point with spice_next_core (see test_consultation.py), so keeping this suite
green after any future rename is part of proving the integration wasn't broken.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration import shukhee_client
from shukhee_integration.shukhee_client import ShukheeAuthExpired


def _settings_doc(base_url="https://shukhee.test", video_call_base_url="https://video.shukhee.test"):
	doc = MagicMock()
	doc.base_url = base_url
	doc.video_call_base_url = video_call_base_url
	return doc


def _shukhee_user_doc(name="SU-1", username="sk1@shukhee.test", password="secret"):
	doc = MagicMock()
	doc.name = name
	doc.shukhee_username = username
	doc.get_password.return_value = password
	return doc


class TestDecodeJwtExp(unittest.TestCase):

	def _token(self, payload_b64):
		return f"eyJhbGciOiJub25lIn0.{payload_b64}."

	def test_valid_exp_claim_returns_ms(self):
		import base64
		import json

		claims = base64.urlsafe_b64encode(json.dumps({"exp": 1700000000}).encode()).decode().rstrip("=")
		result = shukhee_client._decode_jwt_exp_ms(self._token(claims))
		self.assertEqual(result, 1700000000 * 1000)

	def test_missing_exp_claim_returns_none(self):
		import base64
		import json

		claims = base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).decode().rstrip("=")
		self.assertIsNone(shukhee_client._decode_jwt_exp_ms(self._token(claims)))

	def test_malformed_token_returns_none(self):
		self.assertIsNone(shukhee_client._decode_jwt_exp_ms("not-a-jwt"))

	def test_empty_string_returns_none(self):
		self.assertIsNone(shukhee_client._decode_jwt_exp_ms(""))


class TestExtractList(unittest.TestCase):

	def test_data_is_direct_array(self):
		self.assertEqual(shukhee_client._extract_list({"data": [1, 2, 3]}), [1, 2, 3])

	def test_data_is_nested_wrapper(self):
		self.assertEqual(shukhee_client._extract_list({"data": {"data": [1, 2]}}), [1, 2])

	def test_nested_wrapper_missing_inner_data_returns_empty(self):
		self.assertEqual(shukhee_client._extract_list({"data": {}}), [])

	def test_no_data_key_returns_empty(self):
		self.assertEqual(shukhee_client._extract_list({}), [])

	def test_data_is_none_returns_empty(self):
		self.assertEqual(shukhee_client._extract_list({"data": None}), [])


class TestMapGender(unittest.TestCase):

	def test_male_passthrough(self):
		self.assertEqual(shukhee_client._map_gender("Male"), "Male")

	def test_female_passthrough(self):
		self.assertEqual(shukhee_client._map_gender("Female"), "Female")

	def test_other_maps_to_others(self):
		self.assertEqual(shukhee_client._map_gender("Other"), "Others")

	def test_prefer_not_to_say_maps_to_others(self):
		self.assertEqual(shukhee_client._map_gender("Prefer not to say"), "Others")

	def test_none_maps_to_others(self):
		self.assertEqual(shukhee_client._map_gender(None), "Others")


class TestBuildVideoCallUrl(unittest.TestCase):

	@patch("shukhee_integration.shukhee_client._settings")
	def test_url_shape_and_params(self, mock_settings):
		mock_settings.return_value = _settings_doc()
		url = shukhee_client.build_video_call_url("tok-123", "req-456")
		self.assertEqual(
			url,
			"https://video.shukhee.test/video-call/third-party"
			"?token=tok-123&id=req-456&consultation_type=instant-call&auto_join=true",
		)

	@patch("shukhee_integration.shukhee_client._settings")
	def test_trailing_slash_on_base_is_stripped(self, mock_settings):
		mock_settings.return_value = _settings_doc(video_call_base_url="https://video.shukhee.test/")
		url = shukhee_client.build_video_call_url("t", "r")
		self.assertTrue(url.startswith("https://video.shukhee.test/video-call/third-party"))
		self.assertNotIn("test//video-call", url)


class TestGetValidToken(unittest.TestCase):

	def setUp(self):
		self.doc = _shukhee_user_doc()
		self.store = {}
		patcher = patch("frappe.cache")
		self.mock_cache = patcher.start()
		self.addCleanup(patcher.stop)
		self.mock_cache.return_value.get_value.side_effect = self.store.get
		self.mock_cache.return_value.set_value.side_effect = (
			lambda k, v, expires_in_sec=None: self.store.__setitem__(k, v)
		)
		self.mock_cache.return_value.delete_value.side_effect = self.store.pop

	@patch("shukhee_integration.shukhee_client.login")
	def test_no_cache_falls_back_to_login(self, mock_login):
		mock_login.return_value = {"accessToken": "at-1", "refreshToken": "rt-1"}
		token = shukhee_client.get_valid_token(self.doc)
		self.assertEqual(token, "at-1")
		mock_login.assert_called_once_with("sk1@shukhee.test", "secret")

	@patch("shukhee_integration.shukhee_client.login")
	def test_unexpired_cached_token_skips_login(self, mock_login):
		import time

		self.store["shukhee_token:SU-1"] = {
			"access_token": "cached-at",
			"refresh_token": "cached-rt",
			"expires_at": (time.time() * 1000) + 600_000,
		}
		token = shukhee_client.get_valid_token(self.doc)
		self.assertEqual(token, "cached-at")
		mock_login.assert_not_called()

	@patch("shukhee_integration.shukhee_client.login")
	@patch("shukhee_integration.shukhee_client.refresh")
	def test_expired_cached_token_uses_refresh_not_login(self, mock_refresh, mock_login):
		import time

		self.store["shukhee_token:SU-1"] = {
			"access_token": "old-at",
			"refresh_token": "old-rt",
			"expires_at": (time.time() * 1000) - 1000,
		}
		mock_refresh.return_value = {"accessToken": "new-at", "refreshToken": "new-rt"}
		token = shukhee_client.get_valid_token(self.doc)
		self.assertEqual(token, "new-at")
		mock_refresh.assert_called_once_with("old-rt")
		mock_login.assert_not_called()

	@patch("shukhee_integration.shukhee_client.login")
	@patch("shukhee_integration.shukhee_client.refresh")
	def test_refresh_failure_falls_back_to_login(self, mock_refresh, mock_login):
		import time

		self.store["shukhee_token:SU-1"] = {
			"access_token": "old-at",
			"refresh_token": "old-rt",
			"expires_at": (time.time() * 1000) - 1000,
		}
		mock_refresh.side_effect = Exception("refresh rejected")
		mock_login.return_value = {"accessToken": "fresh-at", "refreshToken": "fresh-rt"}
		token = shukhee_client.get_valid_token(self.doc)
		self.assertEqual(token, "fresh-at")
		mock_login.assert_called_once()


class TestAuthedRequest(unittest.TestCase):

	@patch("shukhee_integration.shukhee_client.get_valid_token")
	@patch("shukhee_integration.shukhee_client._request")
	def test_success_on_first_try_no_retry(self, mock_request, mock_token):
		mock_token.return_value = "tok"
		mock_request.return_value = {"success": True}
		result = shukhee_client._authed_request("GET", "https://x/y", _shukhee_user_doc())
		self.assertEqual(result, {"success": True})
		mock_request.assert_called_once()

	@patch("frappe.cache")
	@patch("shukhee_integration.shukhee_client.get_valid_token")
	@patch("shukhee_integration.shukhee_client._request")
	def test_401_invalidates_cache_and_retries_once(self, mock_request, mock_token, mock_cache):
		mock_token.side_effect = ["stale-tok", "fresh-tok"]
		mock_request.side_effect = [ShukheeAuthExpired(), {"success": True}]

		doc = _shukhee_user_doc()
		result = shukhee_client._authed_request("GET", "https://x/y", doc)

		self.assertEqual(result, {"success": True})
		self.assertEqual(mock_request.call_count, 2)
		mock_cache.return_value.delete_value.assert_called_once_with("shukhee_token:SU-1")
		# the retry must not itself opt into raise_on_401 (no infinite-retry loop)
		_, retry_kwargs = mock_request.call_args
		self.assertNotIn("raise_on_401", retry_kwargs)


class TestResolveSpecialityId(unittest.TestCase):

	@patch("shukhee_integration.shukhee_client.list_specialities")
	def test_case_insensitive_exact_match(self, mock_list):
		mock_list.return_value = [
			{"specialityId": "1", "title": "General Medicine"},
			{"specialityId": "2", "title": "Pediatrics"},
		]
		speciality_id, title = shukhee_client.resolve_speciality_id(MagicMock(), "pediatrics")
		self.assertEqual((speciality_id, title), ("2", "Pediatrics"))

	@patch("shukhee_integration.shukhee_client.list_specialities")
	def test_no_match_falls_back_to_first(self, mock_list):
		mock_list.return_value = [
			{"specialityId": "1", "title": "General Medicine"},
			{"specialityId": "2", "title": "Pediatrics"},
		]
		speciality_id, title = shukhee_client.resolve_speciality_id(MagicMock(), "Cardiology")
		self.assertEqual((speciality_id, title), ("1", "General Medicine"))

	@patch("shukhee_integration.shukhee_client.list_specialities")
	def test_empty_list_raises(self, mock_list):
		mock_list.return_value = []
		with self.assertRaises(frappe.ValidationError):
			shukhee_client.resolve_speciality_id(MagicMock(), "anything")


class TestResolveRequestId(unittest.TestCase):
	"""Covers the pagination fix: /ssk/all-emergency-request returns oldest-first and
	paginates, so the newest record for a contact number can land on a later page —
	resolve_request_id must fetch the real last page (via pagination.totalItems), not
	trust page 0 alone."""

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_single_page_picks_newest_by_created_at(self, mock_authed):
		mock_authed.return_value = {
			"data": {
				"data": [
					{"id": "r1", "createdAt": "2026-01-01T00:00:00Z"},
					{"id": "r2", "createdAt": "2026-01-02T00:00:00Z"},
				],
				"pagination": {"totalItems": 2},
			}
		}
		result = shukhee_client.resolve_request_id(MagicMock(), "01410820112")
		self.assertEqual(result, "r2")
		mock_authed.assert_called_once()

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_multi_page_fetches_true_last_page_not_page_zero(self, mock_authed):
		# totalItems=15, size=10 -> last_page = (15-1)//10 = 1. Page 0 (oldest 10) does
		# not contain the newest record; only the real last page does.
		page_0 = {
			"data": {
				"data": [{"id": f"old-{i}", "createdAt": f"2026-01-{i:02d}T00:00:00Z"} for i in range(1, 11)],
				"pagination": {"totalItems": 15},
			}
		}
		page_1 = {
			"data": {
				"data": [
					{"id": "newest", "createdAt": "2026-01-20T00:00:00Z"},
					{"id": "r12", "createdAt": "2026-01-15T00:00:00Z"},
				],
				"pagination": {"totalItems": 15},
			}
		}
		mock_authed.side_effect = [page_0, page_1]
		result = shukhee_client.resolve_request_id(MagicMock(), "01410820112")
		self.assertEqual(result, "newest")
		self.assertEqual(mock_authed.call_count, 2)
		_, second_kwargs = mock_authed.call_args
		self.assertEqual(second_kwargs["params"]["page"], 1)

	@patch("time.sleep", lambda *_: None)
	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_no_match_retries_then_throws(self, mock_authed):
		empty = {"data": {"data": [], "pagination": {"totalItems": 0}}}
		mock_authed.return_value = empty
		with self.assertRaises(frappe.ValidationError):
			shukhee_client.resolve_request_id(MagicMock(), "01410820112", attempts=2, delay_s=0)
		self.assertEqual(mock_authed.call_count, 2)

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_last_10_digits_used_as_search_term(self, mock_authed):
		mock_authed.return_value = {"data": {"data": [{"id": "r1", "createdAt": "x"}], "pagination": {"totalItems": 1}}}
		shukhee_client.resolve_request_id(MagicMock(), "+8801410820112")
		_, kwargs = mock_authed.call_args
		self.assertEqual(kwargs["params"]["search"], "1410820112")


class TestFindOrCreateShukheePatient(unittest.TestCase):

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_existing_shukhee_patient_short_circuits(self, mock_authed):
		mock_authed.return_value = {
			"data": [{"id": "sk-p1", "name": "Jane Doe", "dob": "1990-01-01T00:00:00", "gender": "Female"}]
		}
		patient_id, details = shukhee_client.find_or_create_shukhee_patient(
			MagicMock(), "01410820112"
		)
		self.assertEqual(patient_id, "sk-p1")
		self.assertEqual(details["fullName"], "Jane Doe")
		self.assertEqual(details["gender"], "Female")
		mock_authed.assert_called_once()  # only the lookup call — no create call

	@patch("shukhee_integration.shukhee_client._find_uhis_patient")
	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_new_patient_uses_local_uhis_record_when_available(self, mock_authed, mock_find_uhis):
		mock_authed.side_effect = [
			{"data": []},  # initial lookup: not found on Shukhee
			{"data": {"id": "sk-p2"}},  # create call
		]
		mock_find_uhis.return_value = MagicMock(full_name="Local Patient", dob="1985-05-05", gender="Male")
		patient_id, details = shukhee_client.find_or_create_shukhee_patient(
			MagicMock(), "01711111111"
		)
		self.assertEqual(patient_id, "sk-p2")
		self.assertEqual(details["fullName"], "Local Patient")
		self.assertEqual(details["gender"], "Male")

	@patch("shukhee_integration.shukhee_client._find_uhis_patient")
	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_new_patient_uses_explicit_args_when_no_local_record(self, mock_authed, mock_find_uhis):
		mock_authed.side_effect = [{"data": []}, {"data": {"id": "sk-p3"}}]
		mock_find_uhis.return_value = None
		patient_id, details = shukhee_client.find_or_create_shukhee_patient(
			MagicMock(), "01722222222", patient_name="New Patient", patient_dob="2000-01-01", patient_gender="Other"
		)
		self.assertEqual(patient_id, "sk-p3")
		self.assertEqual(details["fullName"], "New Patient")
		self.assertEqual(details["gender"], "Others")

	@patch("shukhee_integration.shukhee_client._find_uhis_patient")
	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_no_local_record_and_no_explicit_args_raises(self, mock_authed, mock_find_uhis):
		mock_authed.return_value = {"data": []}
		mock_find_uhis.return_value = None
		with self.assertRaises(frappe.DoesNotExistError):
			shukhee_client.find_or_create_shukhee_patient(MagicMock(), "01733333333")

	@patch("shukhee_integration.shukhee_client._find_uhis_patient")
	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_create_response_missing_id_falls_back_to_relist(self, mock_authed, mock_find_uhis):
		mock_authed.side_effect = [
			{"data": []},  # initial lookup
			{"data": {}},  # create call: no id in response (documented sandbox quirk)
			{"data": [{"id": "sk-p4"}]},  # relist recovery
		]
		mock_find_uhis.return_value = MagicMock(full_name="Recovered Patient", dob="1970-01-01", gender="Female")
		patient_id, _ = shukhee_client.find_or_create_shukhee_patient(MagicMock(), "01744444444")
		self.assertEqual(patient_id, "sk-p4")
		self.assertEqual(mock_authed.call_count, 3)

	@patch("shukhee_integration.shukhee_client._find_uhis_patient")
	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_create_and_relist_both_fail_raises(self, mock_authed, mock_find_uhis):
		mock_authed.side_effect = [{"data": []}, {"data": {}}, {"data": []}]
		mock_find_uhis.return_value = MagicMock(full_name="X", dob="1970-01-01", gender="Female")
		with self.assertRaises(frappe.ValidationError):
			shukhee_client.find_or_create_shukhee_patient(MagicMock(), "01755555555")


class TestBookInstantCall(unittest.TestCase):

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_success_returns_transaction_id(self, mock_authed):
		mock_authed.return_value = {"success": True, "data": {"transactionId": "txn-1"}}
		result = shukhee_client.book_instant_call(
			MagicMock(),
			shukhee_patient_id="sk-p1",
			patient_details={"fullName": "Jane"},
			contact_number="01410820112",
			reason="fever",
			speciality_id="1",
			requested_speciality="General Medicine",
		)
		self.assertEqual(result, "txn-1")

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_success_false_raises(self, mock_authed):
		mock_authed.return_value = {"success": False}
		with self.assertRaises(frappe.ValidationError):
			shukhee_client.book_instant_call(
				MagicMock(),
				shukhee_patient_id="sk-p1",
				patient_details={"fullName": "Jane"},
				contact_number="01410820112",
				reason="fever",
				speciality_id="1",
				requested_speciality="General Medicine",
			)

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_medical_document_ids_joined_as_csv(self, mock_authed):
		mock_authed.return_value = {"success": True, "data": {"transactionId": "txn-2"}}
		shukhee_client.book_instant_call(
			MagicMock(),
			shukhee_patient_id="sk-p1",
			patient_details={"fullName": "Jane"},
			contact_number="01410820112",
			reason="fever",
			speciality_id="1",
			requested_speciality="General Medicine",
			medical_document_ids=[10, 11],
		)
		_, kwargs = mock_authed.call_args
		self.assertEqual(kwargs["data"]["medicalDocumentIds"], "10,11")

	@patch("shukhee_integration.shukhee_client._authed_request")
	def test_no_medical_document_ids_omits_field(self, mock_authed):
		mock_authed.return_value = {"success": True, "data": {"transactionId": "txn-3"}}
		shukhee_client.book_instant_call(
			MagicMock(),
			shukhee_patient_id="sk-p1",
			patient_details={"fullName": "Jane"},
			contact_number="01410820112",
			reason="fever",
			speciality_id="1",
			requested_speciality="General Medicine",
		)
		_, kwargs = mock_authed.call_args
		self.assertNotIn("medicalDocumentIds", kwargs["data"])


class TestDownloadAndAttach(unittest.TestCase):

	@patch("frappe.utils.file_manager.save_file")
	@patch("requests.get")
	def test_success_returns_file_url(self, mock_get, mock_save_file):
		mock_get.return_value = MagicMock(content=b"pdf-bytes")
		mock_get.return_value.raise_for_status = MagicMock()
		mock_save_file.return_value = MagicMock(file_url="/files/CL-1-prescription.pdf")

		result = shukhee_client.download_and_attach("Call Logs", "CL-1", "prescription", "https://x/doc.pdf")

		self.assertEqual(result, "/files/CL-1-prescription.pdf")
		mock_save_file.assert_called_once_with(
			"CL-1-prescription.pdf", b"pdf-bytes", "Call Logs", "CL-1", is_private=1
		)

	@patch("requests.get")
	def test_download_failure_raises_validation_error(self, mock_get):
		import requests as requests_module

		mock_get.side_effect = requests_module.RequestException("timeout")
		with self.assertRaises(frappe.ValidationError):
			shukhee_client.download_and_attach("Call Logs", "CL-1", "invoice", "https://x/doc.pdf")


if __name__ == "__main__":
	unittest.main()
