"""
LogiDoc-RAG historical trends tab (Streamlit)

Draws trend_analytics over the recorded audit history. It is separate from
the Audit Analytics tab, which shows the documents as they are now: this
tab shows what the audits recorded, and when. It never reads the PDFs and
never recalculates an audit.
"""

import pandas as pd
import streamlit as st

import authz
import trend_analytics as trends
from display_text import escape_markdown_dollars, money_text


PERIOD_LABELS = {"week": "Week", "month": "Month", "quarter": "Quarter"}

RATES = (
    ("Discrepancy rate", "discrepancy"),
    ("Delay rate", "delay"),
    ("Shortage rate", "shortage"),
    ("Invoice arithmetic mismatch rate", "invoice_mismatch"),
)


def percent(item):

    return "n/a" if item["rate"] is None else f"{item['rate'] * 100:.0f}%"


def interval(item):

    if item["low"] is None:
        return ""

    return f"{item['low'] * 100:.0f}–{item['high'] * 100:.0f}%"


def sample(item):

    return f"{item['count']} of {item['n']}"


def money(value, currency):

    if value is None:
        return "n/a"

    text = money_text(value)

    return text if currency == trends.NOT_STATED else f"{text} {currency}"


def definitions():

    with st.expander("Metric definitions and limits"):

        for name, text in trends.METRICS.items():
            st.markdown(f"- **{name.replace('_', ' ')}**: {escape_markdown_dollars(text)}")

        st.markdown(
            f"- Periods are the time the audit ran, not the delivery date. A shipment counts once per period, "
            f"with its latest audit in it.\n- {trends.OVERBILLING_NOTE}"
        )


def show_series(result):

    entries = result["series"]

    if not entries:
        return

    if len(entries) >= 2:

        frame = pd.DataFrame(
            {label: [None if entry[key]["rate"] is None else entry[key]["rate"] * 100 for entry in entries]
             for label, key in RATES},
            index=[entry["period"] for entry in entries],
        )

        st.line_chart(frame)
        st.caption("Rates in percent; a period where a rate could not be calculated has a gap, not a zero.")

    st.dataframe(
        pd.DataFrame([
            {
                "Period": entry["period"], "Shipments": entry["shipments"],
                **{f"{label} (n)": f"{percent(entry[key])} ({sample(entry[key])})" for label, key in RATES},
                "Needs review": sample(entry["needs_review"]),
                "Average days late (delayed)": (
                    "n/a" if entry["average_days_late"]["value"] is None
                    else f"{entry['average_days_late']['value']} (n={entry['average_days_late']['n']})"
                ),
            }
            for entry in entries
        ]),
        hide_index=True, use_container_width=True,
    )

    variance = [
        {
            "Period": entry["period"], "Currency": group["currency"],
            "Invoices checked": group["invoices_checked"], "Arithmetic mismatches": group["mismatches"],
            "Net difference": money(group["net"], group["currency"]),
            "Absolute variance": money(group["absolute"], group["currency"]),
            "Amount unknown": group["unknown_amount"],
        }
        for entry in entries for group in entry["variance"]
    ]

    st.markdown("**Invoice variance (arithmetic mismatches, per currency)**")
    st.caption(trends.OVERBILLING_NOTE + " Currencies are never added together; 'Not stated' means the documents give none.")
    st.dataframe(pd.DataFrame(variance), hide_index=True, use_container_width=True)


def show_carriers(result):

    scorecards = result["carriers"]

    if not scorecards["carriers"]:
        return

    st.markdown("**Carrier scorecards**")

    for note in scorecards["notes"]:
        st.caption(note)

    st.dataframe(
        pd.DataFrame([
            {
                "Rank": card["rank"] if card["rank"] else "not ranked", "Carrier": card["carrier"],
                "Shipments": card["shipments"],
                "Discrepancy rate": f"{percent(card['discrepancy'])} ({sample(card['discrepancy'])})",
                "95% interval": interval(card["discrepancy"]),
                "Delay rate": f"{percent(card['delay'])} ({sample(card['delay'])})",
                "Shortage rate": f"{percent(card['shortage'])} ({sample(card['shortage'])})",
                "Invoice mismatch": f"{percent(card['invoice_mismatch'])} ({sample(card['invoice_mismatch'])})",
                "Needs review": sample(card["needs_review"]),
                "Sample": "enough" if card["sufficient"] else "too small",
            }
            for card in scorecards["carriers"]
        ]),
        hide_index=True, use_container_width=True,
    )


def show_categories(result):

    if not result["categories"]:
        return

    st.markdown("**Discrepancy categories over time**")
    st.caption("'Repeating' counts shipments where two or more audit runs saw the category.")

    st.dataframe(
        pd.DataFrame([
            {
                "Category": item["category"], "Shipments affected": item["shipments_affected"],
                "Audit observations": item["audit_observations"],
                "Shipments repeating": item["shipments_repeating"], "Carriers": ", ".join(item["carriers"]),
            }
            for item in result["categories"]
        ]),
        hide_index=True, use_container_width=True,
    )


