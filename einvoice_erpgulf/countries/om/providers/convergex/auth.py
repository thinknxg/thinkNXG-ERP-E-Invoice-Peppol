"""ConvergeXAuthentication: client-credentials token, refresh, expiry.

* Tokens live only in the cache (Redis in production), keyed by the Provider
  Settings row, so sandbox and production never share a token.
* A cached access token is reused until 30 s before it expires; then the
  refresh token is tried, then a fresh client-credentials request.
* Credentials and tokens are never logged; the log gets a redacted body.
"""

import time

from .exceptions import (
	ConvergeXAuthError,
	ConvergeXNetworkError,
	ConvergeXServerError,
)
from .response import sanitize

SAFETY_MARGIN_SEC = 30
MIN_TTL_SEC = 30


def _first(body, keys):
	if not isinstance(body, dict):
		return None
	for key in keys or []:
		value = body.get(key)
		if value not in (None, ""):
			return value
	inner = body.get("data")
	if isinstance(inner, dict):
		return _first(inner, keys)
	return None


def jwt_expiry(token):
	"""'exp' claim of a JWT (unverified read, for cache lifetime only)."""
	try:
		import jwt

		claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
		exp = claims.get("exp")
		return float(exp) if exp else None
	except Exception:
		return None


class ConvergeXAuthentication:
	def __init__(self, config, cache, http, logger=None, clock=time.time):
		self.config = config
		self.cache = cache
		self.http = http
		self.logger = logger
		self.clock = clock
		self.auth = config.contract["auth"]
		self.timeout = config.timeout

	# ---- cache keys ------------------------------------------------------
	def _key(self, kind):
		return f"convergex:{kind}:{self.config.name}"

	def invalidate(self, refresh_too=False):
		self.cache.delete(self._key("access"))
		if refresh_too:
			self.cache.delete(self._key("refresh"))

	# ---- public ----------------------------------------------------------
	def get_access_token(self, force_new=False):
		if not force_new:
			cached = self.cache.get(self._key("access"))
			if cached:
				return cached
			refresh = self.cache.get(self._key("refresh"))
			if refresh:
				try:
					return self._refresh(refresh)
				except ConvergeXAuthError:
					self.invalidate(refresh_too=True)  # refresh expired: fall through to a new token
		return self._obtain()

	def auth_header(self, force_new=False):
		scheme = self.auth.get("header_scheme") or "Bearer"
		return {"Authorization": f"{scheme} {self.get_access_token(force_new=force_new)}"}

	def token_expiry(self):
		raw = self.cache.get(self._key("expiry"))
		return float(raw) if raw else None

	# ---- internals -------------------------------------------------------
	def _obtain(self):
		self.config.assert_usable()
		body = {
			self.auth.get("client_id_key") or "client_id": self.config.client_id,
			self.auth.get("client_secret_key") or "client_secret": self.config.client_secret(),
		}
		data = self._post(self.config.auth_url("token_path"), body, "auth.token")
		return self._store(data, "auth.token")

	def _refresh(self, refresh_token):
		body = {self.auth.get("refresh_request_key") or "refresh": refresh_token}
		data = self._post(self.config.auth_url("refresh_path"), body, "auth.refresh")
		return self._store(data, "auth.refresh", previous_refresh=refresh_token)

	def _post(self, url, body, operation):
		kwargs = {"timeout": self.timeout, "headers": {"Accept": "application/json", **self.config.extra_headers()}}
		if (self.auth.get("request_format") or "json") == "form":
			kwargs["data"] = body
		else:
			kwargs["json"] = body
		started = self.clock()
		try:
			response = self.http.post(url, **kwargs)
		except Exception as e:
			self._log(operation, url, None, None, str(e), started)
			raise ConvergeXNetworkError(f"ConvergeX authentication server unreachable: {e}", endpoint=url) from e

		try:
			data = response.json()
		except Exception:
			data = getattr(response, "text", None)
		self._log(operation, url, response.status_code, data, None, started)

		if response.status_code in (400, 401, 403):
			raise ConvergeXAuthError(
				"ConvergeX rejected the Client ID / Client Secret (or the refresh token).",
				http_status=response.status_code,
				endpoint=url,
			)
		if response.status_code >= 500:
			raise ConvergeXServerError(
				"ConvergeX authentication server error.", http_status=response.status_code, endpoint=url
			)
		if not 200 <= response.status_code < 300:
			raise ConvergeXAuthError(
				"Unexpected answer from the ConvergeX token endpoint.",
				http_status=response.status_code,
				endpoint=url,
			)
		return data

	def _store(self, data, operation, previous_refresh=None):
		access = _first(data, self.auth.get("access_token_keys"))
		if not access or not isinstance(access, str):
			raise ConvergeXAuthError(
				f"No access token found in the ConvergeX {operation} response (looked for "
				f"{', '.join(self.auth.get('access_token_keys') or [])}). Check auth.access_token_keys in the contract."
			)
		now = self.clock()
		expires_in = _first(data, self.auth.get("expires_in_keys"))
		ttl = None
		if isinstance(expires_in, (int, float)) or (isinstance(expires_in, str) and expires_in.isdigit()):
			ttl = float(expires_in)
		if ttl is None:
			exp = jwt_expiry(access)
			ttl = (exp - now) if exp else float(self.auth.get("default_ttl_seconds") or 300)
		ttl = max(MIN_TTL_SEC, int(ttl - SAFETY_MARGIN_SEC))

		self.cache.set(self._key("access"), access, ttl)
		self.cache.set(self._key("expiry"), str(now + ttl), ttl)

		refresh = _first(data, self.auth.get("refresh_token_keys")) or previous_refresh
		if refresh and isinstance(refresh, str):
			exp = jwt_expiry(refresh)
			refresh_ttl = int(exp - now - SAFETY_MARGIN_SEC) if exp else 24 * 3600
			if refresh_ttl > MIN_TTL_SEC:
				self.cache.set(self._key("refresh"), refresh, refresh_ttl)
		return access

	def _log(self, operation, url, status, body, error, started):
		if not self.logger:
			return
		self.logger.log(
			operation=operation,
			endpoint=url,
			method="POST",
			http_status=status,
			request_payload={"client_id": self.config.client_id, "client_secret": "***redacted***"}
			if operation == "auth.token"
			else {"refresh": "***redacted***"},
			response_payload=sanitize(body) if body is not None else None,
			error_message=error,
			duration_ms=int((self.clock() - started) * 1000),
		)
