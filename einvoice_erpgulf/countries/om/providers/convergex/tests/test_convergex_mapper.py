# ruff: noqa: I001  (helpers must be imported first: it installs the frappe stand-in)
import ast
import os
import unittest

from . import helpers  # installs the frappe stand-in when needed
from .helpers import FakeSettings

from einvoice_erpgulf.countries.om.profile import OmProfile
from einvoice_erpgulf.countries.om.providers.convergex.config import ConvergeXConfig
from einvoice_erpgulf.countries.om.providers.convergex.exceptions import ConvergeXValidationError
from einvoice_erpgulf.countries.om.providers.convergex.mapper import ConvergeXInvoiceMapper


class Source:
	def __init__(self, records):
		self.records = records

	def get(self, doctype, name):
		return self.records.get((doctype, name)) if name else None

	def company_address(self, company):
		return self.records.get(("Address", "COMPANY-ADDR"))

	def company_currency(self, company):
		return "OMR"


def records(changes=None):
	base = {
		("Company", "Test Oman LLC"): {"name": "Test Oman LLC", "company_name": "Test Oman LLC", "tax_id": "OM1100223344"},
		("Customer", "Buyer SAOG"): {"name": "Buyer SAOG", "customer_name": "Buyer SAOG", "tax_id": "OM1100998877"},
		("Address", "COMPANY-ADDR"): {"name": "COMPANY-ADDR", "address_line1": "Way 1", "city": "Muscat",
			"country": "Oman", "custom_governorate": "Muscat"},
		("Address", "BUYER-ADDR"): {"name": "BUYER-ADDR", "address_line1": "Way 2", "city": "Sohar", "country": "Oman"},
		("Sales Invoice", "SINV-0001"): {"name": "SINV-0001", "posting_date": "2026-11-01", "custom_document_id": "CX-1"},
	}
	base.update(changes or {})
	return base


def invoice(**changes):
	doc = {
		"doctype": "Sales Invoice", "name": "SINV-0002", "company": "Test Oman LLC", "customer": "Buyer SAOG",
		"customer_name": "Buyer SAOG", "customer_address": "BUYER-ADDR", "posting_date": "2026-11-20",
		"posting_time": "10:15:30.123", "due_date": "2026-12-20", "currency": "OMR", "conversion_rate": 1,
		"custom_vat_category": "S - Standard rated", "total_taxes_and_charges": 5.25,
		"taxes": [{"rate": 5}],
		"items": [
			{"idx": 1, "item_code": "SRV-1", "item_name": "Consulting", "qty": 2, "uom": "Nos",
				"price_list_rate": 50, "rate": 50, "amount": 100, "net_amount": 100},
			{"idx": 2, "item_code": "GD-1", "item_name": "Cable", "qty": 1, "uom": "Kg",
				"price_list_rate": 6, "rate": 5, "amount": 5, "net_amount": 5},
		],
	}
	doc.update(changes)
	return doc


def mapper(contract=None, source=None, **settings):
	import json

	s = FakeSettings(custom_convergex_invoice_type_uuid="type-inv", custom_convergex_credit_note_type_uuid="type-cn", **settings)
	if contract:
		s["custom_convergex_contract"] = json.dumps(contract)
	return ConvergeXInvoiceMapper(OmProfile(), ConvergeXConfig(s), source or Source(records()))


