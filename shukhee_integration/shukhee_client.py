"""
Low-level HTTP client for the external Shukhee (SSK) teleconsultation API.

Source of truth: /Users/amresh/labs/UHIS/leapfrog-setup/sukhee-integration/postman
(Shukhee-SSK-Integration.postman_collection.json + Shukhee-SSK-Dev.postman_environment.json),
the SSK Third-Party Video Call Integration doc, and the Teleconsultation Integration
Workflow doc (LEAPWELL <-> Shukhee).

Unlike spice_next_core/fhir/push.py (fire-and-forget background push, log-and-continue on
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


class ShukheeAuthExpired(Exception):
	"""Internal signal only: Shukhee rejected a request with 401 even though our
	cached token looked unexpired (clock skew, server-side revocation, etc.).
	Raised by _request only when explicitly asked to (raise_on_401=True) --
	login()/refresh() never opt in, so a real bad-credentials 401 from those
	still surfaces as the normal frappe.throw below, not this signal. Caught
	only by _authed_request, which forces a fresh login and retries once;
	never expected to escape this module."""


def _request(method, url, *, raise_on_401=False, **kwargs):
	try:
		resp = requests.request(method, url, timeout=_REQUEST_TIMEOUT_S, **kwargs)
		resp.raise_for_status()
		return resp.json()
	except requests.HTTPError as e:
		if raise_on_401 and e.response is not None and e.response.status_code == 401:
			raise ShukheeAuthExpired() from e
		# Surface Shukhee's own validation message (e.g. "reason" field name,
		# missing/invalid param) instead of just the bare status code -- this
		# is the actual actionable detail for both logs and the end user.
		detail = None
		try:
			detail = e.response.json()
		except Exception:
			detail = e.response.text[:500] if e.response is not None else None
		frappe.log_error(
			frappe.get_traceback(), f"Shukhee API call failed: {method} {url} -- {detail}"
		)
		frappe.throw(_("Shukhee API call failed: {0} -- {1}").format(str(e), detail), frappe.ValidationError)
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


def _authed_request(method, url, shukhee_user_doc, **kwargs):
	"""The login -> cache -> reuse -> relogin-on-401 contract for every
	authenticated Shukhee call. Uses get_valid_token's cache first (cheap, no
	network round trip when unexpired); if Shukhee itself rejects that token
	live with a 401 -- meaning our cache thought it was valid but Shukhee
	disagrees -- invalidates the cache, forces a fresh login, and retries the
	same request exactly once. A 401 on that retry falls through to the
	normal frappe.throw(ValidationError) path with full detail rather than
	looping."""
	token = get_valid_token(shukhee_user_doc)
	try:
		return _request(method, url, headers=_auth_headers(token), raise_on_401=True, **kwargs)
	except ShukheeAuthExpired:
		frappe.cache().delete_value(f"shukhee_token:{shukhee_user_doc.name}")
		token = get_valid_token(shukhee_user_doc)
		return _request(method, url, headers=_auth_headers(token), **kwargs)


def _extract_list(data):
	"""Shukhee's list endpoints aren't consistent about whether `data` is the
	array directly (confirmed for /ssk/patient-list: {totalItems, data: [...],
	page, size, hasNext}, no `success` key) or a wrapper object with a nested
	`data` array (confirmed for /patient/emergency-request-specialities:
	{success, data: {data: [...]}}). Handle both rather than guess per call site."""
	inner = data.get("data")
	if isinstance(inner, list):
		return inner
	if isinstance(inner, dict):
		return inner.get("data") or []
	return []


# ── specialities ─────────────────────────────────────────────────────────────


def list_specialities(shukhee_user_doc):
	"""GET /patient/emergency-request-specialities -- selectable specialities for booking."""
	data = _authed_request(
		"GET",
		f"{_api_base()}/patient/emergency-request-specialities",
		shukhee_user_doc,
		params={"page": 0, "size": 20},
	)
	return _extract_list(data)


def resolve_speciality_id(shukhee_user_doc, requested_speciality):
	"""Matches requested_speciality (free text from the app) against the real specialities
	list by title (case-insensitive); falls back to the first entry if no match, mirroring
	the Postman collection's own Tests-script default behavior."""
	specialities = list_specialities(shukhee_user_doc)
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


