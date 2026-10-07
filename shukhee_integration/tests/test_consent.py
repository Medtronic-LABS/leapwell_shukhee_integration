"""
Unit tests for shukhee_integration.api.consent.

Mirrors tests/test_consultation.py's style: inspect.unwrap(...) reaches the raw business
logic beneath frappe.whitelist's own argument-typing wrapper and @whitelist(remote_auth=True)'s
require_remote_auth guard. get_consent is a read-only config fetch (like
consultation.get_specialities), so unlike this app's call-lifecycle endpoints it is not
wrapped by audit_inbound -- there's nothing here for that decorator to log.

record_consent_decline IS a call-lifecycle/state-changing endpoint (mirrors
start_consultation), so it is wrapped by audit_inbound too -- inspect.unwrap walks through
both layers, same as test_consultation.py's endpoints.

An Agreed decision is tested where it's actually recorded now: see test_consultation.py's
own coverage of start_consultation's consent_version/consent_lng/consent_items/
consent_filled_text fields on the inserted Call Logs row.
"""

import inspect
import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration.api import consent

_FIXED_NOW = datetime(2026, 1, 1, 12, 0)

get_consent = inspect.unwrap(consent.get_consent)
record_consent_decline = inspect.unwrap(consent.record_consent_decline)


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


class TestConsentItems(unittest.TestCase):

	@patch("frappe.get_all")
	def test_filters_by_both_parenttype_and_parent(self, mock_get_all):
		"""Shukhee Consent and Shukhee Consent Version both autoincrement their own `name`
		independently, so their ids collide -- a parent-only filter would silently blend rows
		from the wrong parent doctype."""
		mock_get_all.return_value = [{"description": "x", "mandatory": 1}]

		result = consent._consent_items("Shukhee Consent Version", "3")

		self.assertEqual(result, [{"description": "x", "mandatory": 1}])
		mock_get_all.assert_called_once_with(
			"Shukhee Consent Item",
			filters={"parenttype": "Shukhee Consent Version", "parent": "3"},
			fields=["description", "mandatory"],
			order_by="idx asc",
		)


class TestResolveVersionId(unittest.TestCase):

	@patch("frappe.db.exists")
	def test_returns_id_when_valid(self, mock_exists):
		mock_exists.return_value = True

		result = consent.resolve_version_id({"version_id": "VER-1"})

		self.assertEqual(result, "VER-1")
		mock_exists.assert_called_once_with("Shukhee Consent Version", "VER-1")

	@patch("frappe.db.exists")
	def test_returns_none_when_unknown(self, mock_exists):
		mock_exists.return_value = False

		self.assertIsNone(consent.resolve_version_id({"version_id": "VER-missing"}))

	def test_returns_none_when_missing(self):
		self.assertIsNone(consent.resolve_version_id({}))


class TestResolveAuthoritativeItems(unittest.TestCase):

	@patch("shukhee_integration.api.consent._consent_items")
	def test_uses_version_items_when_version_id_given(self, mock_consent_items):
		mock_consent_items.return_value = [{"description": "v", "mandatory": 1}]

		result = consent.resolve_authoritative_items("VER-1", "en")

		self.assertEqual(result, [{"description": "v", "mandatory": 1}])
		mock_consent_items.assert_called_once_with("Shukhee Consent Version", "VER-1")

	@patch("shukhee_integration.api.consent._consent_items")
	@patch("frappe.get_all")
	def test_falls_back_to_live_consent_items_when_no_version_id(self, mock_get_all, mock_consent_items):
		mock_get_all.return_value = [{"name": "1"}]
		mock_consent_items.return_value = [{"description": "live", "mandatory": 0}]

		result = consent.resolve_authoritative_items(None, "en")

		self.assertEqual(result, [{"description": "live", "mandatory": 0}])
		mock_get_all.assert_called_once_with(
			"Shukhee Consent", filters={"lng": "en"}, fields=["name"], limit=1
		)
		mock_consent_items.assert_called_once_with("Shukhee Consent", "1")

	@patch("frappe.get_all")
	def test_returns_empty_list_when_no_live_row_and_no_version_id(self, mock_get_all):
		mock_get_all.return_value = []

		result = consent.resolve_authoritative_items(None, "en")

		self.assertEqual(result, [])


class TestResolveProviderName(unittest.TestCase):

	def test_returns_none_when_no_provider(self):
		self.assertIsNone(consent.resolve_provider_name(None, "en"))

	@patch("frappe.db.get_value")
	def test_returns_full_name(self, mock_get_value):
		mock_get_value.return_value = "Dr. Rahman"

		result = consent.resolve_provider_name("PROV-1", "en")

		self.assertEqual(result, "Dr. Rahman")
		mock_get_value.assert_called_once_with("Provider", "PROV-1", "full_name")


