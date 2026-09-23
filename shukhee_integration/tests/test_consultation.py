"""
Unit tests for shukhee_integration.api.consultation.

These specifically guard the spice_next_core rename: consultation.py imports
`current_remote_user_id`/`whitelist` from spice_next_core.auth.decorators (not
uhis_next_core) — TestSpiceNextCoreIntegration below fails loudly (ImportError at
collection, or an identity mismatch) if that dependency is ever pointed at a stale or
divergent copy instead of the real spice_next_core module.

Every other test class below calls each endpoint via inspect.unwrap(...) — the raw
business logic underneath frappe.whitelist's own argument-typing wrapper, then
@whitelist(remote_auth=True)'s require_remote_auth guard, then (for every endpoint
except get_specialities) shukhee_integration.audit.audit_inbound; all three use
functools.wraps, so inspect.unwrap walks through all of them — verified via bench
console. This deliberately bypasses both the X-Auth-Token guard (already exhaustively
covered by spice_next_core's own TestRequireRemoteAuthDecorator/TestWhitelistWrapper)
and audit logging (covered by tests/test_audit.py) — re-mocking either on every one of
these tests would just be noise on top of the business logic these tests actually
target. TestSpiceNextCoreIntegration separately confirms every endpoint here really is
wired through the require_remote_auth guard.
"""

import inspect
import json
import unittest
from unittest.mock import MagicMock, patch

import frappe

from shukhee_integration.api import consultation

get_specialities = inspect.unwrap(consultation.get_specialities)
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


class _FakeUploadedFile:
	"""Minimal stand-in for a werkzeug FileStorage -- start_consultation only ever calls
	.filename, .read(), and .mimetype on each entry from frappe.request.files.getlist."""

	def __init__(self, filename, content, mimetype="image/jpeg"):
		self.filename = filename
		self.mimetype = mimetype
		self._content = content

	def read(self):
		return self._content


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
		appointment_status=None,
		clinical_data=None,
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
			consultation.get_specialities,
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
			consultation.get_specialities,
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


class TestAuditInboundWiring(unittest.TestCase):
	"""Confirms audit_inbound is wired to exactly the 4 call-related endpoints, not
	get_specialities (excluded -- it has no call context, see audit.py's docstring).

	functools.wraps sets __wrapped__ on every decorator layer that uses it, so
	__wrapped__ alone can't distinguish "wrapped by audit_inbound" from "wrapped by
	require_remote_auth" -- audit_inbound's wrapper carries an explicit
	__audit_inbound__ marker for exactly this reason (see audit.py)."""

	@staticmethod
	def _has_audit_inbound_layer(fn):
		seen = fn
		while seen is not None:
			if getattr(seen, "__audit_inbound__", False):
				return True
			seen = getattr(seen, "__wrapped__", None)
		return False

	def test_call_related_endpoints_are_wrapped(self):
		for fn in (
			consultation.start_consultation,
			consultation.get_consultation_status,
			consultation.get_prescription,
			consultation.download_document,
		):
			with self.subTest(fn=fn.__name__):
				self.assertTrue(self._has_audit_inbound_layer(fn))

	def test_get_specialities_is_not_wrapped_by_audit_inbound(self):
		self.assertFalse(self._has_audit_inbound_layer(consultation.get_specialities))


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


