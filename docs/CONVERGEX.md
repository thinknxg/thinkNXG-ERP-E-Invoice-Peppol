# ConvergeX (Oman) — Integration Guide

**Intended for:** developers and implementers connecting a thinkNXG ERP company in **Oman** to **ConvergeX**, an e-invoicing service provider for the Oman Tax Authority's Fawtara programme. ConvergeX is an independent provider registered under Oman (`"OM"` in `einvoice_providers`). It replaces nothing and changes nothing for the UAE or Belgian providers.

> **Status in one line:** the integration is built and its logic is covered by 57 automated tests against mocked HTTP. ConvergeX has not published an API schema, so the routes, JSON property names and status words are kept in an overridable **API contract**. They must be confirmed with ConvergeX and then tested on the ConvergeX sandbox before going live. Section 14 lists exactly what to ask for.

---

## 1. Architecture

```
Sales Invoice ──on_submit──▶ core/submit_sales.py ──▶ core/registry.get_adapter() ──▶ ConvergeXAdapter
                                                                                        │
countries/om/providers/convergex/                                                       │
├── adapter.py      ConvergeXAdapter (the provider: BaseAdapter contract) ◀─────────────┘
├── config.py       ConvergeXConfig       settings row → validated config (env, URL, credentials, contract)
├── contract.py     API contract          every route / key / status word, overridable per settings row
├── auth.py         ConvergeXAuthentication  token, refresh, expiry; tokens only in Redis
├── client.py       ConvergeXApiClient    HTTP, retries, error classification, API log
├── mapper.py       ConvergeXInvoiceMapper   Sales Invoice → canonical JSON → ConvergeX JSON
├── services.py     invoice (create/submit), job polling, compliance reports, customer sync
├── status.py       ConvergeXStatusService   provider status → ERP status
├── response.py     ConvergeXResponseMapper + sanitiser (credentials/tokens never stored)
├── exceptions.py   ConvergeXError hierarchy (one class per failure kind)
├── persistence.py  Redis token cache, E-Invoice API Log writer, submission lock
├── jobs.py         async job polling (enqueued after a 202 + every 5 minutes)
├── api.py          whitelisted actions behind the Sales Invoice buttons
└── tests/          unit tests (mocked HTTP; no network)
```

Responsibilities are split so the parts that talk HTTP (`auth`, `client`, `services`) never touch the database. The `adapter` decides what to store. The `mapper` reads data only through a small data source. That split is what lets the tests run without a site.

Country rules stay in the Oman profile (`countries/om/profile.py`: PINT-OM customization ID, 3-decimal OMR, governorate codes, tax categories). The adapter only holds ConvergeX's API.

## 2. Installation and configuration

1. Update the app and migrate (this installs the Oman fields, the **E-Invoice API Log** doctype, and adds *ConvergeX* to the provider dropdowns):
   ```bash
   cd ~/frappe-bench && bench update --apps einvoice_erpgulf   # or git pull in apps/einvoice_erpgulf
   bench --site <site> migrate
   bench --site <site> clear-cache && bench restart             # hooks are cached
   ```
   The Oman fields are installed only on sites that have a Company with *Country = Oman*. Create that Company first, or run `migrate` again afterwards.
2. **Company** (Country = Oman) → *E-Invoicing* tab: tick **E-Invoice Enabled** and choose **ConvergeX** as the provider.
3. **E-Invoice Provider Settings** → New. Create one row per environment:

| Field | Value |
| --- | --- |
| Company | the Oman company |
| Provider | ConvergeX |
| Environment | Sandbox, Pre-Production or Production (one row each; only one enabled per company) |
| Auth Type | `oauth2` |
| Base URL | the URL ConvergeX issued for that environment, `https://` only. **No default is shipped.** |
| Client ID / Client Secret | from ConvergeX. The secret is a Password field, stored encrypted |
| Company / Tenant Identifier, API Version | only if ConvergeX issues them (see §4) |
| Invoice Type UUID / Credit Note Type UUID | from ConvergeX for your account |
| Asynchronous Processing | tick to create invoices with `mode=async` (§7) |
| Register Customers Before Invoicing | leave ticked (ConvergeX requires it) |
| API Contract Overrides | JSON, see §3 |
| Enabled | tick when the row is complete. Saving an enabled row with a missing URL or credential is refused |

