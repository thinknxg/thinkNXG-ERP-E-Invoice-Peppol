# ruff: noqa: I001  (helpers must be imported first: it installs the frappe stand-in)
import unittest

from . import helpers  # installs the frappe stand-in when needed
from .helpers import FakeResponse, FakeSettings, make_stack, token

from einvoice_erpgulf.countries.om.providers.convergex import exceptions as ex
from einvoice_erpgulf.countries.om.providers.convergex.services import (
	ConvergeXComplianceService,
	ConvergeXCustomerService,
	ConvergeXInvoiceService,
	ConvergeXJobService,
)
from einvoice_erpgulf.countries.om.providers.convergex.status import ConvergeXStatusService

CTX = {"reference_doctype": "Sales Invoice", "reference_name": "SINV-0001"}
PAYLOAD = {"invoice_number": "SINV-0001"}


class TestInvoiceSubmission(unittest.TestCase):
	def test_create_then_submit(self):
		s = make_stack(token(), FakeResponse(201, {"id": "CX-9", "status": "received"}),
			FakeResponse(200, {"status": "submitted", "tracking_number": "TRK-1"}))
		created = []
		result = ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX, on_created=created.append)
		self.assertEqual(result.stage, "submitted")
		self.assertEqual(result.invoice_id, "CX-9")
		self.assertEqual(result.provider_status, "submitted")
		self.assertEqual(result.tracking_number, "TRK-1")
		self.assertEqual(created[0].invoice_id, "CX-9")  # persisted before submit
		self.assertEqual(s.http.calls[1]["json"], PAYLOAD)
		self.assertTrue(s.http.calls[2]["url"].endswith("/api/invoices/CX-9/submit/"))

	def test_submit_without_status_is_not_reported(self):
		s = make_stack(token(), FakeResponse(201, {"id": "CX-9"}), FakeResponse(200, {}))
		result = ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX)
		self.assertEqual(result.provider_status, "submit_accepted")
		self.assertEqual(ConvergeXStatusService(s.config.contract).to_reporting_status(result.provider_status), "pending")

	def test_invalid_invoice_rejected_by_convergex(self):
		s = make_stack(token(), FakeResponse(400, {"errors": {"lines": ["at least one line is required"]}}))
		with self.assertRaises(ex.ConvergeXValidationError) as ctx:
			ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX)
		self.assertIn("lines: at least one line is required", ctx.exception.errors)
		self.assertEqual(len(s.http.calls), 2)  # submit never called

	def test_create_without_invoice_id_does_not_submit(self):
		s = make_stack(token(), FakeResponse(201, {"message": "ok"}))
		with self.assertRaises(ex.ConvergeXError):
			ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX)
		self.assertEqual(len(s.http.calls), 2)

	def test_resume_created_invoice_skips_create(self):
		s = make_stack(token(), FakeResponse(200, {"status": "submitted"}))
		result = ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX, existing_invoice_id="CX-7")
		self.assertEqual(result.invoice_id, "CX-7")
		self.assertEqual(len(s.http.calls), 2)
		self.assertTrue(s.http.calls[1]["url"].endswith("/api/invoices/CX-7/submit/"))

	def test_duplicate_submission_conflict(self):
		s = make_stack(token(), FakeResponse(409, {"detail": "Invoice SINV-0001 already exists"}))
		with self.assertRaises(ex.ConvergeXDuplicateError):
			ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX)

	def test_async_job_creation(self):
		s = make_stack(token(), FakeResponse(202, {"job_id": "JOB-42", "status": "queued"}))
		result = ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX, async_mode=True)
		self.assertEqual(result.stage, "queued")
		self.assertEqual(result.job_id, "JOB-42")
		self.assertIsNone(result.invoice_id)
		self.assertEqual(s.http.calls[1]["params"], {"mode": "async"})

	def test_async_202_without_job_id_is_an_error(self):
		s = make_stack(token(), FakeResponse(202, {}))
		with self.assertRaises(ex.ConvergeXError):
			ConvergeXInvoiceService(s.client, s.config.contract).send(PAYLOAD, CTX, async_mode=True)


class TestJobStatus(unittest.TestCase):
	def _check(self, body):
		s = make_stack(token(), FakeResponse(200, body))
		return ConvergeXJobService(s.client, s.config.contract).check("JOB-42", CTX), s

	def test_pending(self):
		info, s = self._check({"status": "processing"})
		self.assertEqual(info["state"], "pending")
		self.assertTrue(s.http.calls[1]["url"].endswith("/api/integration/jobs/JOB-42/"))

	def test_completed(self):
		info, _ = self._check({"status": "completed", "result": {"invoice_id": "CX-77"}})
		self.assertEqual(info["state"], "completed")
		self.assertEqual(info["invoice_id"], "CX-77")

	def test_failed(self):
		info, _ = self._check({"status": "failed", "detail": "Buyer TIN invalid"})
		self.assertEqual(info["state"], "failed")
		self.assertIn("Buyer TIN invalid", info["error"])

	def test_unknown_word_keeps_polling(self):
		info, _ = self._check({"status": "weird"})
		self.assertEqual(info["state"], "pending")


