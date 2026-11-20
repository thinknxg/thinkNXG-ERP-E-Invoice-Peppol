"""ConvergeXInvoiceMapper: thinkNXG ERP Sales Invoice -> ConvergeX request.

Two steps, kept apart on purpose:

1. ``build_canonical`` turns the invoice into a neutral, fully named
   structure (header, seller, buyer, lines, tax summary, totals, credit-note
   reference) using the Oman profile for every coded value (tax categories,
   UN/ECE units, 3-decimal OMR amounts, PINT-OM customization id). Missing
   mandatory data is collected and raised together, never dropped.
2. ``to_provider`` renames keys with the contract's ``payload_field_map``
   (and wraps it in ``payload_root_key``) so ConvergeX's real property names,
   once supplied, are configuration rather than code.

ConvergeX takes JSON and produces the PINT-OM UBL itself (as its ASP role
implies), so no XML is generated here.

All reads go through a small data source so the mapper can be tested
without a database.
"""

from decimal import ROUND_HALF_UP, Decimal

from .exceptions import ConvergeXValidationError


def _d(value):
	return Decimal(str(value or 0))


def _s(value):
	return (str(value).strip() if value is not None else "") or None


class FrappeDataSource:
	"""Live reads; tests pass their own object with the same methods."""

	def __init__(self):
		import frappe

		self.frappe = frappe

	def get(self, doctype, name):
		return self.frappe.get_cached_doc(doctype, name) if name else None

	def company_address(self, company):
		frappe = self.frappe
		name = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
			"parent",
		)
		return self.get("Address", name) if name else None

	def company_currency(self, company):
		return self.frappe.get_cached_value("Company", company, "default_currency")


