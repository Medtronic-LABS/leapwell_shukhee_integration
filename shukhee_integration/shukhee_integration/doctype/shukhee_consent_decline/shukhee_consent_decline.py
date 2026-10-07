from frappe.model.document import Document


class ShukheeConsentDecline(Document):
	"""Append-only record of a patient's Declined decision on the teleconsult consent shown
	by shukhee_integration.api.consent.get_consent. A decline never produces a Call Logs row
	(booking never happens on decline -- see api.consultation.start_consultation), so unlike
	an Agreed decision (recorded directly on the resulting Call Logs row's own consent_*
	fields), this is the only server-side record a decline gets. Deliberately lightweight --
	no items/filled-text, mirroring Call Logs' own posture of recording only what it can at
	the point a request actually reaches the server.

	No scheduled purge, same rationale as Shukhee Consent Log before it: this is a legal
	consent record, not a debug/observability trail."""

	pass
