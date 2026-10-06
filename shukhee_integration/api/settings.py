"""
Mobile-facing server-side feature-flag endpoint for Shukhee Settings.

  shukhee_integration.api.settings.get_controls

Mirrors spice_next_core.api.controls.get_controls's auth posture and
single-live-read shape, scoped to this app's own settings doctype. The
environment-wide Shukhee Settings.teleconsult_enabled kill-switch is only
HALF the gate: the calling SK must also have an Active UHIS Shukhee User
mapping -- the same per-SK provisioning check consultation.
_current_shukhee_user() enforces before any real Shukhee call. Surfacing
it here lets the mobile app hide/disable "Call a doctor" entirely for an
SK who has no Shukhee account configured yet, instead of showing it and
only failing once tapped.

Request:
  POST /api/method/shukhee_integration.api.settings.get_controls
  Header:  X-Auth-Token: Bearer <jwt>   (validated by remote_auth=True)

Response (Frappe wraps in {"message": ...}):
  { "teleconsultEnabled": false }
"""

import frappe

from shukhee_integration.api.consultation import _current_provider
from spice_next_core.auth.decorators import whitelist


@whitelist(methods=["POST"], remote_auth=True)
def get_controls():
	settings = frappe.get_single("Shukhee Settings")
	teleconsult_enabled = bool(settings.teleconsult_enabled) and _sk_has_active_shukhee_mapping()
	return {"teleconsultEnabled": teleconsult_enabled}


def _sk_has_active_shukhee_mapping():
	"""Whether the calling SK has an Active UHIS Shukhee User mapping.
	Short-circuited by the caller when the environment-wide flag is already
	off, so an unmapped/unrecognized caller never pays for the extra
	lookup in the common (feature-off) case."""
	try:
		provider_name = _current_provider()
	except frappe.DoesNotExistError:
		return False
	return frappe.db.get_value("UHIS Shukhee User", provider_name, "status") == "Active"
