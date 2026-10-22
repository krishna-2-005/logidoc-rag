"""
LogiDoc-RAG exception workflow service

The only way the application changes the review state of an exception.
It combines three things:

    who may do it     authz permissions of the acting Principal
    what is allowed   the rules of review_store (lifecycle, required
                      notes and evidence, approval), enforced inside
                      the database transaction
    who did it        the Principal's user name, role and whether the
                      user signed in, stored on every event

Lifecycle

    Open -> In review -> Resolved
                      -> Dismissed
    Resolved / Dismissed -> Open (reopen)

    With approval switched on (review_store.Policy.require_approval) a
    resolution or dismissal waits for an administrator, who approves it
    or rejects it back to review.

Permissions

    reviewer   view; assign to self; start; note; resolve; reopen
    admin      everything: also assign to anyone, dismiss, approve or
               reject, and verify the history chain

    An exception assigned to someone else can only be started or
    resolved by its assignee or an administrator.

The workflow records what people did. It never changes a reconciliation
result: resolving an exception does not alter the audit that found it,
and the next audit that finds it again reopens it if the audited values
changed (review_store.sync).
"""

import authz
from authz import PermissionDenied, Principal, require
from review_store import (
    Actor,
    DISMISSED,
    EVENT_APPROVE,
    EVENT_ASSIGN,
    EVENT_DISMISS,
    EVENT_NOTE,
    EVENT_REJECT,
    EVENT_REOPEN,
    EVENT_RESOLVE,
    EVENT_START,
    OPEN,
    RESOLVED,
    ReviewError,
)


def actor_of(principal):

    return Actor(principal.username, principal.role, bool(principal.authenticated))


class ExceptionWorkflow:

    def __init__(self, store, assignable=None):
        """
        store: a review_store.ReviewStore. assignable: a callable that
        returns the user names exceptions may be assigned to (the
        active accounts in required mode), or None to accept any name
        (local development mode).
        """

        self.store = store
        self.assignable = assignable

    # --------------------------------------------------------
    # Checks
    # --------------------------------------------------------

    def current(self, exception):

        row = self.store.states().get(exception["exception_key"])

        if row is None:
            raise ReviewError(
                "This exception has not been recorded yet; show the exception register first."
            )

        return row

    def check_assignee(self, principal, assignee):

        name = (assignee or "").strip()

        if name == principal.username:
            require(principal, authz.EXCEPTIONS_ASSIGN_SELF)
        else:
            require(principal, authz.EXCEPTIONS_ASSIGN_ANY)

        if self.assignable is not None and name not in set(self.assignable()):
            raise ReviewError(f"'{name}' is not an active user, so the exception cannot be assigned to them.")

        return name

    def check_owner(self, principal, row):
        """
        Starting or resolving an assigned exception belongs to its
        assignee, or to someone who may assign exceptions to anyone.
        """

        owner = row.get("assignee")

        if owner and owner != principal.username and not principal.can(authz.EXCEPTIONS_ASSIGN_ANY):
            raise PermissionDenied(f"This exception is assigned to {owner}.")

    # --------------------------------------------------------
    # Actions
    # --------------------------------------------------------

    def assign(self, principal, exception, assignee, note=None):

        require(principal, authz.EXCEPTIONS_UPDATE)
        name = self.check_assignee(principal, assignee)

        return self.store.apply(exception, EVENT_ASSIGN, actor_of(principal),
                                note=note, assignee=name)

    def start(self, principal, exception, assignee=None, note=None):
        """
        Open -> In review. Without an assignee the exception is
        assigned to the person starting the review.
        """

        require(principal, authz.EXCEPTIONS_UPDATE)

        row = self.current(exception)
        self.check_owner(principal, row)

        name = self.check_assignee(principal, assignee or row.get("assignee") or principal.username)

        return self.store.apply(exception, EVENT_START, actor_of(principal),
                                note=note, assignee=name)

    def add_note(self, principal, exception, note=None, evidence=()):

        require(principal, authz.EXCEPTIONS_UPDATE)
        self.current(exception)

        return self.store.apply(exception, EVENT_NOTE, actor_of(principal),
                                note=note, evidence=evidence)

    def resolve(self, principal, exception, note, evidence=()):

        require(principal, authz.EXCEPTIONS_UPDATE)

        row = self.current(exception)
        self.check_owner(principal, row)

        return self.store.apply(exception, EVENT_RESOLVE, actor_of(principal),
                                note=note, evidence=evidence)

    def dismiss(self, principal, exception, note, evidence=()):

        require(principal, authz.EXCEPTIONS_DISMISS)
        self.current(exception)

        return self.store.apply(exception, EVENT_DISMISS, actor_of(principal),
                                note=note, evidence=evidence)

    def reopen(self, principal, exception, note):

        require(principal, authz.EXCEPTIONS_REOPEN)
        self.current(exception)

        return self.store.apply(exception, EVENT_REOPEN, actor_of(principal), note=note)

    def approve(self, principal, exception, note=None):

        require(principal, authz.EXCEPTIONS_APPROVE)
        self.current(exception)

        return self.store.apply(exception, EVENT_APPROVE, actor_of(principal), note=note)

    def reject(self, principal, exception, note):

        require(principal, authz.EXCEPTIONS_APPROVE)
        self.current(exception)

        return self.store.apply(exception, EVENT_REJECT, actor_of(principal), note=note)

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    def allowed_actions(self, principal, row):
        """
        The workflow actions the principal could take now on a stored
        review row (for showing only the buttons that can work). The
        store still enforces every rule when an action is taken.
        """

        if not isinstance(principal, Principal):
            return []

        owner = row.get("assignee")
        mine = not owner or owner == principal.username or principal.can(authz.EXCEPTIONS_ASSIGN_ANY)
        status = row["status"]
        actions = []

        if principal.can(authz.EXCEPTIONS_UPDATE):

            if status in (OPEN, "in_review"):
                actions.append(EVENT_ASSIGN)

            if status == OPEN and mine:
                actions.append(EVENT_START)

            actions.append(EVENT_NOTE)

            if status == "in_review" and mine:
                actions.append(EVENT_RESOLVE)

        if status in (OPEN, "in_review") and principal.can(authz.EXCEPTIONS_DISMISS):
            actions.append(EVENT_DISMISS)

        if status in (RESOLVED, DISMISSED) and principal.can(authz.EXCEPTIONS_REOPEN):
            actions.append(EVENT_REOPEN)

        if row.get("approval_state") == "pending" and principal.can(authz.EXCEPTIONS_APPROVE):
            actions.extend([EVENT_APPROVE, EVENT_REJECT])

        return actions

    def queue(self, principal, **filters):
        """
        Stored review rows with the filters of ReviewStore.list_reviews
        (assignee, unassigned, statuses, categories, min_age_days,
        max_age_days, pending_approval, search).
        """

        require(principal, authz.EXCEPTIONS_VIEW)

        return self.store.list_reviews(**filters)

    def history(self, principal, exception_key):

        require(principal, authz.EXCEPTIONS_VIEW)

        return self.store.history(exception_key)

    def activity(self, principal, limit=200):

        require(principal, authz.EXCEPTIONS_VIEW)

        return self.store.all_events(limit)

    def verify_history(self, principal):

        require(principal, authz.ADMIN_MAINTENANCE)

        return self.store.verify_integrity()
