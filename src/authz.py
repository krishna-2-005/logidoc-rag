"""
LogiDoc-RAG roles and permissions

Two roles:

    admin      manages users, settings, the audit history, document
               ingestion and database maintenance, and approves or
               dismisses exceptions
    reviewer   (auditor) works the exception queue: sees everything,
               assigns exceptions to themself, reviews, adds notes and
               evidence, resolves, reopens and downloads reports

A Principal is who is acting. Every operation that changes something
takes a Principal and calls require() itself, so a screen or an API
route that forgets to hide a button still cannot do the action.

Two kinds of principal exist:

    authenticated   a user who signed in (auth.UserStore.login)
    local           the single-user development principal
                    (auth.local_principal). It is never authenticated:
                    its actions are recorded with authenticated = False,
                    and it only exists when LOGIDOC_AUTH_MODE is not
                    "required".
"""

from dataclasses import dataclass


ADMIN = "admin"
REVIEWER = "reviewer"

ROLES = (ADMIN, REVIEWER)

ROLE_LABELS = {ADMIN: "Administrator", REVIEWER: "Auditor / reviewer"}

# Permission names
EXCEPTIONS_VIEW = "exceptions.view"
EXCEPTIONS_ASSIGN_SELF = "exceptions.assign_self"
EXCEPTIONS_ASSIGN_ANY = "exceptions.assign_any"
EXCEPTIONS_UPDATE = "exceptions.update"
EXCEPTIONS_DISMISS = "exceptions.dismiss"
EXCEPTIONS_APPROVE = "exceptions.approve"
EXCEPTIONS_REOPEN = "exceptions.reopen"

HISTORY_VIEW = "history.view"
HISTORY_RECORD = "history.record"

REPORTS_DOWNLOAD = "reports.download"

INGESTION_VIEW = "ingestion.view"
INGESTION_WRITE = "ingestion.write"

EXTRACTION_VIEW = "extraction.view"
EXTRACTION_CORRECT = "extraction.correct"
EXTRACTION_RUN = "extraction.run"
EXTRACTION_APPROVE = "extraction.approve"

ADMIN_USERS = "admin.users"
ADMIN_SETTINGS = "admin.settings"
ADMIN_MAINTENANCE = "admin.maintenance"

REVIEWER_PERMISSIONS = frozenset({
    EXCEPTIONS_VIEW, EXCEPTIONS_ASSIGN_SELF, EXCEPTIONS_UPDATE, EXCEPTIONS_REOPEN,
    HISTORY_VIEW, REPORTS_DOWNLOAD, INGESTION_VIEW, EXTRACTION_VIEW, EXTRACTION_CORRECT,
})

ADMIN_PERMISSIONS = REVIEWER_PERMISSIONS | frozenset({
    EXCEPTIONS_ASSIGN_ANY, EXCEPTIONS_DISMISS, EXCEPTIONS_APPROVE,
    HISTORY_RECORD, INGESTION_WRITE, EXTRACTION_RUN, EXTRACTION_APPROVE,
    ADMIN_USERS, ADMIN_SETTINGS, ADMIN_MAINTENANCE,
})

PERMISSIONS = {ADMIN: ADMIN_PERMISSIONS, REVIEWER: REVIEWER_PERMISSIONS}


class PermissionDenied(PermissionError):
    """
    The principal is missing, not signed in, or lacks the permission.
    """


@dataclass(frozen=True)
class Principal:
    """
    Who is acting. username is the account name for a signed-in user
    and "local-dev" for the development principal.
    """

    username: str
    role: str
    authenticated: bool = True
    user_id: int | None = None

    def can(self, permission):

        return permission in PERMISSIONS.get(self.role, frozenset())

    @property
    def label(self):

        return self.username if self.authenticated else f"{self.username} (not authenticated)"


def require(principal, permission):
    """
    Raise PermissionDenied unless principal holds permission. Returns
    the principal, so a call can be written inline.
    """

    if not isinstance(principal, Principal):
        raise PermissionDenied("Sign in to continue.")

    if not principal.can(permission):
        raise PermissionDenied(
            f"The {ROLE_LABELS.get(principal.role, principal.role)} role "
            f"cannot perform this action ({permission})."
        )

    return principal
