"""
Consultation flow endpoints — video-consultation via the external Shukhee (SSK) system.

  shukhee_integration.api.consultation.start_consultation        SK taps "Start Consultation"
  shukhee_integration.api.consultation.get_consultation_status    polled by the client
  shukhee_integration.api.consultation.get_prescription           "View Prescription" button

Shukhee has no webhooks — status is observed by polling get_emergency_request (see
shukhee_client.py). There is no "end consultation" call: the doctor ends the call on
Shukhee's own side, and the client observes that transition itself (the video-call WebView
redirects/closes); this module only ever reports Shukhee's own status back.
"""

import frappe
from frappe import _

from shukhee_integration import shukhee_client
from uhis_next_core.auth.decorators import current_remote_user_id, whitelist

_TERMINAL_STATUSES = {"completed", "rejected", "cancelled", "on-hold"}


def _resolve_env(payload=None):
	"""Same dual-mode parsing as uhis_next_core.api.sync — accepts either a legacy `payload`
	string or the raw JSON body merged into form_dict."""
	if payload:
		return frappe.parse_json(payload)
	return frappe.local.form_dict


def _current_provider():
	user_id = current_remote_user_id()
	provider_name = frappe.db.get_value("Provider", {"user": user_id}, "name")
	if not provider_name:
		frappe.throw(_("No Provider record found for the calling user."), frappe.DoesNotExistError)
	return provider_name


def _current_shukhee_user(provider_name):
	if not frappe.db.exists("UHIS Shukhee User", provider_name):
		frappe.throw(
			_("No Shukhee credentials configured for this Provider."), frappe.DoesNotExistError
		)
	doc = frappe.get_doc("UHIS Shukhee User", provider_name)
	if doc.status != "Active":
		frappe.throw(_("This Provider's Shukhee account is inactive."), frappe.ValidationError)
	return doc


@whitelist(methods=["POST"], remote_auth=True)
def start_consultation():
	"""Books an instant call with Shukhee and returns the video-call join URL. Multipart
	form fields: contact_number, reason, requested_speciality, encounter_id (optional),
	patient_name/patient_dob/patient_gender (optional -- only needed to create a new
	Shukhee patient when neither Shukhee nor this platform's own Patient doctype already
	has a record for contact_number), medias (files, optional)."""
	env = frappe.local.form_dict
	contact_number = env.get("contact_number")
	reason = env.get("reason")
	requested_speciality = env.get("requested_speciality")
	encounter_id = env.get("encounter_id")
	patient_name = env.get("patient_name")
	patient_dob = env.get("patient_dob")
	patient_gender = env.get("patient_gender")

	if not contact_number or not reason or not requested_speciality:
		frappe.throw(
			_("contact_number, reason, and requested_speciality are required."), frappe.ValidationError
		)

	provider_name = _current_provider()
	shukhee_user_doc = _current_shukhee_user(provider_name)
	token = shukhee_client.get_valid_token(shukhee_user_doc)

	shukhee_patient_id, patient_details = shukhee_client.find_or_create_shukhee_patient(
		token, contact_number, patient_name, patient_dob, patient_gender
	)

	media_files = frappe.request.files.getlist("medias") if frappe.request.files else []
	medical_document_ids = shukhee_client.upload_medias(token, shukhee_patient_id, media_files)

	speciality_id, matched_speciality = shukhee_client.resolve_speciality_id(
		token, requested_speciality
	)

	transaction_id = shukhee_client.book_instant_call(
		token,
		shukhee_patient_id=shukhee_patient_id,
		patient_details=patient_details,
		contact_number=contact_number,
		reason=reason,
		speciality_id=speciality_id,
		requested_speciality=matched_speciality,
		call_type="video",
		medical_document_ids=medical_document_ids,
	)

	request_id = shukhee_client.resolve_request_id(token, contact_number)
	call_url = shukhee_client.build_video_call_url(token, request_id)

	call_log = frappe.get_doc(
		{
			"doctype": "Call Logs",
			"shukhee_user": shukhee_user_doc.name,
			"uhis_user": provider_name,
			"patient_id": shukhee_patient_id,
			"contact_number": contact_number,
			"encounter_id": encounter_id,
			"reason": reason,
			"speciality_id": speciality_id,
			"requested_speciality": matched_speciality,
			"call_type": "video",
			"request_id": request_id,
			"transaction_id": transaction_id,
			"call_url": call_url,
			"status": "pending",
		}
	)
	call_log.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"call_log": call_log.name, "call_url": call_url, "status": call_log.status}


@whitelist(methods=["POST"], remote_auth=True)
def get_consultation_status(payload=None):
	"""Polled by the client. Returns cached status/links with no external call once the
	consultation has reached a terminal state; otherwise checks Shukhee live."""
	env = _resolve_env(payload)
	call_log_name = env.get("call_log")
	if not call_log_name:
		frappe.throw(_("call_log is required."), frappe.ValidationError)

	doc = frappe.get_doc("Call Logs", call_log_name)

	if doc.status in _TERMINAL_STATUSES:
		return {
			"status": doc.status,
			"prescription_link": doc.prescription_link,
			"invoice_link": doc.invoice_link,
		}

	provider_name = doc.uhis_user
	shukhee_user_doc = _current_shukhee_user(provider_name)
	token = shukhee_client.get_valid_token(shukhee_user_doc)

	detail = shukhee_client.get_emergency_request(token, doc.request_id)
	new_status = detail.get("status") or doc.status

	updates = {"status": new_status}
	if new_status == "completed":
		appointment = detail.get("appointment") or {}
		if appointment.get("prescriptionLink"):
			updates["prescription_link"] = shukhee_client.download_and_attach(
				"Call Logs", doc.name, "prescription", appointment["prescriptionLink"]
			)
		if appointment.get("invoiceLink"):
			updates["invoice_link"] = shukhee_client.download_and_attach(
				"Call Logs", doc.name, "invoice", appointment["invoiceLink"]
			)

	for field, value in updates.items():
		frappe.db.set_value("Call Logs", doc.name, field, value)
	frappe.db.commit()

	return {
		"status": updates.get("status", doc.status),
		"prescription_link": updates.get("prescription_link", doc.prescription_link),
		"invoice_link": updates.get("invoice_link", doc.invoice_link),
	}


@whitelist(methods=["POST"], remote_auth=True)
def get_prescription(payload=None):
	"""Returns the already-downloaded prescription/invoice links for a completed consultation."""
	env = _resolve_env(payload)
	call_log_name = env.get("call_log")
	if not call_log_name:
		frappe.throw(_("call_log is required."), frappe.ValidationError)

	doc = frappe.get_doc("Call Logs", call_log_name)
	if doc.status != "completed":
		frappe.throw(_("This consultation has not completed yet."), frappe.ValidationError)

	return {"prescription_link": doc.prescription_link, "invoice_link": doc.invoice_link}
