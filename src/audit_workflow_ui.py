"""
LogiDoc-RAG Audit Operations tab (Streamlit)

Presentation only. Every number, status and finding comes from the
business modules:

    document_completeness   are the BOL, invoice and POD there and usable
    financial_exposure      invoice money figures
    audit_explanations      what each finding means, with its evidence
    audit_history           recorded audits and what changed between them
    audit_analytics         the audit snapshot all of them read

The tab follows the audit workflow: documents, financial exposure, then
one shipment at a time: readiness, findings and evidence, review,
recording the audit, comparing with earlier audits, exporting.

Widget keys all start with "op_" so they never collide with the Audit
Analytics tab, which is rendered at the same time.
"""

import pandas as pd
import streamlit as st

import audit_explanations as explanations
import authz
import document_completeness as completeness
import financial_exposure as exposure
from audit_analytics import build_exceptions, with_reviews
from audit_history import record_run_as
from audit_export import NOT_AVAILABLE, ExportError, audit_csv, export_filename
from chunker import describe_problems
from audit_pdf_export import audit_pdf, pdf_filename
from display_text import escape_markdown_dollars, money_text


MONEY_FIELDS = {"invoice_total", "line_item_sum", "invoice_difference"}

STATE_LABELS = {
    "verified": "Verified",
    "unavailable": "Unavailable (not found)",
    "conflicting": "Conflicting values",
    "review_required": "Review required",
    "not_assessed": "Not assessed",
}


# ============================================================
# SMALL HELPERS
# ============================================================

def amount(value):

    return NOT_AVAILABLE if value is None else money_text(value)


def when(timestamp):
    """
    A stored UTC timestamp as readable text.
    """

    return str(timestamp).replace("T", " ").replace("+00:00", " UTC")


def shown(value):

    return NOT_AVAILABLE if value is None else str(value)


def value_text(field, value):

    if value is None:
        return NOT_AVAILABLE

    return money_text(value) if field in MONEY_FIELDS else str(value)


def reset_filters(prefix):

    for key in [key for key in st.session_state if key.startswith(prefix)]:
        del st.session_state[key]


# ============================================================
# DOCUMENT COMPLETENESS
# ============================================================

def show_completeness(files, indexed_sources):

    st.subheader("1. Document completeness")

    st.caption(
        "A freight reconciliation needs a BOL, an invoice and a POD, all "
        "readable, for the same shipment. Files that could not be read are "
        "never attributed to a shipment."
    )

    if not files:
        st.info("No PDFs in the document folder yet. Upload documents in the sidebar.")
        return None

    result = completeness.register(files, indexed_sources=indexed_sources)
    register = result["shipments"]

    ready = sum(row["readiness"] == completeness.READY for row in register)

    columns = st.columns(4)
    columns[0].metric("Shipments", len(register))
    columns[1].metric("Ready for reconciliation", ready)
    columns[2].metric("Incomplete", len(register) - ready)
    columns[3].metric("Files that could not be used", len(result["unusable"]) + len(result["unidentified"]))

    filters = st.columns(5)

    search = filters[0].text_input("Shipment ID", key="op_dc_search")
    incomplete = filters[1].checkbox("Incomplete only", key="op_dc_incomplete")
    missing = filters[2].multiselect("Missing", list(completeness.REQUIRED_TYPES), key="op_dc_missing")
    extraction = filters[3].checkbox("Extraction issues", key="op_dc_extraction")
    filters[4].button("Reset filters", on_click=reset_filters, args=("op_dc_",), key="op_dc_reset")

    rows = completeness.filter_shipments(
        register, search=search, incomplete_only=incomplete, missing_types=missing,
        extraction_issues=extraction,
    )

    st.caption(f"{len(rows)} of {len(register)} shipment(s).")

    if rows:

        st.dataframe(
            pd.DataFrame([
                {
                    "Shipment": row["shipment_id"],
                    "Readiness": "Ready" if row["readiness"] == completeness.READY else "Incomplete",
                    "BOL": "present" if row["by_type"]["BOL"] else "MISSING",
                    "Invoice": "present" if row["by_type"]["INVOICE"] else "MISSING",
                    "POD": "present" if row["by_type"]["POD"] else "MISSING",
                    "Documents": row["documents"],
                    "Indexed": row["indexed_documents"],
                    "Warnings": len(row["warnings"]),
                    "What is needed": row["next_step"],
                }
                for row in rows
            ]),
            hide_index=True,
            width="stretch",
        )

        st.download_button(
            "Download completeness register (CSV)",
            data=completeness.register_csv(rows),
            file_name="document_completeness.csv",
            mime="text/csv",
            on_click="ignore",
            key="op_dc_csv",
        )

    elif register:
        st.info("No shipments match these filters.")

    for row in rows:
        for warning in row["warnings"]:
            st.caption(f"{row['shipment_id']}: {warning}")

    if result["unusable"]:

        with st.expander(f"Files that could not be used ({len(result['unusable'])})"):

            st.caption(
                "These PDFs exist but no usable text could be extracted. "
                "They have no shipment ID, so they are not part of any shipment."
            )

            st.dataframe(
                pd.DataFrame([
                    {"File": item["filename"], "Status": item["status"], "What happened": item["message"],
                     "Pages": item["pages"], "Pages with text": item["pages_with_text"],
                     "Error": item["error"] or ""}
                    for item in result["unusable"]
                ]),
                hide_index=True,
                width="stretch",
            )

    if result["unidentified"]:

        with st.expander(f"Documents without a shipment ID ({len(result['unidentified'])})"):
            st.dataframe(
                pd.DataFrame([
                    {"File": item["filename"], "Type": item["document_type"], "Note": item["message"]}
                    for item in result["unidentified"]
                ]),
                hide_index=True,
                width="stretch",
            )

    if result["duplicates"]:
        st.warning(
            "Files with identical content: "
            + "; ".join(", ".join(group) for group in result["duplicates"])
        )

    return register


