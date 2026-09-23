"""
Desk-only read endpoint powering the Call Logs "Audit Log" tab's timeline
visualization (see the sva_ft Property Setter fixture on Call Logs.sva_audit_log,
and the "Shukhee Call Audit Timeline" Custom HTML Block fixture that calls this).

Returns Shukhee Call Audit Log rows verbatim -- audit.log_call/to_json already
redacted and size-capped request/response payloads synchronously before they
were ever written to the DB, so no further processing happens here.

Not decorated with spice_next_core.auth.decorators.whitelist(remote_auth=True):
this is called via frappe.call from an already-authenticated Desk session
rendering the Call Logs form, not the mobile bearer-token surface
api/consultation.py serves.
"""

import frappe
from frappe import _

_TIMELINE_FIELDS = [
	"name",
	"direction",
	"endpoint",
	"shukhee_user",
	"correlation_id",
	"status",
	"status_code",
	"duration_ms",
	"request_payload",
	"response_payload",
	"error",
	"logged_at",
]


@frappe.whitelist()
def get_call_audit_timeline(call_log):
	"""Every Shukhee Call Audit Log row linked to `call_log` (including rows
	backfilled after the fact -- see audit.backfill_call_log), oldest first.

	Uses frappe.get_list (permission-checked), not frappe.get_all: Call Logs
	and Shukhee Call Audit Log both already restrict read to System Manager,
	so anyone who can open this Call Logs form already has read on the rows
	it links to -- no ignore_permissions escape hatch is needed."""
	if not call_log:
		frappe.throw(_("call_log is required."), frappe.ValidationError)
	# Call Logs.name is a bigint (autoname: autoincrement) -- frappe.db.exists
	# passes call_log straight into a `WHERE "name" = %s` comparison against
	# that column, and Postgres raises a raw type error (not a clean False) for
	# a non-numeric string, rather than the intended DoesNotExistError below.
	try:
		int(call_log)
	except (TypeError, ValueError):
		frappe.throw(_("Call Logs {0} not found.").format(call_log), frappe.DoesNotExistError)
	if not frappe.db.exists("Call Logs", call_log):
		frappe.throw(_("Call Logs {0} not found.").format(call_log), frappe.DoesNotExistError)

	return frappe.get_list(
		"Shukhee Call Audit Log",
		filters={"call_log": call_log},
		fields=_TIMELINE_FIELDS,
		order_by="logged_at asc, creation asc",
		limit_page_length=0,
	)
