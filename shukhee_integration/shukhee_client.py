"""
Low-level HTTP client for the external Shukhee (SSK) teleconsultation API.

Source of truth: /Users/amresh/labs/UHIS/leapfrog-setup/sukhee-integration/postman
(Shukhee-SSK-Integration.postman_collection.json + Shukhee-SSK-Dev.postman_environment.json),
the SSK Third-Party Video Call Integration doc, and the Teleconsultation Integration
Workflow doc (LEAPWELL <-> Shukhee).

Unlike uhis_next_core/fhir/push.py (fire-and-forget background push, log-and-continue on
failure), every call here is inside a synchronous, user-facing request -- the caller is
waiting on the response -- so failures are raised via frappe.throw rather than swallowed.
"""

import base64
import json
import time

import frappe
import requests
from frappe import _

_TOKEN_REFRESH_BUFFER_S = 30
_DEFAULT_TOKEN_TTL_S = 3300  # fallback if the JWT has no exp claim
_REQUEST_TIMEOUT_S = 10


def _settings():
	return frappe.get_single("Shukhee Settings")


def _api_base():
	return _settings().base_url.rstrip("/")


def _video_call_base():
	return _settings().video_call_base_url.rstrip("/")


def _decode_jwt_exp_ms(token):
	"""Decode a JWT's `exp` claim without verifying the signature -- mirrors the Postman
	collection's own pre-request script exactly. Returns None if undecodable."""
	try:
		payload_b64 = token.split(".")[1]
		padded = payload_b64 + "=" * (-len(payload_b64) % 4)
		claims = json.loads(base64.urlsafe_b64decode(padded))
		exp = claims.get("exp")
		return exp * 1000 if isinstance(exp, (int, float)) else None
	except Exception:
		return None


def _request(method, url, **kwargs):
	try:
		resp = requests.request(method, url, timeout=_REQUEST_TIMEOUT_S, **kwargs)
		resp.raise_for_status()
		return resp.json()
	except requests.RequestException as e:
		frappe.log_error(frappe.get_traceback(), f"Shukhee API call failed: {method} {url}")
		frappe.throw(_("Shukhee API call failed: {0}").format(str(e)), frappe.ValidationError)


# ── auth ─────────────────────────────────────────────────────────────────────


def login(username, password):
	"""POST /ssk/login-v2 -- exchanges Shukhee credentials for an access/refresh token pair."""
	body = {"password": password, "channel": "sskPortal", "portal": "third-party", "email": username}
	data = _request("POST", f"{_api_base()}/ssk/login-v2", json=body)
	if not data.get("success") or not data.get("data", {}).get("accessToken"):
		frappe.throw(_("Shukhee login failed."), frappe.AuthenticationError)
	return data["data"]


def refresh(refresh_token):
	"""POST /ssk/refresh -- exchanges a valid refresh token for a new token pair."""
	data = _request("POST", f"{_api_base()}/ssk/refresh", json={"refreshToken": refresh_token})
	if not data.get("success") or not data.get("data", {}).get("accessToken"):
		frappe.throw(_("Shukhee token refresh failed."), frappe.AuthenticationError)
	return data["data"]


