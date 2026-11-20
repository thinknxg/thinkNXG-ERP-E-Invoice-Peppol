"""Framework-facing helpers: Redis-backed token cache, the API log writer and
a per-invoice submission lock. Kept apart so the services stay pure."""

import frappe

from .response import to_text

LOG_DOCTYPE = "E-Invoice API Log"
PAYLOAD_LIMIT = 100_000


class FrappeCache:
	"""get / set(ttl) / delete over frappe.cache() (Redis)."""

	def get(self, key):
		return frappe.cache().get_value(key)

	def set(self, key, value, ttl):
		frappe.cache().set_value(key, value, expires_in_sec=int(ttl))

	def delete(self, key):
		frappe.cache().delete_value(key)


class ApiLogger:
	"""Writes one E-Invoice API Log row per HTTP attempt. Never raises: a
	logging failure must not turn a good submission into a failed one."""

	def __init__(self, provider, company, settings_name, reference_doctype=None, reference_name=None):
		self.base = {
			"provider": provider,
			"company": company,
			"provider_settings": settings_name,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}

	def bind(self, reference_doctype, reference_name):
		self.base["reference_doctype"] = reference_doctype
		self.base["reference_name"] = reference_name
		return self

	def log(self, **fields):
		try:
			if not frappe.db.exists("DocType", LOG_DOCTYPE):
				return
			row = {**self.base, **{k: v for k, v in fields.items() if v is not None}}
			row["request_payload"] = to_text(row.get("request_payload"), PAYLOAD_LIMIT)
			row["response_payload"] = to_text(row.get("response_payload"), PAYLOAD_LIMIT)
			if row.get("error_message"):
				row["error_message"] = str(row["error_message"])[:2000]
			if not (row.get("reference_doctype") and row.get("reference_name")):
				row.pop("reference_doctype", None)
				row.pop("reference_name", None)
			frappe.get_doc({"doctype": LOG_DOCTYPE, **row}).insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "E-Invoice API Log Write Error")


class SubmissionLock:
	"""Redis SET NX lock so two workers (button + scheduler, double click)
	never submit the same invoice at the same time."""

	def __init__(self, doctype, name, ttl=180):
		self.key = f"{frappe.local.site}|convergex-submit|{doctype}|{name}"
		self.ttl = ttl
		self.acquired = False

	def __enter__(self):
		self.acquired = bool(frappe.cache().set(self.key, 1, nx=True, ex=self.ttl))
		return self.acquired

	def __exit__(self, *exc):
		if self.acquired:
			frappe.cache().delete(self.key)
		return False
