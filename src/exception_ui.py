"""
LogiDoc-RAG exception queue and review actions (Streamlit)

The user interface of the exception workflow (exception_workflow): a
filterable queue (assignee, state, category, age, search), the actions the
signed-in user may take on an exception, and who changed what and when.

Every action goes through ExceptionWorkflow with the acting Principal, so
the role checks, the required notes and evidence, the transition rules and
the approval rules are enforced below this screen. Buttons the user may
not use are not drawn, but hiding them is not the protection.
"""

import pandas as pd
import streamlit as st

from authz import PermissionDenied
from display_text import escape_markdown_dollars
from review_store import (
    EVENT_APPROVE, EVENT_ASSIGN, EVENT_DISMISS, EVENT_NOTE, EVENT_REJECT, EVENT_REOPEN,
    EVENT_RESOLVE, EVENT_START, STATUS_LABELS, STATUSES, ReviewError,
)


ACTION_LABELS = {
    EVENT_ASSIGN: "Assign", EVENT_START: "Start review", EVENT_NOTE: "Add note or evidence",
    EVENT_RESOLVE: "Resolve", EVENT_DISMISS: "Dismiss", EVENT_REOPEN: "Reopen",
    EVENT_APPROVE: "Approve", EVENT_REJECT: "Reject",
}

EVENT_LABELS = {
    "detected": "Detected by the audit", "audit_reopened": "Reopened by the audit", "status": "Status changed",
    **{key: value for key, value in ACTION_LABELS.items()},
}

RULES = (
    "Rules: a review is started before it is resolved. Resolving needs a note and at least one evidence "
    "reference (a document and page, a credit memo, a ticket). Dismissing needs a note giving the reason, "
    "and only an administrator can dismiss. Reopening needs a note. When approval is switched on, an "
    "administrator other than the person who resolved it approves or rejects the resolution."
)


def evidence_lines(text):

    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def status_label(status):

    return STATUS_LABELS.get(status, status)


def history_frame(events):

    return pd.DataFrame([
        {
            "When": event["created_at"],
            "Event": EVENT_LABELS.get(event.get("event_type"), event.get("event_type")),
            "By": event.get("actor") or event.get("reviewer") or "",
            "Role": event.get("actor_role") or "",
            "Signed in": "yes" if event.get("authenticated") else "no",
            "From": status_label(event["from_status"]) if event["from_status"] else "",
            "To": status_label(event["to_status"]),
            "Assignee": event.get("assignee") or "",
            "Note": event.get("note") or "",
            "Evidence": "; ".join(event.get("evidence_refs") or []),
        }
        for event in events
    ])


def show_history(workflow, principal, exception_key):

    events = workflow.history(principal, exception_key)

    if not events:
        return

    with st.expander(f"Who changed what and when ({len(events)})"):
        st.dataframe(history_frame(events), hide_index=True, use_container_width=True)


def run_action(label, action, prefix, key):
    """
    Runs one workflow action, shows the refusal when there is one, and
    reloads on success.
    """

    try:
        action()
    except (ReviewError, PermissionDenied) as error:
        st.error(str(error))
    else:
        st.session_state[f"{prefix}_saved"] = f"{key}: {label}"
        st.rerun()