4. Click **E-Invoicing → Verify Token**. It requests a token and shows the environment, base URL and token lifetime, and sets *Last Test Status* (the connection status). The token itself is never shown. *Get Access Token* is hidden for ConvergeX.
5. **Get Participant Details** shows what the row is configured to use, plus any problem that would block submission.

### Environment variables

None. As with every provider in this app, configuration lives on the Provider Settings row (one row per environment). Secrets sit in encrypted Password fields and tokens in Redis. Nothing goes in `site_config.json` or source control.

## 3. The API contract (`contract.py`)

All provider-specific routes, request keys, response keys and status words are in `DEFAULT_CONTRACT`. The **API Contract Overrides** JSON on the settings row is deep-merged over it, so a renamed route or field is a configuration change, not a code change. Example:

```json
{
  "verified": true,
  "routes": { "invoice_status": "/api/invoices/{id}/" },
  "auth": { "access_token_keys": ["access"], "request_format": "json" },
  "headers": { "tenant_header": "X-Tenant-ID" },
  "payload_field_map": { "invoice_number": "invoice_no", "lines": "items" },
  "payload_root_key": null
}
```

`"verified": true` is the go-live switch. **A Production row refuses to submit until it is set.** Sandbox rows submit without it. Set it only after ConvergeX has confirmed every route and key in use.

Each default carries one of these tags:
- *public*: from published material on ConvergeX integrations.
- *candidate*: a route from the ConvergeX integration material supplied with the brief.
- *UNVERIFIED*: a placeholder.

## 4. Authentication flow

1. `POST {base}/api/auth/token/` *(candidate)* with `{"client_id": …, "client_secret": …}` *(keys UNVERIFIED; `auth.request_format` can switch to form-encoded)*.
2. The access token is read from the first of `access_token`, `access` or `token` found in the response. The refresh token comes from `refresh_token` or `refresh`, and the lifetime from `expires_in`. If there is no `expires_in`, the JWT `exp` claim is used, then a 300 s default.
3. Tokens are cached in Redis per settings row (sandbox and production never share one) until 30 s before expiry. When the access token runs out, `POST /api/auth/token/refresh/` *(candidate)* with `{"refresh": …}` is tried first. If the refresh is rejected, a new token is requested.
4. Every API call sends `Authorization: Bearer <token>`. A 401 drops the cached token and retries once with a fresh one.
5. The tenant and API-version headers are sent only if ConvergeX names them (`headers.tenant_header`, `headers.api_version_header`).
6. Credentials and tokens are never logged. The API log records `client_secret: ***redacted***`.

## 5. API endpoints used

| Purpose | Method and route (default) | Contract key | Status |
| --- | --- | --- | --- |
| Token | `POST /api/auth/token/` | `auth.token_path` | candidate |
| Refresh | `POST /api/auth/token/refresh/` | `auth.refresh_path` | candidate |
| Create invoice | `POST /api/invoices/create/` | `routes.invoice_create` | candidate |
| Create (async) | `POST /api/invoices/create/?mode=async` → 202 + job id | `async_query` | candidate |
| Submit invoice | `POST /api/invoices/{id}/submit/` | `routes.invoice_submit` | candidate |
| Job status | `GET /api/integration/jobs/{job_id}/` | `routes.job_status` | candidate |
| Compliance result | `GET /api/invoices/{id}/compliance/` | `routes.compliance` | candidate |
| Technical (TDD) report | `GET /api/invoices/{id}/tdd-report/` | `routes.tdd_report` | candidate |
| OTA preview | `GET /api/invoices/{id}/ota-preview/` | `routes.ota_preview` | candidate |
| Customer sync | `POST /api/customers/erp/sync/` | `routes.customer_sync` | candidate |
| Invoice status | none documented; the compliance route is used | `routes.invoice_status` | **missing** |