# ============================================================
# FINANCIAL EXPOSURE
# ============================================================

def show_financial_exposure(snapshot):

    st.subheader("2. Financial exposure")

    st.caption(exposure.NOTE + " Amounts are shown as stated in the documents (no currency conversion).")

    rows = exposure.exposure_rows(snapshot)

    if not rows:
        st.info("No shipments to summarise yet.")
        return

    filters = st.columns(5)

    search = filters[0].text_input("Shipment ID", key="op_fx_search")
    carriers = filters[1].multiselect(
        "Carrier", sorted({row["carrier"] or NOT_AVAILABLE for row in rows}), key="op_fx_carriers",
    )
    classifications = filters[2].multiselect(
        "Invoice result", list(exposure.CLASS_LABELS),
        format_func=lambda value: exposure.CLASS_LABELS[value], key="op_fx_classes",
    )
    categories = filters[3].multiselect(
        "Discrepancy category",
        sorted({category for row in rows for category in row["discrepancy_categories"]}),
        key="op_fx_categories",
    )
    filters[4].button("Reset filters", on_click=reset_filters, args=("op_fx_",), key="op_fx_reset")

    filtered = exposure.filter_rows(rows, search, carriers, classifications, categories)

    figures = exposure.summary(filtered)

    if figures["mixed_currency"]:
        st.warning(
            "These invoices are in more than one currency (" + ", ".join(figures["currencies"])
            + "): money figures are not added together."
        )

    first = st.columns(4)
    first[0].metric("Invoices", figures["invoices"])
    first[1].metric(
        "Stated invoice total", amount(figures["stated_total"]),
        help=f"{exposure.METRICS['stated_total']} Covers {figures['totals_read']} of {figures['invoices']} invoice(s).",
    )
    first[2].metric("Confirmed mismatches", figures["mismatches"], help=exposure.METRICS["mismatches"])
    first[3].metric("Unverifiable invoices", figures["unverifiable"], help=exposure.METRICS["unverifiable"])

    second = st.columns(4)
    second[0].metric(
        "Net signed difference", amount(figures["net_difference"]), help=exposure.METRICS["net_difference"],
    )
    second[1].metric(
        "Absolute variance", amount(figures["absolute_variance"]), help=exposure.METRICS["absolute_variance"],
    )
    second[2].metric(
        "Total above line items", amount(figures["total_above_line_items"]),
        help="Sum of the mismatches where the stated total is above its line items.",
    )
    second[3].metric(
        "Total below line items", amount(figures["total_below_line_items"]),
        help="Sum of the mismatches where the stated total is below its line items.",
    )

    third = st.columns(2)
    third[0].metric(
        "Amount under review (not a loss)", amount(figures["amount_under_review"]),
        help=(
            f"{exposure.METRICS['amount_under_review']} "
            f"{figures['unverifiable_without_total']} unverifiable invoice(s) have no readable total."
        ),
    )
    third[1].metric("Verified invoices", figures["verified"])

    if not filtered:
        st.info("No invoices match these filters.")
        return

    groups = st.columns(2)

    with groups[0]:
        st.markdown("**By carrier**")
        st.dataframe(
            pd.DataFrame([
                {
                    "Carrier": item["carrier"], "Invoices": item["invoices"],
                    "Mismatches": item["mismatches"], "Unverifiable": item["unverifiable"],
                    "Stated total": item["stated_total"], "Net difference": item["net_difference"],
                    "Absolute variance": item["absolute_variance"],
                    "Amount under review": item["amount_under_review"],
                }
                for item in exposure.by_carrier(filtered)
            ]),
            hide_index=True, width="stretch",
            column_config={
                name: st.column_config.NumberColumn(format="dollar")
                for name in ("Stated total", "Net difference", "Absolute variance", "Amount under review")
            },
        )

    with groups[1]:
        st.markdown("**By discrepancy category**")
        categories_table = exposure.by_category(filtered)

        if categories_table:
            st.dataframe(
                pd.DataFrame([
                    {"Category": item["category"], "Shipments": item["shipments"],
                     "Absolute variance": item["absolute_variance"], "Amount basis": item["amount_basis"]}
                    for item in categories_table
                ]),
                hide_index=True, width="stretch",
                column_config={"Absolute variance": st.column_config.NumberColumn(format="dollar")},
            )
        else:
            st.caption("No discrepancies in these shipments.")

    st.markdown("**By shipment**")

    st.dataframe(
        pd.DataFrame([
            {
                "Shipment": row["shipment_id"], "Carrier": shown(row["carrier"]),
                "Invoice status": row["invoice_status"],
                "Result": exposure.CLASS_LABELS[row["classification"]],
                "Stated total": row["stated_total"], "Line-item sum": row["line_item_sum"],
                "Signed difference": row["signed_difference"],
                "Amount under review": row["amount_under_review"],
            }
            for row in filtered
        ]),
        hide_index=True, width="stretch",
        column_config={
            name: st.column_config.NumberColumn(format="dollar")
            for name in ("Stated total", "Line-item sum", "Signed difference", "Amount under review")
        },
    )

    st.caption("An amount shown as None is NOT AVAILABLE, not 0.")

    st.download_button(
        "Download financial exposure (CSV)",
        data=exposure.rows_csv(filtered),
        file_name="financial_exposure.csv",
        mime="text/csv",
        on_click="ignore",
        key="op_fx_csv",
    )


