"""
Manage LogiDoc-RAG user accounts from the command line.

The web application has no self-registration: accounts are created here,
by whoever runs the server. Passwords are read from a prompt (or, for
automation, from the environment variable named by --password-env). They
are never accepted as command-line arguments, which would leave them in
shell history and process lists, and they are stored only as salted scrypt
hashes.

    python scripts/manage_users.py create alice --role admin
    python scripts/manage_users.py create bob --role reviewer
    python scripts/manage_users.py list
    python scripts/manage_users.py set-role bob admin
    python scripts/manage_users.py deactivate bob
    python scripts/manage_users.py activate bob
    python scripts/manage_users.py reset-password bob
    python scripts/manage_users.py events

The user database is config.AUTH_DB_PATH (data/auth.sqlite3, or
LOGIDOC_AUTH_DB).
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config
from auth import AuthError, UserStore
from authz import ROLES


def read_password(arguments):
    """
    The new password: from the environment variable named by
    --password-env, otherwise asked for twice.
    """

    if arguments.password_env:

        password = os.environ.get(arguments.password_env)

        if not password:
            raise AuthError(f"The environment variable {arguments.password_env} is not set.")

        return password

    first = getpass.getpass("Password: ")

    if first != getpass.getpass("Repeat the password: "):
        raise AuthError("The passwords do not match.")

    return first


def main(argv=None):

    parser = argparse.ArgumentParser(description="Manage LogiDoc-RAG user accounts.")
    parser.add_argument("--database", default=None, help="User database file (default: config.AUTH_DB_PATH)")

    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create an account")
    create.add_argument("username")
    create.add_argument("--role", choices=ROLES, required=True)
    create.add_argument("--password-env", help="Read the password from this environment variable")

    commands.add_parser("list", help="List the accounts")
    commands.add_parser("events", help="Show the latest account events")

    for name, help_text in (("activate", "Activate an account"), ("deactivate", "Deactivate an account")):
        commands.add_parser(name, help=help_text).add_argument("username")

    role = commands.add_parser("set-role", help="Change the role of an account")
    role.add_argument("username")
    role.add_argument("role", choices=ROLES)

    reset = commands.add_parser("reset-password", help="Set a new password (ends the account's sessions)")
    reset.add_argument("username")
    reset.add_argument("--password-env", help="Read the password from this environment variable")

    arguments = parser.parse_args(argv)

    store = UserStore(arguments.database or config.AUTH_DB_PATH)

    try:

        if arguments.command == "create":
            name = store.create_user(arguments.username, read_password(arguments), arguments.role)
            print(f"Created {name} ({arguments.role}).")

        elif arguments.command == "list":
            for user in store.users():
                state = "active" if user["active"] else "INACTIVE"
                print(f"{user['username']:<24} {user['role']:<9} {state:<9} created {user['created_at']}")

        elif arguments.command == "events":
            for event in store.events(limit=50):
                print(f"{event['created_at']}  {event['event']:<16} {event['username'] or '-':<20} "
                      f"{event['actor'] or '-':<12} {event['detail'] or ''}")

        elif arguments.command in ("activate", "deactivate"):
            store.change_user(arguments.username, active=arguments.command == "activate")
            print(f"{arguments.username}: {arguments.command}d.")

        elif arguments.command == "set-role":
            store.change_user(arguments.username, role=arguments.role)
            print(f"{arguments.username} is now {arguments.role}; their sessions were ended.")

        elif arguments.command == "reset-password":
            store.change_user(arguments.username, password=read_password(arguments))
            print(f"Password changed for {arguments.username}; their sessions were ended.")

    except AuthError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