There are no webhook, XML-download or PDF-download routes. Those buttons report "not supported" for ConvergeX, and status comes from polling. Purchase invoices (self-billing) and incoming invoices are not implemented, because nothing says ConvergeX offers them.

## 6. Invoice field mapping

The mapper first builds a **canonical** payload with descriptive keys (left column), then renames keys through `payload_field_map`. Until ConvergeX supplies its schema, the canonical keys are what is sent. **They are not ConvergeX's confirmed property names.** No sample ConvergeX payload is given here for that reason. The exact JSON sent for each invoice is attached to it as `<invoice>_convergex_payload.json`.

| Canonical key | Source in thinkNXG ERP |
| --- | --- |
| `document_type_uuid` | Provider Settings → Invoice / Credit Note Type UUID |
| `erp_reference`, `invoice_number` | Sales Invoice name |
| `invoice_type_code` | 380 invoice / 381 credit note (Oman profile) |
| `customization_id`, `profile_id` | `urn:peppol:pint:billing-1@om-1`, `urn:peppol:bis:billing` (Oman profile, suffix still to be confirmed by OTA) |
| `issue_date`, `issue_time`, `due_date` | posting date / time, due date (not on credit notes) |
| `currency`, `tax_currency`, `exchange_rate` | invoice currency; company currency and rate when they differ |
| `purchase_order_reference`, `payment_terms`, `note` | PO No, Payment Terms Template, Invoice Note / Remarks |
| `seller.*` | Company: name, Arabic name, Tax ID (**required**), trade licence, Peppol ID or endpoint derived from the Tax ID, email; address from Company Address (line 1, city, country **required**; governorate → ISO 3166-2:OM) |
| `buyer.*` | Customer: same fields; Tax ID **required unless "Is B2C Customer"** is ticked; `provider_customer_id` from the customer sync |
| `lines[]` | per item: code, name, description, qty, UN/ECE unit, unit price (price-list rate), discount (gross − net), taxable amount (net amount), VAT category / rate / amount, line total, exemption reason, HS code |
| `tax_summary[]` | per VAT category + rate + exemption reason: taxable and VAT amounts |
| `totals.*` | line extension, discount, tax exclusive, tax, tax inclusive, rounding, payable, amount paid, outstanding |
| `credit_note.*` | original invoice (Return Against, or Return Against For E-Invoice) with its date and ConvergeX id; Credit Note Reason Code (**required**) |

Amounts are in OMR with 3 decimals (baisa), half-up. VAT categories come from the Item Tax Template or the invoice's VAT Category. E and O need an exemption reason, and every category other than S goes out with a 0 rate. Missing mandatory data is collected and shown together, and nothing is sent. Nothing is silently dropped. If the per-line VAT differs from the invoice's tax total (because of non-VAT charge rows), a warning is written to the API log.

ConvergeX takes JSON and produces the PINT-OM UBL itself, so the ERP generates no XML for ConvergeX. Generic Peppol BIS 3.0 is not used for Oman.

## 7. Submission workflow

1. The Sales Invoice is submitted (or **Send Invoice** / **Retry Submission** is clicked).
2. The shared validation checks the company setup and the Oman profile rules.
3. The adapter takes a per-invoice Redis lock, so a double click or a scheduler overlap cannot send twice.
4. **Duplicate guard:**
   - already with ConvergeX (has an id and a non-failed status): refused;
   - queued as a job: refused, with "use Check Job Status";
   - created but not submitted: only the submit step is repeated;
   - failed or rejected: a new attempt is allowed.
