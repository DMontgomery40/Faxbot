"""Current configuration access on one configuration/access transaction.

Read results are internal snapshots, never authority for a later operation.
Write preparation authorizes only the entry operation and editor revision;
the canonical manager still rechecks the complete change at final commit.
"""
from contextlib import contextmanager
from datetime import datetime, timezone

from ..config_store import (ConfigurationConflict, ConfigurationNotInitialized,
    ConfigurationRevision, ConfigurationSnapshot, ConfigurationStore, ConfigurationStoreError)
from .mutation_types import MutationDeniedError, MutationReason
from .policy import AccessControl
from .types import AuthenticationError, DecisionReason, InvalidTransactionError, ResourceRef, StaleCredentialError


class AuthorizedConfiguration:
    def __init__(self, configuration, control, *, clock=None):
        if (not isinstance(configuration, ConfigurationStore) or not isinstance(control, AccessControl)
                or control.store is not configuration.access_store):
            raise InvalidTransactionError()
        self.configuration, self.control = configuration, control
        self.store = configuration.access_store
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc).replace(tzinfo=None)

    def _require_pairing(self):
        if (self.configuration.access_store is not self.store
                or self.control.store is not self.store):
            raise InvalidTransactionError()

    @contextmanager
    def _transaction(self):
        self._require_pairing()
        with self.configuration._locked() as connection:
            self.store.lock_on(connection)
            self._require_pairing()
            yield connection, self._clock()

    def _authorize_on(self, connection, actor, permission, now):
        """Catch only source authentication failure, before configuration work."""
        try:
            decision = self.control.authorize_on(connection, actor, permission,
                ResourceRef('installation'), now=now)
        except AuthenticationError:
            return (None, None, None), StaleCredentialError()
        attribution = (actor.principal_id, getattr(actor.credential, 'binding_id', None),
            getattr(actor.credential, 'session_id', None))
        if not decision.allowed:
            reason = (MutationReason.RESET_REQUIRED if decision.reason == DecisionReason.RESET_REQUIRED
                else MutationReason.FORBIDDEN)
            return attribution, MutationDeniedError(reason)
        return attribution, None

    def _read(self, actor, permission):
        with self._transaction() as (connection, now):
            _, error = self._authorize_on(connection, actor, permission, now)
            if error is not None:
                raise error
            head = self.configuration._head(connection)
            if head is None:
                raise ConfigurationNotInitialized('Configuration has not been initialized.')
            return self.configuration._snapshot(connection, self.configuration._cipher(), head)

    def settings(self, actor) -> ConfigurationSnapshot:
        return self._read(actor, 'settings:read')

    def providers(self, actor) -> ConfigurationSnapshot:
        return self._read(actor, 'providers:read')

    def _prepare_write(self, actor, expected, expected_revision_id, *, permission, operation):
        error = None
        with self._transaction() as (connection, now):
            before = self.store.require_lock_on(connection)
            attribution, error = self._authorize_on(connection, actor, permission, now)
            if error is None:
                if type(expected) is not ConfigurationSnapshot:
                    raise ConfigurationStoreError('Invalid configuration editor snapshot.')
                try:
                    if expected_revision_id is not None and expected_revision_id != expected.desired.id:
                        raise ConfigurationConflict('Configuration changed; reload before applying edits.')
                    current, _ = self.configuration._checked_current_on(connection, expected)
                except ConfigurationConflict as conflict:
                    error = conflict
                else:
                    if operation == 'providers.configure' and not current.active.values.feature_v3_plugins:
                        error = MutationDeniedError(MutationReason.FORBIDDEN)
            if error is not None:
                reason = ('configuration_conflict' if isinstance(error, ConfigurationConflict)
                    else 'credential_stale' if isinstance(error, StaleCredentialError)
                    else error.reason.value)
                after = self.store.require_lock_on(connection)
                self.configuration._audit_configuration_on(connection, attribution, operation,
                    before, after, True, {'reason': reason}, now)
        # A routine denial has a committed audit. Internal/late failures instead
        # unwind the transaction, including failures while inserting that audit.
        if error is not None:
            raise error

    def prepare_settings_write(self, actor, expected, expected_revision_id=None) -> None:
        self._prepare_write(actor, expected, expected_revision_id,
            permission='settings:write', operation='settings.update')

    def prepare_provider_write(self, actor, expected, expected_revision_id=None) -> None:
        self._prepare_write(actor, expected, expected_revision_id,
            permission='providers:write', operation='providers.configure')


def _receipt_snapshot(snapshot):
    if (type(snapshot) is not ConfigurationSnapshot
            or type(snapshot.generation) is not int or snapshot.generation < 1
            or type(snapshot.active) is not ConfigurationRevision
            or snapshot.pending is not None and type(snapshot.pending) is not ConfigurationRevision):
        raise ValueError('Invalid configuration write receipt snapshot.')
    for identity in (snapshot.installation_id, snapshot.active.id, snapshot.desired.id):
        if (type(identity) is not str or not 0 < len(identity) <= 40
                or any(not 32 <= ord(character) < 127 for character in identity)):
            raise ValueError('Invalid configuration write receipt snapshot.')


def configuration_write_receipt(before, after) -> dict:
    """Closed acknowledgement of a confirmed canonical write, without a read."""
    _receipt_snapshot(before)
    _receipt_snapshot(after)
    pending = after.pending is not None
    return {'ok': True, 'changed': before.generation != after.generation, '_meta': {
        'active_revision_id': after.active.id, 'desired_revision_id': after.desired.id,
        'generation': after.generation, 'apply_state': 'pending_restart' if pending else 'applied',
        'restart_recommended': pending}}