class TestInvoiceMapping(unittest.TestCase):
	def test_valid_invoice(self):
		m = mapper()
		payload, canonical = m.build(invoice(), customer_reference="CUST-5")
		self.assertEqual(payload, canonical)  # no field map configured
		self.assertEqual(payload["document_type_uuid"], "type-inv")
		self.assertEqual(payload["invoice_type_code"], "380")
		self.assertEqual(payload["customization_id"], "urn:peppol:pint:billing-1@om-1")
		self.assertEqual(payload["issue_time"], "10:15:30")
		self.assertEqual(payload["seller"]["vat_number"], "OM1100223344")
		self.assertEqual(payload["seller"]["address"]["subdivision"], "MA")
		self.assertEqual(payload["buyer"]["provider_customer_id"], "CUST-5")
		line1, line2 = payload["lines"]
		self.assertEqual(line1["unit_code"], "EA")
		self.assertEqual(line1["vat_amount"], "5.000")  # OMR: 3 decimals
		self.assertEqual(line2["unit_code"], "KGM")
		self.assertEqual(line2["discount_amount"], "1.000")
		self.assertEqual(payload["totals"]["tax_exclusive_amount"], "105.000")
		self.assertEqual(payload["totals"]["tax_amount"], "5.250")
		self.assertEqual(payload["totals"]["tax_inclusive_amount"], "110.250")
		self.assertEqual(payload["tax_summary"], [{"vat_category": "S", "vat_rate": "5", "taxable_amount": "105.000", "vat_amount": "5.250"}])
		self.assertEqual(m.warnings, [])

	def test_missing_mandatory_fields_are_all_reported(self):
		recs = records({("Company", "Test Oman LLC"): {"name": "Test Oman LLC", "company_name": "Test Oman LLC"}})
		doc = invoice(custom_vat_category=None, items=[{"idx": 1, "item_code": "X", "qty": 0, "amount": 0, "net_amount": 0}])
		with self.assertRaises(ConvergeXValidationError) as ctx:
			mapper(source=Source(recs)).build(doc)
		errors = " | ".join(ctx.exception.errors)
		self.assertIn("no Tax ID (seller VAT number)", errors)
		self.assertIn("quantity is zero", errors)
		self.assertIn("no VAT Category", errors)

	def test_b2b_buyer_needs_vat_but_b2c_does_not(self):
		recs = records({("Customer", "Buyer SAOG"): {"name": "Buyer SAOG", "customer_name": "Buyer SAOG"}})
		with self.assertRaises(ConvergeXValidationError):
			mapper(source=Source(recs)).build(invoice())
		recs[("Customer", "Buyer SAOG")]["custom_is_b2c_customer"] = 1
		payload, _ = mapper(source=Source(recs)).build(invoice())
		self.assertTrue(payload["buyer"]["is_b2c"])

	def test_exempt_line_needs_reason(self):
		with self.assertRaises(ConvergeXValidationError):
			mapper().build(invoice(custom_vat_category="E - Exempt from tax"))
		payload, _ = mapper().build(invoice(custom_vat_category="E - Exempt from tax",
			custom_vat_exemption_reason_code="VATEX-OM-01 - Financial services"))
		self.assertEqual(payload["lines"][0]["vat_amount"], "0.000")
		self.assertEqual(payload["lines"][0]["vat_exemption_reason_code"], "VATEX-OM-01")

	def test_credit_note(self):
		doc = invoice(is_return=1, return_against="SINV-0001", custom_credit_note_reason_code="DL8.61.1.A-Cancellation",
			items=[{"idx": 1, "item_code": "SRV-1", "qty": -2, "uom": "Nos", "rate": 50, "amount": -100, "net_amount": -100}])
		payload, _ = mapper().build(doc)
		self.assertEqual(payload["invoice_type_code"], "381")
		self.assertEqual(payload["document_type_uuid"], "type-cn")
		self.assertNotIn("due_date", payload)
		self.assertEqual(payload["credit_note"], {"original_invoice_number": "SINV-0001", "original_invoice_date": "2026-11-01",
			"original_provider_invoice_id": "CX-1", "reason_code": "DL8.61.1.A", "reason": "Cancellation"})
		self.assertEqual(payload["lines"][0]["quantity"], "2.000000")

	def test_credit_note_without_original_or_reason(self):
		doc = invoice(is_return=1, items=[{"idx": 1, "item_code": "A", "qty": -1, "rate": 1, "amount": -1, "net_amount": -1}])
		with self.assertRaises(ConvergeXValidationError) as ctx:
			mapper().build(doc)
		self.assertEqual(len(ctx.exception.errors), 2)

	def test_credit_notes_switched_off(self):
		with self.assertRaises(ConvergeXValidationError):
			mapper(contract={"credit_notes_supported": False}).build(invoice(is_return=1))

	def test_field_map_and_root_key(self):
		payload, canonical = mapper(contract={"payload_field_map": {"invoice_number": "invoiceNo", "lines": "items"},
			"payload_root_key": "invoice"}).build(invoice())
		self.assertEqual(payload["invoice"]["invoiceNo"], "SINV-0002")
		self.assertIn("items", payload["invoice"])
		self.assertIn("invoice_number", canonical)

	def test_foreign_currency_adds_tax_currency(self):
		payload, _ = mapper().build(invoice(currency="USD", conversion_rate=0.385, base_total_taxes_and_charges=2.021))
		self.assertEqual(payload["tax_currency"], "OMR")
		self.assertEqual(payload["totals"]["tax_amount_in_tax_currency"], "2.021")


class TestRegressionExistingProviders(unittest.TestCase):
	"""Static check (no framework needed): ConvergeX is added under OM and
	every existing provider registration is unchanged."""

	def test_hooks_registrations(self):
		path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "hooks.py")
		with open(path) as f:
			tree = ast.parse(f.read())
		value = next(n.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "einvoice_providers")
		providers = ast.literal_eval(value)
		self.assertEqual(providers["OM"]["ConvergeX"],
			"einvoice_erpgulf.countries.om.providers.convergex.adapter.ConvergeXAdapter")
		self.assertEqual(sorted(providers["AE"]), ["A-Cube", "Flick Network L.L.C", "Marmin AI Software Design LLC",
			"Suntech Business Solutions DMCC"])
		self.assertEqual(list(providers["BE"]), ["A-Cube"])
		self.assertNotIn("ConvergeX", providers["AE"])


if __name__ == "__main__":
	unittest.main()
