"""ConvergeX (Oman) Accredited Service Provider adapter.

ConvergeXProvider for the shared e-invoice flow: implements the
core.base.BaseAdapter contract and delegates to the pieces in this folder.

    config.py       ConvergeXConfig       settings row -> validated config
    contract.py     API contract          routes / keys / status words (overridable)
    auth.py         ConvergeXAuthentication
    client.py       ConvergeXApiClient    retries, error classification, logging
    mapper.py       ConvergeXInvoiceMapper
    services.py     invoice / job / compliance / customer services
    status.py       ConvergeXStatusService
    response.py     ConvergeXResponseMapper + sanitiser
    exceptions.py   ConvergeXError hierarchy
    persistence.py  Redis token cache, API log writer, submission lock
    jobs.py         async job polling (scheduler + enqueue)
    api.py          whitelisted buttons (reports, job status, retry, customer sync)

ConvergeX takes JSON and builds the PINT-OM UBL itself, so this adapter does
not call self.build_ubl(). See docs/CONVERGEX.md for what is verified and
what still needs ConvergeX's confirmation.
"""

import json
from datetime import datetime

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from einvoice_erpgulf.core.base import BaseAdapter

from .auth import ConvergeXAuthentication
from .client import ConvergeXApiClient
from .config import ConvergeXConfig
from .exceptions import ConvergeXDuplicateError, ConvergeXError, ConvergeXValidationError
from .mapper import ConvergeXInvoiceMapper
from .persistence import ApiLogger, FrappeCache, SubmissionLock
from .response import ConvergeXResponseMapper
from .services import (
	ConvergeXComplianceService,
	ConvergeXCustomerService,
	ConvergeXInvoiceService,
	ConvergeXJobService,
)
from .status import ConvergeXStatusService

PROVIDER = "ConvergeX"
INVOICE_FIELDS = (
	"custom_provider_status",
	"custom_einvoice_job_id",
	"custom_provider_error",
	"custom_compliance_report",
)
CUSTOMER_IDS_FIELD = "custom_convergex_customer_ids"