def show_actions(principal, workflow, exception, row, prefix, assignable=None):
    """
    The forms for the actions the principal may take on the exception.
    """

    actions = workflow.allowed_actions(principal, row)
    key = exception["exception_key"]
    form = f"{prefix}_{key}"

    if not actions:
        st.caption("No action is available to you on this exception in its current state.")
        return

    if EVENT_ASSIGN in actions or EVENT_START in actions:

        columns = st.columns(2)

        if EVENT_ASSIGN in actions:

            with columns[0].form(f"{form}_assign"):

                if assignable is None:
                    target = st.text_input("Assign to", value=row.get("assignee") or "")
                else:
                    options = sorted(assignable)
                    target = st.selectbox(
                        "Assign to", options,
                        index=options.index(row["assignee"]) if row.get("assignee") in options else 0,
                    ) if options else ""

                if st.form_submit_button("Assign"):
                    run_action("assigned", lambda: workflow.assign(principal, exception, target), prefix, key)

        if EVENT_START in actions:

            with columns[1].form(f"{form}_start"):

                st.caption("Starts the review and assigns it to you unless it is already assigned.")

                if st.form_submit_button("Start review"):
                    run_action("review started", lambda: workflow.start(principal, exception), prefix, key)

    if EVENT_NOTE in actions:

        with st.form(f"{form}_note", clear_on_submit=True):

            note = st.text_area("Note", key=f"{form}_note_text")
            evidence = st.text_area("Evidence references (one per line)", key=f"{form}_note_evidence",
                                    help="For example: INVOICE_SHJ-2026-001.pdf p.1, credit memo CM-77.")

            if st.form_submit_button("Add note or evidence"):
                run_action("note added", lambda: workflow.add_note(
                    principal, exception, note, evidence_lines(evidence)), prefix, key)

    if EVENT_RESOLVE in actions:

        with st.form(f"{form}_resolve", clear_on_submit=True):

            note = st.text_area("Resolution note (what was done)", key=f"{form}_resolve_text")
            evidence = st.text_area("Resolution evidence (one reference per line)", key=f"{form}_resolve_evidence")

            if st.form_submit_button("Resolve"):
                run_action("resolved", lambda: workflow.resolve(
                    principal, exception, note, evidence_lines(evidence)), prefix, key)

    if EVENT_DISMISS in actions:

        with st.form(f"{form}_dismiss", clear_on_submit=True):

            note = st.text_area("Reason for dismissing", key=f"{form}_dismiss_text")

            if st.form_submit_button("Dismiss"):
                run_action("dismissed", lambda: workflow.dismiss(principal, exception, note), prefix, key)

    if EVENT_REOPEN in actions:

        with st.form(f"{form}_reopen", clear_on_submit=True):

            note = st.text_area("Why it is reopened", key=f"{form}_reopen_text")

            if st.form_submit_button("Reopen"):
                run_action("reopened", lambda: workflow.reopen(principal, exception, note), prefix, key)

    if EVENT_APPROVE in actions:

        with st.form(f"{form}_approval", clear_on_submit=True):

            note = st.text_area("Approval or rejection note (required to reject)", key=f"{form}_approval_text")
            columns = st.columns(2)
            approve = columns[0].form_submit_button("Approve")
            reject = columns[1].form_submit_button("Reject")

            if approve:
                run_action("approved", lambda: workflow.approve(principal, exception, note), prefix, key)

            if reject:
                run_action("rejected", lambda: workflow.reject(principal, exception, note), prefix, key)


def show_review(principal, workflow, store, exceptions, prefix="an", assignable=None):
    """
    One exception: its description, evidence, review state, the actions
    available to the principal and its history. Used by the review widgets
    of the analytics tabs when the user is signed in, and by the queue.
    """

    labels = {
        f"{item['shipment_id']} · {item['category']} ({item['kind']})": item for item in exceptions
    }

    choice = st.selectbox("Exception", list(labels), key=f"{prefix}_exception")
    exception = labels[choice]

    show_exception(principal, workflow, store, exception, prefix, assignable)


def show_exception(principal, workflow, store, exception, prefix, assignable=None):

    row = store.states().get(exception["exception_key"])

    st.markdown(escape_markdown_dollars(exception["description"]))

    if row is None:
        st.info("This exception has not been recorded yet; open the exception register first.")
        return

    if row.get("needs_recheck"):
        st.warning(
            "Reopened: the audited values changed, or the exception came back, after it had been resolved "
            "or dismissed."
        )

    for reference in exception["evidence"]:
        st.caption(escape_markdown_dollars(reference))

    summary = [f"**State:** {status_label(row['status'])}", f"**Assignee:** {row.get('assignee') or 'nobody'}"]

    if row.get("approval_state"):
        summary.append(f"**Approval:** {row['approval_state']}")

    st.markdown(" · ".join(summary))

    if row.get("evidence_refs"):
        st.caption("Evidence on record: " + "; ".join(escape_markdown_dollars(item) for item in row["evidence_refs"]))

    if st.session_state.get(f"{prefix}_saved"):
        st.success(f"Saved: {st.session_state.pop(f'{prefix}_saved')}")

    show_actions(principal, workflow, exception, row, prefix, assignable)
    show_history(workflow, principal, exception["exception_key"])


