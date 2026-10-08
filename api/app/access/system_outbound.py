"""Faxes Faxbot itself accepts for a dedicated sender, with no person or key behind each one.

A partner relay (``direct/relay.py``) queues every fax a partner sends through
it as this installation's own fax, under a dedicated integration principal
("Relayed for Leeds HQ"). That principal is created once, when an
administrator grants the relay, through the ordinary access mutations (so the
administrator needs permission to manage users), and it never has a password,
key or session.

Each relayed fax is then accepted in one installation transaction, exactly as
``AuthorizedOutbound.accept`` does for a person, except that the signed relay
agreement is the authority instead of a live credential:

- the trusted configuration acceptance (``ConfigurationStore._accept_outbound_on``);
- an outbound resource under the principal's own personal container, so the
  fax shows in Sent under "Relayed for …" for anyone who may read it;
- an audit row naming the principal, with ``source: partner_relay``;
- the installation's own sending rules: the decision is read before the lock
  and decided again in the transaction (``routing.rules_acceptance``), exactly
  as for a fax a person sends, so this organization's limits (a cost cap,
  "never use" an account, an approval) apply to every fax it relays. A relayed
  fax is never relayed again: the planner leaves out every partner relay for
  it (``direct.relay.relay_candidates`` with its ``job_id``);
- the caller's own step (``also``), which records the relay ledger row in the
  same transaction, so a fax is never queued without its record or the reverse.
  A fax the rules hold, or send first by another account than the trunk, never
  waits to go with others, as ``POST /fax`` decides it.

When the agreement ends, ``retire`` turns the principal off (a new security
version, so nothing it held stays valid) and audits it. Faxes it already queued
are not touched: they finish, or the relay reports why they did not.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import sqlalchemy as sa

from .mutation_types import IntegrationValues, MutationReason
from .types import AccessError


class SystemSenderError(RuntimeError):
    """One plain sentence for the administrator."""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _audit(connection, tables, *, actor, operation, target_kind, target_id, before, after, details, now):
    connection.execute(tables['access_audit'].insert().values(
        id=uuid.uuid4().hex, actor_principal_id=actor, actor_key_binding_id=None, actor_session_id=None,
        operation=operation, target_kind=target_kind, target_id=target_id, policy_version_before=before,
        policy_version_after=after, outcome='allowed',
        details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True), created_at=now))


def create_sender(runtime, actor, display_name, *, now=None):
    """Create the dedicated integration principal through the access mutations; returns its ID.

    ``actor`` is the administrator granting the relay. Refused with one sentence
    when they may not manage users or the access store is unavailable.
    """
    now = now or _now()
    try:
        with runtime.store.transaction() as connection:
            version = runtime.store.require_lock_on(connection)
            outcome = runtime.mutations.create_integration_on(
                connection, actor, IntegrationValues(display_name[:200], True), expected_policy_version=version,
                now=now)
    except AccessError:
        raise SystemSenderError('Faxbot could not create the sender for relayed faxes; try again.') from None
    if outcome.reason in (MutationReason.FORBIDDEN, MutationReason.RESET_REQUIRED):
        raise SystemSenderError('Relaying for a partner creates a sender named after them, so it needs '
                                'permission to manage users.')
    if outcome.reason is not None or outcome.receipt is None:
        raise SystemSenderError('Faxbot could not create the sender for relayed faxes; try again.')
    return outcome.receipt.target.id


def _personal(connection, tables, principal_id):
    principals, resources = tables['access_principals'], tables['access_resources']
    row = connection.execute(sa.select(principals.c.kind, principals.c.enabled).where(
        principals.c.id == principal_id)).first()
    if row is None or row.kind != 'integration' or int(row.enabled) != 1:
        raise SystemSenderError('The sender for relayed faxes is turned off.')
    resource = connection.execute(sa.select(resources.c.id).where(
        resources.c.kind == 'personal', resources.c.principal_id == principal_id,
        resources.c.enabled == 1)).scalar_one_or_none()
    if resource is None:
        raise SystemSenderError('The sender for relayed faxes has no place for its faxes.')
    return resource


class _SystemSender:
    """The dedicated sender as the sending rules read it: its own principal, accepted by Faxbot itself."""
    credential = None
    system_sender = True

    def __init__(self, principal_id):
        self.principal_id = principal_id


def _rules(configuration, revision, principal_id, job):
    """The sending rules' facts and preview for ``job``, read before the lock, and the step that decides in it.

    Raises when the rules cannot be read, so nothing is accepted outside them.
    """
    from ..routing import rules_acceptance
    from ..routing.holds import document_sha256
    document = Path(revision.values.fax_data_dir) / f"{job['id']}.pdf"
    present = document.is_file()
    plan = rules_acceptance.prepare(
        configuration.engine, revision, actor=_SystemSender(principal_id), destination=job['to_number'],
        pages=job.get('pages'), size_bytes=document.stat().st_size if present else 0,
        document_sha256=document_sha256(document) if present else None)
    return plan, rules_acceptance.recorder(plan, job['id'], _SystemSender(principal_id))


def _follow_envelope(connection, job_id, bound_key, now):
    """Take the fax out of a sending-together group unless its decision sends it first by the bound trunk."""
    from ..routing import envelope as envelopes
    from ..routing.rules_acceptance import first_route_is_bound
    try:
        pinned = envelopes.load_on(connection, job_id)
    except envelopes.UnreadableDecision:
        pinned = None
    if pinned is not None and first_route_is_bound(pinned.decision, bound_key):
        return
    members = sa.table('outbound_batch_members', sa.column('id'))
    if connection.execute(sa.select(members.c.id).where(members.c.id == job_id)).first() is None:
        return
    from ..batching.store import separate_on, tables as batching_tables
    separate_on(connection, batching_tables(connection.engine, connection), job_id, now)


def accept(configuration, access_store, principal_id, revision, job, *, also=None, source='partner_relay'):
    """Accept ``job`` as an outbound fax of ``principal_id`` in one transaction; returns the bound profile.

    The installation's sending rules decide its route envelope in the same
    transaction. ``also(connection, now)`` runs last in the same transaction;
    anything it raises rolls the whole acceptance back.
    """
    tables = access_store.tables
    plan, decide = _rules(configuration, revision, principal_id, job)
    with configuration._locked() as connection:
        version = access_store.lock_on(connection)
        now = _now()
        parent = _personal(connection, tables, principal_id)
        profile = configuration._accept_outbound_on(connection, revision, job)
        resources = tables['access_resources']
        identity = uuid.uuid4().hex
        connection.execute(resources.insert().values(
            id=identity, kind='outbound', parent_id=parent, parent_kind='personal', principal_id=None,
            mailbox_id=None, fax_job_id=job['id'], inbound_fax_id=None, enabled=1, version=1, created_at=now,
            updated_at=now))
        _audit(connection, tables, actor=principal_id, operation='fax.accept', target_kind='resource',
               target_id=identity, before=version, after=version, details={'source': source}, now=now)
        decide(connection, now)
        if also is not None:
            also(connection, now)
        _follow_envelope(connection, job['id'], plan.bound_key, now)
        return profile


def retire(access_store, principal_id, *, by=None, reason='relay_withdrawn', now=None):
    """Turn the dedicated sender off and audit it; False when it was already off or is not one.

    ``by`` is the administrator who ended the agreement, or None when the partner ended it.
    """
    tables = access_store.tables
    principals, state = tables['access_principals'], tables['access_state']
    now = now or _now()
    with access_store.transaction() as connection:
        before = access_store.require_lock_on(connection)
        row = connection.execute(sa.select(principals).where(principals.c.id == principal_id)).mappings().one_or_none()
        if row is None or row['kind'] != 'integration' or int(row['enabled']) != 1:
            return False
        after = before + 1
        connection.execute(principals.update().where(principals.c.id == principal_id).values(
            enabled=0, security_version=row['security_version'] + 1, version=row['version'] + 1, updated_at=now))
        connection.execute(state.update().where(state.c.id == 'state').values(policy_version=after, updated_at=now))
        _audit(connection, tables, actor=by, operation='relay_sender.retire', target_kind='principal',
               target_id=principal_id, before=before, after=after, details={'reason': reason}, now=now)
        return True
