from frappe.model.document import Document


class ShukheeConsentLog(Document):
	"""Append-only record of a patient's Agreed/Declined decision on the teleconsult
	consent shown by shukhee_integration.api.consent.get_consent -- mirrors Shukhee Call
	Audit Log's shape (own doctype, not Frappe's core Comment/Version/Activity Log;
	System-Manager-only read/create/export permissions, no write/delete).

	Deliberately has NO scheduled purge job, unlike Shukhee Call Audit Log's 180-day one
	(see shukhee_integration.audit.purge_old_audit_logs / hooks.py scheduler_events):
	that job exists because the call audit log is a debug/observability trail. This
	doctype is a legal consent record -- it must persist for as long as the underlying
	patient data does, not be auto-deleted on a debug-log retention cadence."""

	pass