# ============================================================
# EXPLANATIONS
# ============================================================

def show_explanation(item, llm):

    columns = st.columns(3)
    columns[0].markdown(f"**{item['kind']}**")
    columns[1].markdown(f"Severity: **{item['severity']}**")
    columns[2].markdown(f"Difference: **{item['difference_text'] or NOT_AVAILABLE}**")

    st.markdown(escape_markdown_dollars(item["finding"]))

    if item["reason"]:
        st.warning("Reason for review: " + escape_markdown_dollars(item["reason"]))

    st.table(pd.DataFrame(
        [
            ["Expected", shown(item["expected"])],
            ["Observed", shown(item["observed"])],
            ["Comparison rule", shown(item["rule"])],
        ],
        columns=["Item", "Value"],
    ).set_index("Item"))

    if item["caveat"]:
        st.info(escape_markdown_dollars(item["caveat"]))

    if item["sources"]:
        st.dataframe(
            pd.DataFrame([
                {"Source": row["source"], "Page": shown(row["page"]), "Type": row["document_type"],
                 "Shipment": row["shipment_id"], "Field text": row["text"]}
                for row in item["sources"]
            ]),
            hide_index=True, width="stretch",
        )

    if item["evidence_note"]:
        st.caption(item["evidence_note"])

    st.markdown("**Suggested next step:** " + escape_markdown_dollars(item["next_step"] or NOT_AVAILABLE))

    # Optional, separate, and never part of the audit
    key = item["exception_key"]
    store = st.session_state.setdefault("op_commentary", {})

    if llm is not None and st.button("Add optional model commentary", key=f"op_comment_{key}"):

        try:
            with st.spinner("Asking the language model..."):
                store[key] = explanations.generate_commentary(item, llm)
        except Exception as error:
            st.error(f"Commentary could not be generated: {error}")

    if key in store:

        commentary = store[key]

        st.markdown("**" + commentary["label"] + "**")
        st.markdown(escape_markdown_dollars(commentary["text"]))

        if commentary["unsupported_numbers"]:
            st.warning(
                "The commentary mentions figures that are not in the audit: "
                + ", ".join(commentary["unsupported_numbers"]) + ". Rely on the audit values above."
            )


# ============================================================
# HISTORY
# ============================================================

def change_summary(changes):

    if changes["first"]:
        return "First recorded audit"

    parts = [
        f"{label}: {len(changes[name])}"
        for name, label in (
            ("new", "New"), ("persistent", "Persistent"), ("changed", "Changed"),
            ("resolved", "No longer detected"), ("reappeared", "Reappeared"),
            ("unverified", "Cannot be verified now"),
        )
        if changes[name]
    ]

    return " · ".join(parts) or "No change in findings"


