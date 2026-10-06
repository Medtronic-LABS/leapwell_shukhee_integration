"""
Unit tests for shukhee_integration.api.settings.get_controls -- the
mobile-facing server-side kill-switch for the teleconsult feature.

Pure unit tests (mocked frappe.get_single), same convention as
test_consent.py / test_consultation.py: inspect.unwrap reaches the raw
business logic beneath frappe.whitelist's own argument-typing wrapper and
@whitelist(remote_auth=True)'s require_remote_auth guard. Not wrapped by
audit_inbound -- this is a read-only config fetch, same class of endpoint
as consent.get_consent.
"""

import inspect
import unittest
from unittest.mock import MagicMock, patch

from shukhee_integration.api import settings as settings_api

get_controls = inspect.unwrap(settings_api.get_controls)


class TestGetControls(unittest.TestCase):

	@patch("frappe.get_single")
	def test_defaults_to_disabled_when_unset(self, mock_get_single):
		# A Single that has never had this field saved reads back None.
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = None
		mock_get_single.return_value = mock_settings
		self.assertEqual(get_controls(), {"teleconsultEnabled": False})

	@patch("frappe.get_single")
	def test_explicitly_disabled(self, mock_get_single):
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = 0
		mock_get_single.return_value = mock_settings
		self.assertEqual(get_controls(), {"teleconsultEnabled": False})

	@patch("frappe.get_single")
	def test_explicitly_enabled(self, mock_get_single):
		mock_settings = MagicMock()
		mock_settings.teleconsult_enabled = 1
		mock_get_single.return_value = mock_settings
		self.assertEqual(get_controls(), {"teleconsultEnabled": True})
		mock_get_single.assert_called_once_with("Shukhee Settings")


if __name__ == "__main__":
	unittest.main()
