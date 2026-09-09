"""
Unit tests for shukhee_integration.api.consultation.

These specifically guard the spice_next_core rename: consultation.py imports
`current_remote_user_id`/`whitelist` from spice_next_core.auth.decorators (not
uhis_next_core) — TestSpiceNextCoreIntegration below fails loudly (ImportError at
collection, or an identity mismatch) if that dependency is ever pointed at a stale or
divergent copy instead of the real spice_next_core module.

Every other test class below calls each endpoint via inspect.unwrap(...) — the raw
business logic underneath two stacked layers (frappe.whitelist's own argument-typing
wrapper, then @whitelist(remote_auth=True)'s require_remote_auth guard; both use
functools.wraps, so inspect.unwrap walks both — verified via bench console). This
deliberately bypasses the X-Auth-Token guard, which needs a real bound frappe.request and
is already exhaustively covered by spice_next_core's own
TestRequireRemoteAuthDecorator/TestWhitelistWrapper — re-mocking it on every one of these
tests would just be auth-guard noise on top of the business logic these tests actually
target. TestSpiceNextCoreIntegration separately confirms every endpoint here really is
wired through that guard.
"""

import inspect
import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration.api import consultation

start_consultation = inspect.unwrap(consultation.start_consultation)
get_consultation_status = inspect.unwrap(consultation.get_consultation_status)
get_prescription = inspect.unwrap(consultation.get_prescription)
download_document = inspect.unwrap(consultation.download_document)


class _LocalAttr:
	"""Sets a real attribute on frappe.local (e.g. form_dict, request) for the duration of
	a `with` block, then restores whatever was there before (or deletes it if it didn't
	exist). Deliberately NOT unittest.mock.patch: patch("frappe.local", ...) / patch(
	"frappe.request", ...) trip a Python 3.14 unittest.mock regression — its async-detection
	probe calls inspect.isawaitable(original), which does isinstance(...).__class__ against
	Frappe's LocalProxy.__getattribute__, and that raises AttributeError instead of the
	TypeError mock.py expects to swallow. Plain setattr/restore sidesteps the probe entirely."""

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


def _call_log_doc(**overrides):
	defaults = dict(
		name="CL-1",
		status="pending",
		uhis_user="PROV-1",
		request_id="req-1",
		prescription_link=None,
		invoice_link=None,
		doctor_name=None,
		doctor_speciality=None,
		doctor_facility=None,
	)
	defaults.update(overrides)
	doc = MagicMock()
	for key, value in defaults.items():
		setattr(doc, key, value)
	return doc


class TestSpiceNextCoreIntegration(unittest.TestCase):
	"""Confirms consultation.py is wired to the real, live spice_next_core module after
	the rename — not a stale uhis_next_core copy or an incompatible re-implementation."""

	def test_whitelist_is_the_real_spice_next_core_decorator(self):
		from spice_next_core.auth.decorators import whitelist as real_whitelist

		self.assertIs(consultation.whitelist, real_whitelist)

	def test_current_remote_user_id_is_the_real_spice_next_core_function(self):
		from spice_next_core.auth.decorators import current_remote_user_id as real_fn

		self.assertIs(consultation.current_remote_user_id, real_fn)

	def test_all_endpoints_registered_under_remote_auth_whitelist(self):
		# Each endpoint must be both Frappe-whitelisted and a guest method (remote_auth=True
		# implies allow_guest=True) — proves the whitelist() wrapper actually ran, not just
		# that the module imported without error.
		for fn in (
			consultation.start_consultation,
			consultation.get_consultation_status,
			consultation.get_prescription,
			consultation.download_document,
		):
			with self.subTest(fn=fn.__name__):
				self.assertIn(fn, frappe.whitelisted)
				self.assertIn(fn, frappe.guest_methods)

	def test_each_endpoint_has_the_require_remote_auth_guard_beneath_it(self):
		# functools.wraps copies __module__/__name__/__qualname__ from the wrapped fn onto
		# each wrapper, so identity (not metadata) is what proves two distinct layers exist
		# between the module-level whitelisted name and the raw business logic: frappe's own
		# argument-typing wrapper, then require_remote_auth's wrapper (see module docstring).
		for fn in (
			consultation.start_consultation,
			consultation.get_consultation_status,
			consultation.get_prescription,
			consultation.download_document,
		):
			with self.subTest(fn=fn.__name__):
				self.assertTrue(hasattr(fn, "__wrapped__"))
				once_unwrapped = fn.__wrapped__
				fully_unwrapped = inspect.unwrap(fn)
				self.assertIsNot(fn, once_unwrapped)
				self.assertIsNot(once_unwrapped, fully_unwrapped)


class TestResolveEnv(unittest.TestCase):

	def test_payload_string_is_parsed_as_json(self):
		result = consultation._resolve_env('{"call_log": "CL-1"}')
		self.assertEqual(result, {"call_log": "CL-1"})

	def test_no_payload_falls_back_to_form_dict(self):
		with _LocalAttr("form_dict", {"call_log": "CL-2"}):
			result = consultation._resolve_env(None)
		self.assertEqual(result, {"call_log": "CL-2"})


