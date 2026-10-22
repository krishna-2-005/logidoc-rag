"""
Tests for src/auth.py and src/authz.py: accounts, passwords, sessions,
lockout, modes and role permissions.

Every test uses its own temporary SQLite file; the real user database
is never opened (tests/conftest.py checks it).

Run from the project root:
    python -m pytest tests/test_auth.py
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import auth
import authz
from auth import AuthError, ConfigError, UserStore
from authz import ADMIN, REVIEWER, PermissionDenied, Principal, require


PASSWORD = "correct horse battery"


class Clock:

    def __init__(self):
        self.moment = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.moment

    def advance(self, **delta):
        self.moment += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):

    users = UserStore(tmp_path / "auth.sqlite3", clock=clock)
    users.create_user("alice", PASSWORD, ADMIN)
    users.create_user("bob", PASSWORD, REVIEWER)

    return users


# ------------------------------------------------------------
# Passwords
# ------------------------------------------------------------

def test_passwords_are_hashed_with_a_salt_and_never_stored(store):

    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT username, password_hash FROM users").fetchall()

    hashes = [hash_ for _, hash_ in rows]

    assert all(hash_.startswith("scrypt$") for hash_ in hashes)
    assert all(PASSWORD not in hash_ for hash_ in hashes)

    # The same password gets a different salt, so a different hash
    assert hashes[0] != hashes[1]


def test_a_password_is_verified_and_a_wrong_one_is_not():

    stored = auth.hash_password(PASSWORD)

    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password(PASSWORD + "x", stored)
    assert not auth.verify_password(PASSWORD, "not-a-hash")
    assert not auth.verify_password(PASSWORD, "scrypt$1$2$3$AAAA$AAAA")


@pytest.mark.parametrize("password", ["short", "a" * 11, "aaaaaaaaaaaa", "x" * 300])
def test_weak_passwords_are_refused(store, password):

    with pytest.raises(AuthError):
        store.create_user("carol", password, REVIEWER)


@pytest.mark.parametrize("name", ["", "ab", "Bad Name", "-start", "local-dev", "a;drop", "x" * 65])
def test_invalid_user_names_are_refused(store, name):

    with pytest.raises(AuthError):
        store.create_user(name, PASSWORD, REVIEWER)


def test_duplicate_users_and_unknown_roles_are_refused(store):

    with pytest.raises(AuthError, match="already exists"):
        store.create_user("ALICE", PASSWORD, REVIEWER)

    with pytest.raises(AuthError, match="Unknown role"):
        store.create_user("carol", PASSWORD, "superuser")


# ------------------------------------------------------------
# Sign-in and sessions
# ------------------------------------------------------------

def test_login_returns_the_user_and_a_token_that_is_not_stored(store):

    session = store.login("Alice", PASSWORD)

    assert session["principal"] == Principal("alice", ADMIN, True, session["principal"].user_id)
    assert store.principal_for(session["token"]).username == "alice"

    with sqlite3.connect(store.path) as connection:
        stored = [row[0] for row in connection.execute("SELECT token_hash FROM sessions")]

    assert session["token"] not in stored
    assert auth.token_hash(session["token"]) in stored


def test_wrong_password_and_unknown_user_give_the_same_error(store):

    with pytest.raises(AuthError) as wrong:
        store.login("alice", "nope-nope-nope-nope")

    with pytest.raises(AuthError) as unknown:
        store.login("nobody", "nope-nope-nope-nope")

    assert str(wrong.value) == str(unknown.value)


def test_failed_sign_ins_are_counted_and_lock_the_account(store, clock):

    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        with pytest.raises(AuthError):
            store.login("bob", "wrong-password-1")

    # Even the right password is refused while locked
    with pytest.raises(AuthError, match="locked"):
        store.login("bob", PASSWORD)

    clock.advance(minutes=16)

    assert store.login("bob", PASSWORD)["principal"].username == "bob"


def test_failed_attempts_are_recorded_even_though_the_error_is_raised(store):

    with pytest.raises(AuthError):
        store.login("bob", "wrong-password-1")

    row = [user for user in store.users() if user["username"] == "bob"][0]

    assert row["failed_attempts"] == 1
    assert any(event["event"] == "login_failed" for event in store.events())


def test_a_successful_login_resets_the_failed_attempts(store):

    for _ in range(3):
        with pytest.raises(AuthError):
            store.login("bob", "wrong-password-1")

    store.login("bob", PASSWORD)

    assert [u for u in store.users() if u["username"] == "bob"][0]["failed_attempts"] == 0


def test_a_session_ends_after_the_idle_timeout(store, clock):

    token = store.login("bob", PASSWORD)["token"]

    clock.advance(minutes=29)
    assert store.principal_for(token) is not None

    # Activity renewed the idle timer ...
    clock.advance(minutes=29)
    assert store.principal_for(token) is not None

    # ... but 31 quiet minutes end it
    clock.advance(minutes=31)
    assert store.principal_for(token) is None
    assert store.principal_for(token) is None


def test_a_session_ends_after_its_absolute_lifetime_even_when_active(store, clock):

    token = store.login("bob", PASSWORD)["token"]
    started = clock.moment

    # Used every 25 minutes, so it is never idle for 30
    while clock.moment - started < auth.SESSION_LIFETIME - timedelta(minutes=25):
        clock.advance(minutes=25)
        assert store.principal_for(token) is not None

    clock.advance(minutes=25)
    assert clock.moment - started >= auth.SESSION_LIFETIME - timedelta(minutes=25)

    clock.advance(minutes=25)
    assert store.principal_for(token) is None


def test_logout_ends_the_session(store):

    token = store.login("bob", PASSWORD)["token"]
    store.logout(token)

    assert store.principal_for(token) is None
    store.logout(token)
    store.logout(None)


def test_unknown_or_empty_tokens_are_not_users(store):

    assert store.principal_for("") is None
    assert store.principal_for(None) is None
    assert store.principal_for("made-up-token") is None


def test_deactivating_an_account_ends_its_sessions_and_blocks_sign_in(store):

    token = store.login("bob", PASSWORD)["token"]

    store.change_user("bob", active=False)

    assert store.principal_for(token) is None

    with pytest.raises(AuthError, match="Wrong user name or password"):
        store.login("bob", PASSWORD)


def test_a_role_or_password_change_ends_the_sessions(store):

    token = store.login("bob", PASSWORD)["token"]
    store.change_user("bob", role=ADMIN)
    assert store.principal_for(token) is None

    token = store.login("bob", PASSWORD)["token"]
    store.change_user("bob", password="another long password")
    assert store.principal_for(token) is None
    assert store.login("bob", "another long password")


def test_the_last_active_administrator_cannot_be_removed(store):

    with pytest.raises(AuthError, match="last active administrator"):
        store.change_user("alice", active=False)

    with pytest.raises(AuthError, match="last active administrator"):
        store.change_user("alice", role=REVIEWER)

    store.change_user("bob", role=ADMIN)
    store.change_user("alice", role=REVIEWER)


def test_expired_sessions_can_be_purged(store, clock):

    store.login("bob", PASSWORD)
    clock.advance(hours=9)

    assert store.purge_expired() == 1


def test_account_events_are_recorded_without_passwords(store):

    store.login("alice", PASSWORD)

    events = store.events()
    text = repr(events)

    assert {"user_created", "login"} <= {event["event"] for event in events}
    assert PASSWORD not in text


def test_sql_injection_in_the_user_name_is_just_a_wrong_name(store):

    with pytest.raises(AuthError):
        store.login("alice' OR '1'='1", PASSWORD)

    assert len(store.users()) == 2


def test_a_database_of_another_schema_version_is_refused(tmp_path):

    path = tmp_path / "auth.sqlite3"
    UserStore(path)

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_info SET version = 99")

    with pytest.raises(AuthError, match="schema version 99"):
        UserStore(path)


# ------------------------------------------------------------
# Account management needs the administrator role
# ------------------------------------------------------------

def test_only_administrators_manage_accounts(store):

    admin = store.login("alice", PASSWORD)["principal"]
    reviewer = store.login("bob", PASSWORD)["principal"]

    store.create_user("carol", PASSWORD, REVIEWER, actor=admin)

    for action in (
        lambda: store.create_user("dave", PASSWORD, REVIEWER, actor=reviewer),
        lambda: store.users(actor=reviewer),
        lambda: store.change_user("carol", role=ADMIN, actor=reviewer),
        lambda: store.events(actor=reviewer),
        lambda: store.purge_expired(actor=reviewer),
    ):
        with pytest.raises(PermissionDenied):
            action()

    assert [user["username"] for user in store.users(actor=admin)] == ["alice", "bob", "carol"]


# ------------------------------------------------------------
# Modes
# ------------------------------------------------------------

def test_local_mode_is_the_default_and_is_not_authenticated(monkeypatch):

    monkeypatch.delenv(auth.AUTH_MODE_ENV, raising=False)
    monkeypatch.delenv(auth.ENVIRONMENT_ENV, raising=False)

    principal = auth.local_principal()

    assert principal.username == "local-dev"
    assert principal.authenticated is False
    assert principal.can(authz.ADMIN_USERS)


def test_required_mode_has_no_local_principal(monkeypatch, store):

    monkeypatch.setenv(auth.AUTH_MODE_ENV, "required")

    with pytest.raises(ConfigError):
        auth.local_principal()

    # Nobody is signed in without a valid token
    assert auth.current_principal(store, None) is None
    assert auth.current_principal(store, "forged") is None

    token = store.login("bob", PASSWORD)["token"]
    assert auth.current_principal(store, token).username == "bob"


def test_production_refuses_local_mode(monkeypatch):

    monkeypatch.setenv(auth.ENVIRONMENT_ENV, "production")
    monkeypatch.delenv(auth.AUTH_MODE_ENV, raising=False)

    with pytest.raises(ConfigError, match="production"):
        auth.auth_mode()

    monkeypatch.setenv(auth.AUTH_MODE_ENV, "local")

    with pytest.raises(ConfigError):
        auth.auth_mode()

    monkeypatch.setenv(auth.AUTH_MODE_ENV, "required")
    assert auth.auth_mode() == "required"


def test_an_unknown_mode_is_a_configuration_error(monkeypatch):

    monkeypatch.setenv(auth.AUTH_MODE_ENV, "off")

    with pytest.raises(ConfigError):
        auth.auth_mode()


# ------------------------------------------------------------
# Permissions
# ------------------------------------------------------------

def test_reviewers_cannot_do_administrator_things():

    reviewer = Principal("bob", REVIEWER)
    admin = Principal("alice", ADMIN)

    for permission in (
        authz.EXCEPTIONS_DISMISS, authz.EXCEPTIONS_APPROVE, authz.EXCEPTIONS_ASSIGN_ANY,
        authz.HISTORY_RECORD, authz.INGESTION_WRITE, authz.ADMIN_USERS,
        authz.ADMIN_SETTINGS, authz.ADMIN_MAINTENANCE,
    ):
        with pytest.raises(PermissionDenied):
            require(reviewer, permission)

        assert require(admin, permission) is admin

    for permission in (authz.EXCEPTIONS_VIEW, authz.EXCEPTIONS_UPDATE, authz.HISTORY_VIEW,
                       authz.REPORTS_DOWNLOAD, authz.INGESTION_VIEW):
        assert require(reviewer, permission) is reviewer


def test_no_principal_and_unknown_roles_have_no_permissions():

    with pytest.raises(PermissionDenied):
        require(None, authz.EXCEPTIONS_VIEW)

    with pytest.raises(PermissionDenied):
        require(Principal("eve", "root"), authz.EXCEPTIONS_VIEW)