class TestResolveBaseConsentHtml(unittest.TestCase):

	@patch("frappe.db.get_value")
	def test_uses_version_html_when_version_id_given(self, mock_get_value):
		mock_get_value.return_value = "<p>v2</p>"

		result = consent.resolve_base_consent_html("VER-1", "en")

		self.assertEqual(result, "<p>v2</p>")
		mock_get_value.assert_called_once_with("Shukhee Consent Version", "VER-1", "consent")

	@patch("frappe.get_all")
	def test_falls_back_to_live_row_when_no_version_id(self, mock_get_all):
		mock_get_all.return_value = [{"consent": "<p>live</p>"}]

		result = consent.resolve_base_consent_html(None, "en")

		self.assertEqual(result, "<p>live</p>")

	@patch("frappe.get_all")
	def test_returns_none_when_no_live_row_and_no_version_id(self, mock_get_all):
		mock_get_all.return_value = []

		self.assertIsNone(consent.resolve_base_consent_html(None, "en"))


class TestResolveConsentVersionLabel(unittest.TestCase):

	def test_returns_none_when_no_version_id(self):
		self.assertIsNone(consent.resolve_consent_version_label(None))

	@patch("frappe.db.get_value")
	def test_returns_version_label(self, mock_get_value):
		mock_get_value.return_value = "3"

		self.assertEqual(consent.resolve_consent_version_label("VER-1"), "3")


class TestFillConsentTemplate(unittest.TestCase):

	def test_substitutes_known_tokens(self):
		html = "<p>{{participant_name}} ({{participant_id}})</p>"

		result = consent.fill_consent_template(
			html, {"participant_name": "Jane", "participant_id": "P-1"}
		)

		self.assertEqual(result, "<p>Jane (P-1)</p>")

	def test_missing_token_value_substitutes_empty_string(self):
		html = "<p>{{chw_name}}</p>"

		result = consent.fill_consent_template(html, {"chw_name": None})

		self.assertEqual(result, "<p></p>")

	def test_none_html_returns_none(self):
		self.assertIsNone(consent.fill_consent_template(None, {"x": "y"}))

	def test_leaves_unrecognized_placeholders_untouched(self):
		html = "<p>{{unknown_token}}</p>"

		result = consent.fill_consent_template(html, {"participant_name": "Jane"})

		self.assertEqual(result, "<p>{{unknown_token}}</p>")


_CONSENT_FIELDS = ["name", "lng", "consent", "version", "current_version"]


class TestGetConsent(unittest.TestCase):

	@patch("frappe.get_all")
	def test_exact_language_match_returns_that_row_with_items(self, mock_get_all):
		mock_get_all.side_effect = [
			[{"name": "1", "lng": "bn", "consent": "<p>bn consent</p>", "version": "2", "current_version": "4"}],
			[{"description": "mandatory item", "mandatory": 1}, {"description": "optional item", "mandatory": 0}],
		]

		result = get_consent(payload='{"lng": "bn"}')

		self.assertEqual(
			result,
			{
				"lng": "bn",
				"consent": "<p>bn consent</p>",
				"version": "2",
				"version_id": "4",
				"items": [
					{"description": "mandatory item", "mandatory": True},
					{"description": "optional item", "mandatory": False},
				],
			},
		)
		mock_get_all.assert_any_call(
			"Shukhee Consent", filters={"lng": "bn"}, fields=_CONSENT_FIELDS, limit=1
		)
		mock_get_all.assert_any_call(
			"Shukhee Consent Item",
			filters={"parenttype": "Shukhee Consent", "parent": "1"},
			fields=["description", "mandatory"],
			order_by="idx asc",
		)

	@patch("frappe.get_all")
	def test_missing_language_falls_back_to_english(self, mock_get_all):
		mock_get_all.side_effect = [
			[],
			[{"name": "1", "lng": "en", "consent": "<p>en consent</p>", "version": "2", "current_version": "3"}],
			[],
		]

		result = get_consent(payload='{"lng": "bn"}')

		self.assertEqual(result["lng"], "en")
		self.assertEqual(result["items"], [])

	@patch("frappe.get_all")
	def test_no_consent_configured_raises(self, mock_get_all):
		mock_get_all.return_value = []

		with self.assertRaises(frappe.DoesNotExistError):
			get_consent(payload='{"lng": "bn"}')

	@patch("frappe.get_all")
	def test_missing_lng_defaults_to_en(self, mock_get_all):
		mock_get_all.side_effect = [
			[{"name": "1", "lng": "en", "consent": "<p>en consent</p>", "version": "1", "current_version": "3"}],
			[],
		]

		result = get_consent(payload="{}")

		self.assertEqual(result["lng"], "en")
		mock_get_all.assert_any_call(
			"Shukhee Consent", filters={"lng": "en"}, fields=_CONSENT_FIELDS, limit=1
		)


