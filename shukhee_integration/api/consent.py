"""
Consent flow endpoint — patient-consent HTML shown before a Shukhee teleconsult call.

  shukhee_integration.api.consent.get_consent   fetched by the mobile app every time the SK
                                                 taps "Call a doctor now", in the app's
                                                 current language (en/bn).
"""

import frappe
from frappe import _

from spice_next_core.auth.decorators import whitelist


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

	rows = frappe.get_all("Shukhee Consent", filters={"lng": lng}, fields=["lng", "consent"], limit=1)
	if not rows and lng != "en":
		rows = frappe.get_all("Shukhee Consent", filters={"lng": "en"}, fields=["lng", "consent"], limit=1)
	if not rows:
		frappe.throw(_("No consent content configured."), frappe.DoesNotExistError)

	return {"lng": rows[0]["lng"], "consent": rows[0]["consent"]}
