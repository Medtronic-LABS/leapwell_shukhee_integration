"""
Shared audit-logging helpers for shukhee_integration's inbound (mobile) and
outbound (Shukhee) request/response trail -- see the Shukhee Call Audit Log
doctype. Redaction/truncation always happens synchronously in the request
thread, before frappe.enqueue -- passing raw payloads into enqueue would put
unredacted secrets into the Redis-backed job payload before redaction ever
runs.
"""

import functools
import json
import time

import frappe

_MAX_FIELD_CHARS = 4000
_MAX_PAYLOAD_CHARS = 8000
_RETENTION_DAYS = 180  # adjust as compliance requirements are finalized

# Matched case-insensitively against dict keys with "-"/"_" stripped, so
# "X-Auth-Token", "x_auth_token", and "xauthtoken" all match the same entry.
_REDACT_KEYS = {
	"password",
	"shukheepassword",
	"token",
	"accesstoken",
	"refreshtoken",
	"authorization",
	"xauthtoken",
	"clientsecret",
	"secret",
}


def _normalize_key(key):
	return str(key).lower().replace("-", "").replace("_", "")


def _redact(value):
	if isinstance(value, dict):
		return {
			k: "[REDACTED]" if _normalize_key(k) in _REDACT_KEYS else _redact(v)
			for k, v in value.items()
		}
	if isinstance(value, (list, tuple)):
		return [_redact(v) for v in value]
	if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
		return value[:_MAX_FIELD_CHARS] + f"...[truncated, {len(value)} chars total]"
	return value


def to_json(value):
	"""Redacts, serializes, and size-caps a payload for storage. None-safe,
	never raises -- falls back to str(value) if it isn't JSON-serializable."""
	if value is None:
		return None
	try:
		text = json.dumps(_redact(value), default=str)
	except Exception:
		text = str(value)
	if len(text) > _MAX_PAYLOAD_CHARS:
		text = text[:_MAX_PAYLOAD_CHARS] + f"...[truncated, {len(text)} chars total]"
	return text


def sanitize_outbound_kwargs(kwargs):
	"""Builds a loggable dict from shukhee_client._request's **kwargs -- keeps
	only params/data/json/headers (each redacted downstream by to_json), and
	special-cases `files` (the [("attachments", (filename, content, mimetype))]
	shape upload_medias builds) to metadata only. Raw file bytes are never
	included, regardless of size."""
	safe = {}
	for key in ("params", "data", "json", "headers"):
		if key in kwargs:
			safe[key] = kwargs[key]
	if "files" in kwargs:
		safe["files"] = [
			{
				"field": field,
				"filename": file_tuple[0],
				"size": len(file_tuple[1]) if len(file_tuple) > 1 and file_tuple[1] else 0,
				"mimetype": file_tuple[2] if len(file_tuple) > 2 else None,
			}
			for field, file_tuple in kwargs["files"]
		]
	return safe


