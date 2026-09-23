"""
Integration tests for shukhee_integration.api.audit.get_call_audit_timeline --
exercised against REAL Shukhee Call Audit Log rows (the point of this method is
real ordering/filtering behavior, which a mocked test can't exercise; mirrors
test_consultation_lifecycle.py's isolation approach: uniquely-named records,
explicit commit + force-delete teardown since nothing here relies on the test
runner's transaction rollback).
"""

import unittest
import uuid

import frappe

from shukhee_integration.api import audit as api_audit


class TestGetCallAuditTimeline(unittest.TestCase):
	def setUp(self):
		# Unique per test run (not a fixed class constant) -- matches the same
		# hygiene test_consultation_lifecycle.py/test_call_logs_sync.py already
		# apply, for the same reason: a fixed name collides with any leftover row
		# from an interrupted prior run (real, observed failure mode -- a crashed
		# run's tearDown never gets to fire, leaving a real duplicate-key row
		# behind that this class's own next run then collides with).
		self.provider_username = f"test.audit-timeline.provider.{uuid.uuid4().hex[:8]}@shukhee.test"
		self.provider = frappe.get_doc(
			{
				"doctype": "Provider",
				"username": self.provider_username,
				"full_name": "Audit Timeline Test Provider",
			}
		).insert(ignore_permissions=True)

		self.call_log = frappe.get_doc(
			{"doctype": "Call Logs", "uhis_user": self.provider.name}
		).insert(ignore_permissions=True)
		self.other_call_log = frappe.get_doc(
			{"doctype": "Call Logs", "uhis_user": self.provider.name}
		).insert(ignore_permissions=True)

		# Out-of-order inserts with explicit logged_at, to prove the method
		# sorts by logged_at rather than relying on insertion/creation order.
		self.row_late = frappe.get_doc(
			{
				"doctype": "Shukhee Call Audit Log",
				"direction": "Outbound",
				"endpoint": "POST https://shukhee.test/ssk/login-v2",
				"call_log": self.call_log.name,
				"status": "Success",
				"logged_at": "2026-01-01 10:00:05",
			}
		).insert(ignore_permissions=True)
		self.row_early = frappe.get_doc(
			{
				"doctype": "Shukhee Call Audit Log",
				"direction": "Inbound",
				"endpoint": "shukhee_integration.api.consultation.start_consultation",
				"call_log": self.call_log.name,
				"status": "Success",
				"logged_at": "2026-01-01 10:00:00",
			}
		).insert(ignore_permissions=True)
		self.row_other_call = frappe.get_doc(
			{
				"doctype": "Shukhee Call Audit Log",
				"direction": "Inbound",
				"endpoint": "shukhee_integration.api.consultation.get_prescription",
				"call_log": self.other_call_log.name,
				"status": "Success",
				"logged_at": "2026-01-01 10:00:00",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		for doc in (
			self.row_late,
			self.row_early,
			self.row_other_call,
			self.call_log,
			self.other_call_log,
			self.provider,
		):
			frappe.delete_doc(doc.doctype, doc.name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_returns_only_rows_for_the_given_call_log_in_chronological_order(self):
		rows = api_audit.get_call_audit_timeline(self.call_log.name)
		self.assertEqual([r["name"] for r in rows], [self.row_early.name, self.row_late.name])

	def test_field_shape(self):
		rows = api_audit.get_call_audit_timeline(self.call_log.name)
		self.assertEqual(
			set(rows[0].keys()),
			{
				"name",
				"direction",
				"endpoint",
				"shukhee_user",
				"correlation_id",
				"status",
				"status_code",
				"duration_ms",
				"request_payload",
				"response_payload",
				"error",
				"logged_at",
			},
		)

	def test_missing_call_log_raises(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api_audit.get_call_audit_timeline("NONEXISTENT-CALL-LOG")

	def test_blank_call_log_raises_validation_error(self):
		with self.assertRaises(frappe.ValidationError):
			api_audit.get_call_audit_timeline(None)
