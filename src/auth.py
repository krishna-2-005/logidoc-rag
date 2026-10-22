"""
LogiDoc-RAG authentication

A small user and session store (SQLite, separate from the audit,
review and history databases) with salted scrypt password hashes,
account lockout, expiring sessions and an audit trail of account
events. It uses only the standard library.

Modes (environment variable LOGIDOC_AUTH_MODE):

    local      (default) a single-user development mode. Nobody signs
               in; every action is taken by the "local-dev" principal,
               which is recorded as NOT authenticated. Use it on a
               developer machine only.
    required   every use needs a signed-in account. Users are created
               with scripts/manage_users.py; the app has no
               self-registration. If no active administrator exists,
               nobody can sign in and the app says so.

LOGIDOC_ENV=production refuses to start in local mode (ConfigError), so
a production deployment cannot silently run without sign-in by leaving
the variable out.

What this does and does not protect (also in the README):

    - Passwords are never stored: only "scrypt$n$r$p$salt$hash".
    - Session tokens are random, and only their SHA-256 is stored, so a
      copy of the database cannot be replayed as a session.
    - A session ends after an idle timeout and an absolute lifetime, on
      logout, and as soon as the account is deactivated.
    - Five wrong passwords lock the account for 15 minutes. Unknown
      user names take the same time as wrong passwords.
    - It does NOT provide transport security. Serve the app only over
      HTTPS (a reverse proxy), or the password and token cross the
      network in clear text.
    - It does not stop someone with write access to the database file or
      the server, who can create an admin account directly.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from authz import ADMIN, ROLES, Principal, PermissionDenied, require, ADMIN_USERS


AUTH_MODE_ENV = "LOGIDOC_AUTH_MODE"
ENVIRONMENT_ENV = "LOGIDOC_ENV"

MODE_LOCAL = "local"
MODE_REQUIRED = "required"

SCHEMA_VERSION = 1

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_BYTES = 32

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

MAX_FAILED_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)

IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_LIFETIME = timedelta(hours=8)

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

LOCAL_USERNAME = "local-dev"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('admin', 'reviewer')),
    active              INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    failed_attempts     INTEGER NOT NULL DEFAULT 0,
    locked_until        TEXT,
    created_at          TEXT NOT NULL,
    password_changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event      TEXT NOT NULL,
    username   TEXT,
    actor      TEXT,
    detail     TEXT
);
"""


class AuthError(ValueError):
    """
    A sign-in, session or account change that is refused. The message is
    safe to show to the person using the app.
    """


class ConfigError(RuntimeError):
    """
    The authentication configuration is unsafe or incomplete.
    """


# ============================================================
# MODE
# ============================================================

def auth_mode(environ=None):
    """
    "local" or "required", from LOGIDOC_AUTH_MODE. Raises ConfigError
    for an unknown value, and for local mode when LOGIDOC_ENV is
    "production".
    """

    environ = os.environ if environ is None else environ

    mode = (environ.get(AUTH_MODE_ENV) or MODE_LOCAL).strip().lower()

    if mode not in (MODE_LOCAL, MODE_REQUIRED):
        raise ConfigError(
            f"{AUTH_MODE_ENV} must be '{MODE_LOCAL}' or '{MODE_REQUIRED}', not {mode!r}."
        )

    production = (environ.get(ENVIRONMENT_ENV) or "").strip().lower() == "production"

    if mode == MODE_LOCAL and production:
        raise ConfigError(
            f"{ENVIRONMENT_ENV}=production needs {AUTH_MODE_ENV}={MODE_REQUIRED}; the "
            "no-login development mode is not allowed in production."
        )

    return mode


def local_principal(environ=None):
    """
    The development principal of local mode: an administrator who is not
    authenticated. Raises ConfigError in required mode.
    """

    if auth_mode(environ) != MODE_LOCAL:
        raise ConfigError("Local development access is disabled: authentication is required.")

    return Principal(LOCAL_USERNAME, ADMIN, authenticated=False, user_id=None)


