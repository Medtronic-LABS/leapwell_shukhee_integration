"""
Unit tests for shukhee_integration.api.consent.

Mirrors tests/test_consultation.py's style: inspect.unwrap(...) reaches the raw business
logic beneath frappe.whitelist's own argument-typing wrapper and @whitelist(remote_auth=True)'s
require_remote_auth guard. get_consent is a read-only config fetch (like
consultation.get_specialities), so unlike this app's call-lifecycle endpoints it is not
wrapped by audit_inbound -- there's nothing here for that decorator to log.

record_consent_decision IS a call-lifecycle/state-changing endpoint (mirrors
start_consultation), so it is wrapped by audit_inbound too -- inspect.unwrap walks through
both layers, same as test_consultation.py's endpoints.
"""

import inspect
import json
import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration.api import consent

get_consent = inspect.unwrap(consent.get_consent)
record_consent_decision = inspect.unwrap(consent.record_consent_decision)


class _LocalAttr:
	"""Sets a real attribute on frappe.local (e.g. form_dict) for the duration of a `with`
	block, then restores whatever was there before (or deletes it if it didn't exist).
	Deliberately NOT unittest.mock.patch -- see test_consultation.py's _LocalAttr docstring
	for why patching frappe.local trips a Python 3.14 unittest.mock regression."""

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


class TestResolveEnv(unittest.TestCase):

	def test_payload_string_is_parsed_as_json(self):
		result = consent._resolve_env('{"lng": "bn"}')
		self.assertEqual(result, {"lng": "bn"})

	def test_no_payload_falls_back_to_form_dict(self):
		with _LocalAttr("form_dict", {"lng": "en"}):
			result = consent._resolve_env(None)
		self.assertEqual(result, {"lng": "en"})


class TestGetConsent(unittest.TestCase):

	@patch("frappe.get_all")
	def test_exact_language_match_returns_that_row(self, mock_get_all):
		mock_get_all.return_value = [{"lng": "bn", "consent": "<p>bn consent</p>", "version": "2"}]

		result = get_consent(payload='{"lng": "bn"}')

		self.assertEqual(result, {"lng": "bn", "consent": "<p>bn consent</p>", "version": "2"})
		mock_get_all.assert_called_once_with(
			"Shukhee Consent", filters={"lng": "bn"}, fields=["lng", "consent", "version"], limit=1
		)

	@patch("frappe.get_all")
	def test_missing_language_falls_back_to_english(self, mock_get_all):
		mock_get_all.side_effect = [[], [{"lng": "en", "consent": "<p>en consent</p>", "version": "2"}]]

		result = get_consent(payload='{"lng": "bn"}')

		self.assertEqual(result, {"lng": "en", "consent": "<p>en consent</p>", "version": "2"})
		self.assertEqual(mock_get_all.call_count, 2)
		mock_get_all.assert_any_call(
			"Shukhee Consent", filters={"lng": "bn"}, fields=["lng", "consent", "version"], limit=1
		)
		mock_get_all.assert_any_call(
			"Shukhee Consent", filters={"lng": "en"}, fields=["lng", "consent", "version"], limit=1
		)

	@patch("frappe.get_all")
	def test_no_consent_configured_raises(self, mock_get_all):
		mock_get_all.return_value = []

		with self.assertRaises(frappe.DoesNotExistError):
			get_consent(payload='{"lng": "bn"}')

	@patch("frappe.get_all")
	def test_missing_lng_defaults_to_en(self, mock_get_all):
		mock_get_all.return_value = [{"lng": "en", "consent": "<p>en consent</p>", "version": "1"}]

		result = get_consent(payload="{}")

		self.assertEqual(result, {"lng": "en", "consent": "<p>en consent</p>", "version": "1"})
		mock_get_all.assert_called_once_with(
			"Shukhee Consent", filters={"lng": "en"}, fields=["lng", "consent", "version"], limit=1
		)


