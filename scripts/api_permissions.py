"""
Writes the API's permission inventory, docs/API_PERMISSIONS.md: for every
operation of /api (each one is also served as /api/v1), whether it needs a
sign-in, the permission it needs and so the roles that may call it. It is read
from the routes themselves (each route's dependencies), never typed by hand,
apart from the checks a handler makes on top (HANDLER_CHECKS).

tests/test_api_authorization.py fails when the published file and the code
disagree, and calls every operation as nobody, as a reviewer and as an
administrator, so a permission cannot be dropped or loosened unnoticed.

Run from the project root:
    python scripts/api_permissions.py            # write the file
    python scripts/api_permissions.py --check    # exit 1 when it is out of date
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

TARGET = ROOT / "docs" / "API_PERMISSIONS.md"

# Operations open to anyone, with why. Anything else answers 401 without a session.
PUBLIC = {
    ("GET", "/api/health"): "Liveness: answers that the process runs, opens nothing.",
    ("GET", "/api/ready"): "Readiness: status words only, never a path, host or model name.",
    ("GET", "/api/session"): "Says whether the caller is signed in, and as whom; no data.",
    ("POST", "/api/auth/login"): "Sign-in. Failed attempts lock the account (5 failures, 15 minutes) and are rate limited.",
    ("POST", "/api/auth/logout"): "Ends the caller's own session, if any.",
}

# Checks a handler makes on top of what its route declares
HANDLER_CHECKS = {
    ("POST", "/api/exceptions/{key}/actions"): (
        "Then the action's own permission: start, note, resolve, dispute and assign `exceptions.update`; "
        "assigning to oneself `exceptions.assign_self`, to someone else `exceptions.assign_any`; dismiss "
        "`exceptions.dismiss`; waive `exceptions.waive`; reopen `exceptions.reopen`; approve and reject "
        "`exceptions.approve`. A reviewer can work an exception, not dismiss, waive or approve one."
    ),
    ("GET", "/api/reporting/{report}"): "The corrections report also needs `history.view`.",
    ("GET", "/api/reporting/{report}.csv"): "The corrections report also needs `history.view`.",
    ("GET", "/api/reporting/{report}.pdf"): "The corrections report also needs `history.view`.",
    ("GET", "/api/carrier-registry/carriers/{carrier_id}"): "The carrier's change history is included only with `carriers.manage`.",
    ("GET", "/api/settings"): "Any signed-in user: harmless values only (limits and policy switches).",
    ("POST", "/api/shipments/{shipment_id}/tms/resolutions"): (
        "One administrator decides a TMS conflict, with a mandatory reason, recorded in the audit history."
    ),
}

METHOD_ORDER = {"GET": 0, "POST": 1, "PATCH": 2, "PUT": 3, "DELETE": 4}


def dependency_calls(dependant):
    """Every dependency a route resolves, at any depth."""

    for sub in dependant.dependencies:
        yield sub.call
        yield from dependency_calls(sub)


def operations():
    """
    [{method, path, signed_in, permission, roles, note}] for every /api
    operation, sorted by path then method. permission: the one the route
    declares (core.require), or None.
    """

    from fastapi.routing import APIRoute

    import authz
    from api import app as application
    from api.core import get_principal

    found = []
    routes = [route for router in application.ROUTERS for route in router.routes]

    for route in routes:

        if not isinstance(route, APIRoute):
            raise TypeError(f"Unexpected route type {type(route).__name__}")

        calls = list(dependency_calls(route.dependant))
        permissions = sorted({getattr(call, "permission", None) for call in calls} - {None})

        if len(permissions) > 1:
            raise ValueError(f"{route.path_format} declares more than one permission: {permissions}")

        for method in route.methods:
            found.append(operation(method, route.path_format, get_principal in calls,
                                   permissions[0] if permissions else None, authz))

    # Declared on the application itself (src/api/app.py), outside the routers
    for path in ("/api/health", "/api/ready"):
        found.append(operation("GET", path, False, None, authz))

    return sorted(found, key=lambda item: (item["path"], METHOD_ORDER.get(item["method"], 9)))


def operation(method, path, signed_in, permission, authz):

    if permission:
        roles = [role for role in authz.ROLES if permission in authz.PERMISSIONS[role]]
    elif signed_in:
        roles = list(authz.ROLES)
    else:
        roles = None

    return {
        "method": method, "path": path, "signed_in": signed_in, "permission": permission, "roles": roles,
        "note": PUBLIC.get((method, path)) or HANDLER_CHECKS.get((method, path), ""),
    }


def who(item, authz):

    if item["roles"] is None:
        return "Anyone"

    if set(item["roles"]) == set(authz.ROLES):
        return "Administrators and reviewers"

    if item["roles"] == [authz.ADMIN]:
        return "Administrators only"

    return ", ".join(authz.ROLE_LABELS[role] for role in item["roles"])


def area(path):

    return path.split("/")[2]


def document():
    """The inventory as Markdown."""

    import authz

    items = operations()
    lines = [
        "# API permissions",
        "",
        "Who may call each operation of the API. Generated from the routes by `scripts/api_permissions.py`;",
        "`tests/test_api_authorization.py` fails when this file and the code disagree, and calls every operation",
        "without a session, as a reviewer and as an administrator. Do not edit by hand.",
        "",
        "## Rules",
        "",
        "- **Every path is also served as `/api/v1/...`**, the same route with the same permission and the same",
        "  CSRF rule.",
        "- **Without a session:** 401, apart from the operations marked *Anyone*.",
        "- **Without the permission:** 403, before the request body is read or any store is opened.",
        "- **Every POST and PATCH needs the header `X-LogiDoc-Client: web`** (CSRF): 403 without it.",
        "- **Local mode** (`LOGIDOC_AUTH_MODE=local`, for development) runs every request as the local",
        "  administrator, so the server starts in it only on a loopback address, and production refuses it.",
        "",
        "## Roles",
        "",
        "| Permission | " + " | ".join(authz.ROLE_LABELS[role] for role in authz.ROLES) + " |",
        "|---|" + "---|" * len(authz.ROLES),
    ]

    for permission in sorted(set().union(*authz.PERMISSIONS.values())):
        lines.append(f"| `{permission}` | " + " | ".join(
            "yes" if permission in authz.PERMISSIONS[role] else "no" for role in authz.ROLES) + " |")

    counts = {
        "anyone": sum(item["roles"] is None for item in items),
        "signed": sum(item["roles"] is not None and set(item["roles"]) == set(authz.ROLES) for item in items),
        "admin": sum(item["roles"] is not None and set(item["roles"]) != set(authz.ROLES) for item in items),
    }

    lines += [
        "",
        "## Operations",
        "",
        f"{len(items)} operations: {counts['anyone']} open to anyone, {counts['signed']} for administrators and "
        f"reviewers, {counts['admin']} for administrators only.",
    ]

    current = None

    for item in items:

        if area(item["path"]) != current:
            current = area(item["path"])
            lines += ["", f"### {current}", "", "| Method | Path | Who | Permission | Notes |", "|---|---|---|---|---|"]

        permission = f"`{item['permission']}`" if item["permission"] else ("sign-in" if item["signed_in"] else "none")
        lines.append(f"| {item['method']} | `{item['path']}` | {who(item, authz)} | {permission} | {item['note']} |")

    return "\n".join(lines) + "\n"


def main():

    parser = argparse.ArgumentParser(description="Write or check the API permission inventory.")
    parser.add_argument("--check", action="store_true", help="exit 1 when docs/API_PERMISSIONS.md is out of date")
    arguments = parser.parse_args()

    text = document()

    if arguments.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current != text:
            print(f"{TARGET} is out of date: run python scripts/api_permissions.py")
            return 1
        print(f"{TARGET} is up to date")
        return 0

    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {TARGET}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