class TestRecordConsentDecline(unittest.TestCase):
	"""The only server-side record a Decline gets -- see this module's own docstring for why
	an Agreed decision isn't recorded by anything in this file at all (it's embedded directly
	onto the Call Logs row api.consultation.start_consultation creates)."""

	_VALID_ENV = {
		"patient_id": "PAT-1",
		"visit_id": "VISIT-1",
		"lng": "en",
		"version_id": "VER-1",
		"patient_dob": "1990-01-01",
	}

	@patch("shukhee_integration.api.consent.current_provider")
	def test_missing_patient_id_raises(self, mock_current_provider):
		env = dict(self._VALID_ENV)
		del env["patient_id"]
		with self.assertRaises(frappe.ValidationError):
			record_consent_decline(payload=json.dumps(env))
		mock_current_provider.assert_not_called()

	@patch("shukhee_integration.api.consent.current_provider")
	def test_missing_lng_raises(self, mock_current_provider):
		env = dict(self._VALID_ENV)
		del env["lng"]
		with self.assertRaises(frappe.ValidationError):
			record_consent_decline(payload=json.dumps(env))
		mock_current_provider.assert_not_called()

	@patch("frappe.utils.now_datetime", return_value=_FIXED_NOW)
	@patch("frappe.db.commit")
	@patch("frappe.db.exists")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consent.current_provider")
	def test_successful_insert_stamps_the_right_fields(
		self, mock_current_provider, mock_get_doc, mock_exists, mock_commit, mock_now
	):
		mock_current_provider.return_value = "PROV-1"
		mock_exists.return_value = True
		doc = MagicMock()
		mock_get_doc.return_value = doc

		result = record_consent_decline(payload=json.dumps(self._VALID_ENV))

		self.assertEqual(result, {"logged": True})
		doc.insert.assert_called_once_with(ignore_permissions=True)
		mock_commit.assert_called_once()

		(doc_dict,), _kwargs = mock_get_doc.call_args
		self.assertEqual(doc_dict["doctype"], "Shukhee Consent Decline")
		self.assertEqual(doc_dict["patient_id"], "PAT-1")
		self.assertEqual(doc_dict["visit_id"], "VISIT-1")
		self.assertEqual(doc_dict["lng"], "en")
		# consent_version is resolved from version_id (validated against Shukhee Consent
		# Version), not trusted unchecked.
		self.assertEqual(doc_dict["consent_version"], "VER-1")
		mock_exists.assert_called_once_with("Shukhee Consent Version", "VER-1")
		self.assertEqual(doc_dict["provider"], "PROV-1")
		self.assertEqual(doc_dict["patient_dob"], "1990-01-01")
		self.assertIn("occurred_at", doc_dict)

	@patch("frappe.utils.now_datetime", return_value=_FIXED_NOW)
	@patch("frappe.db.commit")
	@patch("frappe.db.exists")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consent.current_provider")
	def test_unknown_version_id_leaves_consent_version_empty(
		self, mock_current_provider, mock_get_doc, mock_exists, mock_commit, mock_now
	):
		"""An invalid/stale version_id is a best-effort no-op on the link value, not an
		error -- the decline itself still gets recorded."""
		mock_current_provider.return_value = "PROV-1"
		mock_exists.return_value = False
		mock_get_doc.return_value = MagicMock()

		result = record_consent_decline(payload=json.dumps(self._VALID_ENV))

		self.assertEqual(result, {"logged": True})
		(doc_dict,), _kwargs = mock_get_doc.call_args
		self.assertIsNone(doc_dict["consent_version"])

	@patch("frappe.utils.now_datetime", return_value=_FIXED_NOW)
	@patch("frappe.db.commit")
	@patch("frappe.db.exists")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consent.current_provider")
	def test_missing_version_id_is_a_soft_degrade_not_an_error(
		self, mock_current_provider, mock_get_doc, mock_exists, mock_commit, mock_now
	):
		mock_current_provider.return_value = "PROV-1"
		mock_get_doc.return_value = MagicMock()

		env = dict(self._VALID_ENV)
		del env["version_id"]
		result = record_consent_decline(payload=json.dumps(env))

		self.assertEqual(result, {"logged": True})
		mock_exists.assert_not_called()
		(doc_dict,), _kwargs = mock_get_doc.call_args
		self.assertIsNone(doc_dict["consent_version"])

	@patch("frappe.utils.now_datetime", return_value=_FIXED_NOW)
	@patch("frappe.db.commit")
	@patch("frappe.db.exists")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consent.current_provider")
	def test_provider_is_resolved_server_side_not_from_client_payload(
		self, mock_current_provider, mock_get_doc, mock_exists, mock_commit, mock_now
	):
		"""A client-supplied `provider` field (if one were ever sent) must never be trusted --
		only current_provider()'s current_remote_user_id() -> Provider.username resolution
		may set this field."""
		mock_current_provider.return_value = "PROV-REAL"
		mock_exists.return_value = True
		mock_get_doc.return_value = MagicMock()

		env = dict(self._VALID_ENV, provider="PROV-SPOOFED")
		record_consent_decline(payload=json.dumps(env))

		mock_current_provider.assert_called_once()
		(doc_dict,), _kwargs = mock_get_doc.call_args
		self.assertEqual(doc_dict["provider"], "PROV-REAL")


if __name__ == "__main__":
	unittest.main()