class TestCompliance(unittest.TestCase):
	def _fetch(self, kind, body, status=200):
		s = make_stack(token(), FakeResponse(status, body))
		return ConvergeXComplianceService(s.client, s.config.contract).fetch(kind, "CX-9", CTX), s

	def test_successful_compliance(self):
		info, s = self._fetch("compliance", {"status": "approved", "errors": []})
		self.assertEqual(info["provider_status"], "approved")
		self.assertEqual(info["validation_errors"], [])
		self.assertTrue(s.http.calls[1]["url"].endswith("/api/invoices/CX-9/compliance/"))

	def test_failed_compliance(self):
		info, _ = self._fetch("compliance", {"status": "rejected", "errors": [
			{"code": "BR-OM-01", "message": "Seller VAT number format"}]})
		self.assertEqual(info["provider_status"], "rejected")
		self.assertEqual(info["validation_errors"], ["BR-OM-01: Seller VAT number format"])

	def test_ota_preview(self):
		info, s = self._fetch("ota_preview", {"preview": "<Invoice/>"})
		self.assertTrue(s.http.calls[1]["url"].endswith("/api/invoices/CX-9/ota-preview/"))
		self.assertEqual(info["response"]["preview"], "<Invoice/>")

	def test_tdd_report(self):
		_info, s = self._fetch("tdd_report", {"report": {"vat_total": "5.000"}})
		self.assertTrue(s.http.calls[1]["url"].endswith("/api/invoices/CX-9/tdd-report/"))

	def test_unknown_report(self):
		s = make_stack()
		with self.assertRaises(ex.ConvergeXContractError):
			ConvergeXComplianceService(s.client, s.config.contract).fetch("xml", "CX-9", CTX)


class TestCustomerSync(unittest.TestCase):
	def test_sync_returns_customer_id(self):
		s = make_stack(token(), FakeResponse(201, {"customer_id": "CUST-5"}))
		svc = ConvergeXCustomerService(s.client, s.config.contract)
		self.assertTrue(svc.enabled)
		self.assertEqual(svc.sync({"legal_name": "Buyer"}, CTX)["customer_id"], "CUST-5")

	def test_sync_disabled_by_contract(self):
		s = make_stack(contract={"customer_sync_required": False})
		self.assertFalse(ConvergeXCustomerService(s.client, s.config.contract).enabled)


class TestStatusMapping(unittest.TestCase):
	def setUp(self):
		self.svc = ConvergeXStatusService(make_stack().config.contract)

	def test_states_are_not_conflated(self):
		m = self.svc.to_reporting_status
		self.assertEqual(m("received"), "pending")
		self.assertEqual(m("submitted"), "pending")
		self.assertEqual(m("ota_accepted"), "accepted")
		self.assertEqual(m("approved"), "reported")
		self.assertEqual(m("Rejected"), "rejected")
		self.assertEqual(m("failed"), "failed")
		self.assertEqual(m("something_new"), "something_new")  # passed through, never guessed final

	def test_final(self):
		self.assertTrue(self.svc.is_final("approved"))
		self.assertFalse(self.svc.is_final("ota_accepted"))
		self.assertTrue(self.svc.is_rejected_or_failed("rejected"))


class TestConfig(unittest.TestCase):
	def test_production_requires_verified_contract(self):
		s = make_stack(settings=FakeSettings(custom_environment="Production"))
		self.assertTrue(any("verified" in p for p in s.config.problems(for_submission=True)))
		s = make_stack(settings=FakeSettings(custom_environment="Production"), contract={"verified": True})
		self.assertEqual(s.config.problems(for_submission=True), [])

	def test_http_base_url_rejected(self):
		s = make_stack(settings=FakeSettings(base_url="http://insecure.example"))
		self.assertTrue(any("https" in p for p in s.config.problems()))

	def test_bad_override_json(self):
		with self.assertRaises(ex.ConvergeXConfigError):
			make_stack(settings=FakeSettings(custom_convergex_contract="{not json"))

	def test_override_merges_routes(self):
		s = make_stack(contract={"routes": {"invoice_create": "/v2/invoices/"}})
		self.assertEqual(s.config.url("invoice_create"), "https://sandbox.convergex.example/v2/invoices/")
		self.assertEqual(s.config.url("invoice_submit", id="X"), "https://sandbox.convergex.example/api/invoices/X/submit/")


if __name__ == "__main__":
	unittest.main()
