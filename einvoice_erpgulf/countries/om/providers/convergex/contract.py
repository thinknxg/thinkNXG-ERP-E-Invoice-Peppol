"""ConvergeX API contract: every route, request key, response key and status
word the adapter relies on, in one overridable place.

WHY THIS FILE EXISTS
--------------------
At the time of writing there is no public ConvergeX OpenAPI / schema. What is
publicly known (see docs/CONVERGEX.md, "Sources") is:

* Credentials are a Client ID and Client Secret.                [public]
* Each account has an Invoice Type UUID and a Credit Note Type
  UUID that go with the document.                               [public]
* The customer must be registered with ConvergeX before an
  invoice for it is sent.                                       [public]
* Credit notes are supported; submitted invoices cannot be
  voided.                                                       [public]
* The routes below were identified in ConvergeX integration
  material supplied with the integration brief.                 [candidate]

Everything marked UNVERIFIED is a placeholder until ConvergeX supplies its
schema. Nothing here is a guess dressed up as fact: each key can be replaced
per Provider Settings row through the "API Contract Overrides" JSON field,
without touching code, and the adapter refuses to submit on a Production row
until that JSON says ``"verified": true``.
"""

import copy
import json

# Route placeholders: {id} = ConvergeX invoice id, {job_id} = integration job id.
DEFAULT_CONTRACT = {
	# Set to true in the overrides JSON once ConvergeX has confirmed every
	# route and key used below for this account. Production rows refuse to
	# submit while this is false.
	"verified": False,
	"auth": {
		"token_path": "/api/auth/token/",  # candidate
		"refresh_path": "/api/auth/token/refresh/",  # candidate
		"request_format": "json",  # UNVERIFIED: "json" or "form"
		"client_id_key": "client_id",  # UNVERIFIED
		"client_secret_key": "client_secret",  # UNVERIFIED
		"refresh_request_key": "refresh",  # UNVERIFIED (SimpleJWT style)
		"access_token_keys": ["access_token", "access", "token"],  # UNVERIFIED, first match wins
		"refresh_token_keys": ["refresh_token", "refresh"],  # UNVERIFIED
		"expires_in_keys": ["expires_in"],  # UNVERIFIED; JWT "exp" used when absent
		"header_scheme": "Bearer",  # UNVERIFIED
		"default_ttl_seconds": 300,  # used only if neither expires_in nor JWT exp is present
	},
	"routes": {
		"invoice_create": "/api/invoices/create/",  # candidate
		"invoice_submit": "/api/invoices/{id}/submit/",  # candidate
		"job_status": "/api/integration/jobs/{job_id}/",  # candidate
		"compliance": "/api/invoices/{id}/compliance/",  # candidate
		"tdd_report": "/api/invoices/{id}/tdd-report/",  # candidate
		"ota_preview": "/api/invoices/{id}/ota-preview/",  # candidate
		"customer_sync": "/api/customers/erp/sync/",  # candidate
		# No documented "get invoice" route; status is read from the
		# compliance route unless ConvergeX names one.
		"invoice_status": None,
	},
	"async_query": {"mode": "async"},  # candidate: POST invoice_create?mode=async -> 202 + job id
	"async_create_includes_submit": False,  # UNVERIFIED: does an async create also submit?
	"headers": {
		# Header names are not documented; leave null to send nothing.
		"tenant_header": None,
		"api_version_header": None,
		"correlation_header": None,
		"idempotency_header": None,
	},
	"response": {
		"envelope_keys": ["data", "result"],  # unwrapped before key lookup
		"invoice_id_keys": ["id", "invoice_id", "uuid"],
		"job_id_keys": ["job_id", "job", "job.id"],
		"status_keys": ["status", "state", "invoice_status"],
		"job_status_keys": ["status", "state"],
		"job_invoice_id_keys": ["invoice_id", "result.invoice_id", "result.id", "invoice.id"],
		"error_code_keys": ["code", "error_code", "error.code"],
		"error_message_keys": ["detail", "message", "error", "error.message", "errors"],
		"validation_errors_keys": ["errors", "validation_errors", "compliance.errors", "issues"],
		"customer_id_keys": ["id", "customer_id", "uuid"],
		"tracking_keys": ["tracking_number", "tracking_no"],  # shown on the invoice when present
		"ota_reference_keys": ["ota_reference", "reference_number", "official_reference"],
	},
	# ConvergeX invoice status (lower case) -> custom_reporting_status.
	# "received", "submitted", "accepted" and "approved" are kept apart: only
	# "approved" counts as reported to the OTA. Raw status is always kept in
	# custom_provider_status and the E-Invoice API Log.
	"status_map": {
		"created": "pending",  # local marker: created on ConvergeX, not yet submitted
		"submit_accepted": "pending",  # local marker: submit call accepted, no status in body
		"received": "pending",
		"submitted": "pending",
		"ota_accepted": "accepted",  # non-final until ConvergeX confirms it is the last stage
		"approved": "reported",
		"rejected": "rejected",
		"failed": "failed",
	},
	"final_provider_statuses": ["approved", "rejected", "failed"],
	# Integration job status -> pending | completed | failed
	"job_status_map": {
		"pending": "pending",
		"queued": "pending",
		"running": "pending",
		"processing": "pending",
		"in_progress": "pending",
		"completed": "completed",
		"complete": "completed",
		"success": "completed",
		"succeeded": "completed",
		"done": "completed",
		"failed": "failed",
		"error": "failed",
		"cancelled": "failed",
	},
	"credit_notes_supported": True,  # public (Odoo connector documents credit notes)
	"customer_sync_required": True,  # public (customer registered before the invoice)
	"invoice_type_uuid_required": False,  # set true once ConvergeX confirms it is mandatory
	# Canonical payload key -> ConvergeX key, applied at every nesting level.
	# Empty = send the canonical keys (see docs/CONVERGEX.md, "Invoice field mapping").
	"payload_field_map": {},
	"payload_root_key": None,  # e.g. "invoice" if ConvergeX wants {"invoice": {...}}
	"customer_field_map": {},
	"retry": {"max_attempts": 3, "backoff_seconds": 2, "max_backoff_seconds": 30},
	"timeout_seconds": 30,
}


def deep_merge(base, override):
	"""Return base with override applied; dicts merge recursively, every
	other value (lists included) replaces."""
	out = copy.deepcopy(base)
	for key, value in (override or {}).items():
		if isinstance(value, dict) and isinstance(out.get(key), dict):
			out[key] = deep_merge(out[key], value)
		else:
			out[key] = copy.deepcopy(value)
	return out


def parse_overrides(raw):
	"""The overrides JSON from Provider Settings -> dict. Empty -> {}.
	Raises ValueError with a readable message on bad JSON."""
	if not raw:
		return {}
	if isinstance(raw, dict):
		return raw
	try:
		value = json.loads(raw)
	except ValueError as e:
		raise ValueError(f"API Contract Overrides is not valid JSON: {e}") from e
	if not isinstance(value, dict):
		raise ValueError("API Contract Overrides must be a JSON object.")
	return value


def build_contract(raw_overrides=None):
	return deep_merge(DEFAULT_CONTRACT, parse_overrides(raw_overrides))
