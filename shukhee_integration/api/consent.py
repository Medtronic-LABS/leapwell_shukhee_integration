"""
Consent flow — patient-consent HTML shown before a Shukhee teleconsult call, and the
append-only record of the patient's actual Agree/Decline decision.

  shukhee_integration.api.consent.get_consent                fetched by the mobile app every
                                                               time the SK taps "Call a doctor
                                                               now", in the app's current
                                                               language (en/bn).
  shukhee_integration.api.consent.record_consent_decline      called immediately, fire-and-
                                                               forget, when the patient
                                                               Declines -- the only server-side
                                                               record a decline gets, since
                                                               declining never leads to booking
                                                               (no Call Logs row to record it on).

An Agreed decision is NOT recorded by a function in this module at all: the mobile client
carries the decision (version_id/lng/items_checked) forward to the booking screen and sends it
as part of `api.consultation.start_consultation`'s own request, which writes it directly onto
the Call Logs row it creates (consent_version/consent_lng/consent_items/consent_filled_text) --
no separate doctype, no after-the-fact linking step. The resolution helpers below
(resolve_version_id, resolve_authoritative_items, etc.) are shared with that function, which is
why they're named without a leading underscore -- they're a cross-module contract, not private
to this file. See shukhee_integration.providers for the current_provider()/current_shukhee_user()
split that keeps this module and api/consultation.py from importing each other directly.
"""

import frappe
from frappe import _

from shukhee_integration.audit import audit_inbound
from shukhee_integration.providers import current_provider
from spice_next_core.auth.decorators import whitelist

CONSENT_METHOD_BY_LNG = {"en": "App", "bn": "অ্যাপ"}


def _resolve_env(payload=None):
	"""Same dual-mode parsing as shukhee_integration.api.consultation / spice_next_core.api.sync
	-- accepts either a legacy `payload` string or the raw JSON body merged into form_dict."""
	if payload:
		return frappe.parse_json(payload)
	return frappe.local.form_dict


def resolve_version_id(env):
	"""Validates a client-supplied `version_id` against Shukhee Consent Version before trusting
	it as a Link value -- shared by record_consent_decline and start_consultation, both of
	which receive this id as an opaque echo of what get_consent returned and must never store it
	unchecked. Returns None (never throws) for a missing/unknown id -- the exact snapshot a
	client saw is always best-effort to record, never a hard requirement."""
	version_id = env.get("version_id")
	if not version_id or not frappe.db.exists("Shukhee Consent Version", version_id):
		return None
	return version_id


def _consent_items(parenttype, parent):
	"""Shukhee Consent Item rows for a given parent -- MUST filter on both parenttype and parent
	(not parent alone): Shukhee Consent and Shukhee Consent Version both autoincrement their own
	`name` independently, so their ids collide (both have a row "1", "2", ...), and this child
	doctype is reused as-is across both parents."""
	return frappe.get_all(
		"Shukhee Consent Item",
		filters={"parenttype": parenttype, "parent": parent},
		fields=["description", "mandatory"],
		order_by="idx asc",
	)


def resolve_authoritative_items(version_id, lng):
	"""The item list a recorded decision treats as the legal record of what was shown --
	sourced from the resolved Shukhee Consent Version snapshot whenever one validated (booking
	can happen some time after get_consent was called, since it's a separate screen the SK fills
	in contact details etc. on first), falling back to the live row only when no version_id
	resolved, mirroring resolve_version_id's own best-effort philosophy."""
	if version_id:
		return _consent_items("Shukhee Consent Version", version_id)

	rows = frappe.get_all("Shukhee Consent", filters={"lng": lng}, fields=["name"], limit=1)
	if not rows:
		return []
	return _consent_items("Shukhee Consent", rows[0]["name"])


def resolve_provider_name(provider, lng):
	if not provider:
		return None
	field = "full_name_bn" if lng == "bn" else "full_name"
	name = frappe.db.get_value("Provider", provider, field)
	return name or frappe.db.get_value("Provider", provider, "full_name")