# ============================================================
# PASSWORDS
# ============================================================

def b64(data):

    return base64.b64encode(data).decode("ascii")


def hash_password(password):
    """
    "scrypt$n$r$p$salt$hash" with a fresh random salt. Raises AuthError
    for a password that is too short or too long.
    """

    check_password_policy(password)

    salt = secrets.token_bytes(16)

    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_KEY_BYTES,
    )

    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(key)}"


def verify_password(password, stored):
    """
    True when password matches the stored hash. Never raises for a
    malformed hash or password: those just do not match.
    """

    try:

        scheme, n, r, p, salt, key = stored.split("$")

        if scheme != "scrypt":
            return False

        expected = base64.b64decode(key)

        actual = hashlib.scrypt(
            str(password).encode("utf-8"), salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=128 * int(n) * int(r) * 2,
        )

        return hmac.compare_digest(actual, expected)

    except Exception:
        return False


def check_password_policy(password):

    if not isinstance(password, str):
        raise AuthError("The password must be text.")

    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")

    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"The password must have at most {MAX_PASSWORD_LENGTH} characters.")

    if len(set(password)) < 5:
        raise AuthError("The password is too repetitive.")


# Verified against when the user name is unknown, so the time taken does
# not tell whether an account exists
DUMMY_HASH = hash_password("not-a-real-password-" + "x" * 8)


def normalize_username(username):

    name = str(username or "").strip().lower()

    if not USERNAME.match(name):
        raise AuthError(
            "A user name has 3 to 64 characters: lower-case letters, digits, '.', '_' or '-', "
            "starting with a letter or digit."
        )

    if name == LOCAL_USERNAME:
        raise AuthError(f"'{LOCAL_USERNAME}' is reserved.")

    return name


def token_hash(token):

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def now():

    return datetime.now(timezone.utc)


def stamp(moment):

    return moment.isoformat(timespec="seconds")


def parse(text):

    return datetime.fromisoformat(text)


# ============================================================
# STORE
# ============================================================

