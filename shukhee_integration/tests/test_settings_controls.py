"""
Unit tests for shukhee_integration.api.settings.get_controls -- the
mobile-facing server-side kill-switch for the teleconsult feature.

Pure unit tests (mocked frappe.get_single / frappe.db.get_value /
_current_provider), same convention as test_consent.py / test_consultation.py:
inspect.unwrap reaches the raw business logic beneath frappe.whitelist's own
argument-typing wrapper and @whitelist(remote_auth=True)'s require_remote_auth
guard. Not wrapped by audit_inbound -- this is a read-only config fetch, same
class of endpoint as consent.get_consent.

teleconsult_enabled is only HALF the gate -- the calling SK must also have an
Active UHIS Shukhee User mapping (same check consultation._current_shukhee_user
enforces before any real Shukhee call), so every "enabled" case here mocks
both the global flag AND the per-SK mapping.
"""

import inspect
import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration.api import settings as settings_api

get_controls = inspect.unwrap(settings_api.get_controls)


class TestGetControls(unittest.TestCase):

	@patch("frappe.get_single")
	def test_defaults_to_disabled_when_unset(self, mock_get_single):
		# A Single that has never had this field saved reads back None --
		# short-circuits before the per-SK mapping lookup ever runs.
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = None
		mock_get_single.return_value = mock_settings
		self.assertEqual(get_controls(), {"teleconsultEnabled": False})

	@patch("frappe.get_single")
	def test_explicitly_disabled_short_circuits_before_mapping_lookup(self, mock_get_single):
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = 0
		mock_get_single.return_value = mock_settings
		with patch(
			"shukhee_integration.api.settings._current_provider"
		) as mock_current_provider:
			self.assertEqual(get_controls(), {"teleconsultEnabled": False})
			mock_current_provider.assert_not_called()

	@patch("shukhee_integration.api.settings._current_provider")
	@patch("frappe.db.get_value")
	@patch("frappe.get_single")
	def test_enabled_globally_and_active_mapping_returns_true(
		self, mock_get_single, mock_db_get_value, mock_current_provider
	):
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = 1
		mock_get_single.return_value = mock_settings
		mock_current_provider.return_value = "PROV-001"
		mock_db_get_value.return_value = "Active"

		self.assertEqual(get_controls(), {"teleconsultEnabled": True})
		mock_db_get_value.assert_called_once_with("UHIS Shukhee User", "PROV-001", "status")

	@patch("shukhee_integration.api.settings._current_provider")
	@patch("frappe.db.get_value")
	@patch("frappe.get_single")
	def test_enabled_globally_but_inactive_mapping_returns_false(
		self, mock_get_single, mock_db_get_value, mock_current_provider
	):
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = 1
		mock_get_single.return_value = mock_settings
		mock_current_provider.return_value = "PROV-001"
		mock_db_get_value.return_value = "Inactive"

		self.assertEqual(get_controls(), {"teleconsultEnabled": False})

	@patch("shukhee_integration.api.settings._current_provider")
	@patch("frappe.get_single")
	def test_enabled_globally_but_no_provider_record_returns_false(
		self, mock_get_single, mock_current_provider
	):
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = 1
		mock_get_single.return_value = mock_settings
		mock_current_provider.side_effect = frappe.DoesNotExistError

		self.assertEqual(get_controls(), {"teleconsultEnabled": False})


if __name__ == "__main__":
	unittest.main()