def find_or_create_shukhee_patient(
	shukhee_user_doc, contact_number, patient_name=None, patient_dob=None, patient_gender=None
):
	"""Finds the Shukhee-side patient for this contact number, creating one if needed.
	Returns (shukhee_patient_id, patient_details) -- Book Instant Call requires
	`patientDetails` in the payload even when `patientId` also references an existing
	patient ("Field 'patientDetails' doesn't have a default value" if omitted), so
	callers need both, not just the id.

	Checks Shukhee's own patient list first -- if already registered there, patient_details
	is built from that record directly, no local data needed. Only when Shukhee doesn't
	already have this patient does creating one require real name/dob/gender from
	somewhere: this platform's own Patient doctype (matched by phone) if one exists, else
	the explicit patient_name/patient_dob/patient_gender args (the caller's own patient
	context -- e.g. from a household visit not yet linked to a Patient record here).
	Never fabricates demographic data -- throws a clear error if neither source has it,
	rather than sending Shukhee guessed/placeholder values for a real patient."""
	existing = _authed_request(
		"GET",
		f"{_api_base()}/ssk/patient-list",
		shukhee_user_doc,
		params={"page": 0, "size": 20, "search": contact_number},
	)
	found = _extract_list(existing)
	if found:
		record = found[0]
		patient_id = record.get("id") or record.get("patientId")
		patient_details = {
			"fullName": record.get("name") or record.get("fullName"),
			"mobile": contact_number,
			"dob": (record.get("dob") or "")[:10] or None,
			"gender": _map_gender(record.get("gender")),
		}
		return patient_id, patient_details

	uhis_patient = _find_uhis_patient(contact_number)
	if uhis_patient:
		name = uhis_patient.full_name
		dob = str(uhis_patient.dob) if uhis_patient.dob else None
		gender = _map_gender(uhis_patient.gender)
	elif patient_name and patient_dob and patient_gender:
		name, dob, gender = patient_name, patient_dob, _map_gender(patient_gender)
	else:
		frappe.throw(
			_(
				"No Patient record found for contact number {0}, and patient_name/patient_dob/"
				"patient_gender were not provided to create one on Shukhee."
			).format(contact_number),
			frappe.DoesNotExistError,
		)

	patient_details = {"fullName": name, "mobile": contact_number, "dob": dob, "gender": gender}

	created = _authed_request(
		"POST",
		f"{_api_base()}/v2/patient/person-patient-create",
		shukhee_user_doc,
		json={"name": name, "mobile": contact_number, "dob": dob, "gender": gender},
	)
	patient_id = (created.get("data") or {}).get("id") or (created.get("data") or {}).get("patientId")
	if patient_id:
		return patient_id, patient_details

	# Sandbox docs note Create Patient's example response doesn't reliably include a top-level
	# id -- fall back to searching again, same recovery path the collection itself documents.
	relist = _authed_request(
		"GET",
		f"{_api_base()}/ssk/patient-list",
		shukhee_user_doc,
		params={"page": 0, "size": 20, "search": contact_number},
	)
	relist_found = _extract_list(relist)
	if relist_found:
		patient_id = relist_found[0].get("id") or relist_found[0].get("patientId")
		return patient_id, patient_details

	frappe.throw(
		_("Could not resolve a Shukhee patient id for contact number {0}.").format(contact_number),
		frappe.ValidationError,
	)


# ── medical documents ────────────────────────────────────────────────────────


def upload_medias(shukhee_user_doc, shukhee_patient_id, files):
	"""One multipart POST /ssk/medical-document/upload call with all files as `attachments`.
	`files` is a list of werkzeug FileStorage objects. Returns a list of Shukhee file ids."""
	if not files:
		return []
	multipart_files = [("attachments", (f.filename, f.stream, f.mimetype)) for f in files]
	data = _authed_request(
		"POST",
		f"{_api_base()}/ssk/medical-document/upload",
		shukhee_user_doc,
		data={"type": "other", "patientId": shukhee_patient_id},
		files=multipart_files,
	)
	uploaded = data.get("data", {}).get("files") if isinstance(data.get("data"), dict) else None
	uploaded = uploaded or []
	return [f["id"] for f in uploaded if f.get("id")]


