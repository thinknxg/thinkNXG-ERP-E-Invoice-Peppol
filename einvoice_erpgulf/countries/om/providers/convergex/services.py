"""ConvergeX business services on top of the API client.

ConvergeXInvoiceService     create / submit (sync or async)
ConvergeXJobService         integration job polling
ConvergeXComplianceService  compliance result, TDD report, OTA preview
ConvergeXCustomerService    customer master sync (required before invoicing)

None of these touch the database: they return plain results and the adapter
decides what to store. That keeps them testable with a mocked HTTP layer.
"""

from .exceptions import ConvergeXContractError, ConvergeXError
from .response import ConvergeXResponseMapper
from .status import ConvergeXStatusService

COMPLIANCE_ROUTES = {"compliance": "compliance", "tdd_report": "tdd_report", "ota_preview": "ota_preview"}


class SubmissionResult:
	"""What happened to one submission attempt, stage by stage."""

	def __init__(self):
		self.stage = None  # created | submitted | queued
		self.invoice_id = None
		self.job_id = None
		self.provider_status = None
		self.http_status = None
		self.correlation_id = None
		self.create_response = None
		self.submit_response = None
		self.tracking_number = None
		self.ota_reference = None
		self.validation_errors = []

	def as_dict(self):
		return {
			"provider": "ConvergeX",
			"stage": self.stage,
			"convergex_invoice_id": self.invoice_id,
			"job_id": self.job_id,
			"provider_status": self.provider_status,
			"tracking_number": self.tracking_number,
			"ota_reference": self.ota_reference,
			"correlation_id": self.correlation_id,
			"validation_errors": self.validation_errors,
			"create_response": self.create_response,
			"submit_response": self.submit_response,
		}


class ConvergeXInvoiceService:
	def __init__(self, client, contract):
		self.client = client
		self.contract = contract
		self.mapper = ConvergeXResponseMapper(contract)

	def create(self, payload, context, async_mode=False):
		params = dict(self.contract.get("async_query") or {}) if async_mode else None
		response = self.client.request(
			"invoice.create", "POST", "invoice_create", json=payload, params=params, context=context
		)
		return response

	def submit(self, invoice_id, context):
		return self.client.request(
			"invoice.submit",
			"POST",
			"invoice_submit",
			path_params={"id": invoice_id},
			context={**context, "provider_document_id": invoice_id},
		)

	def send(self, payload, context, async_mode=False, existing_invoice_id=None, on_created=None):
		"""Create (unless a created-but-unsubmitted id is passed), then submit.

		Async: a 202 with a job id stops at stage "queued"; the job poller
		finishes the work. A synchronous create must return an invoice id or
		nothing is submitted. ``on_created(result)`` runs between the two
		calls so the caller can persist the ConvergeX id before submitting:
		if the submit then fails, a retry submits that id instead of creating
		a second invoice.
		"""
		result = SubmissionResult()
		invoice_id = existing_invoice_id

		if not invoice_id:
			created = self.create(payload, context, async_mode=async_mode)
			result.http_status = created.status_code
			result.correlation_id = created.correlation_id
			result.create_response = created.body
			result.validation_errors = self.mapper.validation_errors(created.body)

			if created.accepted_async or (async_mode and self.mapper.job_id(created.body) and not self.mapper.invoice_id(created.body)):
				result.job_id = self.mapper.job_id(created.body)
				if not result.job_id:
					raise ConvergeXError(
						"ConvergeX answered 202 Accepted without a job id; the invoice state is unknown. "
						"Check job_id_keys in the API contract.",
						http_status=created.status_code,
						response=created.body,
					)
				result.stage = "queued"
				return result

			invoice_id = self.mapper.invoice_id(created.body)
			if not invoice_id:
				raise ConvergeXError(
					"ConvergeX accepted the create call but returned no invoice id; nothing was submitted. "
					"Check invoice_id_keys in the API contract.",
					http_status=created.status_code,
					response=created.body,
				)
			result.provider_status = self.mapper.status(created.body)
			result.stage = "created"
			result.invoice_id = invoice_id
			if on_created:
				on_created(result)

		result.invoice_id = invoice_id
		submitted = self.submit(invoice_id, context)
		result.http_status = submitted.status_code
		result.correlation_id = submitted.correlation_id
		result.submit_response = submitted.body
		result.validation_errors = result.validation_errors or self.mapper.validation_errors(submitted.body)
		result.tracking_number = self.mapper.tracking_number(submitted.body)
		result.ota_reference = self.mapper.ota_reference(submitted.body)
		if submitted.accepted_async and self.mapper.job_id(submitted.body):
			result.job_id = self.mapper.job_id(submitted.body)
		# Status words come from ConvergeX; "submit_accepted" is our own marker
		# for "the submit call returned 2xx without a status" (maps to pending).
		result.provider_status = self.mapper.status(submitted.body) or "submit_accepted"
		result.stage = "submitted"
		return result