class TestGetSpecialities(unittest.TestCase):

	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_passes_through_specialityId_and_title_unchanged(
		self, mock_current_provider, mock_current_shukhee_user, mock_shukhee_client
	):
		mock_current_provider.return_value = "PROV-1"
		shukhee_user_doc = MagicMock()
		mock_current_shukhee_user.return_value = shukhee_user_doc
		mock_shukhee_client.list_specialities.return_value = [
			{"specialityId": "1", "title": "General Physician", "otherField": "ignored"},
			{"specialityId": "2", "title": "Sexual Wellness"},
		]

		result = get_specialities()

		self.assertEqual(
			result,
			{
				"specialities": [
					{"specialityId": "1", "title": "General Physician"},
					{"specialityId": "2", "title": "Sexual Wellness"},
				]
			},
		)
		mock_shukhee_client.list_specialities.assert_called_once_with(shukhee_user_doc)

	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_empty_list_returns_empty_specialities(
		self, mock_current_provider, mock_current_shukhee_user, mock_shukhee_client
	):
		mock_current_shukhee_user.return_value = MagicMock()
		mock_shukhee_client.list_specialities.return_value = []

		self.assertEqual(get_specialities(), {"specialities": []})

	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_not_provisioned_raises(self, mock_current_provider, mock_current_shukhee_user):
		mock_current_shukhee_user.side_effect = frappe.DoesNotExistError
		with self.assertRaises(frappe.DoesNotExistError):
			get_specialities()


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
		self.assertIsNone(mock_shukhee_client.book_instant_call.call_args.kwargs["clinical_data"])

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_clinical_data_is_parsed_and_passed_through(
		self,
		mock_current_provider,
		mock_current_shukhee_user,
		mock_shukhee_client,
		mock_get_doc,
		mock_commit,
	):
		mock_current_provider.return_value = "PROV-1"
		mock_current_shukhee_user.return_value = MagicMock()

		mock_shukhee_client.find_or_create_shukhee_patient.return_value = ("sk-p1", {"fullName": "Jane"})
		mock_shukhee_client.upload_medias.return_value = []
		mock_shukhee_client.resolve_speciality_id.return_value = ("1", "General Medicine")
		mock_shukhee_client.book_instant_call.return_value = "txn-1"
		mock_shukhee_client.resolve_request_id.return_value = "req-1"
		mock_shukhee_client.get_valid_token.return_value = "tok-1"
		mock_shukhee_client.build_video_call_url.return_value = "https://video/call/req-1"
		mock_get_doc.return_value = MagicMock(name="CL-1", status="pending")

		with _LocalAttr(
			"form_dict",
			{
				"contact_number": "01410820112",
				"reason": "fever",
				"requested_speciality": "General Medicine",
				"clinical_data": '{"vitals": [{"temperature": "99"}]}',
			},
		), _LocalAttr("request", MagicMock(files=None)):
			start_consultation()

		self.assertEqual(
			mock_shukhee_client.book_instant_call.call_args.kwargs["clinical_data"],
			{"vitals": [{"temperature": "99"}]},
		)

	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_missing_contact_number_raises(self, mock_current_provider, mock_current_shukhee_user):
		with _LocalAttr("form_dict", {"reason": "fever", "requested_speciality": "General Medicine"}):
			with self.assertRaises(frappe.ValidationError):
				start_consultation()
		mock_current_provider.assert_not_called()

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_media_upload_failure_does_not_block_booking(
		self,
		mock_current_provider,
		mock_current_shukhee_user,
		mock_shukhee_client,
		mock_get_doc,
		mock_commit,
	):
		"""Attaching supporting documents is optional -- a failed upload (bad
		file, Shukhee rejection, timeout) must never cost the SK the actual
		doctor call, which is the whole point of this endpoint. The file still
		gets attached locally (with no shukhee_document_id) even though
		Shukhee itself never got a copy -- there's still a local record of
		what the SK captured."""
		mock_current_provider.return_value = "PROV-1"
		shukhee_user_doc = MagicMock()
		mock_current_shukhee_user.return_value = shukhee_user_doc

		mock_shukhee_client.find_or_create_shukhee_patient.return_value = ("sk-p1", {"fullName": "Jane"})
		mock_shukhee_client.upload_medias.side_effect = Exception("Shukhee upload timed out")
		mock_shukhee_client.resolve_speciality_id.return_value = ("1", "General Medicine")
		mock_shukhee_client.book_instant_call.return_value = "txn-1"
		mock_shukhee_client.resolve_request_id.return_value = "req-1"
		mock_shukhee_client.get_valid_token.return_value = "tok-1"
		mock_shukhee_client.build_video_call_url.return_value = "https://video/call/req-1"
		call_log = MagicMock(name="CL-1", status="pending")
		mock_get_doc.return_value = call_log

		fake_request = MagicMock()
		fake_request.files.getlist.side_effect = lambda field: (
			[_FakeUploadedFile("rx.jpg", b"rx-bytes")] if field == "medias_prescription" else []
		)

		with _LocalAttr(
			"form_dict",
			{
				"contact_number": "01410820112",
				"reason": "fever",
				"requested_speciality": "General Medicine",
			},
		), _LocalAttr("request", fake_request):
			result = start_consultation()

		# The call still gets booked, with no Shukhee-side document ids, instead of the
		# upload failure aborting the whole request.
		self.assertEqual(result["call_url"], "https://video/call/req-1")
		mock_shukhee_client.book_instant_call.assert_called_once()
		self.assertEqual(
			mock_shukhee_client.book_instant_call.call_args.kwargs["medical_document_ids"], []
		)
		call_log.append.assert_called_once()
		args, _ = call_log.append.call_args
		self.assertEqual(args[0], "medias")
		self.assertEqual(args[1]["type"], "prescription")
		self.assertIsNone(args[1]["shukhee_document_id"])

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_uploaded_media_read_once_and_recorded_as_call_log_media_rows(
		self,
		mock_current_provider,
		mock_current_shukhee_user,
		mock_shukhee_client,
		mock_get_doc,
		mock_commit,
	):
		"""Each FileStorage is read exactly once upfront (not passed through directly) --
		its bytes are needed a second time afterward to attach a local copy, and a
		FileStorage's stream can't be read twice. A prescription photo and a lab report
		photo in the *same* booking each keep their own type, via separate
		medias_<type> fields and one upload_medias call per non-empty type."""
		mock_current_provider.return_value = "PROV-1"
		shukhee_user_doc = MagicMock()
		mock_current_shukhee_user.return_value = shukhee_user_doc

		mock_shukhee_client.find_or_create_shukhee_patient.return_value = ("sk-p1", {"fullName": "Jane"})
		mock_shukhee_client.upload_medias.side_effect = [["sk-file-1"], ["sk-file-2"]]
		mock_shukhee_client.attach_uploaded_media.side_effect = ["/files/CL-1-rx.jpg", "/files/CL-1-report.jpg"]
		mock_shukhee_client.resolve_speciality_id.return_value = ("1", "General Medicine")
		mock_shukhee_client.book_instant_call.return_value = "txn-1"
		mock_shukhee_client.resolve_request_id.return_value = "req-1"
		mock_shukhee_client.get_valid_token.return_value = "tok-1"
		mock_shukhee_client.build_video_call_url.return_value = "https://video/call/req-1"
		call_log = MagicMock(status="pending")
		call_log.name = "CL-1"
		mock_get_doc.return_value = call_log

		rx = _FakeUploadedFile("rx.jpg", b"rx-bytes", "image/jpeg")
		report = _FakeUploadedFile("report.jpg", b"report-bytes", "image/jpeg")
		fake_request = MagicMock()
		fake_request.files.getlist.side_effect = lambda field: {
			"medias_prescription": [rx],
			"medias_lab_report": [report],
		}.get(field, [])

		with _LocalAttr(
			"form_dict",
			{
				"contact_number": "01410820112",
				"reason": "fever",
				"requested_speciality": "General Medicine",
			},
		), _LocalAttr("request", fake_request):
			result = start_consultation()

		# One upload_medias call per non-empty type -- Shukhee's own upload API only
		# accepts one `type` per call, so a mixed-type booking can't be a single call.
		self.assertEqual(mock_shukhee_client.upload_medias.call_count, 2)
		mock_shukhee_client.upload_medias.assert_any_call(
			shukhee_user_doc, "sk-p1", [("rx.jpg", b"rx-bytes", "image/jpeg")], doc_type="prescription"
		)
		mock_shukhee_client.upload_medias.assert_any_call(
			shukhee_user_doc, "sk-p1", [("report.jpg", b"report-bytes", "image/jpeg")], doc_type="lab_report"
		)

		self.assertEqual(mock_shukhee_client.attach_uploaded_media.call_count, 2)
		mock_shukhee_client.attach_uploaded_media.assert_any_call("CL-1", "rx.jpg", b"rx-bytes", 0)
		mock_shukhee_client.attach_uploaded_media.assert_any_call("CL-1", "report.jpg", b"report-bytes", 1)

		# One Call Log Media child row per uploaded file, each keeping its own group's
		# type, its own permanent local URL, and Shukhee's own id for it.
		self.assertEqual(call_log.append.call_count, 2)
		call_log.append.assert_any_call(
			"medias",
			{"type": "prescription", "file": "/files/CL-1-rx.jpg", "shukhee_document_id": "sk-file-1"},
		)
		call_log.append.assert_any_call(
			"medias",
			{"type": "lab_report", "file": "/files/CL-1-report.jpg", "shukhee_document_id": "sk-file-2"},
		)
		call_log.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(result["call_log"], "CL-1")

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_no_media_rows_or_save_when_nothing_was_uploaded(
		self,
		mock_current_provider,
		mock_current_shukhee_user,
		mock_shukhee_client,
		mock_get_doc,
		mock_commit,
	):
		mock_current_provider.return_value = "PROV-1"
		mock_current_shukhee_user.return_value = MagicMock()
		mock_shukhee_client.find_or_create_shukhee_patient.return_value = ("sk-p1", {"fullName": "Jane"})
		mock_shukhee_client.upload_medias.return_value = []
		mock_shukhee_client.resolve_speciality_id.return_value = ("1", "General Medicine")
		mock_shukhee_client.book_instant_call.return_value = "txn-1"
		mock_shukhee_client.resolve_request_id.return_value = "req-1"
		mock_shukhee_client.get_valid_token.return_value = "tok-1"
		mock_shukhee_client.build_video_call_url.return_value = "https://video/call/req-1"
		call_log = MagicMock(status="pending")
		call_log.name = "CL-1"
		mock_get_doc.return_value = call_log

		with _LocalAttr(
			"form_dict",
			{"contact_number": "01410820112", "reason": "fever", "requested_speciality": "General Medicine"},
		), _LocalAttr("request", MagicMock(files=None)):
			start_consultation()

		mock_shukhee_client.attach_uploaded_media.assert_not_called()
		call_log.append.assert_not_called()
		call_log.save.assert_not_called()

	@patch("frappe.db.commit")
	@patch("frappe.get_doc")
	@patch("shukhee_integration.api.consultation.shukhee_client")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("shukhee_integration.api.consultation._current_provider")
	def test_attach_failure_on_one_file_does_not_block_booking_or_the_other_file(
		self,
		mock_current_provider,
		mock_current_shukhee_user,
		mock_shukhee_client,
		mock_get_doc,
		mock_commit,
	):
		"""A local-attachment failure (disk issue, File doctype validation, etc.) must never
		cost the SK the call, which already succeeded by this point -- and must not stop
		the *other* uploaded files from still being attached and recorded."""
		mock_current_provider.return_value = "PROV-1"
		mock_current_shukhee_user.return_value = MagicMock()
		mock_shukhee_client.find_or_create_shukhee_patient.return_value = ("sk-p1", {"fullName": "Jane"})
		mock_shukhee_client.upload_medias.return_value = ["sk-file-1", "sk-file-2"]
		mock_shukhee_client.resolve_speciality_id.return_value = ("1", "General Medicine")
		mock_shukhee_client.book_instant_call.return_value = "txn-1"
		mock_shukhee_client.resolve_request_id.return_value = "req-1"
		mock_shukhee_client.get_valid_token.return_value = "tok-1"
		mock_shukhee_client.build_video_call_url.return_value = "https://video/call/req-1"
		call_log = MagicMock(status="pending")
		call_log.name = "CL-1"
		mock_get_doc.return_value = call_log
		mock_shukhee_client.attach_uploaded_media.side_effect = [Exception("disk full"), "/files/CL-1-report.jpg"]

		fake_request = MagicMock()
		fake_request.files.getlist.side_effect = lambda field: (
			[_FakeUploadedFile("rx.jpg", b"rx-bytes"), _FakeUploadedFile("report.jpg", b"report-bytes")]
			if field == "medias_other"
			else []
		)

		with _LocalAttr(
			"form_dict",
			{"contact_number": "01410820112", "reason": "fever", "requested_speciality": "General Medicine"},
		), _LocalAttr("request", fake_request):
			result = start_consultation()

		self.assertEqual(result["call_url"], "https://video/call/req-1")
		self.assertEqual(mock_shukhee_client.attach_uploaded_media.call_count, 2)
		# Only the file that actually attached successfully gets a child row.
		call_log.append.assert_called_once_with(
			"medias",
			{"type": "other", "file": "/files/CL-1-report.jpg", "shukhee_document_id": "sk-file-2"},
		)
		call_log.save.assert_called_once_with(ignore_permissions=True)