def log_call(
	*,
	direction,
	endpoint,
	call_log=None,
	correlation_id=None,
	shukhee_user=None,
	request_payload=None,
	response_payload=None,
	status="Success",
	status_code=None,
	error=None,
	duration_ms=None,
):
	"""Redacts/truncates synchronously, then enqueues the actual DB write so
	logging never adds latency to the request/response path. Never raises --
	an enqueue failure is itself logged to the Error Log and swallowed."""
	request_json = to_json(request_payload)
	response_json = to_json(response_payload)
	error_text = (error or "")[:140] or None
	try:
		frappe.enqueue(
			"shukhee_integration.audit._create_audit_log",
			queue="short",
			now=False,
			direction=direction,
			endpoint=endpoint,
			call_log=call_log or None,
			correlation_id=correlation_id,
			shukhee_user=shukhee_user,
			request_payload=request_json,
			response_payload=response_json,
			status=status,
			status_code=status_code,
			error=error_text,
			duration_ms=duration_ms,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Shukhee Call Audit Log enqueue failed")


def _create_audit_log(
	direction,
	endpoint,
	call_log,
	correlation_id,
	shukhee_user,
	request_payload,
	response_payload,
	status,
	status_code,
	error,
	duration_ms,
):
	frappe.get_doc(
		{
			"doctype": "Shukhee Call Audit Log",
			"direction": direction,
			"endpoint": endpoint,
			"call_log": call_log,
			"correlation_id": correlation_id,
			"shukhee_user": shukhee_user,
			"request_payload": request_payload,
			"response_payload": response_payload,
			"status": status,
			"status_code": status_code,
			"error": error,
			"duration_ms": duration_ms,
			"logged_at": frappe.utils.now_datetime(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def backfill_call_log(correlation_id, call_log):
	"""Links outbound rows logged before a Call Logs record existed (patient
	resolution, media upload, booking -- all made before start_consultation's
	own call_log.insert()) back to it, once it's known."""
	if not correlation_id or not call_log:
		return
	try:
		frappe.enqueue(
			"shukhee_integration.audit._backfill_call_log",
			queue="short",
			now=False,
			correlation_id=correlation_id,
			call_log=call_log,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Shukhee Call Audit Log backfill enqueue failed")


def _backfill_call_log(correlation_id, call_log):
	frappe.db.set_value(
		"Shukhee Call Audit Log",
		{"correlation_id": correlation_id, "call_log": ("in", ("", None))},
		"call_log",
		call_log,
	)
	frappe.db.commit()


def purge_old_audit_logs():
	"""Scheduled daily (see hooks.py scheduler_events) -- deletes audit rows
	older than _RETENTION_DAYS. This table carries real PII/PHI (contact
	numbers, patient names/DOB, reasons, doctor details); unlike Remote Auth
	Activity Log / Sync Op Log (neither of which purges), this one does."""
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -_RETENTION_DAYS)
	old_names = frappe.get_all(
		"Shukhee Call Audit Log", filters={"logged_at": ("<", cutoff)}, pluck="name"
	)
	for name in old_names:
		frappe.delete_doc(
			"Shukhee Call Audit Log", name, ignore_permissions=True, delete_permanently=True
		)
	if old_names:
		frappe.db.commit()


def _capture_inbound_request(kwargs):
	"""Builds a redactable dict of the inbound request: frappe.local.form_dict
	(covers start_consultation's raw form fields) merged with the endpoint's
	own `payload` kwarg if present (frappe.parse_json'd, mirroring
	consultation._resolve_env's own dual-mode parsing), plus uploaded file
	metadata (never raw bytes) from frappe.request.files."""
	data = dict(frappe.local.form_dict or {})

	payload = kwargs.get("payload")
	if payload:
		try:
			parsed = frappe.parse_json(payload)
			if isinstance(parsed, dict):
				data.update(parsed)
		except Exception:
			pass

	if frappe.request and frappe.request.files:
		files_meta = []
		for field_name in frappe.request.files:
			for f in frappe.request.files.getlist(field_name):
				# Peek the length without consuming the stream -- the real
				# endpoint (e.g. start_consultation) reads it again afterward.
				pos = f.stream.tell()
				size = len(f.stream.read())
				f.stream.seek(pos)
				files_meta.append({"field": field_name, "filename": f.filename, "size": size})
		if files_meta:
			data["_files"] = files_meta

	return data


def audit_inbound(fn):
	"""Decorator for shukhee_integration.api.consultation's call-related
	whitelisted endpoints. Must be applied BELOW @whitelist(...) (i.e. closer
	to `def`) so `whitelist` stays the outermost decorator, as its own
	docstring requires for Frappe's by-identity whitelist registration --
	call chain ends up whitelist -> require_remote_auth -> audit_inbound -> fn,
	so auth has already succeeded by the time this runs.

	Sets frappe.local.current_call_log and frappe.local.shukhee_audit_correlation_id
	for the duration of the call so shukhee_client's outbound calls can
	correlate their own audit rows back to this one, even before a Call Logs
	record exists yet (start_consultation creates it partway through)."""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		correlation_id = frappe.generate_hash(length=10)
		frappe.local.shukhee_audit_correlation_id = correlation_id

		request_payload = _capture_inbound_request(kwargs)
		call_log = request_payload.get("call_log") or None
		frappe.local.current_call_log = call_log

		start = time.monotonic()
		status, error, result = "Success", None, None
		try:
			result = fn(*args, **kwargs)
			return result
		except Exception as e:
			status, error = "Failed", str(e)
			raise
		finally:
			response_call_log = result.get("call_log") if isinstance(result, dict) else None
			log_call(
				direction="Inbound",
				endpoint=f"{fn.__module__}.{fn.__name__}",
				call_log=call_log or response_call_log,
				correlation_id=correlation_id,
				request_payload=request_payload,
				response_payload=result,
				status=status,
				error=error,
				duration_ms=int((time.monotonic() - start) * 1000),
			)
			# start_consultation's own patient-resolution/media-upload/booking
			# outbound calls were logged with call_log=None (it didn't exist
			# yet) -- now that it does, link them back.
			if response_call_log and not call_log:
				backfill_call_log(correlation_id, response_call_log)

	# functools.wraps sets __wrapped__ on every decorator layer that uses it
	# (this one included), so __wrapped__ alone can't distinguish "wrapped by
	# audit_inbound" from "wrapped by require_remote_auth" when walking a
	# decorator chain -- this explicit marker can (see test_consultation.py's
	# TestAuditInboundWiring).
	wrapper.__audit_inbound__ = True
	return wrapper