def resolve_base_consent_html(version_id, lng):
	"""The consent HTML text a recorded decision fills tokens into -- the resolved Version
	snapshot's own text whenever one validated, falling back to the live row only when no
	version_id resolved (same best-effort posture as resolve_authoritative_items)."""
	if version_id:
		return frappe.db.get_value("Shukhee Consent Version", version_id, "consent")
	rows = frappe.get_all("Shukhee Consent", filters={"lng": lng}, fields=["consent"], limit=1)
	return rows[0]["consent"] if rows else None


def resolve_consent_version_label(version_id):
	if not version_id:
		return None
	return frappe.db.get_value("Shukhee Consent Version", version_id, "version")


def fill_consent_template(html, tokens):
	"""Plain token substitution -- same {{token}} convention the Shukhee Consent HTML content
	itself uses in its footer. Mirrors (but is independent of) the client's own substitution for
	live on-screen display: that one must work instantly pre-decision; this one is the
	tamper-resistant copy for the stored audit record, computed from server-resolved values only."""
	if not html:
		return html
	filled = html
	for key, value in tokens.items():
		filled = filled.replace("{{" + key + "}}", value or "")
	return filled


@whitelist(methods=["POST"], remote_auth=True)
def get_consent(payload=None):
	"""Returns this app's current consent HTML for the requested language, falling back to
	English when the requested language has no configured row. Called by the mobile app every
	time the SK taps "Call a doctor now" -- deliberately no caching on either side, so an
	edited consent takes effect on the very next call, not the next app release. Read-only
	config fetch, no per-caller identity to check -- same shape as get_specialities in
	consultation.py, so (like that endpoint) this is NOT wrapped by audit_inbound: there's no
	call-lifecycle event here to log.

	`version_id` is the Shukhee Consent Version snapshot row backing this response (see
	ShukheeConsent.on_update) -- the client must carry it forward unchanged (not re-derive it
	later) and echo it back via record_consent_decline (Decline) or start_consultation (Agree),
	since by then Shukhee Consent may have been edited again and no longer reflects what was
	shown here.

	`items` is the structured checkbox list (description, mandatory) the client renders as real
	checkboxes below the HTML body -- the client echoes back which ones were ticked via
	`items_checked`, positional to this same order."""
	env = _resolve_env(payload)
	lng = env.get("lng") or "en"

	fields = ["name", "lng", "consent", "version", "current_version"]
	rows = frappe.get_all("Shukhee Consent", filters={"lng": lng}, fields=fields, limit=1)
	if not rows and lng != "en":
		rows = frappe.get_all("Shukhee Consent", filters={"lng": "en"}, fields=fields, limit=1)
	if not rows:
		frappe.throw(_("No consent content configured."), frappe.DoesNotExistError)

	items = _consent_items("Shukhee Consent", rows[0]["name"])

	return {
		"lng": rows[0]["lng"],
		"consent": rows[0]["consent"],
		"version": rows[0]["version"],
		"version_id": rows[0]["current_version"],
		"items": [{"description": i["description"], "mandatory": bool(i["mandatory"])} for i in items],
	}


@whitelist(methods=["POST"], remote_auth=True)
@audit_inbound
def record_consent_decline(payload=None):
	"""Records a patient's Decline of the teleconsult-specific consent shown by get_consent.
	The only server-side record a decline gets -- a decline never leads to booking, so there's
	no Call Logs row to record it on (contrast api.consultation.start_consultation, which embeds
	an Agreed decision's fields directly onto the Call Logs row it creates). Deliberately
	lightweight: no items/filled-text snapshot, since there's no compliance need to prove exactly
	which checkboxes were shown for a call that never happened."""
	env = _resolve_env(payload)
	patient_id = env.get("patient_id")
	lng = env.get("lng")

	if not patient_id or not lng:
		frappe.throw(_("patient_id and lng are required."), frappe.ValidationError)

	# Never trust a client-supplied identity for who performed the action -- resolved
	# server-side via the same current_remote_user_id() -> Provider.username lookup
	# consultation.py's start_consultation/get_specialities/etc. already use.
	provider = current_provider()
	version_id = resolve_version_id(env)

	frappe.get_doc(
		{
			"doctype": "Shukhee Consent Decline",
			"patient_id": patient_id,
			"visit_id": env.get("visit_id"),
			"lng": lng,
			"consent_version": version_id,
			"provider": provider,
			"patient_dob": env.get("patient_dob"),
			"occurred_at": frappe.utils.now_datetime(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {"logged": True}
