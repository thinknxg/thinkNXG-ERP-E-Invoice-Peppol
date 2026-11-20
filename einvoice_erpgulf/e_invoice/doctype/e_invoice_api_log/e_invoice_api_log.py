# Copyright (c) 2026, thinknxg.com and contributors
# For license information, please see license.txt

"""One row per HTTP call to an e-invoice provider: endpoint, status,
correlation id, retry count and sanitised request / response bodies.
Written by provider adapters (ConvergeX today); never holds credentials
or tokens."""

from frappe.model.document import Document


class EInvoiceAPILog(Document):
	pass