5. **Customer sync**, if the customer has not been registered for this settings row. The ConvergeX customer id is stored on the Customer, per settings row.
6. Mapping and validation (§6). The payload is attached to the invoice.
7. **Create.** The ConvergeX id is saved immediately (`custom_provider_status = created`), so a failed submit is resumed rather than duplicated.
8. **Submit.** The provider status from the response is stored.
9. **Async mode:** the create returns 202 + job id, the invoice records the job id with status *pending*, and a background job polls it. When the job completes, the invoice id is stored and submitted (unless `async_create_includes_submit` is true). A failed job sets *failed* and shows the reason.
10. The invoice is marked **Success** (handed to ConvergeX) only after the submit call is accepted. The **Reporting Status** follows ConvergeX's status (§8). An HTTP 200/202 alone never sets *reported*.

## 8. Status mapping

| ConvergeX status | Reporting Status (ERP) | Final? |
| --- | --- | --- |
| `created` *(local: created, not submitted)* | pending | no |
| `submit_accepted` *(local: submit returned 2xx with no status)* | pending | no |
| `received` | pending | no |
| `submitted` | pending | no |
| `ota_accepted` | accepted | **no**: polling continues until ConvergeX confirms it is the last stage |
| `approved` | reported | yes |
| `rejected` | rejected | yes |
| `failed` | failed | yes |
| anything else | passed through as is | no |

The raw ConvergeX status is always kept in **Provider Status** (`custom_provider_status`). Every call (status, timestamp, endpoint, correlation id) is a row in **E-Invoice API Log**, which is the complete status history. Change the mapping with `status_map` and `final_provider_statuses` in the overrides. Invoices with an invoice id are re-checked by the shared 5-minute status sync. Once final, no further API calls are made.

## 9. Error handling

| Situation | Class | Behaviour |
| --- | --- | --- |
| Missing settings, bad URL, unverified production contract | `ConvergeXConfigError` | nothing sent; message lists every problem |
| Route not in the contract | `ConvergeXContractError` | nothing sent; message names the missing key |
| Invalid invoice data (local) / HTTP 400, 422 | `ConvergeXValidationError` | all field errors shown on the invoice (*Provider Errors*) |
| HTTP 401 / bad credentials / expired token | `ConvergeXAuthError` | token dropped, one retry with a new token, then error |
| HTTP 403 / 404 | `ConvergeXPermissionError` / `ConvergeXNotFoundError` | error, not retried |
| HTTP 409 / local duplicate guard | `ConvergeXDuplicateError` | refused; invoice status unchanged |
| HTTP 429 | `ConvergeXRateLimitError` | retried after `Retry-After` (capped) |
| HTTP 5xx | `ConvergeXServerError` | retried with backoff |
| Timeout / connection error | `ConvergeXNetworkError` | retried with backoff |
| Async job failed | job state *failed* | invoice *failed*, reason stored |

Each HTTP attempt writes an **E-Invoice API Log** row. It stores provider, company, settings row, ERP invoice, ConvergeX id, job id, correlation id, endpoint, method, HTTP status, sanitised request and response, provider status, error code and message, retry count and duration. Keys containing *secret, token, password, authorization, api_key, refresh* or *access* are redacted before storage. Request headers are never logged.

## 10. Retry strategy

- **Automatic:** up to `retry.max_attempts` (default 3) for 429, 5xx, timeouts and connection errors. Backoff is `backoff_seconds × 2^n` (2 s, 4 s…), capped at `max_backoff_seconds` (30 s); `Retry-After` wins when present. A 401 gets one re-authentication. 400/403/404/409 are never retried.
- **Manual:** **ConvergeX → Retry Submission** on the invoice. It appears when the status is *Not Submitted*, *failed*, *rejected* or *created*. The duplicate guard still applies.
- **Polling:** async jobs are polled once immediately and then every 5 minutes for 7 days. A slow job is never marked failed; only a job ConvergeX reports as failed is.

## 11. Sandbox testing