# ── booking + polling ────────────────────────────────────────────────────────


def book_instant_call(
	shukhee_user_doc,
	*,
	shukhee_patient_id,
	patient_details,
	contact_number,
	reason,
	speciality_id,
	requested_speciality,
	call_type="video",
	medical_document_ids=None,
):
	"""POST /ssk/emergency-request/v3 -- books the instant call. Returns transaction_id;
	the sandbox response does NOT include the new request's id -- see resolve_request_id.

	`patientDetails` is required by the live API even when `patientId` already references
	an existing patient -- confirmed against the real sandbox: omitting it fails with
	"Field 'patientDetails' doesn't have a default value" (422), contradicting the
	Postman collection's own description, which implied patientId alone was sufficient."""
	form = {
		"channel": "sskPortal",
		"call_type": call_type,
		"creatorType": "sskuser",
		"reason": reason,
		"specialityId": speciality_id,
		"requested_speciality": requested_speciality,
		"patientDetails": frappe.as_json(patient_details),
		"patientId": shukhee_patient_id,
		"contact": contact_number,
		"serviceSlug": "instant-call",
		"gatewayType": "ssk-balance",
	}
	if medical_document_ids:
		form["medicalDocumentIds"] = ",".join(str(i) for i in medical_document_ids)

	data = _authed_request(
		"POST", f"{_api_base()}/ssk/emergency-request/v3", shukhee_user_doc, data=form
	)
	if not data.get("success"):
		frappe.throw(_("Booking the Shukhee instant call failed."), frappe.ValidationError)
	return (data.get("data") or {}).get("transactionId")


def resolve_request_id(shukhee_user_doc, contact_number, attempts=3, delay_s=1):
	"""Book Instant Call doesn't return the new request's id -- resolve it by listing
	emergency requests and matching the newest entry for this contact number. Known
	fragility: no guaranteed unique match key in the sandbox docs; bounded retry since the
	record may not be listable immediately after booking.

	`/ssk/all-emergency-request` returns results oldest-first (confirmed against the live
	sandbox: ascending createdAt) with no way to request reverse order, and paginates --
	for a contact number with more history than one page, the newest request lands on the
	*last* page, not page 0 (confirmed: totalItems=15/size=10 put the true newest request on
	page 1, invisible to a page-0-only fetch no matter how that page is sorted). So: read
	`pagination.totalItems` off page 0, fetch the actual last page, then pick the max by
	createdAt from there -- never trust page 0 alone once a number has any real history."""
	last_digits = contact_number[-10:] if contact_number else contact_number
	size = 10
	for attempt in range(attempts):
		data = _authed_request(
			"GET",
			f"{_api_base()}/ssk/all-emergency-request",
			shukhee_user_doc,
			params={"page": 0, "size": size, "search": last_digits},
		)
		found = _extract_list(data)
		total_items = ((data.get("data") or {}).get("pagination") or {}).get("totalItems") or len(found)
		last_page = max((total_items - 1) // size, 0)
		if last_page > 0:
			data = _authed_request(
				"GET",
				f"{_api_base()}/ssk/all-emergency-request",
				shukhee_user_doc,
				params={"page": last_page, "size": size, "search": last_digits},
			)
			found = _extract_list(data) or found
		if found:
			newest = max(found, key=lambda r: r.get("createdAt") or "")
			return newest["id"]
		if attempt < attempts - 1:
			time.sleep(delay_s)

	frappe.throw(
		_("Could not resolve the Shukhee request id for contact number {0}.").format(contact_number),
		frappe.ValidationError,
	)


def get_emergency_request(shukhee_user_doc, request_id):
	"""GET /patient/emergency-request/:id -- status + appointment.prescriptionLink/invoiceLink."""
	data = _authed_request(
		"GET", f"{_api_base()}/patient/emergency-request/{request_id}", shukhee_user_doc
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
