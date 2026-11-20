"""ConvergeX errors: one hierarchy, classified from the HTTP status, so the
client, the adapter and the UI all speak the same language.

Every exception carries what the API log needs (endpoint, HTTP status,
provider error code, sanitised response) and whether a retry makes sense.
"""


class ConvergeXError(Exception):
	retryable = False
	category = "error"

	def __init__(self, message, http_status=None, error_code=None, endpoint=None, response=None, retry_after=None):
		super().__init__(message)
		self.message = message
		self.http_status = http_status
		self.error_code = error_code
		self.endpoint = endpoint
		self.response = response
		self.retry_after = retry_after

	def __str__(self):
		parts = [self.message]
		if self.http_status:
			parts.append(f"HTTP {self.http_status}")
		if self.error_code:
			parts.append(f"code {self.error_code}")
		return " | ".join(parts)


class ConvergeXConfigError(ConvergeXError):
	"""Settings are missing or invalid; nothing was sent."""

	category = "configuration"


class ConvergeXContractError(ConvergeXConfigError):
	"""A route or key the call needs is not defined in the API contract."""

	category = "contract"


class ConvergeXValidationError(ConvergeXError):
	"""Invoice data failed local checks, or ConvergeX answered 400 / 422."""

	category = "validation"

	def __init__(self, message, errors=None, **kwargs):
		super().__init__(message, **kwargs)
		self.errors = list(errors or [])

	def __str__(self):
		base = super().__str__()
		if self.errors:
			return base + ": " + "; ".join(str(e) for e in self.errors)
		return base


class ConvergeXAuthError(ConvergeXError):
	"""401 / bad credentials / token could not be obtained."""

	category = "authentication"


class ConvergeXPermissionError(ConvergeXError):
	category = "permission"


class ConvergeXNotFoundError(ConvergeXError):
	category = "not_found"


class ConvergeXDuplicateError(ConvergeXError):
	"""409 from ConvergeX, or the local duplicate-submission guard."""

	category = "duplicate"


class ConvergeXRateLimitError(ConvergeXError):
	retryable = True
	category = "rate_limited"


class ConvergeXServerError(ConvergeXError):
	retryable = True
	category = "server"


class ConvergeXNetworkError(ConvergeXError):
	"""Timeout, DNS, refused connection: the request may not have arrived."""

	retryable = True
	category = "network"


class ConvergeXJobFailedError(ConvergeXError):
	category = "job_failed"


def from_response(http_status, message, error_code=None, endpoint=None, response=None, retry_after=None, errors=None):
	"""The right exception class for an HTTP error status."""
	kwargs = dict(http_status=http_status, error_code=error_code, endpoint=endpoint, response=response)
	if http_status in (400, 422):
		return ConvergeXValidationError(message, errors=errors, **kwargs)
	if http_status == 401:
		return ConvergeXAuthError(message, **kwargs)
	if http_status == 403:
		return ConvergeXPermissionError(message, **kwargs)
	if http_status == 404:
		return ConvergeXNotFoundError(message, **kwargs)
	if http_status == 409:
		return ConvergeXDuplicateError(message, **kwargs)
	if http_status == 429:
		return ConvergeXRateLimitError(message, retry_after=retry_after, **kwargs)
	if http_status and http_status >= 500:
		return ConvergeXServerError(message, **kwargs)
	return ConvergeXError(message, **kwargs)
