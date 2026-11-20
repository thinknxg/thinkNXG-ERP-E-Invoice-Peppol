// ConvergeX (Oman) actions on a submitted Sales Invoice. Shown only when the
// company's e-invoice provider is ConvergeX; other providers are untouched.
frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (frm.is_new() || frm.doc.docstatus !== 1 || !frm.doc.company) return;

        frappe.call({
            method: "einvoice_erpgulf.countries.om.providers.convergex.api.is_convergex_company",
            args: { company: frm.doc.company },
            callback(r) {
                if (r.message) convergex_buttons(frm);
            },
        });
    },
});

function convergex_show(title, data) {
    const pretty = JSON.stringify(data, null, 2);
    frappe.msgprint({
        title: __(title),
        message: `<pre style="white-space:pre-wrap;background:#f6f8fa;padding:12px;border-radius:6px;
            max-height:420px;overflow:auto;font-size:12px;">${frappe.utils.escape_html(pretty)}</pre>`,
        wide: true,
    });
}

function convergex_call(frm, method, args, title) {
    frappe.call({
        method: `einvoice_erpgulf.countries.om.providers.convergex.api.${method}`,
        args: Object.assign({ doctype: frm.doctype, name: frm.doc.name }, args || {}),
        freeze: true,
        freeze_message: __("Contacting ConvergeX..."),
        callback(r) {
            if (r.message) convergex_show(title, r.message);
            frm.reload_doc();
        },
    });
}

function convergex_buttons(frm) {
    const group = __("ConvergeX");
    const provider_status = (frm.doc.custom_provider_status || "").toLowerCase();
    const reporting = (frm.doc.custom_reporting_status || "").toLowerCase();

    if (frm.doc.custom_provider_error) {
        frm.dashboard.set_headline_alert(
            `<div class="text-danger"><b>${__("ConvergeX")}:</b> ${frappe.utils.escape_html(frm.doc.custom_provider_error).replace(/\n/g, "<br>")}</div>`
        );
    }

    if (frm.doc.custom_einvoice_job_id && !frm.doc.custom_document_id) {
        frm.add_custom_button(__("Check Job Status"), () =>
            convergex_call(frm, "check_job_status", {}, "ConvergeX Job Status"), group);
    }

    if (frm.doc.custom_document_id) {
        frm.add_custom_button(__("Compliance Result"), () =>
            convergex_call(frm, "fetch_report", { report: "compliance" }, "ConvergeX Compliance"), group);
        frm.add_custom_button(__("Technical (TDD) Report"), () =>
            convergex_call(frm, "fetch_report", { report: "tdd_report" }, "ConvergeX TDD Report"), group);
        frm.add_custom_button(__("OTA Preview"), () =>
            convergex_call(frm, "fetch_report", { report: "ota_preview" }, "ConvergeX OTA Preview"), group);
    }

    const retryable =
        frm.doc.custom_einvoice_status === "Not Submitted" ||
        ["failed", "rejected", "created"].includes(provider_status) ||
        ["failed", "rejected"].includes(reporting);
    if (retryable) {
        frm.add_custom_button(__("Retry Submission"), () => {
            frappe.confirm(__("Send {0} to ConvergeX again?", [frm.doc.name]), () =>
                convergex_call(frm, "retry_submission", {}, "ConvergeX Retry"));
        }, group);
    }

    frm.add_custom_button(__("API Log"), () =>
        frappe.set_route("List", "E-Invoice API Log", { reference_name: frm.doc.name }), group);
}