class ConvergeXJobService:
	def __init__(self, client, contract):
		self.client = client
		self.mapper = ConvergeXResponseMapper(contract)
		self.status = ConvergeXStatusService(contract)

	def check(self, job_id, context):
		response = self.client.request(
			"job.status", "GET", "job_status", path_params={"job_id": job_id}, context={**context, "job_id": job_id}
		)
		body = response.body
		raw = self.mapper.job_status(body)
		return {
			"http_status": response.status_code,
			"response": body,
			"job_status": raw,
			"state": self.status.job_state(raw),
			"invoice_id": self.mapper.job_invoice_id(body),
			"provider_status": self.mapper.status(body) if self.mapper.job_invoice_id(body) else None,
			"error": self.mapper.error_message(body) if self.status.job_state(raw) == "failed" else None,
			"validation_errors": self.mapper.validation_errors(body),
		}


class ConvergeXComplianceService:
	def __init__(self, client, contract):
		self.client = client
		self.contract = contract
		self.mapper = ConvergeXResponseMapper(contract)

	def fetch(self, kind, invoice_id, context):
		if kind not in COMPLIANCE_ROUTES:
			raise ConvergeXContractError(f"Unknown ConvergeX report '{kind}'.")
		response = self.client.request(
			f"invoice.{kind}",
			"GET",
			COMPLIANCE_ROUTES[kind],
			path_params={"id": invoice_id},
			context={**context, "provider_document_id": invoice_id},
		)
		body = response.body
		return {
			"http_status": response.status_code,
			"response": body,
			"provider_status": self.mapper.status(body),
			"validation_errors": self.mapper.validation_errors(body),
			"tracking_number": self.mapper.tracking_number(body),
			"ota_reference": self.mapper.ota_reference(body),
		}

	def status(self, invoice_id, context):
		"""Current invoice status: the contract's invoice_status route when
		ConvergeX names one, else the compliance route."""
		route = "invoice_status" if (self.contract.get("routes") or {}).get("invoice_status") else "compliance"
		response = self.client.request(
			"invoice.status",
			"GET",
			route,
			path_params={"id": invoice_id},
			context={**context, "provider_document_id": invoice_id},
		)
		body = response.body
		return {
			"http_status": response.status_code,
			"response": body,
			"provider_status": self.mapper.status(body),
			"validation_errors": self.mapper.validation_errors(body),
			"tracking_number": self.mapper.tracking_number(body),
			"ota_reference": self.mapper.ota_reference(body),
		}


class ConvergeXCustomerService:
	def __init__(self, client, contract):
		self.client = client
		self.contract = contract
		self.mapper = ConvergeXResponseMapper(contract)

	@property
	def enabled(self):
		return bool(self.contract.get("customer_sync_required")) and bool(
			(self.contract.get("routes") or {}).get("customer_sync")
		)

	def sync(self, payload, context):
		response = self.client.request("customer.sync", "POST", "customer_sync", json=payload, context=context)
		return {
			"http_status": response.status_code,
			"response": response.body,
			"customer_id": self.mapper.customer_id(response.body),
		}
