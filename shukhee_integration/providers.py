"""Shared "who is calling, and are they Shukhee-provisioned" resolution -- used by
api/consultation.py and api/consent.py. Lives outside api/ (rather than in one of those
two modules importing the other) specifically to avoid a circular import: consent.py
needs this for record_consent_decline's provider resolution, and consultation.py now
also needs consent.py's own consent-rendering helpers (resolve_version_id,
resolve_authoritative_items, etc.) for start_consultation's embedded consent fields --
either direction of a direct import between those two modules would be circular."""

import frappe
from frappe import _

from spice_next_core.auth.decorators import current_remote_user_id


def current_provider():
	"""Resolves the calling SK's identity to a Provider record.

	`current_remote_user_id()` returns whatever the active auth phase decoded
	from the caller's token: once Phase 2 (remote_auth_url configured) is
	active, that's the UHIS mobile platform's own login username (e.g.
	"lf_sk") from the legacy auth-service's /authenticate response --
	matched here against Provider.username, not Provider.user (a Frappe User
	link, which the mobile app's identity has no relationship to)."""
	user_id = current_remote_user_id()
	provider_name = frappe.db.get_value("Provider", {"username": user_id}, "name")
	if not provider_name:
		frappe.throw(_("No Provider record found for the calling user."), frappe.DoesNotExistError)
	return provider_name


def current_shukhee_user(provider_name):
	if not frappe.db.exists("UHIS Shukhee User", provider_name):
		frappe.throw(
			_("No Shukhee credentials configured for this Provider."), frappe.DoesNotExistError
		)
	doc = frappe.get_doc("UHIS Shukhee User", provider_name)
	if doc.status != "Active":
		frappe.throw(_("This Provider's Shukhee account is inactive."), frappe.ValidationError)
	return doc
