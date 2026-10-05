"""
Unit tests for shukhee_integration.api.fhir_proxy -- pure wire-adapter
concerns only (encounter param validation, "Encounter/{id}" reference literal
stripping). The actual FHIR resource synthesis (BP composite grouping etc.)
is spice_next_core's own concern, tested in
spice_next_core/tests/test_mobile_fhir.py -- mocked out here.
"""

import inspect
import unittest
from unittest.mock import patch

import frappe

from shukhee_integration.api import fhir_proxy

observation = inspect.unwrap(fhir_proxy.observation)


class TestFhirProxyObservation(unittest.TestCase):

	def test_missing_encounter_raises(self):
		with self.assertRaises(frappe.ValidationError):
			observation(encounter=None)

	@patch("shukhee_integration.api.fhir_proxy.observations_by_encounter")
	def test_strips_encounter_reference_prefix(self, mock_observations):
		mock_observations.return_value = {"resourceType": "Bundle", "entry": []}
		observation(encounter="Encounter/abc-123")
		mock_observations.assert_called_once_with("abc-123")

	@patch("shukhee_integration.api.fhir_proxy.observations_by_encounter")
	def test_bare_id_without_reference_prefix_also_works(self, mock_observations):
		mock_observations.return_value = {"resourceType": "Bundle", "entry": []}
		observation(encounter="abc-123")
		mock_observations.assert_called_once_with("abc-123")

	@patch("shukhee_integration.api.fhir_proxy.observations_by_encounter")
	def test_returns_the_bundle_unchanged(self, mock_observations):
		bundle = {"resourceType": "Bundle", "type": "searchset", "total": 0, "entry": []}
		mock_observations.return_value = bundle
		self.assertEqual(observation(encounter="Encounter/abc-123"), bundle)


if __name__ == "__main__":
	unittest.main()
