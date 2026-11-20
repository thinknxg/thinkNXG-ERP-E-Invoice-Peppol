# ruff: noqa: I001  (helpers must be imported first: it installs the frappe stand-in)
import unittest

from . import helpers  # installs the frappe stand-in when needed
from .helpers import FakeResponse, FakeSettings, make_stack, token

from einvoice_erpgulf.countries.om.providers.convergex.exceptions import (
	ConvergeXAuthError,
	ConvergeXConfigError,
	ConvergeXNetworkError,
	ConvergeXServerError,
)


class TestConvergeXAuthentication(unittest.TestCase):
	def test_successful_authentication_caches_token(self):
		s = make_stack(token())
		self.assertEqual(s.auth.get_access_token(), "acc-1")
		self.assertEqual(s.auth.get_access_token(), "acc-1")  # cached: no second call
		self.assertEqual(len(s.http.calls), 1)
		call = s.http.calls[0]
		self.assertEqual(call["url"], "https://sandbox.convergex.example/api/auth/token/")
		self.assertEqual(call["json"], {"client_id": "client-123", "client_secret": "s3cr3t-value"})

	def test_invalid_credentials(self):
		s = make_stack(FakeResponse(401, {"detail": "No active account found"}))
		with self.assertRaises(ConvergeXAuthError):
			s.auth.get_access_token()

	def test_token_expiry_uses_refresh_token(self):
		s = make_stack(token(access="acc-1", expires_in=120), token(access="acc-2", refresh=None))
		self.assertEqual(s.auth.get_access_token(), "acc-1")
		s.clock.now += 200  # past expiry (120 - 30 s margin)
		self.assertEqual(s.auth.get_access_token(), "acc-2")
		self.assertTrue(s.http.calls[1]["url"].endswith("/api/auth/token/refresh/"))
		self.assertEqual(s.http.calls[1]["json"], {"refresh": "ref-1"})

	def test_refresh_rejected_falls_back_to_new_token(self):
		s = make_stack(
			token(access="acc-1", expires_in=60),
			FakeResponse(401, {"detail": "Token is invalid or expired"}),
			token(access="acc-3"),
		)
		s.auth.get_access_token()
		s.clock.now += 100
		self.assertEqual(s.auth.get_access_token(), "acc-3")
		self.assertTrue(s.http.calls[2]["url"].endswith("/api/auth/token/"))

	def test_auth_server_unavailable(self):
		s = make_stack(ConnectionError("connection refused"))
		with self.assertRaises(ConvergeXNetworkError):
			s.auth.get_access_token()
		s = make_stack(FakeResponse(503, "Service Unavailable"))
		with self.assertRaises(ConvergeXServerError):
			s.auth.get_access_token()

	def test_missing_access_token_in_response(self):
		s = make_stack(FakeResponse(200, {"unexpected": "shape"}))
		with self.assertRaises(ConvergeXAuthError):
			s.auth.get_access_token()

	def test_jwt_exp_used_when_no_expires_in(self):
		import jwt

		s = make_stack()
		exp_token = jwt.encode({"exp": int(s.clock.now) + 900}, "k", algorithm="HS256")
		s.http.script.append(FakeResponse(200, {"access": exp_token}))
		s.auth.get_access_token()
		self.assertAlmostEqual(s.auth.token_expiry(), s.clock.now + 870, delta=1)

	def test_credentials_and_tokens_never_logged(self):
		s = make_stack(token(access="acc-secret-token", refresh="ref-secret-token"))
		s.auth.get_access_token()
		text = repr(s.logger.rows)
		for secret in ("s3cr3t-value", "acc-secret-token", "ref-secret-token"):
			self.assertNotIn(secret, text)

	def test_incomplete_settings_raise_config_error(self):
		s = make_stack(settings=FakeSettings(client_secret=""))
		with self.assertRaises(ConvergeXConfigError):
			s.auth.get_access_token()
		self.assertEqual(s.http.calls, [])  # nothing sent


if __name__ == "__main__":
	unittest.main()
