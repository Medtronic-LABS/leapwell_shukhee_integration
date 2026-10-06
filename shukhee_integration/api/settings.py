"""
Mobile-facing server-side feature-flag endpoint for Shukhee Settings.

  shukhee_integration.api.settings.get_controls

Mirrors spice_next_core.api.controls.get_controls's auth posture and
single-live-read shape, scoped to this app's own settings doctype. No
per-user override exists for this flag (environment-wide kill-switch only),
unlike get_controls' AIFeature/User App Control tier -- this is meant to be
flipped once per backend via Desk once that backend's Shukhee gateway route
is verified working, not tuned per user.

Request:
  POST /api/method/shukhee_integration.api.settings.get_controls
  Header:  X-Auth-Token: Bearer <jwt>   (validated by remote_auth=True)

Response (Frappe wraps in {"message": ...}):
  { "teleconsultEnabled": false }
"""

import frappe

from spice_next_core.auth.decorators import whitelist


@whitelist(methods=["POST"], remote_auth=True)
def get_controls():
	settings = frappe.get_single("Shukhee Settings")
	return {"teleconsultEnabled": bool(settings.teleconsult_enabled)}