class ConvergeXInvoiceMapper:
	def __init__(self, profile, config, source=None):
		self.profile = profile
		self.config = config
		self.contract = config.contract
		self.source = source or FrappeDataSource()
		self.errors = []
		self.warnings = []

	# ================================================================ public
	def build(self, doc, customer_reference=None):
		canonical = self.build_canonical(doc, customer_reference)
		return self.to_provider(canonical), canonical

	def build_canonical(self, doc, customer_reference=None):
		self.errors, self.warnings = [], []
		if doc.get("doctype") not in (None, "Sales Invoice"):
			raise ConvergeXValidationError("ConvergeX accepts Sales Invoices (and their credit notes) only.")
		is_credit = bool(doc.get("is_return"))
		if is_credit and not self.contract.get("credit_notes_supported"):
			raise ConvergeXValidationError("Credit notes are switched off in the ConvergeX API contract.")

		company = self.source.get("Company", doc.get("company"))
		customer = self.source.get("Customer", doc.get("customer"))
		lines, summary, line_total, discount_total, vat_total = self._lines(doc)

		header = {
			"document_type_uuid": self._type_uuid(is_credit),
			"erp_reference": doc.get("name"),
			"invoice_number": doc.get("name"),
			"invoice_type_code": self.profile.invoice_type_codes["credit_note" if is_credit else "invoice"],
			"customization_id": self.profile.process_billing.get("customization_id"),
			"profile_id": self.profile.process_billing.get("profile_id"),
			"issue_date": _s(doc.get("posting_date")),
			"issue_time": self._time(doc.get("posting_time")),
			"due_date": None if is_credit else _s(doc.get("due_date")),
			"currency": (doc.get("currency") or "").upper() or None,
			"purchase_order_reference": _s(doc.get("po_no")),
			"payment_terms": _s(doc.get("payment_terms_template")),
			"note": _s(doc.get("custom_invoice_note") or doc.get("remarks")),
		}
		self._require(header["issue_date"], "Posting Date is empty.")
		self._require(header["currency"], "Currency is empty.")
		if self.contract.get("invoice_type_uuid_required"):
			self._require(header["document_type_uuid"], "Invoice / Credit Note Type UUID is empty on Provider Settings.")

		company_currency = (self.source.company_currency(doc.get("company")) or self.profile.currency or "").upper()
		if header["currency"] and company_currency and header["currency"] != company_currency:
			header["tax_currency"] = company_currency
			header["exchange_rate"] = self.profile.qty(doc.get("conversion_rate") or 1)

		payload = {
			**header,
			"seller": self._seller(doc, company),
			"buyer": self._buyer(doc, customer, customer_reference),
			"lines": lines,
			"tax_summary": summary,
			"totals": self._totals(doc, line_total, discount_total, vat_total),
		}
		if header.get("tax_currency"):
			payload["totals"]["tax_amount_in_tax_currency"] = self.profile.money(
				abs(_d(doc.get("base_total_taxes_and_charges")))
			)
		if is_credit:
			payload["credit_note"] = self._credit_note(doc)

		if self.errors:
			raise ConvergeXValidationError(
				f"Invoice {doc.get('name')} cannot be sent to ConvergeX", errors=list(self.errors)
			)
		return _prune(payload)

	def to_provider(self, canonical):
		renamed = _rename(canonical, self.contract.get("payload_field_map") or {})
		root = self.contract.get("payload_root_key")
		return {root: renamed} if root else renamed

	def build_customer(self, customer, address=None):
		"""Payload for the customer-sync call (ConvergeX registers the buyer
		before the invoice)."""
		self.errors = []
		party = self._party(customer, address, is_company=False, label=f"Customer {customer.get('name')}")
		party["erp_reference"] = customer.get("name")
		if self.errors:
			raise ConvergeXValidationError(
				f"Customer {customer.get('name')} cannot be synchronised with ConvergeX", errors=list(self.errors)
			)
		return _rename(_prune(party), self.contract.get("customer_field_map") or {})

	# ================================================================ parts
	def _type_uuid(self, is_credit):
		return (self.config.credit_note_type_uuid if is_credit else self.config.invoice_type_uuid) or None

	@staticmethod
	def _time(value):
		if value in (None, ""):
			return None
		if hasattr(value, "total_seconds"):
			s = int(value.total_seconds())
			return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
		text = str(value).split(".")[0]
		parts = text.split(":")
		return ":".join(p.zfill(2) for p in [*parts, "00", "00"][:3])

	def _require(self, value, message):
		if value in (None, "", [], {}):
			self.errors.append(message)

	def _address(self, address, label):
		if not address:
			self.errors.append(f"{label} has no address.")
			return {}
		country = self.profile.country_code_of(address.get("country"))
		self._require(country, f"Address {address.get('name')} ({label}) has no recognised Country.")
		self._require(address.get("address_line1"), f"Address {address.get('name')} ({label}) has no Address Line 1.")
		self._require(address.get("city"), f"Address {address.get('name')} ({label}) has no City.")
		region = self.profile.region_code(address.get(self.profile.region_field)) if self.profile.region_field else None
		return {
			"street": _s(address.get("address_line1")),
			"additional_street": _s(address.get("address_line2")),
			"city": _s(address.get("city")),
			"postal_code": _s(address.get("pincode")),
			"subdivision": region or _s(address.get("state")),
			"country_code": country,
		}

	def _party(self, party_doc, address, is_company, label):
		if not party_doc:
			self.errors.append(f"{label} not found.")
			return {}
		name = party_doc.get("company_name" if is_company else "customer_name") or party_doc.get("name")
		vat = _s(party_doc.get("tax_id"))
		peppol = _s(party_doc.get("custom_peppol_id"))
		endpoint_id, scheme = self.profile.endpoint(peppol) if peppol else (
			self.profile.endpoint_id_from_tax_id(vat) if vat else None,
			self.profile.endpoint_scheme,
		)
		return {
			"legal_name": _s(name),
			"name_ar": _s(party_doc.get("company_name_in_arabic" if is_company else "customer_name_in_arabic")),
			"vat_number": vat,
			"legal_registration_number": _s(party_doc.get("custom_trade_license_number")),
			"electronic_address": endpoint_id or None,
			"electronic_address_scheme": scheme if endpoint_id else None,
			"email": _s(party_doc.get("email") if is_company else party_doc.get("email_id")),
			"address": self._address(address, label),
		}

	def _seller(self, doc, company):
		address = self.source.get("Address", doc.get("company_address")) or self.source.company_address(doc.get("company"))
		party = self._party(company, address, is_company=True, label=f"Company {doc.get('company')}")
		self._require(party.get("vat_number"), f"Company {doc.get('company')} has no Tax ID (seller VAT number).")
		return party

	def _buyer(self, doc, customer, customer_reference):
		address = self.source.get("Address", doc.get("customer_address"))
		party = self._party(customer, address, is_company=False, label=f"Customer {doc.get('customer')}")
		is_b2c = bool(customer and customer.get("custom_is_b2c_customer"))
		if not is_b2c:
			self._require(party.get("vat_number"), f"Customer {doc.get('customer')} has no Tax ID (buyer VAT number). "
				"Tick 'Is B2C Customer' on the Customer if it is a consumer.")
		party["is_b2c"] = is_b2c
		party["provider_customer_id"] = customer_reference
		if doc.get("customer_name"):
			party["legal_name"] = doc.get("customer_name")
		return party

	def _item_tax(self, doc, item):
		label = doc.get("custom_vat_category")
		exemption = doc.get("custom_vat_exemption_reason_code")
		taxes = doc.get("taxes") or []
		rate = taxes[0].get("rate") if taxes else 0
		template_name = item.get("item_tax_template")
		if template_name:
			template = self.source.get("Item Tax Template", template_name)
			if template:
				label = template.get("custom_vat_category") or label
				exemption = template.get("custom_vat_exemption_reason_code") or exemption
				if template.get("taxes"):
					rate = template.get("taxes")[0].get("tax_rate")
		code = None
		if not label:
			self.errors.append(f"Row {item.get('idx')} ({item.get('item_code')}): no VAT Category on the invoice or its Item Tax Template.")
		else:
			try:
				code = self.profile.tax_category_code(label)
			except Exception as e:
				self.errors.append(f"Row {item.get('idx')}: {e}")
		if code in ("E", "O") and not exemption:
			self.errors.append(f"Row {item.get('idx')} ({item.get('item_code')}): VAT category {code} needs a VAT Exemption Reason Code.")
		if code and code != "S":
			rate = 0  # zero-rated, exempt, out of scope, reverse charge, export: no VAT on this invoice
		return code, _d(rate), exemption

	def _lines(self, doc):
		items = doc.get("items") or []
		if not items:
			self.errors.append("The invoice has no items.")
		m = self.profile.money
		q = Decimal(1).scaleb(-int(self.profile.currency_decimals))
		lines, summary = [], {}
		line_total = discount_total = vat_total = Decimal(0)

		for idx, item in enumerate(items, 1):
			qty = abs(_d(item.get("qty")))
			if not qty:
				self.errors.append(f"Row {item.get('idx') or idx} ({item.get('item_code')}): quantity is zero.")
			code, rate, exemption = self._item_tax(doc, item)
			taxable = abs(_d(item.get("net_amount") if item.get("net_amount") not in (None, "") else item.get("amount")))
			list_rate = abs(_d(item.get("price_list_rate"))) or abs(_d(item.get("rate")))
			gross = (list_rate * qty).quantize(q, ROUND_HALF_UP)
			discount = max(Decimal(0), gross - taxable)
			unit_price = (gross / qty) if qty else Decimal(0)
			vat = (taxable * rate / 100).quantize(q, ROUND_HALF_UP)

			line_total += taxable
			discount_total += discount
			vat_total += vat
			key = (code, rate, exemption or "")
			bucket = summary.setdefault(key, {"taxable": Decimal(0), "vat": Decimal(0)})
			bucket["taxable"] += taxable
			bucket["vat"] += vat

			ex_code, ex_text = self.profile.split_code(exemption) if exemption else (None, None)
			lines.append({
				"line_id": str(item.get("idx") or idx),
				"item_code": _s(item.get("item_code")),
				"item_name": _s(item.get("item_name")),
				"description": _s(item.get("description")) or _s(item.get("item_name")),
				"quantity": self.profile.qty(qty),
				"unit_code": self.profile.uom_to_unece(item.get("uom")),
				"unit_price": m(unit_price),
				"discount_amount": m(discount),
				"taxable_amount": m(taxable),
				"vat_category": code,
				"vat_rate": self.profile.num(rate),
				"vat_amount": m(vat),
				"line_total": m(taxable + vat),
				"vat_exemption_reason_code": ex_code,
				"vat_exemption_reason": ex_text if ex_text != ex_code else None,
				"hs_code": _s(item.get("custom_hs_code_")),
			})

		tax_summary = []
		for (code, rate, exemption), b in summary.items():
			ex_code, ex_text = self.profile.split_code(exemption) if exemption else (None, None)
			tax_summary.append({
				"vat_category": code,
				"vat_rate": self.profile.num(rate),
				"taxable_amount": m(b["taxable"]),
				"vat_amount": m(b["vat"]),
				"vat_exemption_reason_code": ex_code,
				"vat_exemption_reason": ex_text if ex_text != ex_code else None,
			})

		erp_tax = abs(_d(doc.get("total_taxes_and_charges")))
		if items and abs(erp_tax - vat_total) > q * len(items):
			self.warnings.append(
				f"VAT computed per line ({m(vat_total)}) differs from the invoice's Total Taxes and Charges ({m(erp_tax)}); "
				"non-VAT charge rows are not sent."
			)
		return lines, tax_summary, line_total, discount_total, vat_total

	def _totals(self, doc, line_total, discount_total, vat_total):
		m = self.profile.money
		inclusive = line_total + vat_total
		rounding = _d(doc.get("rounding_adjustment"))
		if doc.get("is_return"):
			rounding = -rounding
		paid = abs(_d(doc.get("total_advance")))
		if doc.get("is_pos"):
			paid = abs(_d(doc.get("paid_amount"))) - abs(_d(doc.get("change_amount")))
		payable = inclusive + rounding
		outstanding = abs(_d(doc.get("outstanding_amount"))) if doc.get("outstanding_amount") is not None else payable - paid
		return {
			"line_extension_amount": m(line_total),
			"discount_total": m(discount_total),
			"tax_exclusive_amount": m(line_total),
			"tax_amount": m(vat_total),
			"tax_inclusive_amount": m(inclusive),
			"rounding_amount": m(rounding),
			"payable_amount": m(payable),
			"amount_paid": m(paid),
			"outstanding_amount": m(outstanding),
		}

	def _credit_note(self, doc):
		original_name = doc.get("return_against")
		original = self.source.get("Sales Invoice", original_name) if original_name else None
		external = _s(doc.get("custom_return_against_for_einvoice"))
		reason_code, reason = self.profile.split_code(doc.get("custom_credit_note_reason_code")) if doc.get(
			"custom_credit_note_reason_code"
		) else (None, None)
		if not (original_name or external):
			self.errors.append("Credit note has no original invoice (Return Against, or Return Against For E-Invoice).")
		self._require(reason_code, "Credit note has no Credit Note Reason Code.")
		return {
			"original_invoice_number": original_name or external,
			"original_invoice_date": _s(original.get("posting_date")) if original else None,
			"original_provider_invoice_id": _s(original.get("custom_document_id")) if original else None,
			"reason_code": reason_code,
			"reason": reason if reason != reason_code else None,
		}


def _prune(value):
	"""Drop None / empty values recursively (absent beats null for most APIs)."""
	if isinstance(value, dict):
		out = {k: _prune(v) for k, v in value.items()}
		return {k: v for k, v in out.items() if v not in (None, "", [], {})}
	if isinstance(value, list):
		return [_prune(v) for v in value]
	return value


def _rename(value, field_map):
	if not field_map:
		return value
	if isinstance(value, dict):
		return {field_map.get(k, k): _rename(v, field_map) for k, v in value.items()}
	if isinstance(value, list):
		return [_rename(v, field_map) for v in value]
	return value
