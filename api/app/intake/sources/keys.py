"""A sending connector's own key, and the second check on the person who asked.

When an administrator adds an email-to-fax or folder-to-fax connector, Faxbot
issues the connector its own integration key through the access layer
(``issue_integration_key``): a new integration named after the connector, a
fixed role with ``fax:send`` only, and a key whose ceiling is ``fax:send``.
Issuing needs the administrator's own right to manage keys. The token is
sealed into the connector's secrets once the issuing transaction committed,
and never shown. Pausing or removing the connector revokes the key; resuming
issues a new key for the same integration.

Every fax the connector sends authenticates with that key (``header_key``), so
a revoked key stops sending at once, and passes ordinary fax acceptance. For
email to fax the person the sender's address maps to must also hold
``fax:send`` on their own personal resource, checked inside the acceptance
transaction, so the check and the acceptance cannot drift apart.
"""
from datetime import datetime, timezone

import sqlalchemy as sa

from ...access.mutation_types import IntegrationKeyValues, KeyValues, VersionedEntity
from ...access.types import ResourceRef, ScopedPermission


SCOPE = 'fax:send'


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _policy_version(access):
    with access.store.transaction() as connection:
        return access.store.require_lock_on(connection)


def _note(name):
    return f'Faxbot sends faxes for the connector "{name}" with this key. Pausing the connector revokes it.'[:500]


def issue(access, actor, name):
    """A new integration and key for a connector; returns (token, public id, binding id, principal id)."""
    prepared = access.credential_codec.prepare_new_key()
    values = IntegrationKeyValues(display_name=name[:200], owner=None, name=name[:100], note=_note(name),
                                  expires_at=None, permissions=frozenset({SCOPE}))
    receipt = access.mutations.issue_integration_key(actor, values, prepared,
                                                     expected_policy_version=_policy_version(access), now=_now())
    return (prepared._token_for_committed_adapter(), receipt.public_key_id, receipt.target.id,
            receipt.principal_id)


def reissue(access, actor, principal_id, name):
    """A new key for the connector's existing integration (on resume)."""
    principals = access.store.tables['access_principals']
    with access.store.engine.connect() as connection:
        version = connection.execute(sa.select(principals.c.version).where(
            principals.c.id == principal_id)).scalar_one_or_none()
    if version is None:
        return issue(access, actor, name)
    prepared = access.credential_codec.prepare_new_key()
    values = KeyValues(VersionedEntity(principal_id, version), name[:100], _note(name), None,
                       (ScopedPermission(SCOPE, ResourceRef('installation')),))
    receipt = access.mutations.issue_key(actor, values, prepared, expected_policy_version=_policy_version(access),
                                         now=_now())
    return prepared._token_for_committed_adapter(), receipt.public_key_id, receipt.target.id, principal_id


def revoke(access, actor, binding_id):
    """Revoke the connector's key; a key already gone is fine."""
    if not binding_id:
        return
    bindings = access.store.tables['access_key_bindings']
    with access.store.engine.connect() as connection:
        row = connection.execute(sa.select(bindings.c.version, bindings.c.state).where(
            bindings.c.id == binding_id)).first()
    if row is None or row.state == 'revoked':
        return
    access.mutations.revoke_key(actor, VersionedEntity(binding_id, row.version),
                                expected_policy_version=_policy_version(access), now=_now())


def connector_actor(access, token):
    """Authenticate as the connector's key; raises AuthenticationError when it was revoked."""
    return access.authentication.header_key(token)


def may_send_on(control, connection, principal_id):
    """Whether this enabled user holds fax:send on their own personal resource now (no credential needed)."""
    from ...work.store import _target
    if not principal_id:
        return False
    resources = control.tables['access_resources']
    personal = connection.execute(sa.select(resources.c.id).where(
        resources.c.kind == 'personal', resources.c.principal_id == principal_id)).scalar_one_or_none()
    if personal is None:
        return False
    context, source = _target(control, principal_id)
    return connection.execute(control._allowed_query(context, source, SCOPE, resource_id=personal)).first() is not None
