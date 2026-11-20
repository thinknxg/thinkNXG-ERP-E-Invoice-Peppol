"""Whitelisted ConvergeX actions behind the Sales Invoice and Customer buttons."""

import frappe
from frappe import _

from einvoice_erpgulf.core.provider_settings import get_active_provider_settings
from einvoice_erpgulf.core.registry import get_adapter

from .exceptions import ConvergeXError

PROVIDER = "ConvergeX"
REPORTS = ("compliance", "tdd_report", "ota_preview")


def _adapter_for_invoice(doctype, name, ptype="read"):
	if doctype != "Sales Invoice":
		frappe.throw(_("ConvergeX actions are available on Sales Invoices only."))
	doc = frappe.get_doc(doctype, name)
	doc.check_permission(ptype)
	settings = get_active_provider_settings(doc.company)
	if settings.provider != PROVIDER:
		frappe.throw(_("{0}'s e-invoice provider is {1}, not ConvergeX.").format(doc.company, settings.provider))
	return doc, get_adapter(settings)


def _error(e):
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- keep the API log rows
	return {"status": "error", "message": str(e)}


@frappe.whitelist()
def is_convergex_company(company: str):
	return frappe.db.get_value("Company", company, "custom_einvoice_provider") == PROVIDER


@frappe.whitelist(methods=["POST"])
def check_job_status(doctype: str, name: str):
	doc, adapter = _adapter_for_invoice(doctype, name, "write")
	try:
		info = adapter.poll_job(doc)
	except ConvergeXError as e:
		return _error(e)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"status": "success", "state": info.get("state"), "job_status": info.get("job_status"),
		"invoice_id": info.get("invoice_id"), "response": info.get("response")}


@frappe.whitelist(methods=["POST"])
def fetch_report(doctype: str, name: str, report: str = "compliance"):
	if report not in REPORTS:
		frappe.throw(_("Report must be one of {0}.").format(", ".join(REPORTS)))
	doc, adapter = _adapter_for_invoice(doctype, name, "write")
	try:
		info = adapter.fetch_report(doc, report)
	except ConvergeXError as e:
		return _error(e)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"status": "success", "report": report, **info}


@frappe.whitelist(methods=["POST"])
def retry_submission(doctype: str, name: str):
	"""Resend an invoice whose ConvergeX submission failed or was rejected,
	or that was created on ConvergeX but never submitted. The duplicate
	guard in the adapter still applies."""
	doc, _adapter = _adapter_for_invoice(doctype, name, "submit")
	if doc.docstatus != 1:
		frappe.throw(_("Submit the invoice first."))
	from einvoice_erpgulf.core.submit_sales import generate_and_send_einvoice

	generate_and_send_einvoice(doc)
	return frappe.db.get_value(
		doctype, name,
		["custom_einvoice_status", "custom_reporting_status", "custom_provider_status", "custom_document_id",
			"custom_einvoice_job_id", "custom_provider_error"],
		as_dict=True,
	)


@frappe.whitelist(methods=["POST"])
def sync_customer(customer: str, company: str):
	frappe.get_doc("Customer", customer).check_permission("write")
	settings = get_active_provider_settings(company)
	if settings.provider != PROVIDER:
		frappe.throw(_("{0}'s e-invoice provider is not ConvergeX.").format(company))
	result = get_adapter(settings).sync_customer(customer)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return result