class TestGetConsultationStatus(unittest.TestCase):

	@patch("frappe.get_doc")
	def test_terminal_status_short_circuits_no_shukhee_call(self, mock_get_doc):
		mock_get_doc.return_value = _call_log_doc(
			status="completed",
			prescription_link="/files/rx.pdf",
			doctor_name="Dr. A",
			appointment_status="Completed",
			clinical_data={"diagnosis": ["Common cold"]},
		)
		with patch("shukhee_integration.api.consultation.shukhee_client") as mock_client:
			result = get_consultation_status(payload='{"call_log": "CL-1"}')
			mock_client.get_emergency_request.assert_not_called()
		self.assertEqual(result["status"], "completed")
		self.assertEqual(result["prescription_link"], "/files/rx.pdf")
		self.assertEqual(result["doctor_name"], "Dr. A")
		self.assertEqual(result["appointment_status"], "Completed")
		self.assertEqual(result["clinical_data"], {"diagnosis": ["Common cold"]})

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
				"appointment": {"status": "ConsultationStarted"},
				"doctor": {"name": "Dr. B", "specialty": {"title": "Pediatrics"}, "working_at": "Clinic X"},
			}
			result = get_consultation_status(payload='{"call_log": "CL-1"}')

		self.assertEqual(result["doctor_name"], "Dr. B")
		self.assertEqual(result["doctor_speciality"], "Pediatrics")
		self.assertEqual(result["doctor_facility"], "Clinic X")
		self.assertEqual(result["appointment_status"], "ConsultationStarted")
		self.assertIsNone(result["clinical_data"])
		mock_set_value.assert_any_call("Call Logs", "CL-1", "doctor_name", "Dr. B")
		mock_set_value.assert_any_call("Call Logs", "CL-1", "appointment_status", "ConsultationStarted")

	@patch("frappe.db.commit")
	@patch("frappe.db.set_value")
	@patch("shukhee_integration.api.consultation._current_shukhee_user")
	@patch("frappe.get_doc")
	def test_no_clinical_data_when_appointment_status_not_completed(
		self, mock_get_doc, mock_current_shukhee_user, mock_set_value, mock_commit
	):
		"""appointment.status can reach ConsultationEnd before the OUTER status is
		"completed" -- clinical_data must not be read/stored until the outer status
		says the vendor has actually finished writing it."""
		mock_get_doc.return_value = _call_log_doc(status="in-progress")
		mock_current_shukhee_user.return_value = MagicMock()

		with patch("shukhee_integration.api.consultation.shukhee_client") as mock_client:
			mock_client.get_emergency_request.return_value = {
				"status": "in-progress",
				"appointment": {"status": "ConsultationEnd", "clinicalData": {"diagnosis": ["Too early"]}},
			}
			result = get_consultation_status(payload='{"call_log": "CL-1"}')

		self.assertEqual(result["appointment_status"], "ConsultationEnd")
		self.assertIsNone(result["clinical_data"])
		for call in mock_set_value.call_args_list:
			self.assertNotEqual(call.args[2], "clinical_data")

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
					"status": "Completed",
					"prescriptionLink": "https://shukhee/rx.pdf",
					"invoiceLink": "https://shukhee/inv.pdf",
					"clinicalData": {
						"chiefComplaints": ["fever"],
						"diagnosis": ["Fever of other or unknown origin"],
						"medicine": [{"brandName": "Napa", "dosage": "500 mg"}],
					},
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
		self.assertEqual(result["appointment_status"], "Completed")
		self.assertEqual(
			result["clinical_data"],
			{
				"chiefComplaints": ["fever"],
				"diagnosis": ["Fever of other or unknown origin"],
				"medicine": [{"brandName": "Napa", "dosage": "500 mg"}],
			},
		)
		# frappe.db.set_value does NOT auto-serialize JSON fieldtype values -- the
		# persisted value must already be a json.dumps'd string, not a raw dict.
		clinical_data_calls = [c for c in mock_set_value.call_args_list if c.args[2] == "clinical_data"]
		self.assertEqual(len(clinical_data_calls), 1)
		self.assertIsInstance(clinical_data_calls[0].args[3], str)
		self.assertEqual(json.loads(clinical_data_calls[0].args[3]), result["clinical_data"])


class TestGetPrescription(unittest.TestCase):

	@patch("frappe.get_doc")
	def test_completed_returns_links(self, mock_get_doc):
		mock_get_doc.return_value = _call_log_doc(
			status="completed",
			prescription_link="/files/rx.pdf",
			invoice_link="/files/inv.pdf",
			appointment_status="Completed",
			clinical_data={"diagnosis": ["Common cold"]},
		)
		result = get_prescription(payload='{"call_log": "CL-1"}')
		self.assertEqual(result["prescription_link"], "/files/rx.pdf")
		self.assertEqual(result["appointment_status"], "Completed")
		self.assertEqual(result["clinical_data"], {"diagnosis": ["Common cold"]})

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