def show_timeline(history, shipment_key):

    entries = history.timeline(shipment_key)

    if not entries:
        st.info("No audit has been recorded for this shipment yet. Use **Record audit** above.")
        return

    st.caption(
        "Newest first. History records what each audit found (statuses, amounts, "
        "finding summaries); it holds no document text and is never edited."
    )

    for entry in entries:

        shot, changes = entry["snapshot"], entry["changes"]

        title = (
            f"{when(shot['created_at'])} · {shot['final_status'] or 'NOT RECONCILED'} · "
            f"{change_summary(changes)}"
        )

        with st.expander(title):

            if changes["input_changed"]:
                st.warning("The shipment's documents changed since the previous snapshot.")

            if changes["status_change"]:
                st.markdown(
                    f"**Final status:** {shown(changes['status_change'][0])} → {shown(changes['status_change'][1])}"
                )

            if not shot["reconciled"]:
                st.error(f"This shipment could not be reconciled: {shot['error']}")

            st.markdown("**Value states**")
            st.table(pd.DataFrame(
                [
                    [name.capitalize(), STATE_LABELS[state["state"]], state["reason"] or ""]
                    for name, state in shot["audit_states"].items()
                ],
                columns=["Audit", "State", "Reason"],
            ).set_index("Audit"))

            groups = (
                ("New findings", changes["new"]),
                ("Reappeared", changes["reappeared"]),
                ("Changed", changes["changed"]),
                ("Persistent", changes["persistent"]),
                ("No longer detected", changes["resolved"]),
                ("Cannot be verified now (not treated as resolved)", changes["unverified"]),
            )

            for label, items in groups:
                if items:
                    st.markdown(f"**{label}**")
                    for finding in items:
                        st.caption(f"{finding['category']}: " + escape_markdown_dollars(finding["summary"]))

            if changes["value_changes"]:

                st.markdown("**Values that changed**")
                st.table(pd.DataFrame(
                    [
                        [item["label"], value_text(item["field"], item["previous"]),
                         value_text(item["field"], item["current"])]
                        for item in changes["value_changes"]
                    ],
                    columns=["Value", "Previous", "Now"],
                ).set_index("Value"))

            confirmations = [when(run["created_at"]) for run in shot["runs"][1:]]

            st.caption(
                f"Input fingerprint {shot['input_fingerprint'][:12]}…"
                + (f" · found unchanged by {len(confirmations)} later audit(s): " + ", ".join(confirmations)
                   if confirmations else "")
            )


# ============================================================
# ONE SHIPMENT, STEP BY STEP
# ============================================================

def record_audit(history, snapshot, exceptions, label, principal=None):
    """
    The click action of the record buttons: one run, stored once. With a
    principal, recording needs the history.record permission.
    """

    try:
        if principal is not None:
            run = record_run_as(principal, history, snapshot, exceptions, label=label)
        else:
            run = history.record_run(snapshot, exceptions, label=label)
    except authz.PermissionDenied as error:
        st.session_state["op_record_message"] = ("error", str(error))
        return
    except Exception as error:
        st.session_state["op_record_message"] = ("error", f"The audit could not be recorded: {error}")
        return

    if run["snapshots_written"]:
        message = (
            f"Recorded audit run #{run['run_id']}: {run['snapshots_written']} new snapshot(s) "
            f"({', '.join(run['written'])})."
        )
        if run["unchanged"]:
            message += f" Unchanged since their last snapshot: {', '.join(run['unchanged'])}."
        st.session_state["op_record_message"] = ("success", message)
    else:
        st.session_state["op_record_message"] = (
            "info",
            f"Recorded audit run #{run['run_id']}: nothing changed since the last snapshot, "
            "so no new snapshot was written.",
        )