def get_valid_token(shukhee_user_doc):
	"""Returns a live Shukhee access token for this UHIS Shukhee User, reusing a cached one
	when possible. Caches via frappe.cache() (not doctype fields) keyed per record."""
	cache_key = f"shukhee_token:{shukhee_user_doc.name}"
	cached = frappe.cache().get_value(cache_key)
	now_ms = time.time() * 1000

	if cached and cached.get("expires_at", 0) > now_ms + (_TOKEN_REFRESH_BUFFER_S * 1000):
		return cached["access_token"]

	token_data = None
	if cached and cached.get("refresh_token"):
		try:
			token_data = refresh(cached["refresh_token"])
		except Exception:
			token_data = None  # fall through to full login

	if not token_data:
		token_data = login(
			shukhee_user_doc.shukhee_username, shukhee_user_doc.get_password("shukhee_password")
		)

	access_token = token_data["accessToken"]
	refresh_token = token_data.get("refreshToken")
	expires_at_ms = _decode_jwt_exp_ms(access_token) or (now_ms + _DEFAULT_TOKEN_TTL_S * 1000)

	frappe.cache().set_value(
		cache_key,
		{"access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at_ms},
		expires_in_sec=max(int((expires_at_ms - now_ms) / 1000), 60),
	)
	return access_token


def _auth_headers(token):
	return {"Authorization": f"Bearer {token}"}


# ── specialities ─────────────────────────────────────────────────────────────


def list_specialities(token):
	"""GET /patient/emergency-request-specialities -- selectable specialities for booking."""
	data = _request(
		"GET",
		f"{_api_base()}/patient/emergency-request-specialities",
		params={"page": 0, "size": 20},
		headers=_auth_headers(token),
	)
	return (data.get("data") or {}).get("data") or []


def resolve_speciality_id(token, requested_speciality):
	"""Matches requested_speciality (free text from the app) against the real specialities
	list by title (case-insensitive); falls back to the first entry if no match, mirroring
	the Postman collection's own Tests-script default behavior."""
	specialities = list_specialities(token)
	if not specialities:
		frappe.throw(_("Shukhee returned no available specialities."), frappe.ValidationError)

	wanted = (requested_speciality or "").strip().lower()
	for s in specialities:
		if (s.get("title") or "").strip().lower() == wanted:
			return s["specialityId"], s["title"]

	first = specialities[0]
	return first["specialityId"], first["title"]


# ── patients ─────────────────────────────────────────────────────────────────


def _map_gender(gender):
	"""This platform's Patient.gender options (Male/Female/Other/Prefer not to say) don't
	map 1:1 onto Shukhee's (Male/Female/Others) -- anything not Male/Female becomes Others."""
	if gender == "Male":
		return "Male"
	if gender == "Female":
		return "Female"
	return "Others"


def _find_uhis_patient(contact_number):
	return frappe.db.get_value(
		"Patient", {"phone": contact_number}, ["full_name", "dob", "gender"], as_dict=True
	)


def find_or_create_shukhee_patient(token, contact_number):
	"""Resolves our own Patient by phone (throws if not found -- never fabricates
	dob/gender to send to Shukhee), then finds or creates the corresponding Shukhee-side
	patient record. Returns the Shukhee patient id."""
	uhis_patient = _find_uhis_patient(contact_number)
	if not uhis_patient:
		frappe.throw(
			_("No Patient record found for contact number {0}.").format(contact_number),
			frappe.DoesNotExistError,
		)

	existing = _request(
		"GET",
		f"{_api_base()}/ssk/patient-list",
		params={"page": 0, "size": 20, "search": contact_number},
		headers=_auth_headers(token),
	)
	found = ((existing.get("data") or {}).get("data")) or []
	if found:
		return found[0].get("id") or found[0].get("patientId")

	created = _request(
		"POST",
		f"{_api_base()}/v2/patient/person-patient-create",
		json={
			"name": uhis_patient.full_name,
			"mobile": contact_number,
			"dob": str(uhis_patient.dob) if uhis_patient.dob else None,
			"gender": _map_gender(uhis_patient.gender),
		},
		headers=_auth_headers(token),
	)
	patient_id = (created.get("data") or {}).get("id") or (created.get("data") or {}).get("patientId")
	if patient_id:
		return patient_id

	# Sandbox docs note Create Patient's example response doesn't reliably include a top-level
	# id -- fall back to searching again, same recovery path the collection itself documents.
	relist = _request(
		"GET",
		f"{_api_base()}/ssk/patient-list",
		params={"page": 0, "size": 20, "search": contact_number},
		headers=_auth_headers(token),
	)
	relist_found = ((relist.get("data") or {}).get("data")) or []
	if relist_found:
		return relist_found[0].get("id") or relist_found[0].get("patientId")

	frappe.throw(
		_("Could not resolve a Shukhee patient id for contact number {0}.").format(contact_number),
		frappe.ValidationError,
	)


# ── medical documents ────────────────────────────────────────────────────────


def upload_medias(token, shukhee_patient_id, files):
	"""One multipart POST /ssk/medical-document/upload call with all files as `attachments`.
	`files` is a list of werkzeug FileStorage objects. Returns a list of Shukhee file ids."""
	if not files:
		return []
	multipart_files = [("attachments", (f.filename, f.stream, f.mimetype)) for f in files]
	data = _request(
		"POST",
		f"{_api_base()}/ssk/medical-document/upload",
		data={"type": "other", "patientId": shukhee_patient_id},
		files=multipart_files,
		headers=_auth_headers(token),
	)
	uploaded = (data.get("data") or {}).get("files") or []
	return [f["id"] for f in uploaded if f.get("id")]


# ── booking + polling ────────────────────────────────────────────────────────


def book_instant_call(
	token,
	*,
	shukhee_patient_id,
	contact_number,
	reason,
	speciality_id,
	requested_speciality,
	call_type="video",
	medical_document_ids=None,
):
	"""POST /ssk/emergency-request/v3 -- books the instant call. Returns transaction_id;
	the sandbox response does NOT include the new request's id -- see resolve_request_id."""
	form = {
		"channel": "sskPortal",
		"call_type": call_type,
		"creatorType": "sskuser",
		"reason": reason,
		"specialityId": speciality_id,
		"requested_speciality": requested_speciality,
		"patientId": shukhee_patient_id,
		"contact": contact_number,
		"serviceSlug": "instant-call",
		"gatewayType": "ssk-balance",
	}
	if medical_document_ids:
		form["medicalDocumentIds"] = ",".join(str(i) for i in medical_document_ids)

	data = _request(
		"POST", f"{_api_base()}/ssk/emergency-request/v3", data=form, headers=_auth_headers(token)
	)
	if not data.get("success"):
		frappe.throw(_("Booking the Shukhee instant call failed."), frappe.ValidationError)
	return (data.get("data") or {}).get("transactionId")


def resolve_request_id(token, contact_number, attempts=3, delay_s=1):
	"""Book Instant Call doesn't return the new request's id -- resolve it by listing
	emergency requests and matching the newest entry for this contact number. Known
	fragility: no guaranteed unique match key in the sandbox docs; bounded retry since the
	record may not be listable immediately after booking."""
	last_digits = contact_number[-10:] if contact_number else contact_number
	for attempt in range(attempts):
		data = _request(
			"GET",
			f"{_api_base()}/ssk/all-emergency-request",
			params={"page": 0, "size": 10, "search": last_digits},
			headers=_auth_headers(token),
		)
		found = ((data.get("data") or {}).get("data")) or []
		if found:
			return found[0]["id"]
		if attempt < attempts - 1:
			time.sleep(delay_s)

	frappe.throw(
		_("Could not resolve the Shukhee request id for contact number {0}.").format(contact_number),
		frappe.ValidationError,
	)


def get_emergency_request(token, request_id):
	"""GET /patient/emergency-request/:id -- status + appointment.prescriptionLink/invoiceLink."""
	data = _request(
		"GET", f"{_api_base()}/patient/emergency-request/{request_id}", headers=_auth_headers(token)
	)
	return data.get("data") or {}


def build_video_call_url(token, request_id):
	"""Pure string builder -- per the integration doc this is a WebView navigation target,
	never fetched as JSON. Case-sensitive param names/values, per the contract."""
	return (
		f"{_video_call_base()}/video-call/third-party"
		f"?token={token}&id={request_id}&consultation_type=instant-call&auto_join=true"
	)


# ── prescription/invoice download ───────────────────────────────────────────


def download_and_attach(doctype, docname, fieldname, url):
	"""Downloads a (short-lived, presigned) Shukhee document link and attaches it as a
	permanent Frappe File on the given record. Returns the permanent /files/... URL."""
	from frappe.utils.file_manager import save_file

	try:
		resp = requests.get(url, timeout=_REQUEST_TIMEOUT_S)
		resp.raise_for_status()
	except requests.RequestException:
		frappe.log_error(frappe.get_traceback(), f"Shukhee document download failed: {url}")
		frappe.throw(_("Could not download the document from Shukhee."), frappe.ValidationError)

	file_doc = save_file(
		f"{docname}-{fieldname}.pdf", resp.content, doctype, docname, is_private=1
	)
	return file_doc.file_url