class TestCurrentProvider(unittest.TestCase):

	@patch("shukhee_integration.api.consultation.current_remote_user_id")
	@patch("frappe.db.get_value")
	def test_matches_by_username_not_by_frappe_user_link(self, mock_get_value, mock_user_id):
		mock_user_id.return_value = "lf_sk"
		mock_get_value.return_value = "PROV-1"

		result = consultation._current_provider()

		self.assertEqual(result, "PROV-1")
		mock_get_value.assert_called_once_with("Provider", {"username": "lf_sk"}, "name")

	@patch("shukhee_integration.api.consultation.current_remote_user_id")
	@patch("frappe.db.get_value")
	def test_no_matching_provider_raises(self, mock_get_value, mock_user_id):
		mock_user_id.return_value = "unknown_user"
		mock_get_value.return_value = None
		with self.assertRaises(frappe.DoesNotExistError):
			consultation._current_provider()


class TestCurrentShukheeUser(unittest.TestCase):

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_active_user_returns_doc(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		doc = MagicMock(status="Active")
		mock_get_doc.return_value = doc
		self.assertIs(consultation._current_shukhee_user("PROV-1"), doc)

	@patch("frappe.db.exists")
	def test_no_shukhee_credentials_raises(self, mock_exists):
		mock_exists.return_value = False
		with self.assertRaises(frappe.DoesNotExistError):
			consultation._current_shukhee_user("PROV-1")

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_inactive_account_raises(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		mock_get_doc.return_value = MagicMock(status="Inactive")
		with self.assertRaises(frappe.ValidationError):
			consultation._current_shukhee_user("PROV-1")


class TestStartConsultation(unittest.TestCase):

	def test_missing_required_field_raises(self):
		with _LocalAttr("form_dict", {"contact_number": "01410820112"}):  # reason/speciality missing
			with self.assertRaises(frappe.ValidationError):
				start_consultation()

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_happy_path_creates_call_log(
		self,
		mock_current_provider,
		mock_current_shukhee_user,
		mock_shukhee_client,
		mock_get_doc,
		mock_commit,
	):
		mock_current_provider.return_value = "PROV-1"
		shukhee_user_doc = MagicMock()
		mock_current_shukhee_user.return_value = shukhee_user_doc

		mock_shukhee_client.find_or_create_shukhee_patient.return_value = ("sk-p1", {"fullName": "Jane"})
		mock_shukhee_client.upload_medias.return_value = []
		mock_shukhee_client.resolve_speciality_id.return_value = ("1", "General Medicine")
		mock_shukhee_client.book_instant_call.return_value = "txn-1"
		mock_shukhee_client.resolve_request_id.return_value = "req-1"
		mock_shukhee_client.get_valid_token.return_value = "tok-1"
		mock_shukhee_client.build_video_call_url.return_value = "https://video/call/req-1"

		call_log = MagicMock(name="CL-1", status="pending")
		mock_get_doc.return_value = call_log

		with _LocalAttr(
			"form_dict",
			{
				"contact_number": "01410820112",
				"reason": "fever",
				"requested_speciality": "General Medicine",
			},
		), _LocalAttr("request", MagicMock(files=None)):
			result = start_consultation()

		call_log.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(result["call_url"], "https://video/call/req-1")
		self.assertEqual(result["status"], call_log.status)

	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_missing_contact_number_raises(self, mock_current_provider, mock_current_shukhee_user):
		with _LocalAttr("form_dict", {"reason": "fever", "requested_speciality": "General Medicine"}):
			with self.assertRaises(frappe.ValidationError):
				start_consultation()
		mock_current_provider.assert_not_called()


class TestGetConsultationStatus(unittest.TestCase):

	@patch("frappe.get_doc")
	def test_terminal_status_short_circuits_no_shukhee_call(self, mock_get_doc):
		mock_get_doc.return_value = _call_log_doc(
			status="completed", prescription_link="/files/rx.pdf", doctor_name="Dr. A"
		)
		with patch("shukhee_integration.api.consultation.shukhee_client") as mock_client:
			result = get_consultation_status(payload='{"call_log": "CL-1"}')
			mock_client.get_emergency_request.assert_not_called()
		self.assertEqual(result["status"], "completed")
		self.assertEqual(result["prescription_link"], "/files/rx.pdf")
		self.assertEqual(result["doctor_name"], "Dr. A")

	def test_missing_call_log_raises(self):
		with self.assertRaises(frappe.ValidationError):
			get_consultation_status(payload="{}")

	@patch("frappe.db.commit")
	@patch("frappe.db.set_value")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("frappe.get_doc")
	def test_non_terminal_polls_shukhee_and_persists_doctor_fields(
		self, mock_get_doc, mock_current_shukhee_user, mock_set_value, mock_commit
	):
		mock_get_doc.return_value = _call_log_doc(status="in-progress")
		mock_current_shukhee_user.return_value = MagicMock()

		with patch("shukhee_integration.api.consultation.shukhee_client") as mock_client:
			mock_client.get_emergency_request.return_value = {
				"status": "in-progress",
				"doctor": {"name": "Dr. B", "specialty": {"title": "Pediatrics"}, "working_at": "Clinic X"},
			}
			result = get_consultation_status(payload='{"call_log": "CL-1"}')

		self.assertEqual(result["doctor_name"], "Dr. B")
		self.assertEqual(result["doctor_speciality"], "Pediatrics")
		self.assertEqual(result["doctor_facility"], "Clinic X")
		mock_set_value.assert_any_call("Call Logs", "CL-1", "doctor_name", "Dr. B")

	@patch("frappe.db.commit")
	@patch("frappe.db.set_value")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("frappe.get_doc")
	def test_completion_downloads_and_attaches_documents(
		self, mock_get_doc, mock_current_shukhee_user, mock_set_value, mock_commit
	):
		mock_get_doc.return_value = _call_log_doc(status="in-progress")
		mock_current_shukhee_user.return_value = MagicMock()

		with patch("shukhee_integration.api.consultation.shukhee_client") as mock_client:
			mock_client.get_emergency_request.return_value = {
				"status": "completed",
				"appointment": {
					"prescriptionLink": "https://shukhee/rx.pdf",
					"invoiceLink": "https://shukhee/inv.pdf",
				},
			}
			mock_client.download_and_attach.side_effect = [
				"/files/CL-1-prescription.pdf",
				"/files/CL-1-invoice.pdf",
			]
			result = get_consultation_status(payload='{"call_log": "CL-1"}')

		self.assertEqual(result["status"], "completed")
		self.assertEqual(result["prescription_link"], "/files/CL-1-prescription.pdf")
		self.assertEqual(result["invoice_link"], "/files/CL-1-invoice.pdf")


class TestGetPrescription(unittest.TestCase):

	@patch("frappe.get_doc")
	def test_completed_returns_links(self, mock_get_doc):
		mock_get_doc.return_value = _call_log_doc(
			status="completed", prescription_link="/files/rx.pdf", invoice_link="/files/inv.pdf"
		)
		result = get_prescription(payload='{"call_log": "CL-1"}')
		self.assertEqual(result["prescription_link"], "/files/rx.pdf")

	@patch("frappe.get_doc")
	def test_not_completed_raises(self, mock_get_doc):
		mock_get_doc.return_value = _call_log_doc(status="pending")
		with self.assertRaises(frappe.ValidationError):
			get_prescription(payload='{"call_log": "CL-1"}')

	def test_missing_call_log_raises(self):
		with self.assertRaises(frappe.ValidationError):
			get_prescription(payload="{}")


class TestDownloadDocument(unittest.TestCase):

	def test_missing_call_log_raises(self):
		with self.assertRaises(frappe.ValidationError):
			download_document(payload='{"doc_type": "prescription"}')

	@patch("frappe.get_doc")
	def test_invalid_doc_type_raises(self, mock_get_doc):
		with self.assertRaises(frappe.ValidationError):
			download_document(payload='{"call_log": "CL-1", "doc_type": "xray"}')

	@patch("shukhee_integration.api.consultation._current_provider")
	@patch("frappe.get_doc")
	def test_wrong_owner_raises_permission_error(self, mock_get_doc, mock_current_provider):
		mock_get_doc.return_value = _call_log_doc(uhis_user="PROV-OTHER")
		mock_current_provider.return_value = "PROV-1"
		with self.assertRaises(frappe.PermissionError):
			download_document(payload='{"call_log": "CL-1", "doc_type": "prescription"}')

	@patch("shukhee_integration.api.consultation._current_provider")
	@patch("frappe.get_doc")
	def test_no_document_available_raises(self, mock_get_doc, mock_current_provider):
		mock_get_doc.return_value = _call_log_doc(uhis_user="PROV-1", prescription_link=None)
		mock_current_provider.return_value = "PROV-1"
		with self.assertRaises(frappe.DoesNotExistError):
			download_document(payload='{"call_log": "CL-1", "doc_type": "prescription"}')

	@patch("shukhee_integration.api.consultation._current_provider")
	@patch("frappe.get_doc")
	def test_success_returns_base64_content(self, mock_get_doc, mock_current_provider):
		call_log = _call_log_doc(uhis_user="PROV-1", prescription_link="/files/rx.pdf")
		file_doc = MagicMock(file_name="rx.pdf")
		file_doc.get_content.return_value = b"pdf-bytes"
		mock_get_doc.side_effect = [call_log, file_doc]
		mock_current_provider.return_value = "PROV-1"

		result = download_document(payload='{"call_log": "CL-1", "doc_type": "prescription"}')

		self.assertEqual(result["filename"], "rx.pdf")
		import base64

		self.assertEqual(base64.b64decode(result["content_base64"]), b"pdf-bytes")


if __name__ == "__main__":
	unittest.main()
