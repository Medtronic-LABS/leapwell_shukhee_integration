"""
Unit tests for shukhee_integration.providers.

Extracted from api/consultation.py (see that module's own test file for why) to break a
circular import between api/consultation.py and api/consent.py. Also specifically guards the
spice_next_core rename: providers.py imports `current_remote_user_id` from
spice_next_core.auth.decorators (not uhis_next_core) -- TestSpiceNextCoreIntegration below
fails loudly (ImportError at collection, or an identity mismatch) if that dependency is ever
pointed at a stale or divergent copy instead of the real spice_next_core module.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration import providers


class TestSpiceNextCoreIntegration(unittest.TestCase):

	def test_current_remote_user_id_is_the_real_spice_next_core_function(self):
		from spice_next_core.auth.decorators import current_remote_user_id as real_fn

		self.assertIs(providers.current_remote_user_id, real_fn)


class TestCurrentProvider(unittest.TestCase):

	@patch("shukhee_integration.providers.current_remote_user_id")
	@patch("frappe.db.get_value")
	def test_matches_by_username_not_by_frappe_user_link(self, mock_get_value, mock_user_id):
		mock_user_id.return_value = "lf_sk"
		mock_get_value.return_value = "PROV-1"

		result = providers.current_provider()

		self.assertEqual(result, "PROV-1")
		mock_get_value.assert_called_once_with("Provider", {"username": "lf_sk"}, "name")

	@patch("shukhee_integration.providers.current_remote_user_id")
	@patch("frappe.db.get_value")
	def test_no_matching_provider_raises(self, mock_get_value, mock_user_id):
		mock_user_id.return_value = "unknown_user"
		mock_get_value.return_value = None
		with self.assertRaises(frappe.DoesNotExistError):
			providers.current_provider()


class TestCurrentShukheeUser(unittest.TestCase):

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_active_user_returns_doc(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		doc = MagicMock(status="Active")
		mock_get_doc.return_value = doc
		self.assertIs(providers.current_shukhee_user("PROV-1"), doc)

	@patch("frappe.db.exists")
	def test_no_shukhee_credentials_raises(self, mock_exists):
		mock_exists.return_value = False
		with self.assertRaises(frappe.DoesNotExistError):
			providers.current_shukhee_user("PROV-1")

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_inactive_account_raises(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		mock_get_doc.return_value = MagicMock(status="Inactive")
		with self.assertRaises(frappe.ValidationError):
			providers.current_shukhee_user("PROV-1")


if __name__ == "__main__":
	unittest.main()
