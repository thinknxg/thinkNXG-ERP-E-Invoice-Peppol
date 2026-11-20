# ruff: noqa: I001  (helpers must be imported first: it installs the frappe stand-in)
import unittest

from . import helpers  # installs the frappe stand-in when needed
from .helpers import FakeResponse, make_stack, token

from einvoice_erpgulf.countries.om.providers.convergex import exceptions as ex


def _compliance(s):
	return s.client.request("invoice.compliance", "GET", "compliance", path_params={"id": "INV-1"})


class TestConvergeXErrorHandling(unittest.TestCase):
	def test_http_400_is_validation_error_with_details(self):
		s = make_stack(token(), FakeResponse(400, {"errors": [{"field": "buyer.vat_number", "message": "required"}]}))
		with self.assertRaises(ex.ConvergeXValidationError) as ctx:
			_compliance(s)
		self.assertIn("buyer.vat_number: required", ctx.exception.errors)
		self.assertEqual(len(s.http.calls), 2)  # not retried

	def test_http_401_reauthenticates_once(self):
		s = make_stack(token(access="old"), FakeResponse(401, {"detail": "expired"}), token(access="new"),
			FakeResponse(200, {"status": "approved"}))
		self.assertEqual(_compliance(s).body["status"], "approved")
		self.assertEqual(s.http.calls[3]["headers"]["Authorization"], "Bearer new")

	def test_http_401_twice_raises_auth_error(self):
		s = make_stack(token(), FakeResponse(401, {}), token(access="new"), FakeResponse(401, {}))
		with self.assertRaises(ex.ConvergeXAuthError):
			_compliance(s)

	def test_http_403(self):
		s = make_stack(token(), FakeResponse(403, {"detail": "forbidden"}))
		with self.assertRaises(ex.ConvergeXPermissionError):
			_compliance(s)

	def test_http_404(self):
		s = make_stack(token(), FakeResponse(404, {"detail": "Not found."}))
		with self.assertRaises(ex.ConvergeXNotFoundError):
			_compliance(s)

	def test_http_409(self):
		s = make_stack(token(), FakeResponse(409, {"detail": "duplicate invoice number"}))
		with self.assertRaises(ex.ConvergeXDuplicateError):
			_compliance(s)

	def test_http_429_retries_after_retry_after(self):
		s = make_stack(token(), FakeResponse(429, {"detail": "slow down"}, {"Retry-After": "5"}),
			FakeResponse(200, {"status": "submitted"}))
		self.assertEqual(_compliance(s).attempts, 2)
		self.assertEqual(s.sleeps, [5.0])

	def test_http_429_exhausted(self):
		s = make_stack(token(), *[FakeResponse(429, {}) for _ in range(3)])
		with self.assertRaises(ex.ConvergeXRateLimitError):
			_compliance(s)

	def test_http_500_retried_then_raises(self):
		s = make_stack(token(), FakeResponse(500, "boom"), FakeResponse(502, "bad gateway"), FakeResponse(503, "x"))
		with self.assertRaises(ex.ConvergeXServerError):
			_compliance(s)
		self.assertEqual(s.sleeps, [2.0, 4.0])

	def test_timeout_retried_then_succeeds(self):
		s = make_stack(token(), TimeoutError("read timed out"), FakeResponse(200, {"status": "received"}))
		self.assertEqual(_compliance(s).body["status"], "received")

	def test_network_failure(self):
		s = make_stack(token(), *[ConnectionError("dns") for _ in range(3)])
		with self.assertRaises(ex.ConvergeXNetworkError):
			_compliance(s)

	def test_log_rows_are_sanitised_and_complete(self):
		s = make_stack(token(access="tok-xyz"), FakeResponse(200, {"id": "CX-1", "status": "submitted", "access_token": "leak"}))
		s.client.request("invoice.create", "POST", "invoice_create", json={"client_secret": "nope", "n": 1},
			context={"reference_doctype": "Sales Invoice", "reference_name": "SINV-1"})
		row = s.logger.rows[-1]
		self.assertEqual(row["operation"], "invoice.create")
		self.assertEqual(row["http_status"], 200)
		self.assertEqual(row["provider_document_id"], "CX-1")
		self.assertEqual(row["reference_name"], "SINV-1")
		self.assertTrue(row["correlation_id"])
		self.assertNotIn("tok-xyz", repr(s.logger.rows))
		self.assertNotIn("leak", repr(row))
		self.assertNotIn("nope", repr(row))

	def test_undefined_route_is_contract_error_not_a_guess(self):
		s = make_stack(contract={"routes": {"compliance": None}})
		with self.assertRaises(ex.ConvergeXContractError):
			_compliance(s)
		self.assertEqual(s.http.calls, [])


if __name__ == "__main__":
	unittest.main()
