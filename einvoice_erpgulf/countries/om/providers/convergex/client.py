"""ConvergeXApiClient: authenticated HTTP with bounded retries and logging.

* 401 -> drop the cached token, get a new one, retry once.
* 429 / 5xx / timeout / connection error -> retry with backoff, up to
  contract.retry.max_attempts, honouring Retry-After (capped).
* Any other non-2xx -> a classified ConvergeXError, never retried.
* Every attempt is written to the API log with a sanitised request and
  response, the endpoint, HTTP status, correlation id and retry count.
  Headers (where the token lives) are never logged.
"""

import time
import uuid

from .exceptions import (
	ConvergeXAuthError,
	ConvergeXError,
	ConvergeXNetworkError,
	from_response,
)
from .response import ConvergeXResponseMapper, sanitize


class ApiResponse:
	def __init__(self, status_code, body, correlation_id, endpoint, attempts):
		self.status_code = status_code
		self.body = body
		self.correlation_id = correlation_id
		self.endpoint = endpoint
		self.attempts = attempts

	@property
	def accepted_async(self):
		return self.status_code == 202


class ConvergeXApiClient:
	def __init__(self, config, auth, http, logger=None, sleep=time.sleep, clock=time.time):
		self.config = config
		self.auth = auth
		self.http = http
		self.logger = logger
		self.sleep = sleep
		self.clock = clock
		self.mapper = ConvergeXResponseMapper(config.contract)

	def request(self, operation, method, route_key, path_params=None, json=None, params=None, context=None):
		url = self.config.url(route_key, **(path_params or {}))
		context = dict(context or {})
		correlation_id = context.pop("correlation_id", None) or str(uuid.uuid4())

		retry_cfg = self.config.retry
		max_attempts = max(1, int(retry_cfg.get("max_attempts") or 1))
		backoff = float(retry_cfg.get("backoff_seconds") or 0)
		max_backoff = float(retry_cfg.get("max_backoff_seconds") or 30)

		header_names = self.config.contract.get("headers") or {}
		base_headers = {"Accept": "application/json", **self.config.extra_headers()}
		if header_names.get("correlation_header"):
			base_headers[header_names["correlation_header"]] = correlation_id
		if header_names.get("idempotency_header") and context.get("idempotency_key"):
			base_headers[header_names["idempotency_header"]] = context["idempotency_key"]

		attempt = 0
		reauthed = False
		while True:
			attempt += 1
			started = self.clock()
			try:
				headers = {**base_headers, **self.auth.auth_header()}
				response = self.http.request(
					method, url, headers=headers, json=json, params=params, timeout=self.config.timeout
				)
			except ConvergeXError:
				raise  # auth failure while getting a token: already classified and logged
			except Exception as e:
				error = ConvergeXNetworkError(f"Could not reach ConvergeX: {e}", endpoint=url)
				self._log(operation, method, url, None, json, None, error, attempt, correlation_id, context, started)
				if attempt < max_attempts:
					self.sleep(min(max_backoff, backoff * (2 ** (attempt - 1))))
					continue
				raise error from e

			body = _body(response)
			status = response.status_code

			if 200 <= status < 300:
				self._log(operation, method, url, status, json, body, None, attempt, correlation_id, context, started)
				return ApiResponse(status, body, correlation_id, url, attempt)

			error = from_response(
				status,
				self.mapper.error_message(body) or f"ConvergeX returned HTTP {status}",
				error_code=self.mapper.error_code(body),
				endpoint=url,
				response=sanitize(body),
				retry_after=_retry_after(response),
				errors=self.mapper.validation_errors(body) if isinstance(body, dict) else None,
			)
			self._log(operation, method, url, status, json, body, error, attempt, correlation_id, context, started)

			if isinstance(error, ConvergeXAuthError) and not reauthed:
				reauthed = True
				self.auth.invalidate()
				continue
			if error.retryable and attempt < max_attempts:
				wait = error.retry_after if error.retry_after is not None else backoff * (2 ** (attempt - 1))
				self.sleep(min(max_backoff, max(0.0, wait)))
				continue
			raise error

	def _log(self, operation, method, url, status, request_body, response_body, error, attempt, correlation_id, context, started):
		if not self.logger:
			return
		mapper = self.mapper
		self.logger.log(
			operation=operation,
			endpoint=url,
			method=method,
			http_status=status,
			correlation_id=correlation_id,
			retry_count=attempt - 1,
			request_payload=sanitize(request_body) if request_body is not None else None,
			response_payload=sanitize(response_body) if response_body is not None else None,
			error_code=getattr(error, "error_code", None),
			error_message=str(error) if error else None,
			provider_document_id=context.get("provider_document_id") or mapper.invoice_id(response_body),
			job_id=context.get("job_id") or mapper.job_id(response_body),
			provider_status=mapper.status(response_body),
			reference_doctype=context.get("reference_doctype"),
			reference_name=context.get("reference_name"),
			duration_ms=int((self.clock() - started) * 1000),
		)


def _body(response):
	try:
		return response.json()
	except Exception:
		return getattr(response, "text", None)


def _retry_after(response):
	try:
		value = (getattr(response, "headers", None) or {}).get("Retry-After")
		return float(value) if value is not None else None
	except (TypeError, ValueError):
		return None
