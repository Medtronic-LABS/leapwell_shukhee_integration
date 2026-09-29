"""
Unit tests for shukhee_integration.api.consent.

Mirrors tests/test_consultation.py's style: inspect.unwrap(...) reaches the raw business
logic beneath frappe.whitelist's own argument-typing wrapper and @whitelist(remote_auth=True)'s
require_remote_auth guard. get_consent is a read-only config fetch (like
consultation.get_specialities), so unlike this app's call-lifecycle endpoints it is not
wrapped by audit_inbound -- there's nothing here for that decorator to log.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

from shukhee_integration.api import consent

get_consent = inspect.unwrap(consent.get_consent)


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
		mock_get_all.return_value = [{"lng": "bn", "consent": "<p>bn consent</p>"}]

		result = get_consent(payload='{"lng": "bn"}')

		self.assertEqual(result, {"lng": "bn", "consent": "<p>bn consent</p>"})
		mock_get_all.assert_called_once_with(
			"Shukhee Consent", filters={"lng": "bn"}, fields=["lng", "consent"], limit=1
		)

	@patch("frappe.get_all")
	def test_missing_language_falls_back_to_english(self, mock_get_all):
		mock_get_all.side_effect = [[], [{"lng": "en", "consent": "<p>en consent</p>"}]]

		result = get_consent(payload='{"lng": "bn"}')

		self.assertEqual(result, {"lng": "en", "consent": "<p>en consent</p>"})
		self.assertEqual(mock_get_all.call_count, 2)
		mock_get_all.assert_any_call(
			"Shukhee Consent", filters={"lng": "bn"}, fields=["lng", "consent"], limit=1
		)
		mock_get_all.assert_any_call(
			"Shukhee Consent", filters={"lng": "en"}, fields=["lng", "consent"], limit=1
		)

	@patch("frappe.get_all")
	def test_no_consent_configured_raises(self, mock_get_all):
		mock_get_all.return_value = []

		with self.assertRaises(frappe.DoesNotExistError):
			get_consent(payload='{"lng": "bn"}')

	@patch("frappe.get_all")
	def test_missing_lng_defaults_to_en(self, mock_get_all):
		mock_get_all.return_value = [{"lng": "en", "consent": "<p>en consent</p>"}]

		result = get_consent(payload="{}")

		self.assertEqual(result, {"lng": "en", "consent": "<p>en consent</p>"})
		mock_get_all.assert_called_once_with(
			"Shukhee Consent", filters={"lng": "en"}, fields=["lng", "consent"], limit=1
		)


if __name__ == "__main__":
	unittest.main()
