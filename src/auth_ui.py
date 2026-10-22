"""
LogiDoc-RAG sign-in screen and administration tab (Streamlit)

gate() runs at the top of app.py, before anything else is drawn, and
returns the acting Principal:

    local mode      the "local-dev" principal (not authenticated), with a
                    banner saying so
    required mode   a signed-in user. Without a valid session it shows the
                    sign-in form and stops the script (st.stop()), so no
                    data is read or drawn for an anonymous visitor. The
                    session token lives in Streamlit's per-browser-session
                    state and is checked against the user database on every
                    run, so an idle timeout, a logout elsewhere or a
                    deactivated account ends the session at the next click.

The sign-in only protects the Streamlit page: serve the app over HTTPS
(see the README). Business rules are enforced below the UI, in the
services, so hiding a button is never the only protection.
"""

import streamlit as st

import auth
import authz
import config
from auth import AuthError, ConfigError, UserStore


TOKEN_KEY = "auth_token"


def open_store():

    return UserStore(config.AUTH_DB_PATH)


def sign_out(store):

    store.logout(st.session_state.pop(TOKEN_KEY, None))


def show_login(store):

    st.markdown("### Sign in")

    if not store.has_active_admin():
        st.error(
            "No administrator account exists, so nobody can sign in. The server operator creates the "
            "first one with: python scripts/manage_users.py create <name> --role admin"
        )
        return

    with st.form("login_form"):

        username = st.text_input("User name", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:

        try:
            session = store.login(username, password)
        except AuthError as error:
            st.error(str(error))
        else:
            st.session_state[TOKEN_KEY] = session["token"]
            st.rerun()


def gate():
    """
    The acting Principal, or the sign-in form and a stopped script.
    """

    try:
        mode = auth.auth_mode()
    except ConfigError as error:
        st.error(f"Configuration problem: {error}")
        st.stop()

    if mode == auth.MODE_LOCAL:

        with st.sidebar:
            st.caption(
                "🛠️ Local development mode: nobody signs in, and every action is recorded as "
                "'local-dev' (not authenticated). Set LOGIDOC_AUTH_MODE=required to require sign-in."
            )

        return auth.local_principal()

    try:
        store = open_store()
    except Exception as error:
        st.error(f"The user database is unavailable ({error}). Nobody can sign in.")
        st.stop()

    principal = store.principal_for(st.session_state.get(TOKEN_KEY))

    if principal is None:

        st.session_state.pop(TOKEN_KEY, None)
        st.markdown("## 📦 LogiDoc-RAG")
        show_login(store)
        st.stop()

    with st.sidebar:
        st.caption(f"Signed in as **{principal.username}** ({authz.ROLE_LABELS.get(principal.role, principal.role)})")
        st.button("Sign out", on_click=sign_out, args=(store,), key="sign_out")

    return principal


# ============================================================
# ADMINISTRATION TAB
# ============================================================

def show_admin(principal, history_stores=None):
    """
    Accounts, security events and integrity checks. history_stores:
    {label: callable returning a dict with "ok" / "problems"} for the
    integrity checks the administrator can run.
    """

    if not principal.can(authz.ADMIN_USERS):
        st.info("This tab is for administrators.")
        return

    st.subheader("Settings (read-only)")
    st.caption(
        "These are set on the server (environment variables) and cannot be changed from this page."
    )
    st.table({
        "Setting": [
            "Sign-in mode", "Resolution needs approval", "Shortest resolution/dismissal note",
            "Upload limit",
        ],
        "Value": [
            auth.auth_mode(), "yes" if config.REQUIRE_APPROVAL else "no",
            f"{config.MIN_NOTE_LENGTH} characters", f"{config.MAX_UPLOAD_MB} MB",
        ],
    })

    if history_stores:

        st.subheader("History integrity")
        st.caption(
            "Recomputes the hash chain of the exception review history. It detects edits and removals "
            "made outside the application; it cannot protect against someone who can rewrite the whole "
            "database file."
        )

        for label, check in history_stores.items():

            if st.button(f"Verify {label}", key=f"verify_{label}"):

                report = check()

                if report["ok"]:
                    st.success(
                        f"{label}: {report['checked']} event(s) verified"
                        + (f"; {report['legacy_events']} earlier event(s) have no hash and cannot be verified."
                           if report.get("legacy_events") else ".")
                    )
                else:
                    st.error(f"{label}: " + " ".join(report["problems"]))

    if not principal.authenticated:

        st.info(
            "Local development mode has no user accounts. Set LOGIDOC_AUTH_MODE=required and create "
            "accounts with scripts/manage_users.py."
        )
        return

    store = open_store()

    st.subheader("Accounts")

    st.dataframe(
        [
            {"User": user["username"], "Role": authz.ROLE_LABELS[user["role"]],
             "Active": bool(user["active"]), "Locked until": user["locked_until"] or "",
             "Created": user["created_at"], "Password changed": user["password_changed_at"]}
            for user in store.users(principal)
        ],
        hide_index=True,
    )

    with st.form("create_user_form", clear_on_submit=True):

        st.markdown("**Create an account**")
        name = st.text_input("User name", key="new_user_name")
        password = st.text_input("Password (at least 12 characters)", type="password", key="new_user_password")
        role = st.selectbox("Role", authz.ROLES, format_func=lambda value: authz.ROLE_LABELS[value])

        if st.form_submit_button("Create account"):

            try:
                store.create_user(name, password, role, actor=principal)
            except (AuthError, authz.PermissionDenied) as error:
                st.error(str(error))
            else:
                st.success(f"Created {name.strip().lower()}.")
                st.rerun()

    users = [user["username"] for user in store.users(principal)]

    with st.form("change_user_form"):

        st.markdown("**Change an account**")
        target = st.selectbox("Account", users, key="change_user_target")
        new_role = st.selectbox("Role", ["(unchanged)", *authz.ROLES], key="change_user_role")
        state = st.selectbox("Status", ["(unchanged)", "active", "inactive"], key="change_user_state")
        new_password = st.text_input("New password (leave empty to keep)", type="password", key="change_user_password")

        if st.form_submit_button("Apply"):

            try:
                store.change_user(
                    target, actor=principal,
                    role=None if new_role == "(unchanged)" else new_role,
                    active=None if state == "(unchanged)" else state == "active",
                    password=new_password or None,
                )
            except (AuthError, authz.PermissionDenied) as error:
                st.error(str(error))
            else:
                st.success(f"Updated {target}. Their sessions were ended.")

    with st.expander("Security events"):

        st.dataframe(store.events(principal, limit=100), hide_index=True)
