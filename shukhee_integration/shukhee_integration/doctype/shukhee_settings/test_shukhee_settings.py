# Copyright (c) 2026, Medtronic Labs and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase


# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]



class IntegrationTestShukheeSettings(IntegrationTestCase):
	"""
	Integration tests for ShukheeSettings.
	Use this class for testing interactions between multiple components.
	"""

	def test_teleconsult_enabled_defaults_falsy_and_round_trips(self):
		# Shukhee Settings is a Single -- there is no row to create/delete,
		# only a stored value to restore afterwards.
		original = frappe.db.get_single_value("Shukhee Settings", "teleconsult_enabled")
		self.addCleanup(
			frappe.db.set_single_value, "Shukhee Settings", "teleconsult_enabled", original
		)
		self.assertFalse(bool(original))

		frappe.db.set_single_value("Shukhee Settings", "teleconsult_enabled", 1)
		self.assertTrue(bool(frappe.get_single("Shukhee Settings").teleconsult_enabled))
