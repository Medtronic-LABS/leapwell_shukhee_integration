# Copyright (c) 2026, Medtronic Labs and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestShukheeConsent(IntegrationTestCase):
	"""Covers ShukheeConsent.on_update -- the hook that keeps Shukhee Consent Version
	history in sync whenever a Shukhee Consent row's content changes, since Shukhee
	Consent itself only ever holds the current wording per language."""

	def setUp(self):
		# Shukhee Consent keeps exactly one row per language (see its own doc comment),
		# so two tests both inserting lng="en" would collide on a real site -- track and
		# delete only what this test itself created, never the whole table (this site's
		# real Shukhee Consent/Shukhee Consent Version rows are live dev-environment data,
		# not test fixtures).
		self._created_consent_names = []

	def _make_consent(self, lng="en", version="1", consent="<p>v1</p>"):
		doc = frappe.get_doc(
			{
				"doctype": "Shukhee Consent",
				"lng": lng,
				"version": version,
				"consent": consent,
			}
		).insert(ignore_permissions=True)
		self._created_consent_names.append(doc.name)
		return doc

	def tearDown(self):
		# The two doctypes Link to each other in opposite directions (Version.shukhee_consent
		# -> Consent, Consent.current_version -> Version), so either deletion order alone
		# trips Frappe's LinkExistsError. Clear the Consent -> Version reference first, then
		# delete child Versions, then the Consent row itself.
		for name in self._created_consent_names:
			frappe.db.set_value("Shukhee Consent", name, "current_version", None)
			for version_name in frappe.get_all(
				"Shukhee Consent Version", filters={"shukhee_consent": name}, pluck="name"
			):
				frappe.delete_doc(
					"Shukhee Consent Version",
					version_name,
					ignore_permissions=True,
					delete_permanently=True,
				)
			frappe.delete_doc(
				"Shukhee Consent", name, ignore_permissions=True, delete_permanently=True
			)
		frappe.db.commit()

	def test_first_save_creates_a_snapshot_and_sets_current_version(self):
		doc = self._make_consent()

		versions = frappe.get_all(
			"Shukhee Consent Version", filters={"shukhee_consent": doc.name}, fields=["name", "version", "consent"]
		)
		self.assertEqual(len(versions), 1)
		self.assertEqual(versions[0]["version"], "1")
		self.assertEqual(versions[0]["consent"], "<p>v1</p>")
		self.assertEqual(
			str(frappe.db.get_value("Shukhee Consent", doc.name, "current_version")), str(versions[0]["name"])
		)

	def test_resaving_with_no_content_change_does_not_create_another_snapshot(self):
		doc = self._make_consent()
		first_current_version = frappe.db.get_value("Shukhee Consent", doc.name, "current_version")

		doc.reload()
		doc.save()

		versions = frappe.get_all("Shukhee Consent Version", filters={"shukhee_consent": doc.name})
		self.assertEqual(len(versions), 1)
		self.assertEqual(
			frappe.db.get_value("Shukhee Consent", doc.name, "current_version"), first_current_version
		)

	def test_editing_consent_text_creates_a_new_snapshot_even_if_version_label_is_unchanged(self):
		"""History must never silently miss an edit just because the editor forgot to
		bump the version label."""
		doc = self._make_consent()

		doc.reload()
		doc.consent = "<p>v1, edited</p>"
		doc.save()

		versions = frappe.get_all(
			"Shukhee Consent Version",
			filters={"shukhee_consent": doc.name},
			fields=["version", "consent"],
			order_by="creation asc",
		)
		self.assertEqual(len(versions), 2)
		self.assertEqual(versions[1]["version"], "1")
		self.assertEqual(versions[1]["consent"], "<p>v1, edited</p>")

	def test_bumping_version_label_creates_a_new_snapshot(self):
		doc = self._make_consent()

		doc.reload()
		doc.version = "2"
		doc.consent = "<p>v2</p>"
		doc.save()

		versions = frappe.get_all(
			"Shukhee Consent Version",
			filters={"shukhee_consent": doc.name},
			fields=["name", "version"],
			order_by="creation asc",
		)
		self.assertEqual([v["version"] for v in versions], ["1", "2"])
		self.assertEqual(
			str(frappe.db.get_value("Shukhee Consent", doc.name, "current_version")), str(versions[1]["name"])
		)