class ConvergeXAdapter(BaseAdapter):
	# No documented XML / PDF download: nothing to fetch after submit.
	AUTO_FETCH_DOCUMENTS_ON_SUBMIT = False
	ATTACH_DOCUMENTS_IN_SYNC = False
	# "accepted" (ota_accepted) is deliberately not final: polling continues
	# until ConvergeX reports approved / rejected / failed.
	FINAL_REPORTING_STATUSES = ("reported", "rejected", "failed")

	def __init__(self, settings, profile=None):
		super().__init__(settings, profile)
		self._config = None
		self._logger = None
		self._auth = None
		self._client = None

	# ================================================================ wiring
	@property
	def config(self):
		if self._config is None:
			self._config = ConvergeXConfig(self.settings)
		return self._config

	@property
	def logger(self):
		if self._logger is None:
			self._logger = ApiLogger(PROVIDER, self.settings.company, self.settings.name)
		return self._logger

	@property
	def auth(self):
		if self._auth is None:
			self._auth = ConvergeXAuthentication(self.config, FrappeCache(), requests, logger=self.logger)
		return self._auth

	@property
	def client(self):
		if self._client is None:
			self._client = ConvergeXApiClient(self.config, self.auth, requests, logger=self.logger)
		return self._client

	@property
	def status_service(self):
		return ConvergeXStatusService(self.config.contract)

	@property
	def responses(self):
		return ConvergeXResponseMapper(self.config.contract)

	def _context(self, doc):
		self.logger.bind(doc.doctype, doc.name)
		return {
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
			"idempotency_key": f"{frappe.local.site}:{doc.doctype}:{doc.name}",
		}

	# ================================================================ settings
	def validate_settings(self):
		"""Called by the Provider Settings controller on save. Returns
		(errors, warnings); errors block saving an enabled row."""
		try:
			config = self.config
		except ConvergeXError as e:
			return [str(e)], []
		errors = config.problems() if config.enabled else []
		warnings = []
		if (self.settings.get("auth_type") or "oauth2") != "oauth2":
			errors.append(_("ConvergeX uses Client ID / Client Secret: set Auth Type to oauth2."))
		if config.is_production and not config.contract_verified:
			warnings.append(
				_(
					"Production row: invoices will not be submitted until the ConvergeX API contract is "
					'confirmed and API Contract Overrides contains "verified": true.'
				)
			)
		return errors, warnings

	# ================================================================ auth
	def get_auth_headers(self, extra=None):
		return {**(extra or {}), **self.auth.auth_header()}

	def get_valid_token(self):
		# The shared "Get Access Token" button would show and store the token;
		# ConvergeX tokens stay in Redis only. Verify Token tests the login.
		self._not_supported(_("Showing the ConvergeX access token (use Verify Token instead)"))

	def verify_auth(self):
		ok, message, details = False, "", {}
		try:
			self.config.assert_usable()
			self.auth.get_access_token(force_new=True)
			expiry = self.auth.token_expiry()
			ok = True
			message = _("ConvergeX issued an access token for {0} ({1}).").format(
				self.config.client_id, self.config.environment
			)
			details = {
				"environment": self.config.environment,
				"base_url": self.config.base_url,
				"token_valid_until": str(datetime.fromtimestamp(expiry).replace(microsecond=0)) if expiry else None,
				"api_contract_verified": self.config.contract_verified,
				"async_mode": self.config.async_mode,
			}
		except ConvergeXError as e:
			message = str(e)
		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "ConvergeX Verify Error")
			message = str(e)

		self.settings.db_set("last_test_status", "Passed" if ok else "Failed")
		self.settings.db_set("last_test_on", now_datetime())
		return {"status": "success" if ok else "error", "message": message, "response": details or None}

	def get_participant_details(self):
		"""ConvergeX has no documented account endpoint; show what this row
		is configured to use (no secrets)."""
		c = self.config
		return {
			"status": "success",
			"response": {
				"provider": PROVIDER,
				"environment": c.environment,
				"base_url": c.base_url,
				"client_id": c.client_id,
				"tenant_id": c.tenant_id or None,
				"api_version": c.api_version or None,
				"invoice_type_uuid": c.invoice_type_uuid or None,
				"credit_note_type_uuid": c.credit_note_type_uuid or None,
				"async_mode": c.async_mode,
				"customer_sync": c.sync_customers,
				"api_contract_verified": c.contract_verified,
				"routes": c.contract.get("routes"),
				"problems": c.problems(for_submission=True),
			},
		}

	# ================================================================ submit
	def submit_invoice(self, doctype, doc, json_data=None):
		if doctype != "Sales Invoice":
			self._not_supported(_("{0} via ConvergeX").format(doctype))

		with SubmissionLock(doctype, doc.name) as acquired:
			if not acquired:
				frappe.throw(_("{0} is already being sent to ConvergeX. Wait a moment and refresh.").format(doc.name))
			try:
				self.config.assert_usable(for_submission=True)
				existing_id = self._guard_duplicate(doc)
				ctx = self._context(doc)
				mapper = ConvergeXInvoiceMapper(self.profile, self.config)
				mapper.build_canonical(doc)  # validate locally before anything reaches ConvergeX
				customer_ref = self._ensure_customer(doc, ctx)
				payload, _canonical = mapper.build(doc, customer_ref)
				if mapper.warnings:
					self.logger.log(operation="invoice.mapping", error_message="; ".join(mapper.warnings))
				self.save_outgoing_payload(doc, payload)

				result = ConvergeXInvoiceService(self.client, self.config.contract).send(
					payload,
					ctx,
					async_mode=self.config.async_mode,
					existing_invoice_id=existing_id,
					on_created=lambda r: self._mark_created(doc, r),
				)
			except ConvergeXError as e:
				self._store_failure(doc, e)
				frappe.throw(self._user_message(e), title=_("ConvergeX"))

		self._store_result(doc, result)
		return result.http_status, result.as_dict()

	def parse_submit_response(self, status_code, response_data):
		body = response_data if isinstance(response_data, dict) else {}
		stage = body.get("stage")
		provider_status = body.get("provider_status")
		if stage == "submitted":
			return {
				"document_id": body.get("convergex_invoice_id"),
				"reporting_status": self.status_service.to_reporting_status(provider_status) or "pending",
				"exchange_status": provider_status,
				"success": True,  # handed to the ASP; OTA status follows in reporting_status
			}
		if stage == "queued":
			return {"document_id": None, "reporting_status": "pending", "exchange_status": "queued", "success": False}
		return {"document_id": body.get("convergex_invoice_id"), "reporting_status": None, "exchange_status": provider_status, "success": False}

	# ---- duplicate guard -------------------------------------------------
	def _guard_duplicate(self, doc):
		"""Return a created-but-unsubmitted ConvergeX id to resume, None to
		create a new invoice, or raise if it is already with ConvergeX."""
		fresh = frappe.db.get_value(
			doc.doctype,
			doc.name,
			["custom_document_id", "custom_einvoice_status", "custom_provider_status", "custom_einvoice_job_id"],
			as_dict=True,
		) or {}
		invoice_id = fresh.get("custom_document_id")
		raw = (fresh.get("custom_provider_status") or "").lower()
		job_id = fresh.get("custom_einvoice_job_id")
		svc = self.status_service

		if invoice_id and raw == "created":
			return invoice_id
		if raw and svc.is_rejected_or_failed(raw):
			return None
		if invoice_id and (raw or fresh.get("custom_einvoice_status") == "Success"):
			raise ConvergeXDuplicateError(
				_("{0} is already with ConvergeX as {1} (status: {2}). It will not be sent twice.").format(
					doc.name, invoice_id, raw or "submitted"
				)
			)
		if job_id and not invoice_id:
			raise ConvergeXDuplicateError(
				_("{0} is queued at ConvergeX as job {1}. Use Check Job Status instead of sending it again.").format(
					doc.name, job_id
				)
			)
		return None

	# ---- customer --------------------------------------------------------
	def _ensure_customer(self, doc, ctx, force=False):
		customers = ConvergeXCustomerService(self.client, self.config.contract)
		if not (self.config.sync_customers and customers.enabled):
			return None
		customer = frappe.get_doc("Customer", doc.customer)
		ids = _json_dict(customer.get(CUSTOMER_IDS_FIELD))
		known = ids.get(self.settings.name)
		if known and not force:
			return None if known == "synced" else known

		address_name = doc.get("customer_address") or frappe.db.get_value(
			"Dynamic Link", {"link_doctype": "Customer", "link_name": customer.name, "parenttype": "Address"}, "parent"
		)
		address = frappe.get_cached_doc("Address", address_name) if address_name else None
		payload = ConvergeXInvoiceMapper(self.profile, self.config).build_customer(customer, address)
		result = customers.sync(payload, {**ctx, "idempotency_key": f"{frappe.local.site}:Customer:{customer.name}"})
		ids[self.settings.name] = result.get("customer_id") or "synced"
		if customer.meta.has_field(CUSTOMER_IDS_FIELD):
			customer.db_set(CUSTOMER_IDS_FIELD, json.dumps(ids), update_modified=False)
		return result.get("customer_id")

	def sync_customer(self, customer_name, force=True):
		"""Button: push one customer to ConvergeX now."""
		stub = frappe._dict(doctype="Customer", name=customer_name, customer=customer_name, customer_address=None)
		ctx = self._context(stub)
		try:
			return {"status": "success", "customer_id": self._ensure_customer(stub, ctx, force=force)}
		except ConvergeXError as e:
			return {"status": "error", "message": self._user_message(e)}

	# ---- storing ---------------------------------------------------------
	def _set(self, doc, values):
		meta = frappe.get_meta(doc.doctype)
		clean = {k: v for k, v in values.items() if meta.has_field(k)}
		if clean:
			frappe.db.set_value(doc.doctype, doc.name, clean, update_modified=False)
			for k, v in clean.items():
				doc.set(k, v)

	def _mark_created(self, doc, result):
		self._set(doc, {"custom_document_id": result.invoice_id, "custom_provider_status": "created"})
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- keep the id even if submit fails next

	def _store_result(self, doc, result):
		values = {
			"custom_provider_status": result.provider_status or ("queued" if result.stage == "queued" else None),
			"custom_einvoice_job_id": result.job_id,
			"custom_provider_error": "\n".join(result.validation_errors) or "",
		}
		if result.invoice_id:
			values["custom_document_id"] = result.invoice_id
		self._set(doc, {k: v for k, v in values.items() if v is not None})
		if result.stage == "queued" or (result.job_id and not result.invoice_id):
			frappe.enqueue(
				"einvoice_erpgulf.countries.om.providers.convergex.jobs.poll_invoice_job",
				doctype=doc.doctype,
				name=doc.name,
				queue="short",
				enqueue_after_commit=True,
			)

	def _store_failure(self, doc, error):
		values = {"custom_provider_error": self._user_message(error)}
		if not isinstance(error, ConvergeXDuplicateError):
			fresh_status = frappe.db.get_value(doc.doctype, doc.name, "custom_einvoice_status")
			if fresh_status != "Success":
				values["custom_einvoice_status"] = "Not Submitted"
		self._set(doc, values)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the error must survive the rollback of the throw

	@staticmethod
	def _user_message(error):
		if isinstance(error, ConvergeXValidationError) and error.errors:
			return error.message + ":\n- " + "\n- ".join(map(str, error.errors))
		return str(error)

	def _apply_status(self, doc, info):
		provider_status = info.get("provider_status")
		values = {}
		if provider_status:
			values["custom_provider_status"] = provider_status
			values["custom_reporting_status"] = self.status_service.to_reporting_status(provider_status)
		errors = info.get("validation_errors") or []
		if errors:
			values["custom_provider_error"] = "\n".join(errors)
		self._set(doc, values)
		return values.get("custom_reporting_status")

	# ================================================================ status
	def get_document_status(self, doctype, doc):
		ctx = self._context(doc)
		invoice_id = doc.get("custom_document_id")
		provider_status = doc.get("custom_provider_status")
		if invoice_id and self.status_service.is_final(provider_status):
			# Final at ConvergeX already: the shared 5-minute sync keeps
			# selecting invoices without an XML attachment, so answer from what
			# is stored instead of calling the API again.
			return {
				"http_status": None,
				"response": _json_dict(doc.get("custom_document_status_response")) or {"status": provider_status},
				"reporting_status": self.status_service.to_reporting_status(provider_status),
			}
		try:
			if not invoice_id and doc.get("custom_einvoice_job_id"):
				info = self.poll_job(doc)
				return {"http_status": info.get("http_status"), "response": info.get("response"),
					"reporting_status": frappe.db.get_value(doctype, doc.name, "custom_reporting_status")}
			if not invoice_id:
				frappe.throw(_("{0} has no ConvergeX invoice id yet.").format(doc.name))
			info = ConvergeXComplianceService(self.client, self.config.contract).status(invoice_id, ctx)
		except ConvergeXError as e:
			self._set(doc, {"custom_provider_error": self._user_message(e)})
			return {"http_status": e.http_status, "response": {"error": str(e), "detail": e.response}}
		reporting = self._apply_status(doc, info)
		result = {"http_status": info["http_status"], "response": info["response"]}
		if reporting:
			result["reporting_status"] = reporting
		return result

	def get_status_from_document_status(self, body):
		return self.status_service.to_reporting_status(self.responses.status(body))

	def poll_job(self, doc):
		"""One poll of the integration job; finishes the submission when the
		job has produced a ConvergeX invoice."""
		job_id = doc.get("custom_einvoice_job_id")
		if not job_id:
			frappe.throw(_("{0} has no ConvergeX job id.").format(doc.name))
		ctx = self._context(doc)
		info = ConvergeXJobService(self.client, self.config.contract).check(job_id, ctx)

		if info["state"] == "failed":
			self._set(doc, {
				"custom_provider_status": "failed",
				"custom_reporting_status": "failed",
				"custom_einvoice_status": "Not Submitted",
				"custom_provider_error": "\n".join(filter(None, [info.get("error"), *info.get("validation_errors", [])])) or _("ConvergeX job {0} failed.").format(job_id),
			})
			return info

		if info["state"] != "completed":
			return info  # still pending; the scheduler polls again

		invoice_id = info.get("invoice_id")
		if not invoice_id:
			self._set(doc, {"custom_provider_error": _("ConvergeX job {0} completed but returned no invoice id. Check job_invoice_id_keys in the API contract.").format(job_id)})
			return info

		self._set(doc, {"custom_document_id": invoice_id, "custom_provider_status": info.get("provider_status") or "created"})
		if self.config.contract.get("async_create_includes_submit"):
			status = info.get("provider_status") or "submit_accepted"
		else:
			result = ConvergeXInvoiceService(self.client, self.config.contract).send(None, ctx, existing_invoice_id=invoice_id)
			self._store_result(doc, result)
			status = result.provider_status
		self._set(doc, {
			"custom_provider_status": status,
			"custom_reporting_status": self.status_service.to_reporting_status(status) or "pending",
			"custom_einvoice_status": "Success",
			"custom_provider_error": "",
		})
		return info

	def fetch_report(self, doc, kind):
		invoice_id = doc.get("custom_document_id")
		if not invoice_id:
			frappe.throw(_("{0} has no ConvergeX invoice id yet.").format(doc.name))
		info = ConvergeXComplianceService(self.client, self.config.contract).fetch(kind, invoice_id, self._context(doc))
		reports = _json_dict(frappe.db.get_value(doc.doctype, doc.name, "custom_compliance_report"))
		reports[kind] = {"fetched_on": str(now_datetime()), "http_status": info["http_status"], "response": info["response"]}
		self._set(doc, {"custom_compliance_report": json.dumps(reports, indent=2, default=str)})
		if kind == "compliance":
			self._apply_status(doc, info)
		elif info.get("validation_errors"):
			self._set(doc, {"custom_provider_error": "\n".join(info["validation_errors"])})
		return info

	# ================================================================ not offered
	def get_document_xml(self, doctype, doc):
		self._not_supported(_("XML download (ConvergeX has not documented a download route)"))

	def get_document_pdf(self, doctype, doc):
		self._not_supported(_("PDF download (ConvergeX has not documented a download route)"))


def _json_dict(raw):
	if not raw:
		return {}
	if isinstance(raw, dict):
		return raw
	try:
		value = json.loads(raw)
		return value if isinstance(value, dict) else {}
	except ValueError:
		return {}
