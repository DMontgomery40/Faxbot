"""Current human authority composed with durable configuration-owned acceptance.

Configuration lock precedes access lock. Provider/background delivery work keeps
its captured state and does not use this human-facing authorization adapter.
"""

from datetime import datetime, timezone
from contextlib import contextmanager

from ..request_identity import (
    RequestIdentity,
    find_scoped_request,
    IdempotencyConflict,
    IdempotentReplay,
)
from .fax_resources import FaxAccessError
from .types import InvalidTransactionError


class AuthorizedOutbound:
    def __init__(self, configuration, resources, *, clock=None):
        if resources.store is not configuration.access_store:
            raise InvalidTransactionError()
        self.configuration, self.resources = configuration, resources
        self.store = configuration.access_store
        self._clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    @contextmanager
    def _transaction(self, actor):
        with self.configuration._locked() as connection:
            self.store.lock_on(connection)
            now = self._clock()
            self.resources.authorize_send_on(connection, actor, now=now)
            yield connection, now

    def _candidate(self, connection, actor, identity, now):
        if type(identity) is not RequestIdentity or identity.principal_scope != actor.replay_scope:
            raise FaxAccessError("invalid_input")
        row = find_scoped_request(
            connection, self.configuration.delivery_tables["outbound_deliveries"], identity
        )
        if row is not None:
            # Visibility precedes fingerprint conflict and captured-limit output.
            self.resources.require_outbound_on(connection, actor, row["id"], "fax:read", now=now)
        return row

    def check_send(self, actor):
        """Reject before conversion work; acceptance still rechecks on its lock."""
        with self._transaction(actor):
            pass

    def replay_max_bytes(self, actor, identity):
        with self._transaction(actor) as (connection, now):
            row = self._candidate(connection, actor, identity, now)
            if row is None:
                return None
            revision, _ = self.configuration._outbound_context(connection, row["id"])
            return revision.values.max_file_size_mb * 1024 * 1024

    def replay_values(self, actor, identity):
        """The configuration this scoped key's accepted request was resolved under.

        Same authorization and visibility as ``replay_max_bytes``; None when the
        key has no accepted request.
        """
        with self._transaction(actor) as (connection, now):
            row = self._candidate(connection, actor, identity, now)
            if row is None:
                return None
            revision, _ = self.configuration._outbound_context(connection, row["id"])
            return revision.values

    def find_replay(self, actor, identity):
        with self._transaction(actor) as (connection, now):
            row = self._candidate(connection, actor, identity, now)
            if row is None:
                return None
            if not identity.matches(row["request_fingerprint"]):
                raise IdempotencyConflict()
            return row["id"]

    def accept(self, actor, revision, job, *, request_identity=None, also=None):
        """``also(connection, now)`` runs last in the same transaction (a fax that waits to go with others)."""
        with self._transaction(actor) as (connection, now):
            if request_identity is not None:
                row = self._candidate(connection, actor, request_identity, now)
                if row is not None:
                    if not request_identity.matches(row["request_fingerprint"]):
                        raise IdempotencyConflict()
                    raise IdempotentReplay(row["id"])
            profile = self.configuration._accept_outbound_on(
                connection, revision, job, request_identity=request_identity
            )
            self.resources.record_outbound_on(connection, actor, job["id"], now=now)
            if also is not None:
                also(connection, now)
            return profile
