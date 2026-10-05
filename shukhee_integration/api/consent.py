"""
Consent flow endpoints — patient-consent HTML shown before a Shukhee teleconsult call, and the
append-only record of the patient's actual Agree/Decline decision.

  shukhee_integration.api.consent.get_consent               fetched by the mobile app every time
                                                              the SK taps "Call a doctor now", in
                                                              the app's current language (en/bn).
  shukhee_integration.api.consent.record_consent_decision    called once the patient has
                                                              Agreed/Declined, to append a
                                                              Shukhee Consent Log row.
  shukhee_integration.api.consent.attach_consent_to_call     called once booking succeeds, to
                                                              denormalize the accepted version
                                                              onto the resulting Call Logs row.
"""

import frappe
from frappe import _

from shukhee_integration.api.consultation import _current_provider
from shukhee_integration.audit import audit_inbound
from spice_next_core.auth.decorators import whitelist

_DECISIONS = ("Agreed", "Declined")


def _resolve_env(payload=None):
	"""Same dual-mode parsing as shukhee_integration.api.consultation / spice_next_core.api.sync
	-- accepts either a legacy `payload` string or the raw JSON body merged into form_dict."""
	if payload:
		return frappe.parse_json(payload)
	return frappe.local.form_dict


@whitelist(methods=["POST"], remote_auth=True)
def get_consent(payload=None):
	"""Returns this app's current consent HTML for the requested language, falling back to
	English when the requested language has no configured row. Called by the mobile app every
	time the SK taps "Call a doctor now" -- deliberately no caching on either side, so an
	edited consent takes effect on the very next call, not the next app release. Read-only
	config fetch, no per-caller identity to check -- same shape as get_specialities in
	consultation.py, so (like that endpoint) this is NOT wrapped by audit_inbound: there's no
	call-lifecycle event here to log."""
	env = _resolve_env(payload)
	lng = env.get("lng") or "en"

	fields = ["lng", "consent", "version"]
	rows = frappe.get_all("Shukhee Consent", filters={"lng": lng}, fields=fields, limit=1)
	if not rows and lng != "en":
		rows = frappe.get_all("Shukhee Consent", filters={"lng": "en"}, fields=fields, limit=1)
	if not rows:
		frappe.throw(_("No consent content configured."), frappe.DoesNotExistError)

	return {"lng": rows[0]["lng"], "consent": rows[0]["consent"], "version": rows[0]["version"]}


@whitelist(methods=["POST"], remote_auth=True)
@audit_inbound
def record_consent_decision(payload=None):
	"""Appends a Shukhee Consent Log row once the patient has Agreed/Declined the
	teleconsult-specific consent shown by get_consent. Wrapped in audit_inbound per the
	established convention for state-changing endpoints (mirrors start_consultation) --
	that decorator logs the generic call-lifecycle event (request/response, timing,
	success/failure) independent of the domain-specific Shukhee Consent Log row this
	function itself creates below; the two are separate, complementary trails."""
	env = _resolve_env(payload)
	patient_id = env.get("patient_id")
	decision = env.get("decision")
	lng = env.get("lng")

	if not patient_id or not decision or not lng:
		frappe.throw(_("patient_id, decision, and lng are required."), frappe.ValidationError)
	if decision not in _DECISIONS:
		frappe.throw(_("decision must be one of: {0}.").format(", ".join(_DECISIONS)), frappe.ValidationError)

	# Never trust a client-supplied identity for who performed the action -- resolved
	# server-side via the same current_remote_user_id() -> Provider.username lookup
	# consultation.py's start_consultation/get_specialities/etc. already use.
	provider = _current_provider()

	frappe.get_doc(
		{
			"doctype": "Shukhee Consent Log",
			"patient_id": patient_id,
			"visit_id": env.get("visit_id"),
			"decision": decision,
			"lng": lng,
			"consent_version": env.get("consent_version"),
			"provider": provider,
			"patient_dob": env.get("patient_dob"),
			"occurred_at": frappe.utils.now_datetime(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {"logged": True}


@whitelist(methods=["POST"], remote_auth=True)
@audit_inbound
def attach_consent_to_call(payload=None):
	"""Called once start_consultation's booking succeeds, to denormalize the consent version/
	language the patient just agreed to onto the resulting Call Logs row -- the consent gate
	runs before booking, so Call Logs doesn't exist yet at get_consent/record_consent_decision
	time, and this is the first point afterward both are known. Every decision (including
	declines, which never produce a Call Logs row at all) is already durably recorded in
	Shukhee Consent Log by record_consent_decision above; this is only a convenience
	denormalization so a specific call's accepted version is visible without cross-referencing
	that log by visit_id.

	Mirrors consultation.attach_fhir_encounter_id's exact posture: only call_log is required,
	an unknown one is a no-op rather than an error (the client can't distinguish "never
	attached" from "a previous attempt succeeded but the response was lost" and must be free
	to simply retry), and a repeat call is safe (last write wins)."""
	env = _resolve_env(payload)
	call_log_name = env.get("call_log")
	if not call_log_name:
		frappe.throw(_("call_log is required."), frappe.ValidationError)

	if not frappe.db.exists("Call Logs", call_log_name):
		return {"attached": False}

	frappe.db.set_value(
		"Call Logs",
		call_log_name,
		{"consent_version": env.get("consent_version"), "consent_lng": env.get("lng")},
	)
	frappe.db.commit()
	return {"attached": True}