def show_shipment_workflow(
    snapshot, register, review_store, history, review_widget, llm,
    review_error=None, history_error=None, principal=None,
):

    st.subheader("3. Audit a shipment")

    shipments = snapshot["shipments"]

    if not shipments:
        st.info("No identified shipments.")
        return

    chosen = st.selectbox("Shipment", [item["shipment_id"] for item in shipments], key="op_shipment")

    shipment = next(item for item in shipments if item["shipment_id"] == chosen)
    key = shipment["shipment_key"]
    result = shipment["result"]

    # Step 1: documents
    st.markdown("**Step 1 · Documents**")

    row = next((item for item in register or [] if item["shipment_key"] == key), None)

    if row is None:
        st.warning("This shipment has no usable documents in the completeness register.")
    elif row["readiness"] == completeness.READY:
        st.success("All required documents are present.")
    else:
        st.warning(row["next_step"])

    # Step 2: reconciliation
    st.markdown("**Step 2 · Reconciliation**")

    if not result.get("success"):
        st.error(f"{chosen} could not be reconciled: {result.get('error')}")
    else:
        status = result.get("final_status")
        (st.error if status == "DISCREPANCIES DETECTED" else st.warning if status == "NEEDS REVIEW" else st.success)(
            f"{chosen}: {status}"
        )

    # Step 3: findings and evidence
    st.markdown("**Step 3 · Findings and evidence**")

    exceptions = [item for item in build_exceptions(snapshot) if item["shipment_key"] == key]

    reviews = {}

    if review_store is not None:
        try:
            reviews = review_store.sync(build_exceptions(snapshot))
        except Exception as error:
            review_error = str(error)

    merged = with_reviews(exceptions, reviews)

    if not exceptions:
        st.success("No findings for this shipment.")

    for item in explanations.explain_all(snapshot, exceptions):

        review = next(entry for entry in merged if entry["exception_key"] == item["exception_key"])

        with st.expander(f"{item['category']} · {item['severity']} · {review['review_status'].replace('_', ' ')}"):
            show_explanation(item, llm)

    # Step 4: review
    st.markdown("**Step 4 · Review**")

    if review_error:
        st.warning(f"Review database unavailable ({review_error}); review changes cannot be saved.")

    if merged:
        review_widget(review_store, merged, prefix="op")
    else:
        st.caption("Nothing to review.")

    # Step 5: record
    st.markdown("**Step 5 · Record this audit**")

    st.caption(
        "Recording stores the current result in the audit history. A shipment whose "
        "result and documents are unchanged since its last snapshot is not stored again."
    )

    if history is None:
        st.warning(f"Audit history unavailable ({history_error}); audits cannot be recorded.")
    else:

        one = {
            "shipments": [shipment],
            "unidentified_sources": [],
            "unreadable_files": snapshot["unreadable_files"],
        }

        columns = st.columns(2)

        may_record = principal is None or principal.can(authz.HISTORY_RECORD)

        if not may_record:
            st.caption("Only administrators can record audits; you can still compare earlier ones.")

        columns[0].button(
            "Record audit for this shipment",
            on_click=record_audit, args=(history, one, exceptions, f"Single shipment: {chosen}", principal),
            key="op_record_one", disabled=not may_record,
        )
        columns[1].button(
            "Record audit for all shipments",
            on_click=record_audit, args=(history, snapshot, build_exceptions(snapshot), "All shipments", principal),
            key="op_record_all", disabled=not may_record,
        )

        message = st.session_state.get("op_record_message")

        if message:
            getattr(st, message[0])(message[1])

        # Step 6: compare
        st.markdown("**Step 6 · Compare with earlier audits**")
        show_timeline(history, key)

    # Step 7: export
    st.markdown("**Step 7 · Export**")

    if not result.get("success"):
        st.caption("A report needs a successful reconciliation.")
        return

    columns = st.columns(2)

    try:

        columns[0].download_button(
            "Shipment audit (CSV)", data=audit_csv(result, chosen),
            file_name=export_filename(result), mime="text/csv", on_click="ignore", key="op_export_csv",
        )
        columns[1].download_button(
            "Shipment audit (PDF)", data=audit_pdf(result, chosen),
            file_name=pdf_filename(result), mime="application/pdf", on_click="ignore", key="op_export_pdf",
        )

    except ExportError as error:
        st.caption(f"Export unavailable: {error}")


# ============================================================
# THE TAB
# ============================================================

def render(snapshot, files, indexed_sources, review_store, history, review_widget, llm=None,
           review_error=None, history_error=None, principal=None):
    """
    The whole Audit Operations tab for an audit snapshot
    (audit_analytics.build_snapshot), the scanned files
    (document_completeness.scan_folder) and the stores.
    """

    if snapshot["unreadable_files"]:
        st.warning(
            f"{len(snapshot['unreadable_files'])} file(s) could not be used and belong to no "
            "shipment: " + describe_problems(snapshot["unreadable_files"])
            + ". No shipment is shown as clean while they cannot be ruled out; "
            "the Documents finding of each shipment says so."
        )

    register = show_completeness(files, indexed_sources)

    st.divider()

    show_financial_exposure(snapshot)

    st.divider()

    show_shipment_workflow(
        snapshot, register, review_store, history, review_widget, llm,
        review_error=review_error, history_error=history_error, principal=principal,
    )