class UserStore:

    def __init__(self, path, clock=now):

        self.path = Path(path)
        self.clock = clock
        self.initialize()

    def connect(self):

        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    def initialize(self):

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection:

            with connection:

                connection.executescript(SCHEMA)

                version = connection.execute("SELECT version FROM schema_info").fetchone()

                if version is None:
                    connection.execute(
                        "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
                    )
                elif version["version"] != SCHEMA_VERSION:
                    raise AuthError(
                        f"User database {self.path} has schema version "
                        f"{version['version']}, expected {SCHEMA_VERSION}."
                    )

    # --------------------------------------------------------
    # Accounts
    # --------------------------------------------------------

    def event(self, connection, event, username=None, actor=None, detail=None):

        connection.execute(
            "INSERT INTO auth_events (created_at, event, username, actor, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (stamp(self.clock()), event, username, actor, detail),
        )

    def create_user(self, username, password, role, actor=None):
        """
        Creates an account. actor is the administrator Principal making
        the change, or None when the server operator does it from the
        command line (scripts/manage_users.py).
        """

        if actor is not None:
            require(actor, ADMIN_USERS)

        name = normalize_username(username)

        if role not in ROLES:
            raise AuthError(f"Unknown role: {role!r}.")

        password_hash = hash_password(password)
        at = stamp(self.clock())

        with closing(self.connect()) as connection:

            with connection:

                try:
                    connection.execute(
                        "INSERT INTO users (username, password_hash, role, created_at, "
                        "password_changed_at) VALUES (?, ?, ?, ?, ?)",
                        (name, password_hash, role, at, at),
                    )
                except sqlite3.IntegrityError:
                    raise AuthError(f"The user '{name}' already exists.") from None

                self.event(connection, "user_created", name,
                           actor.username if actor else "operator", f"role={role}")

        return name

    def users(self, actor=None):
        """
        Accounts without their password hashes.
        """

        if actor is not None:
            require(actor, ADMIN_USERS)

        with closing(self.connect()) as connection:

            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id, username, role, active, failed_attempts, locked_until, "
                    "created_at, password_changed_at FROM users ORDER BY username"
                )
            ]

    def active_admins(self, connection):

        return connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
        ).fetchone()[0]

    def change_user(self, username, actor=None, role=None, active=None, password=None):
        """
        Changes the role, the active flag or the password of an account.
        The last active administrator can be neither demoted nor
        deactivated. Deactivating an account, changing its role or its
        password ends its sessions.
        """

        if actor is not None:
            require(actor, ADMIN_USERS)

        name = normalize_username(username)

        if role is not None and role not in ROLES:
            raise AuthError(f"Unknown role: {role!r}.")

        new_hash = hash_password(password) if password is not None else None

        who = actor.username if actor else "operator"
        at = stamp(self.clock())

        with closing(self.connect()) as connection:

            with connection:

                row = connection.execute(
                    "SELECT * FROM users WHERE username = ?", (name,)
                ).fetchone()

                if row is None:
                    raise AuthError(f"There is no user '{name}'.")

                loses_admin = (
                    row["role"] == ADMIN and row["active"] == 1
                    and (role not in (None, ADMIN) or active is False or active == 0)
                )

                if loses_admin and self.active_admins(connection) <= 1:
                    raise AuthError("The last active administrator cannot be demoted or deactivated.")

                end_sessions = False

                if role is not None and role != row["role"]:
                    connection.execute("UPDATE users SET role = ? WHERE id = ?", (role, row["id"]))
                    self.event(connection, "role_changed", name, who, f"{row['role']} -> {role}")
                    end_sessions = True

                if active is not None and int(bool(active)) != row["active"]:
                    connection.execute(
                        "UPDATE users SET active = ? WHERE id = ?", (int(bool(active)), row["id"])
                    )
                    self.event(connection, "activated" if active else "deactivated", name, who)
                    end_sessions = end_sessions or not active

                if new_hash is not None:
                    connection.execute(
                        "UPDATE users SET password_hash = ?, password_changed_at = ?, "
                        "failed_attempts = 0, locked_until = NULL WHERE id = ?",
                        (new_hash, at, row["id"]),
                    )
                    self.event(connection, "password_changed", name, who)
                    end_sessions = True

                if end_sessions:
                    connection.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))

    def events(self, actor=None, limit=200):

        if actor is not None:
            require(actor, ADMIN_USERS)

        with closing(self.connect()) as connection:

            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM auth_events ORDER BY id DESC LIMIT ?", (int(limit),)
                )
            ]

    def has_active_admin(self):

        with closing(self.connect()) as connection:

            return self.active_admins(connection) > 0

    # --------------------------------------------------------
    # Sign in and sessions
    # --------------------------------------------------------

    def login(self, username, password):
        """
        Checks the credentials and opens a session. Returns
        {"token", "principal", "expires_at"}. Raises AuthError with the
        same message for an unknown user, a wrong password and an
        inactive account, so the answer does not reveal which accounts
        exist; a locked account says so (after the lockout starts).
        """

        generic = AuthError("Wrong user name or password.")

        try:
            name = normalize_username(username)
        except AuthError:
            verify_password(str(password), DUMMY_HASH)
            raise generic from None

        moment = self.clock()
        failure = None

        with closing(self.connect()) as connection:

            # Failures are recorded (counter, lockout, event) and
            # committed before the error is raised: raising inside the
            # transaction would roll the record back
            with connection:

                row = connection.execute(
                    "SELECT * FROM users WHERE username = ?", (name,)
                ).fetchone()

                if row is None:
                    verify_password(str(password), DUMMY_HASH)
                    self.event(connection, "login_failed", name, detail="unknown user")
                    failure = generic

                elif row["locked_until"] and parse(row["locked_until"]) > moment:
                    verify_password(str(password), DUMMY_HASH)
                    self.event(connection, "login_blocked", name, detail="account locked")
                    failure = AuthError(
                        "Too many failed sign-ins. The account is locked for a while; try again later."
                    )

                else:

                    correct = verify_password(str(password), row["password_hash"])

                    if not correct or not row["active"]:

                        attempts = row["failed_attempts"] + 1
                        locked_until = None

                        if attempts >= MAX_FAILED_ATTEMPTS:
                            locked_until = stamp(moment + LOCKOUT)
                            attempts = 0

                        connection.execute(
                            "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                            (attempts, locked_until, row["id"]),
                        )

                        self.event(connection, "login_failed", name,
                                   detail="inactive account" if correct else "wrong password")
                        failure = generic

                    else:

                        connection.execute(
                            "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
                            (row["id"],),
                        )

                        token = secrets.token_urlsafe(32)
                        expires = moment + SESSION_LIFETIME

                        connection.execute(
                            "INSERT INTO sessions (token_hash, user_id, created_at, last_seen, "
                            "expires_at) VALUES (?, ?, ?, ?, ?)",
                            (token_hash(token), row["id"], stamp(moment), stamp(moment),
                             stamp(expires)),
                        )

                        self.event(connection, "login", name)

        if failure is not None:
            raise failure

        return {
            "token": token,
            "principal": Principal(row["username"], row["role"], True, row["id"]),
            "expires_at": stamp(expires),
        }

    def principal_for(self, token):
        """
        The Principal of a session token, or None when the token is
        unknown, expired, idle for too long, or its account is no longer
        active. A valid check renews the idle timer. Expired sessions
        are deleted.
        """

        if not token:
            return None

        moment = self.clock()

        with closing(self.connect()) as connection:

            with connection:

                row = connection.execute(
                    "SELECT sessions.*, users.username, users.role, users.active "
                    "FROM sessions JOIN users ON users.id = sessions.user_id "
                    "WHERE sessions.token_hash = ?",
                    (token_hash(token),),
                ).fetchone()

                if row is None:
                    return None

                idle = moment - parse(row["last_seen"])

                if (
                    not row["active"]
                    or parse(row["expires_at"]) <= moment
                    or idle > IDLE_TIMEOUT
                ):
                    connection.execute(
                        "DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],)
                    )
                    return None

                connection.execute(
                    "UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                    (stamp(moment), row["token_hash"]),
                )

        return Principal(row["username"], row["role"], True, row["user_id"])

    def logout(self, token):

        if not token:
            return

        with closing(self.connect()) as connection:

            with connection:

                row = connection.execute(
                    "SELECT users.username FROM sessions JOIN users ON users.id = sessions.user_id "
                    "WHERE token_hash = ?", (token_hash(token),)
                ).fetchone()

                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

                if row:
                    self.event(connection, "logout", row["username"])

    def purge_expired(self, actor=None):
        """
        Deletes expired and idle sessions (maintenance). Returns how many.
        """

        if actor is not None:
            require(actor, ADMIN_USERS)

        moment = self.clock()
        idle_limit = stamp(moment - IDLE_TIMEOUT)

        with closing(self.connect()) as connection:

            with connection:

                cursor = connection.execute(
                    "DELETE FROM sessions WHERE expires_at <= ? OR last_seen < ?",
                    (stamp(moment), idle_limit),
                )

                return cursor.rowcount


def current_principal(store, token, environ=None):
    """
    The acting Principal for a request or a browser session:

        required mode   the signed-in user of the token, or None
        local mode      the development principal (never authenticated)
    """

    if auth_mode(environ) == MODE_LOCAL:
        return local_principal(environ)

    return store.principal_for(token) if store is not None else None


__all__ = [
    "AuthError", "ConfigError", "UserStore", "PermissionDenied", "auth_mode",
    "local_principal", "current_principal", "hash_password", "verify_password",
    "MODE_LOCAL", "MODE_REQUIRED",
]