def show_shipment_history(history, principal):
    """
    A shipment's recorded snapshots and a comparison of any two.
    """

    shipments = history.shipments()

    if not shipments:
        return

    st.markdown("**Shipment history and comparison**")

    key = st.selectbox(
        "Shipment", sorted(shipments, key=lambda item: shipments[item]),
        format_func=lambda item: shipments[item], key="trend_shipment",
    )

    snapshots = history.snapshots(key)

    st.dataframe(
        pd.DataFrame([
            {
                "Snapshot": snapshot["id"], "Recorded": snapshot["created_at"],
                "Audits that found it": len(snapshot["runs"]), "Final status": snapshot["final_status"] or "not reconciled",
                "Carrier": snapshot.get("carrier") or trends.NOT_STATED,
                "Discrepancies": snapshot["discrepancy_count"], "Needs review": snapshot["review_count"],
            }
            for snapshot in snapshots
        ]),
        hide_index=True, use_container_width=True,
    )

    if len(snapshots) < 2:
        st.caption("Only one snapshot is recorded for this shipment, so there is nothing to compare yet.")
        return

    ids = [snapshot["id"] for snapshot in snapshots]
    columns = st.columns(2)
    first = columns[0].selectbox("Compare", ids, index=0, key="trend_first")
    second = columns[1].selectbox("with", ids, index=len(ids) - 1, key="trend_second")

    if first == second:
        st.caption("Pick two different snapshots.")
        return

    comparison = trends.compare_snapshots(snapshots, first, second)
    changes = comparison["changes"]

    st.caption(f"Snapshot {comparison['earlier']['id']} (earlier) → snapshot {comparison['later']['id']} (later)")

    if changes["status_change"]:
        st.markdown(f"**Final status:** {changes['status_change'][0]} → {changes['status_change'][1]}")

    if changes["input_changed"]:
        st.markdown("**The shipment's documents changed** between these snapshots.")

    for label, key_ in (("New", "new"), ("Reappeared", "reappeared"), ("Changed", "changed"),
                        ("Resolved", "resolved"), ("Unverified (not treated as resolved)", "unverified"),
                        ("Persistent", "persistent")):
        for item in changes[key_]:
            st.markdown(f"- **{label}:** {item['category']} — {escape_markdown_dollars(item['summary'])}")

    if changes["value_changes"]:
        st.dataframe(
            pd.DataFrame([
                {"Value": item["label"], "Earlier": str(item["previous"]), "Later": str(item["current"])}
                for item in changes["value_changes"]
            ]),
            hide_index=True, use_container_width=True,
        )

    if not any(changes[key_] for key_ in ("new", "reappeared", "changed", "resolved", "unverified", "value_changes")) \
            and not changes["status_change"]:
        st.caption("No difference between these two snapshots.")


def show_trends(principal, history, key_prefix="trend"):
    """
    The whole tab for a HistoryStore.
    """

    st.caption(
        "Recorded audit snapshots, not the current documents: this tab only shows what past audit runs "
        "recorded. Current figures are in Audit Analytics."
    )

    if not principal.can(authz.HISTORY_VIEW):
        st.info("You do not have access to the audit history.")
        return

    observations = history.observations()

    if not observations:
        st.info(
            "No audit history has been recorded yet. Record an audit run in the Audit Operations tab "
            "(Audit history), then come back here."
        )
        return

    options = trends.filter_options(observations)

    top = st.columns(4)
    period = top[0].selectbox("Period", list(PERIOD_LABELS), format_func=PERIOD_LABELS.get, index=1,
                              key=f"{key_prefix}_period")
    carriers = top[1].multiselect("Carrier", options["carriers"], key=f"{key_prefix}_carriers")
    statuses = top[2].multiselect("Final status", options["statuses"], key=f"{key_prefix}_statuses")
    categories = top[3].multiselect("Discrepancy category", options["categories"], key=f"{key_prefix}_categories")

    dates = st.columns(3)
    start = dates[0].date_input("From (audit date)", value=None, min_value=options["first"],
                                max_value=options["last"], key=f"{key_prefix}_start")
    end = dates[1].date_input("To (audit date)", value=None, min_value=options["first"],
                              max_value=options["last"], key=f"{key_prefix}_end")
    minimum = dates[2].number_input("Shortest sample to rank a carrier", min_value=1, value=trends.MIN_SAMPLE,
                                    step=1, key=f"{key_prefix}_min")

    result = trends.analyze(
        observations, period=period, carriers=tuple(carriers), statuses=tuple(statuses),
        categories=tuple(categories), start=start, end=end, min_sample=int(minimum),
    )

    st.caption(
        f"{result['observations']} audit observation(s) recorded; {result['shipments']} shipment(s) match the filters."
    )

    definitions()

    if result["insufficient_reason"]:
        st.info(result["insufficient_reason"])

    show_series(result)
    show_carriers(result)
    show_categories(result)

    if result["series"] and principal.can(authz.REPORTS_DOWNLOAD):

        st.download_button(
            "Download filtered analytics (CSV)", trends.analytics_csv(result),
            file_name="audit_trends.csv", mime="text/csv", key=f"{key_prefix}_download",
        )

    st.divider()
    show_shipment_history(history, principal)
