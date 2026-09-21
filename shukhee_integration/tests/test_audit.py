"""
Unit tests for shukhee_integration.audit -- the shared redaction/logging helpers
behind Shukhee Call Audit Log. All Frappe DB/queue interaction is mocked; nothing
here writes a real doctype record or enqueues a real background job.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration import audit


class _LocalAttr:
	"""Sets a real attribute on frappe.local for the duration of a `with` block,
	then restores whatever was there before. Mirrors test_consultation.py's own
	helper (see its docstring for why plain setattr/restore is used instead of
	unittest.mock.patch on frappe.local)."""

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


class TestRedact(unittest.TestCase):

	def test_redacts_known_sensitive_keys(self):
		result = audit._redact({"password": "hunter2", "username": "sk1"})
		self.assertEqual(result, {"password": "[REDACTED]", "username": "sk1"})

	def test_redacts_regardless_of_case_and_separator(self):
		for key in ("Authorization", "X-Auth-Token", "access_token", "ACCESSTOKEN", "shukhee_password"):
			with self.subTest(key=key):
				result = audit._redact({key: "secret-value"})
				self.assertEqual(result[key], "[REDACTED]")

	def test_recurses_into_nested_dicts_and_lists(self):
		result = audit._redact({"data": [{"token": "t1"}, {"name": "ok"}]})
		self.assertEqual(result, {"data": [{"token": "[REDACTED]"}, {"name": "ok"}]})

	def test_truncates_long_string_values(self):
		long_value = "x" * (audit._MAX_FIELD_CHARS + 100)
		result = audit._redact({"note": long_value})
		self.assertLess(len(result["note"]), len(long_value))
		self.assertIn("truncated", result["note"])

	def test_non_dict_values_pass_through(self):
		self.assertEqual(audit._redact(42), 42)
		self.assertEqual(audit._redact(None), None)


class TestToJson(unittest.TestCase):

	def test_none_returns_none(self):
		self.assertIsNone(audit.to_json(None))

	def test_serializes_and_redacts(self):
		result = audit.to_json({"password": "hunter2", "ok": True})
		self.assertIn('"password": "[REDACTED]"', result)
		self.assertNotIn("hunter2", result)

	def test_truncates_oversized_payload(self):
		result = audit.to_json({"blob": "x" * (audit._MAX_PAYLOAD_CHARS + 500)})
		self.assertLessEqual(len(result), audit._MAX_PAYLOAD_CHARS + 100)
		self.assertIn("truncated", result)

	def test_unserializable_value_falls_back_to_str_without_raising(self):
		class Weird:
			def __repr__(self):
				return "<Weird>"

		result = audit.to_json({"obj": Weird()})
		self.assertIsInstance(result, str)


class TestSanitizeOutboundKwargs(unittest.TestCase):

	def test_keeps_only_known_keys(self):
		result = audit.sanitize_outbound_kwargs(
			{"params": {"a": 1}, "timeout": 10, "json": {"b": 2}}
		)
		self.assertEqual(result, {"params": {"a": 1}, "json": {"b": 2}})

	def test_files_reduced_to_metadata_never_raw_bytes(self):
		result = audit.sanitize_outbound_kwargs(
			{"files": [("attachments", ("rx.jpg", b"binary-content-here", "image/jpeg"))]}
		)
		self.assertEqual(
			result["files"],
			[{"field": "attachments", "filename": "rx.jpg", "size": len(b"binary-content-here"), "mimetype": "image/jpeg"}],
		)
		self.assertNotIn("binary-content-here", str(result))

	def test_no_relevant_keys_returns_empty_dict(self):
		self.assertEqual(audit.sanitize_outbound_kwargs({"timeout": 10}), {})


class TestLogCall(unittest.TestCase):

	@patch("frappe.enqueue")
	def test_enqueues_already_redacted_payloads(self, mock_enqueue):
		audit.log_call(
			direction="Outbound",
			endpoint="POST /x",
			request_payload={"password": "hunter2"},
			response_payload={"ok": True},
			status="Success",
		)
		mock_enqueue.assert_called_once()
		_, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs["direction"], "Outbound")
		self.assertIn("[REDACTED]", kwargs["request_payload"])
		self.assertNotIn("hunter2", kwargs["request_payload"])

	@patch("frappe.enqueue")
	def test_error_is_truncated(self, mock_enqueue):
		audit.log_call(direction="Outbound", endpoint="POST /x", status="Failed", error="x" * 500)
		_, kwargs = mock_enqueue.call_args
		self.assertLessEqual(len(kwargs["error"]), 140)

	@patch("frappe.log_error")
	@patch("frappe.enqueue", side_effect=Exception("queue down"))
	def test_enqueue_failure_never_raises(self, mock_enqueue, mock_log_error):
		audit.log_call(direction="Outbound", endpoint="POST /x")  # must not raise
		mock_log_error.assert_called_once()


class TestBackfillCallLog(unittest.TestCase):

	@patch("frappe.enqueue")
	def test_enqueues_with_correlation_id_and_call_log(self, mock_enqueue):
		audit.backfill_call_log("corr-1", "CL-1")
		mock_enqueue.assert_called_once_with(
			"shukhee_integration.audit._backfill_call_log",
			queue="short",
			now=False,
			correlation_id="corr-1",
			call_log="CL-1",
		)

	@patch("frappe.enqueue")
	def test_missing_correlation_id_or_call_log_is_a_noop(self, mock_enqueue):
		audit.backfill_call_log(None, "CL-1")
		audit.backfill_call_log("corr-1", None)
		mock_enqueue.assert_not_called()


class TestAuditInbound(unittest.TestCase):

	def _wrapped(self, fn):
		return audit.audit_inbound(fn)

	@patch("shukhee_integration.audit.log_call")
	def test_successful_call_is_logged_with_response(self, mock_log_call):
		fn = MagicMock(__module__="shukhee_integration.api.consultation", __name__="get_prescription")
		fn.return_value = {"prescription_link": "/files/x.pdf"}

		with _LocalAttr("form_dict", {"call_log": "CL-1"}):
			result = self._wrapped(fn)()

		self.assertEqual(result, {"prescription_link": "/files/x.pdf"})
		mock_log_call.assert_called_once()
		_, kwargs = mock_log_call.call_args
		self.assertEqual(kwargs["direction"], "Inbound")
		self.assertEqual(kwargs["endpoint"], "shukhee_integration.api.consultation.get_prescription")
		self.assertEqual(kwargs["call_log"], "CL-1")
		self.assertEqual(kwargs["status"], "Success")

	@patch("shukhee_integration.audit.log_call")
	def test_call_log_absent_from_request_is_picked_up_from_response(self, mock_log_call):
		# start_consultation's own shape: no call_log in the request (it doesn't
		# exist yet), but the response carries the newly created one.
		fn = MagicMock(__module__="shukhee_integration.api.consultation", __name__="start_consultation")
		fn.return_value = {"call_log": "CL-9", "call_url": "https://..."}

		with _LocalAttr("form_dict", {"contact_number": "555"}):
			self._wrapped(fn)()

		_, kwargs = mock_log_call.call_args
		self.assertEqual(kwargs["call_log"], "CL-9")

	@patch("shukhee_integration.audit.backfill_call_log")
	@patch("shukhee_integration.audit.log_call")
	def test_backfill_runs_only_when_call_log_was_unknown_at_request_time(
		self, mock_log_call, mock_backfill
	):
		fn = MagicMock(__module__="m", __name__="start_consultation")
		fn.return_value = {"call_log": "CL-9"}
		with _LocalAttr("form_dict", {}):
			self._wrapped(fn)()
		mock_backfill.assert_called_once()

		mock_backfill.reset_mock()
		fn2 = MagicMock(__module__="m", __name__="get_prescription")
		fn2.return_value = {"prescription_link": "x"}
		with _LocalAttr("form_dict", {"call_log": "CL-1"}):
			self._wrapped(fn2)()
		mock_backfill.assert_not_called()

	@patch("shukhee_integration.audit.log_call")
	def test_exception_is_logged_as_failed_and_reraised(self, mock_log_call):
		fn = MagicMock(__module__="m", __name__="get_prescription")
		fn.side_effect = frappe.ValidationError("nope")

		with _LocalAttr("form_dict", {"call_log": "CL-1"}):
			with self.assertRaises(frappe.ValidationError):
				self._wrapped(fn)()

		_, kwargs = mock_log_call.call_args
		self.assertEqual(kwargs["status"], "Failed")
		self.assertIn("nope", kwargs["error"])

	@patch("shukhee_integration.audit.log_call")
	def test_sets_and_the_correlation_id_and_current_call_log_locals(self, mock_log_call):
		fn = MagicMock(__module__="m", __name__="get_prescription", return_value={})
		with _LocalAttr("form_dict", {"call_log": "CL-1"}):
			self._wrapped(fn)()
			self.assertEqual(frappe.local.current_call_log, "CL-1")
			self.assertIsNotNone(frappe.local.shukhee_audit_correlation_id)


class TestCaptureInboundRequest(unittest.TestCase):

	def test_merges_form_dict_and_payload_kwarg(self):
		with _LocalAttr("form_dict", {"a": "1"}), _LocalAttr("request", None):
			result = audit._capture_inbound_request({"payload": '{"call_log": "CL-1"}'})
		self.assertEqual(result, {"a": "1", "call_log": "CL-1"})

	def test_no_request_files_is_safe(self):
		with _LocalAttr("form_dict", {}), _LocalAttr("request", None):
			result = audit._capture_inbound_request({})
		self.assertEqual(result, {})
