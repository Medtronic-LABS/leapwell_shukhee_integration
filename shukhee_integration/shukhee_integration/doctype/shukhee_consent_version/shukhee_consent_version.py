from frappe.model.document import Document


class ShukheeConsentVersion(Document):
	"""Immutable snapshot of one Shukhee Consent row's content, taken whenever that
	row's `consent` or `version` changes -- see ShukheeConsent.on_update. Never edited
	after creation (System-Manager-only read/create/export permissions, no write/
	delete), so Call Logs.consent_version and Shukhee Consent Log.consent_version can
	link to a specific row here and always resolve to the exact wording that was live
	at that moment, independent of how many times Shukhee Consent has been edited
	since.

	Deliberately gets no scheduled purge job, same legal-record reasoning as Shukhee
	Consent Log: it must persist for as long as the Call Logs / Shukhee Consent Log
	rows that reference it do."""

	pass
