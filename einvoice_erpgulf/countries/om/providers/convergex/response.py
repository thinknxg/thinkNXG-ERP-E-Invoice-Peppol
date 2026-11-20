"""ConvergeXResponseMapper and the sanitiser.

Reads ids, statuses and errors out of ConvergeX responses using the key lists
in the contract, so a renamed field is a JSON override, not a code change.
"""

import json

# Any key containing one of these (case-insensitive) is redacted before
# anything is logged or stored.
SENSITIVE_FRAGMENTS = ("secret", "token", "password", "authorization", "api_key", "apikey", "refresh", "access")
REDACTED = "***redacted***"


def sanitize(value):
	"""Deep copy of value with credentials and tokens blanked out."""
	if isinstance(value, dict):
		out = {}
		for k, v in value.items():
			key = str(k).lower()
			out[k] = REDACTED if any(f in key for f in SENSITIVE_FRAGMENTS) else sanitize(v)
		return out
	if isinstance(value, list):
		return [sanitize(v) for v in value]
	if isinstance(value, str) and value.lower().startswith("bearer "):
		return REDACTED
	return value


def to_text(value, limit=None):
	"""JSON text of a sanitised value, optionally truncated for log fields."""
	if value is None:
		return None
	text = value if isinstance(value, str) else json.dumps(sanitize(value), indent=2, default=str)
	if limit and len(text) > limit:
		text = text[:limit] + "\n... (truncated)"
	return text


def dig(body, path):
	"""'a.b.c' lookup through dicts; None when any step is missing."""
	current = body
	for part in path.split("."):
		if not isinstance(current, dict):
			return None
		current = current.get(part)
	return current


class ConvergeXResponseMapper:
	def __init__(self, contract):
		self.keys = contract["response"]

	def _candidates(self, body):
		"""The body itself, then each configured envelope (data, result...)."""
		if not isinstance(body, dict):
			return []
		out = [body]
		for key in self.keys.get("envelope_keys") or []:
			inner = body.get(key)
			if isinstance(inner, dict):
				out.append(inner)
		return out

	def first(self, body, key_list_name):
		for candidate in self._candidates(body):
			for path in self.keys.get(key_list_name) or []:
				value = dig(candidate, path)
				if value not in (None, "", [], {}):
					return value
		return None

	def invoice_id(self, body):
		value = self.first(body, "invoice_id_keys")
		return str(value) if value not in (None, "") and not isinstance(value, (dict, list)) else None

	def job_id(self, body):
		value = self.first(body, "job_id_keys")
		return str(value) if value not in (None, "") and not isinstance(value, (dict, list)) else None

	def status(self, body):
		value = self.first(body, "status_keys")
		return value.strip().lower() if isinstance(value, str) and value.strip() else None

	def job_status(self, body):
		value = self.first(body, "job_status_keys")
		return value.strip().lower() if isinstance(value, str) and value.strip() else None

	def job_invoice_id(self, body):
		value = self.first(body, "job_invoice_id_keys")
		return str(value) if value not in (None, "") and not isinstance(value, (dict, list)) else None

	def customer_id(self, body):
		value = self.first(body, "customer_id_keys")
		return str(value) if value not in (None, "") and not isinstance(value, (dict, list)) else None

	def error_code(self, body):
		value = self.first(body, "error_code_keys")
		return str(value) if value not in (None, "") and not isinstance(value, (dict, list)) else None

	def error_message(self, body):
		"""Readable message from whatever error shape came back."""
		if isinstance(body, str):
			return body.strip()[:500] or None
		value = self.first(body, "error_message_keys")
		if value is None and isinstance(body, dict):
			# DRF style: {"field": ["message"]}
			field_errors = [
				f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
				for k, v in body.items()
				if isinstance(v, (list, str)) and k not in ("status", "id")
			]
			value = field_errors or None
		return flatten_errors(value)

	def validation_errors(self, body):
		value = self.first(body, "validation_errors_keys")
		return flatten_errors(value, as_list=True) or []

	def tracking_number(self, body):
		return self.first(body, "tracking_keys")

	def ota_reference(self, body):
		return self.first(body, "ota_reference_keys")


def flatten_errors(value, as_list=False):
	"""Lists / dicts of errors -> list of strings or one joined string."""
	items = []

	def walk(v, prefix=""):
		if v in (None, "", [], {}):
			return
		if isinstance(v, dict):
			msg = v.get("message") or v.get("detail") or v.get("description")
			if msg:
				code = v.get("code") or v.get("rule") or v.get("field")
				items.append(f"{prefix}{code + ': ' if code else ''}{msg}")
				return
			for k, inner in v.items():
				walk(inner, f"{prefix}{k}: ")
		elif isinstance(v, list):
			for inner in v:
				walk(inner, prefix)
		else:
			items.append(f"{prefix}{v}")

	walk(value)
	if as_list:
		return items
	return "; ".join(items) if items else None