# ============================================================
# QUEUE
# ============================================================

def show_queue(principal, workflow, store, exceptions, assignable=None, now=None):
    """
    The exception queue with its filters, the selected exception, and the
    latest activity. exceptions: build_exceptions() output merged with the
    review state (with_reviews).
    """

    st.caption(RULES)

    detected = {item["exception_key"]: item for item in exceptions}
    known = store.states()

    categories = sorted({row["category"] for row in known.values()})
    people = sorted(set(store.assignees()))

    first = st.columns(4)
    search = first[0].text_input("Search shipment", key="queue_search")
    assignee = first[1].selectbox("Assignee", ["Anyone", "Unassigned", "Me", *people], key="queue_assignee")
    states = first[2].multiselect("State", STATUSES, format_func=status_label, key="queue_states")
    chosen_categories = first[3].multiselect("Category", categories, key="queue_categories")

    second = st.columns(4)
    older = second[0].number_input("At least this many days old", min_value=0, value=0, step=1, key="queue_older")
    newer = second[1].number_input("At most this many days old (0 = any)", min_value=0, value=0, step=1,
                                   key="queue_newer")
    waiting = second[2].checkbox("Waiting for approval", key="queue_waiting")
    current_only = second[3].checkbox("Still detected only", value=True, key="queue_current")

    rows = workflow.queue(
        principal,
        search=search,
        assignee=principal.username if assignee == "Me" else (None if assignee in ("Anyone", "Unassigned") else assignee),
        unassigned=assignee == "Unassigned",
        statuses=tuple(states),
        categories=tuple(chosen_categories),
        min_age_days=int(older) or None,
        max_age_days=int(newer) or None,
        pending_approval=waiting,
        include_absent=not current_only,
        now=now,
    )

    if not rows:
        st.info("No exception matches these filters." if known else "No exceptions have been recorded yet.")
        return

    st.dataframe(
        pd.DataFrame([
            {
                "Shipment": row["shipment_id"], "Category": row["category"], "State": status_label(row["status"]),
                "Assignee": row.get("assignee") or "", "Age (days)": row["age_days"],
                "Approval": row.get("approval_state") or "", "Recheck": bool(row["needs_recheck"]),
                "No longer detected": bool(row["absent_since"]), "Updated": row["updated_at"],
            }
            for row in rows
        ]),
        hide_index=True, use_container_width=True,
    )

    st.caption(f"{len(rows)} exception(s).")

    labels = {f"{row['shipment_id']} · {row['category']} · {status_label(row['status'])}": row for row in rows}
    choice = st.selectbox("Open an exception", list(labels), key="queue_pick")
    picked = labels[choice]

    exception = detected.get(picked["exception_key"])

    if exception is None:
        st.info(
            "The audit no longer detects this exception, so it cannot be changed. It keeps its state and "
            "history; it is reopened if the audit finds it again."
        )
        show_history(workflow, principal, picked["exception_key"])
    else:
        show_exception(principal, workflow, store, exception, "queue", assignable)

    activity = workflow.activity(principal, 100)

    if activity:
        with st.expander(f"Latest activity ({len(activity)})"):
            frame = history_frame(activity)
            frame.insert(1, "Shipment", [event["shipment_id"] for event in activity])
            frame.insert(2, "Category", [event["category"] for event in activity])
            st.dataframe(frame, hide_index=True, use_container_width=True)