class TestRecordConsentDecision(unittest.TestCase):

	_VALID_ENV = {
		"patient_id": "PAT-1",
		"visit_id": "VISIT-1",
		"decision": "Agreed",
		"lng": "en",
		"consent_version": "2",
		"patient_dob": "1990-01-01",
	}

	@patch("shukhee_integration.api.consent._current_provider")
	def test_missing_patient_id_raises(self, mock_current_provider):
		env = dict(self._VALID_ENV)
		del env["patient_id"]
		with self.assertRaises(frappe.ValidationError):
			record_consent_decision(payload=json.dumps(env))
		mock_current_provider.assert_not_called()

	@patch("shukhee_integration.api.consent._current_provider")
	def test_missing_decision_raises(self, mock_current_provider):
		env = dict(self._VALID_ENV)
		del env["decision"]
		with self.assertRaises(frappe.ValidationError):
			record_consent_decision(payload=json.dumps(env))
		mock_current_provider.assert_not_called()

	@patch("shukhee_integration.api.consent._current_provider")
	def test_missing_lng_raises(self, mock_current_provider):
		env = dict(self._VALID_ENV)
		del env["lng"]
		with self.assertRaises(frappe.ValidationError):
			record_consent_decision(payload=json.dumps(env))
		mock_current_provider.assert_not_called()

	@patch("shukhee_integration.api.consent._current_provider")
	def test_invalid_decision_value_raises(self, mock_current_provider):
		env = dict(self._VALID_ENV, decision="Maybe")
		with self.assertRaises(frappe.ValidationError):
			record_consent_decision(payload=json.dumps(env))
		mock_current_provider.assert_not_called()

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consent._current_provider")
	def test_successful_insert_stamps_the_right_fields(
		self, mock_current_provider, mock_get_doc, mock_commit
	):
		mock_current_provider.return_value = "PROV-1"
		log_doc = MagicMock()
		mock_get_doc.return_value = log_doc

		result = record_consent_decision(payload=json.dumps(self._VALID_ENV))

		self.assertEqual(result, {"logged": True})
		log_doc.insert.assert_called_once_with(ignore_permissions=True)
		mock_commit.assert_called_once()

		(doc_dict,), _kwargs = mock_get_doc.call_args
		self.assertEqual(doc_dict["doctype"], "Shukhee Consent Log")
		self.assertEqual(doc_dict["patient_id"], "PAT-1")
		self.assertEqual(doc_dict["visit_id"], "VISIT-1")
		self.assertEqual(doc_dict["decision"], "Agreed")
		self.assertEqual(doc_dict["lng"], "en")
		self.assertEqual(doc_dict["consent_version"], "2")
		self.assertEqual(doc_dict["provider"], "PROV-1")
		self.assertEqual(doc_dict["patient_dob"], "1990-01-01")
		self.assertIn("occurred_at", doc_dict)

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consent._current_provider")
	def test_declined_decision_is_recorded(self, mock_current_provider, mock_get_doc, mock_commit):
		mock_current_provider.return_value = "PROV-1"
		mock_get_doc.return_value = MagicMock()

		result = record_consent_decision(payload=json.dumps(dict(self._VALID_ENV, decision="Declined")))

		self.assertEqual(result, {"logged": True})
		(doc_dict,), _kwargs = mock_get_doc.call_args
		self.assertEqual(doc_dict["decision"], "Declined")

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consent._current_provider")
	def test_provider_is_resolved_server_side_not_from_client_payload(
		self, mock_current_provider, mock_get_doc, mock_commit
	):
		"""A client-supplied `provider` field (if one were ever sent) must never be trusted --
		only _current_provider()'s current_remote_user_id() -> Provider.username resolution
		may set this field."""
		mock_current_provider.return_value = "PROV-REAL"
		mock_get_doc.return_value = MagicMock()

		env = dict(self._VALID_ENV, provider="PROV-SPOOFED")
		record_consent_decision(payload=json.dumps(env))

		mock_current_provider.assert_called_once()
		(doc_dict,), _kwargs = mock_get_doc.call_args
		self.assertEqual(doc_dict["provider"], "PROV-REAL")


if __name__ == "__main__":
	unittest.main()
