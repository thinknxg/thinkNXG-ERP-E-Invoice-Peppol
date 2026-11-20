"""ConvergeXConfig: everything the integration reads from one E-Invoice
Provider Settings row, validated in one place.

Sandbox and production are separate Provider Settings rows (the existing
"Environment" field), each with its own Base URL and credentials. No base URL
is hard-coded: ConvergeX issues it per environment.
"""

from urllib.parse import urlparse

from .contract import build_contract
from .exceptions import ConvergeXConfigError, ConvergeXContractError

PRODUCTION = "production"


def _get(settings, field, default=None):
	value = settings.get(field) if hasattr(settings, "get") else getattr(settings, field, None)
	return default if value in (None, "") else value


def _password(settings, field):
	"""Password fields are encrypted at rest by the framework; read them
	only through get_password so the clear value never lands in a doc dict."""
	getter = getattr(settings, "get_password", None)
	if getter:
		try:
			return (getter(field, raise_exception=False) or "").strip()
		except Exception:
			return ""
	return (_get(settings, field) or "").strip()


class ConvergeXConfig:
	def __init__(self, settings):
		self.settings = settings
		self.name = _get(settings, "name") or "convergex"
		self.company = _get(settings, "company")
		self.environment = (_get(settings, "custom_environment") or "Sandbox").strip()
		self.base_url = (_get(settings, "base_url_override") or _get(settings, "base_url") or "").strip().rstrip("/")
		self.client_id = (_get(settings, "client_id") or "").strip()
		self.tenant_id = (_get(settings, "custom_convergex_tenant_id") or "").strip()
		self.api_version = (_get(settings, "custom_convergex_api_version") or "").strip()
		self.invoice_type_uuid = (_get(settings, "custom_convergex_invoice_type_uuid") or "").strip()
		self.credit_note_type_uuid = (_get(settings, "custom_convergex_credit_note_type_uuid") or "").strip()
		self.async_mode = bool(int(_get(settings, "custom_convergex_async_mode", 0) or 0))
		self.sync_customers = bool(int(_get(settings, "custom_convergex_sync_customers", 1) or 0))
		self.enabled = bool(int(_get(settings, "enabled", 0) or 0))
		try:
			self.contract = build_contract(_get(settings, "custom_convergex_contract"))
		except ValueError as e:
			raise ConvergeXConfigError(str(e)) from e

	# ---- secrets (never stored on the object) -------------------------
	def client_secret(self):
		return _password(self.settings, "client_secret")

	# ---- derived ---------------------------------------------------------
	@property
	def is_production(self):
		return self.environment.lower() == PRODUCTION

	@property
	def contract_verified(self):
		return bool(self.contract.get("verified"))

	@property
	def timeout(self):
		return float(self.contract.get("timeout_seconds") or 30)

	@property
	def retry(self):
		return self.contract.get("retry") or {}

	def url(self, route_key, **params):
		"""Absolute URL for a contract route; ContractError if the route is
		not defined (an undocumented call is never improvised)."""
		path = (self.contract.get("routes") or {}).get(route_key)
		if not path:
			raise ConvergeXContractError(
				f"The ConvergeX API contract has no '{route_key}' route. Ask ConvergeX for it and add it under "
				f"routes.{route_key} in API Contract Overrides."
			)
		return self.base_url + path.format(**params)

	def auth_url(self, key):
		path = (self.contract.get("auth") or {}).get(key)
		if not path:
			raise ConvergeXContractError(f"The ConvergeX API contract has no auth.{key}.")
		return self.base_url + path

	def extra_headers(self):
		names = self.contract.get("headers") or {}
		headers = {}
		if names.get("tenant_header") and self.tenant_id:
			headers[names["tenant_header"]] = self.tenant_id
		if names.get("api_version_header") and self.api_version:
			headers[names["api_version_header"]] = self.api_version
		return headers

	# ---- validation ------------------------------------------------------
	def problems(self, for_submission=False):
		"""Human-readable list of what is wrong; empty when usable."""
		out = []
		if not self.base_url:
			out.append("Base URL is empty. Enter the URL ConvergeX issued for this environment.")
		else:
			parsed = urlparse(self.base_url)
			if parsed.scheme != "https" or not parsed.netloc:
				out.append(f"Base URL must be an https:// URL (got {self.base_url}).")
		if not self.client_id:
			out.append("Client ID is empty.")
		if not self.client_secret():
			out.append("Client Secret is empty.")
		if for_submission:
			if self.is_production and not self.contract_verified:
				out.append(
					'This is a Production row and the API contract is not marked verified. Confirm the routes and '
					'field names with ConvergeX, then set "verified": true in API Contract Overrides.'
				)
			if self.contract.get("invoice_type_uuid_required") and not self.invoice_type_uuid:
				out.append("Invoice Type UUID is empty.")
		return out

	def assert_usable(self, for_submission=False):
		problems = self.problems(for_submission=for_submission)
		if problems:
			raise ConvergeXConfigError("ConvergeX settings are incomplete: " + " ".join(problems))
