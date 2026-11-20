"""Test doubles for the ConvergeX tests: a frappe stand-in (only when the
real framework is not importable), fake HTTP, fake cache, list logger.

No test ever opens a network connection: every HTTP call goes to FakeHttp.
"""

import sys
import types

try:  # under `bench run-tests` the real framework is present
	import frappe
except ImportError:  # plain `python -m unittest`: minimal stand-in
	fake = types.ModuleType("frappe")

	class _ValidationError(Exception):
		pass

	def _throw(msg, exc=None, title=None, **kwargs):
		raise (exc or _ValidationError)(msg)

	fake._ = lambda text, *a, **k: text
	fake.throw = _throw
	fake.ValidationError = _ValidationError
	fake.local = types.SimpleNamespace(site="test.local")
	sys.modules["frappe"] = fake


class FakeResponse:
	def __init__(self, status_code, body=None, headers=None):
		self.status_code = status_code
		self._body = body
		self.headers = headers or {}
		self.text = body if isinstance(body, str) else ""

	def json(self):
		if isinstance(self._body, (dict, list)):
			return self._body
		raise ValueError("not json")


class FakeHttp:
	"""Scripted responses. Each item is a FakeResponse or an Exception."""

	def __init__(self, *script):
		self.script = list(script)
		self.calls = []

	def _next(self, method, url, kwargs):
		self.calls.append({"method": method, "url": url, **kwargs})
		if not self.script:
			raise AssertionError(f"unexpected HTTP call {method} {url}")
		item = self.script.pop(0)
		if isinstance(item, Exception):
			raise item
		return item

	def post(self, url, **kwargs):
		return self._next("POST", url, kwargs)

	def request(self, method, url, **kwargs):
		return self._next(method, url, kwargs)


class Clock:
	def __init__(self, now=1_000_000.0):
		self.now = now

	def __call__(self):
		return self.now


class FakeCache:
	def __init__(self, clock):
		self.clock = clock
		self.data = {}

	def get(self, key):
		value, expires = self.data.get(key, (None, 0))
		return value if expires > self.clock() else None

	def set(self, key, value, ttl):
		self.data[key] = (value, self.clock() + ttl)

	def delete(self, key):
		self.data.pop(key, None)


class ListLogger:
	def __init__(self):
		self.rows = []

	def log(self, **fields):
		self.rows.append(fields)

	def bind(self, *a):
		return self


class FakeSettings(dict):
	"""Provider Settings row stand-in (get / get_password)."""

	def __init__(self, **overrides):
		base = {
			"name": "CX-SANDBOX",
			"company": "Test Oman LLC",
			"provider": "ConvergeX",
			"custom_environment": "Sandbox",
			"base_url": "https://sandbox.convergex.example",
			"client_id": "client-123",
			"client_secret": "s3cr3t-value",
			"enabled": 1,
			"custom_convergex_sync_customers": 1,
		}
		base.update(overrides)
		super().__init__(base)

	def get_password(self, field, raise_exception=True):
		return self.get(field)


def make_stack(*script, settings=None, contract=None):
	"""config, auth, client, http, cache, logger, clock wired together."""
	import json

	from einvoice_erpgulf.countries.om.providers.convergex.auth import ConvergeXAuthentication
	from einvoice_erpgulf.countries.om.providers.convergex.client import ConvergeXApiClient
	from einvoice_erpgulf.countries.om.providers.convergex.config import ConvergeXConfig

	settings = settings or FakeSettings()
	if contract is not None:
		settings["custom_convergex_contract"] = json.dumps(contract)
	config = ConvergeXConfig(settings)
	clock = Clock()
	cache = FakeCache(clock)
	http = FakeHttp(*script)
	logger = ListLogger()
	sleeps = []
	auth = ConvergeXAuthentication(config, cache, http, logger=logger, clock=clock)
	client = ConvergeXApiClient(config, auth, http, logger=logger, sleep=sleeps.append, clock=clock)
	return types.SimpleNamespace(
		config=config, auth=auth, client=client, http=http, cache=cache, logger=logger, clock=clock, sleeps=sleeps
	)


def token(access="acc-1", refresh="ref-1", expires_in=600):
	body = {"access": access}
	if refresh:
		body["refresh"] = refresh
	if expires_in is not None:
		body["expires_in"] = expires_in
	return FakeResponse(200, body)
