"""Asynchronous ConvergeX job tracking.

* Right after an async create (HTTP 202 + job id) the adapter enqueues
  ``poll_invoice_job`` once.
* ``poll_pending_jobs`` runs every 5 minutes (hooks.py) and polls every
  Sales Invoice that still has a job id but no ConvergeX invoice id, within
  the same look-back window as the shared status sync.

Polling never marks an invoice failed because it is slow: only a job that
ConvergeX reports as failed does that. Invoices with a ConvergeX id are
followed by the shared core.status_sync (get_document_status).
"""

import frappe

from einvoice_erpgulf.core.provider_settings import get_active_provider_settings
from einvoice_erpgulf.core.registry import get_adapter

PROVIDER = "ConvergeX"
LOOKBACK_DAYS = 7
BATCH_SIZE = 50


def poll_invoice_job(doctype, name):
	doc = frappe.get_doc(doctype, name)
	settings = get_active_provider_settings(doc.company)
	if settings.provider != PROVIDER:
		return None
	try:
		info = get_adapter(settings).poll_job(doc)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return info
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"ConvergeX Job Poll Error - {name}")
		return None


def poll_pending_jobs():
	meta = frappe.get_meta("Sales Invoice")
	if not meta.has_field("custom_einvoice_job_id"):
		return  # Oman fields not installed on this site
	companies = frappe.get_all("Company", filters={"custom_einvoice_provider": PROVIDER}, pluck="name")
	if not companies:
		return
	since = frappe.utils.add_days(frappe.utils.nowdate(), -LOOKBACK_DAYS)
	rows = frappe.get_all(
		"Sales Invoice",
		filters={
			"docstatus": 1,
			"company": ["in", companies],
			"custom_einvoice_job_id": ["is", "set"],
			"custom_document_id": ["is", "not set"],
			"posting_date": [">=", since],
		},
		or_filters=[
			["Sales Invoice", "custom_provider_status", "is", "not set"],
			["Sales Invoice", "custom_provider_status", "not in", ["failed", "rejected"]],
		],
		pluck="name",
		order_by="modified asc",
		limit=BATCH_SIZE,
	)
	for name in rows:
		poll_invoice_job("Sales Invoice", name)