1. Create a Sandbox settings row with the sandbox URL and credentials, then click **Verify Token**.
2. Fill in **API Contract Overrides** with whatever ConvergeX's documentation says differs from §5 and §6.
3. Submit a simple standard-rated invoice to a B2B customer. Check the attached payload, the **E-Invoice API Log** rows, *Provider Status* and *Reporting Status*. Then use **Compliance Result**, **Technical (TDD) Report** and **OTA Preview**.
4. Repeat for a credit note, an exempt line, a B2C customer, a foreign-currency invoice and async mode. Then run the [Testing Guide](TESTING_GUIDE.md) section 9 rows that apply.
5. Unit tests (no network, no site needed):
   ```bash
   cd apps/einvoice_erpgulf
   python -m unittest discover -s einvoice_erpgulf/countries/om/providers/convergex/tests -t .
   # or inside a bench:
   bench --site <site> run-tests --module einvoice_erpgulf.countries.om.providers.convergex.tests.test_convergex_client
   ```

## 12. Production deployment

1. Create a Production row with the production URL and credentials, then Verify Token.
2. Copy the confirmed overrides from the sandbox row and add `"verified": true`.
3. Enable the row. Only one row per company and provider can be enabled, so enabling Production disables Sandbox.
4. Watch the first invoices in **E-Invoice API Log** and the E-Invoice dashboard.

## 13. Troubleshooting

| Symptom | Check |
| --- | --- |
| "ConvergeX settings are incomplete" | the message lists the missing items; Production also needs `"verified": true` |
| "No access token found in the … response" | the token response uses another key: set `auth.access_token_keys` |
| "accepted the create call but returned no invoice id" | set `response.invoice_id_keys` to ConvergeX's id field |
| "answered 202 Accepted without a job id" | set `response.job_id_keys` |
| Invoice stays *pending* | **Check Job Status** (async) or *Get Document Status*; look at the newest API Log row |
| "already with ConvergeX as …" | working as intended (duplicate guard); use the compliance buttons |
| Fields missing on the invoice | the site has no Oman company yet, or `migrate` has not run since |
| Every call 404s | Base URL includes or omits a prefix the routes assume; fix the URL or the routes |

## 14. Information still required from ConvergeX

1. Base URLs for sandbox and production.
2. The token request: path, JSON or form, field names, and the response shape (access/refresh keys, lifetime).
3. Refresh token behaviour and lifetime.
4. Whether a tenant / company identifier or API-version header is required, and its name.
5. The full invoice create schema: property names, mandatory fields, nesting, and how the Invoice / Credit Note Type UUIDs and the customer id are referenced.
6. The submit response: does it return a status? Which?
7. The full status vocabulary and which statuses are final. In particular, is `ota_accepted` final or followed by `approved`?
8. The async create: response shape, job id field, and whether the completed job also submits the invoice.
9. The job status response: status words, where the invoice id appears.
10. The compliance / TDD / OTA-preview response shapes, and where validation errors appear.
11. The customer sync: payload, upsert key, response (customer id field).
12. Whether there is an invoice status route, XML/PDF download, webhooks, purchase (self-billed) invoices or incoming invoices.
13. Error response format and error codes; idempotency and correlation headers; rate limits.
14. Whether credit notes need anything beyond the original invoice reference and reason code.

Also confirm the Oman profile constants with the OTA PINT-OM specification (customization ID suffix, endpoint scheme, VATIN length, governorate requirement): see [Country Guide](COUNTRIES.md).

## Sources

- Cybrosys, *How Odoo integrates with Fawtara for Oman's e-invoicing system*: the public description of a ConvergeX connector (Client ID/Secret, Invoice and Credit Note Type UUIDs, customer registration before invoicing, credit notes, no voiding).
- OpenPeppol, *PINT Oman (PINT-OM) BIS*: https://docs.peppol.eu/poac/om/pint-om/bis/
- The candidate API routes in §5 come from the ConvergeX integration material supplied with the integration brief.
