"""
LogiDoc-RAG ingestion tab (Streamlit) and the sidebar result adapter

The tab shows the ingestion register (the status of every file offered),
the open errors with their corrective actions, and, for administrators
only, the actions on stored documents: retry, reprocess, replace and
remove. Replace and remove ask for an explicit confirmation, archive the
original PDF first and never delete it.

legacy_view() turns the result of ingestion_service.process_files into
the shape the sidebar's "Last upload" panel has always shown.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import authz
import config
import document_lifecycle as lifecycle
import ingestion_register as register_module
import ingestion_service as service
from authz import PermissionDenied
from ingestion_register import STATUS_LABELS, STATUSES


STATUS_ICONS = {
    "queued": "⏳", "processing": "⚙️", "indexed": "✅", "partial": "⚠️", "failed": "❌", "unsupported": "🚫",
}


def legacy_view(summary):
    """
    The old ingestion_manager result shape for a process_files summary:
    counts of added, duplicate, changed, inconsistent and failed files,
    and one outcome per file with the legacy status names.
    """

    outcomes = []

    for record in summary["files"]:

        code, status = record["code"], record["status"]

        if status in (register_module.INDEXED, register_module.PARTIAL) and code == service.DUPLICATE:
            legacy = "duplicate"
        elif status in (register_module.INDEXED, register_module.PARTIAL):
            legacy = "added"
        elif code == service.NAME_CONFLICT:
            legacy = "changed"
        elif code == service.INDEX_INCONSISTENT:
            legacy = "inconsistent"
        else:
            legacy = "failed"

        problem = legacy == "failed"

        outcomes.append({
            "filename": record["filename"], "status": legacy, "sha256": record["sha256"],
            "error": record["message"] if problem else None,
            "message": None if problem else record["message"],
            "action": record["action"], "rollback_error": None, "warnings": [],
            "duplicate_of": record["duplicate_of"], "matched_files": record.get("matched_files") or [],
            "document_type": record["document_type"],
            "shipment_id": record["shipment_id"], "chunks_indexed": record["chunks_indexed"] or 0,
            "register_status": status, "register_code": code,
        })

    def count(name):
        return sum(1 for item in outcomes if item["status"] == name)

    return {
        "files_discovered": len(outcomes), "files_added": count("added"),
        "duplicates_skipped": count("duplicate"), "changed_detected": count("changed"),
        "inconsistent_detected": count("inconsistent"), "files_processed": count("added"),
        "files_failed": count("failed"), "success": count("failed") == 0,
        "errors": [f"{item['filename']}: {item['error']}" for item in outcomes if item["status"] == "failed"],
        "rollback_errors": [], "warnings": [], "files": outcomes,
        "corpus_changed": summary["corpus_changed"],
    }


def open_register(raw_dir):

    return register_module.IngestionRegister(config.ingestion_locations(raw_dir)["register"])


def clear_caches():
    """
    A document was added, replaced or removed: everything cached from the
    documents is stale.
    """

    st.cache_data.clear()


def register_frame(records):

    return pd.DataFrame([
        {
            "File": record["filename"], "Status": f"{STATUS_ICONS.get(record['status'], '')} {STATUS_LABELS[record['status']]}",
            "Reason": record["message"] or "", "What to do": record["action"] or "",
            "Type": record["document_type"] or "", "Shipment": record["shipment_id"] or "",
            "Pages": record["pages"] if record["pages"] is not None else "",
            "Chunks": record["chunks_indexed"] if record["chunks_indexed"] is not None else "",
            "Attempts": record["attempts"], "Open error": record["status"] in register_module.ERROR_STATUSES and not record["resolved"],
            "By": record["actor"] or "", "Updated": record["updated_at"],
        }
        for record in records
    ])


def show_errors(principal, register, raw_dir, chroma_dir):

    errors = register.errors()

    if not errors:
        st.success("No open ingestion errors.")
        return

    st.warning(
        f"{len(errors)} open ingestion error(s). Until each is corrected or resolved, no shipment is shown "
        "as clean in the audit: a file that could not be ingested might belong to it."
    )

    st.dataframe(register_frame(errors), hide_index=True, use_container_width=True)

    if not principal.can(authz.INGESTION_WRITE):
        return

    locations = config.ingestion_locations(raw_dir)

    labels = {f"#{record['id']} {record['filename']} ({record['code']})": record for record in errors}
    choice = st.selectbox("Error", list(labels), key="ingest_error_pick")
    record = labels[choice]

    columns = st.columns(2)

    if record["quarantine_name"]:

        if columns[0].button("Retry from the kept copy", key="ingest_retry"):

            try:
                with st.spinner("Retrying..."):
                    summary = service.retry(
                        principal, register, record["id"], raw_dir, chroma_dir, locations["quarantine"]
                    )
            except (register_module.RegisterError, PermissionDenied) as error:
                st.error(str(error))
            else:
                if summary["corpus_changed"]:
                    clear_caches()
                st.session_state["ingest_notice"] = f"Retry: {summary['files'][0]['status']}"
                st.rerun()

    with columns[1].form("ingest_resolve_form", clear_on_submit=True):

        note = st.text_area("What was done about this error (at least 10 characters)", key="ingest_resolve_note")

        if st.form_submit_button("Mark as resolved"):

            try:
                service.resolve_error(principal, register, record["id"], note)
            except (register_module.RegisterError, PermissionDenied) as error:
                st.error(str(error))
            else:
                st.session_state["ingest_notice"] = f"Resolved #{record['id']}"
                st.rerun()


def stored_files(raw_dir):

    return sorted(path.name for path in Path(raw_dir).glob("*.pdf"))


def show_admin_actions(principal, register, raw_dir, chroma_dir):

    if not principal.can(authz.INGESTION_WRITE):
        st.info("Only administrators can reprocess, replace or remove stored documents.")
        return

    locations = config.ingestion_locations(raw_dir)
    names = stored_files(raw_dir)

    st.caption(
        "These actions change the document store. A replaced or removed original is first copied into "
        f"the archive folder ({locations['archive']}) and verified: it is never deleted. Each action is logged."
    )

    if not names:
        st.info("No documents are stored yet.")
        return

    target = st.selectbox("Stored document", names, key="ingest_target")

    with st.expander("Reprocess (index a stored PDF that has no vectors)"):

        if st.button("Reprocess", key="ingest_reprocess"):

            try:
                with st.spinner("Indexing..."):
                    result = lifecycle.reprocess(principal, register, target, raw_dir, chroma_dir)
            except (lifecycle.LifecycleError, PermissionDenied) as error:
                st.error(str(error))
            except Exception as error:
                st.error(f"Reprocessing failed and the index was left as it was: {error}")
            else:
                clear_caches()
                st.success(
                    "Already indexed; nothing was written." if result["action"] == "already_indexed"
                    else f"Indexed {result['chunks']} chunk(s)."
                )

    with st.expander("Replace with a corrected PDF"):

        corrected = st.file_uploader("Corrected PDF", type=["pdf"], key="ingest_replace_file")
        confirm = st.checkbox(f"Replace {target}. The current PDF is archived first.", key="ingest_replace_confirm")

        if st.button("Replace", key="ingest_replace", disabled=not (corrected and confirm)):

            try:
                with st.spinner("Replacing..."):
                    result = lifecycle.replace(
                        principal, register, target, bytes(corrected.getbuffer()), raw_dir, chroma_dir,
                        locations["archive"],
                    )
            except (lifecycle.LifecycleError, PermissionDenied) as error:
                st.error(str(error))
            except Exception as error:
                st.error(f"Replacing failed and the previous document was restored: {error}")
            else:
                clear_caches()

                if result["action"] == "unchanged":
                    st.info("The new file is identical to the stored one; nothing changed.")
                else:
                    st.success(f"Replaced {target}. The previous version is archived as {Path(result['archived']).name}.")

    with st.expander("Remove from the store and the index"):

        confirm = st.checkbox(
            f"Remove {target}. Its PDF is archived, not deleted; its vectors are removed from the index.",
            key="ingest_remove_confirm",
        )

        if st.button("Remove", key="ingest_remove", disabled=not confirm):

            try:
                result = lifecycle.remove(principal, register, target, raw_dir, chroma_dir, locations["archive"])
            except (lifecycle.LifecycleError, PermissionDenied) as error:
                st.error(str(error))
            except Exception as error:
                st.error(f"Removing failed and the document was kept: {error}")
            else:
                clear_caches()
                st.success(f"Removed {target}; archived as {Path(result['archived']).name}.")


def show_ingestion(principal, raw_dir, chroma_dir):
    """
    The whole Ingestion tab.
    """

    if not principal.can(authz.INGESTION_VIEW):
        st.info("You do not have access to the ingestion register.")
        return

    try:
        register = open_register(raw_dir)
    except Exception as error:
        st.error(f"The ingestion register is unavailable: {error}")
        return

    if st.session_state.get("ingest_notice"):
        st.success(st.session_state.pop("ingest_notice"))

    summary = register.summary()

    columns = st.columns(len(STATUSES) + 1)

    for column, status in zip(columns, STATUSES):
        column.metric(STATUS_LABELS[status], summary[status])

    columns[-1].metric("Open errors", summary["open_errors"])

    st.caption(
        "Status of the latest record of each file. Image-only PDFs are 'Unsupported (needs OCR)': there is "
        "no OCR in this installation, so they are never indexed."
    )

    st.subheader("Errors and corrective actions")
    show_errors(principal, register, raw_dir, chroma_dir)

    st.subheader("Register")

    first = st.columns(2)
    chosen = first[0].multiselect("Status", STATUSES, format_func=STATUS_LABELS.get, key="ingest_status")
    search = first[1].text_input("Search file or shipment", key="ingest_search")

    records = register.files(statuses=tuple(chosen), search=search)

    if records:
        st.dataframe(register_frame(records), hide_index=True, use_container_width=True)
    else:
        st.info("No files have been offered for ingestion yet." if not (chosen or search) else "No file matches.")

    actions = register.actions(50)

    if actions:
        with st.expander(f"Administrator actions ({len(actions)})"):
            st.dataframe(pd.DataFrame(actions)[["at", "action", "filename", "detail", "actor"]],
                         hide_index=True, use_container_width=True)

    st.subheader("Stored documents")
    show_admin_actions(principal, register, raw_dir, chroma_dir)
