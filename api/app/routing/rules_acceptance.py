"""Decide a new fax's route envelope when it is accepted, and record it in the acceptance transaction.

``prepare`` gathers the fax's facts the way the dry run does (``rules.explain.
FactsReader``) before any lock: the destination, the sender and their groups,
the mailbox, workflow and labels, the document, a quote per account. The
``recorder`` then runs inside the transaction that writes the fax and its
configuration binding (``access.outbound.accept(also=...)``):

1. it reads the active rules of each scope on that same connection, so a rules
   publish can never slip between the decision and the fax;
2. it decides (pure: ``rules.evaluate.decide``) and inserts the decision;
3. it writes the holds the decision asks for (approval, time window, or no
   route), so the claim never offers the fax while one is open;
4. it keeps the number the fax dials consistent with the decision: a rule that
   says "never dial the alternate" clears the alternate the dialing step chose.

With no rules published the decision is the automatic choice: exactly the
routes Faxbot used before rules existed. Nothing here sends a fax.
"""
from dataclasses import dataclass
from weakref import WeakKeyDictionary

import sqlalchemy as sa

from ..rules import model
from ..rules.evaluate import decide
from ..rules.explain import FactsReader
from ..rules.store import RuleStore, compile_scopes
from . import envelope as envelopes, holds as hold_store


_STORES = WeakKeyDictionary()


def _stores(engine):
    """``(RuleStore, RouteStore)`` reflected once per engine: every fax accepted reuses them."""
    found = _STORES.get(engine)
    if found is None:
        from .store import RouteStore
        found = _STORES[engine] = (RuleStore(engine), RouteStore(engine))
    return found


class RulesAcceptanceError(ValueError):
    """The send form's mailbox, workflow or labels cannot be used; one plain sentence for the sender."""


@dataclass(frozen=True)
class Prepared:
    facts: model.Facts
    accounts: tuple
    decision: model.Decision           # the preview decided before the lock
    store: RuleStore
    document_sha256: str | None
    zone_name: str
    bound_key: str | None
    # Where Faxbot may dial (``guard.preview``): the dialed number's class, read before the lock.
    dialing: object = None


def sender_of(actor):
    """``(principal, kind, key binding)`` of the accepting identity, as the rules read it."""
    credential = getattr(actor, 'credential', None)
    binding = getattr(credential, 'binding_id', None)
    principal = getattr(actor, 'principal_id', None)
    if not principal:
        return None, 'system', None
    if getattr(actor, 'system_sender', False):
        # A dedicated sender Faxbot accepts faxes for itself, such as a partner relay's "Relayed for …".
        return principal, 'system', None
    return principal, 'key' if binding else 'person', binding


def alternate_lookup(engine):
    """AD's approved alternate for a number as the rules read it (``model.Alternate``), or None."""
    from . import alternates

    def lookup(number):
        approval = alternates.current(number, engine=engine)
        if approval is None:
            return None
        approved_at = approval.approved_at.replace(microsecond=0).isoformat() if approval.approved_at else None
        return model.Alternate(number=approval.alternate, approval_id=approval.id or approval.alternate,
                               approved_by=None, approved_at=approved_at, note=None)
    return lookup


def _organization_document(store):
    import json
    found = store.active(model.ORGANIZATION)
    return json.loads(found['document']) if found else {}


def check_choices(engine, *, mailbox=None, workflow=None, labels=()):
    """Refuse a mailbox that does not exist, or a workflow or label the organization's rules do not define."""
    if not mailbox and not workflow and not labels:
        return
    if mailbox:
        mailboxes = sa.table('mailboxes', sa.column('id'))
        with engine.connect() as connection:
            if connection.execute(sa.select(mailboxes.c.id).where(mailboxes.c.id == mailbox)).first() is None:
                raise RulesAcceptanceError('There is no such mailbox to send from. Choose one of your mailboxes.')
    if not workflow and not labels:
        return
    document = _organization_document(_stores(engine)[0])
    if workflow and workflow not in {item.get('key') for item in document.get('workflows') or ()
                                     if isinstance(item, dict)}:
        raise RulesAcceptanceError(f'There is no workflow called {workflow}. Add it on Providers → Rules → Workflows, '
                                   'or leave the workflow out.')
    known = set(document.get('labels') or ())
    unknown = sorted(set(labels) - known)
    if unknown:
        raise RulesAcceptanceError(f'{", ".join(unknown)} {"is not a label" if len(unknown) == 1 else "are not labels"}'
                                   ' your rules define. Add labels on Providers → Rules → Lists.')


def may_send_from(connection, control, actor, mailbox_id, *, now):
    """Whether this person works in the mailbox (sees its faxes or its work) or may read every mailbox."""
    from ..access.types import ResourceRef
    if control.authorize_on(connection, actor, 'mailboxes:read', ResourceRef('installation'), now=now).allowed:
        return True
    resources = sa.table('access_resources', sa.column('id'), sa.column('kind'), sa.column('mailbox_id'))
    resource = connection.execute(sa.select(resources.c.id).where(
        resources.c.kind == 'mailbox', resources.c.mailbox_id == mailbox_id)).scalar_one_or_none()
    if resource is None:
        return False
    return any(control.authorize_on(connection, actor, permission, ResourceRef(resource), now=now).allowed
               for permission in ('inbound:list', 'work:read'))


def sendable_mailboxes(connection, control, actor, *, now):
    """``[{'id', 'label'}]``: the mailboxes this person may send from (Send a fax's "Send from mailbox")."""
    mailboxes = sa.table('mailboxes', sa.column('id'), sa.column('label'))
    rows = connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label).order_by(mailboxes.c.label)).all()
    return [{'id': row[0], 'label': row[1]} for row in rows
            if may_send_from(connection, control, actor, row[0], now=now)]


def _organization_on(connection):
    """The organization's active rules document, read on ``connection`` with no reflection; {} without rules."""
    import json
    revisions = sa.table('routing_rule_revisions', sa.column('id'), sa.column('document'))
    state = sa.table('routing_rule_state', sa.column('scope_kind'), sa.column('scope_id'),
                     sa.column('active_revision_id'))
    text = connection.execute(sa.select(revisions.c.document).join(
        state, state.c.active_revision_id == revisions.c.id).where(
        state.c.scope_kind == model.ORGANIZATION, state.c.scope_id == '')).scalar_one_or_none()
    return json.loads(text) if text else {}


def send_choices(engine=None, *, connection=None):
    """The organization's workflows and labels, for Send a fax; empty without rules."""
    try:
        if connection is not None:
            document = _organization_on(connection)
        else:
            with engine.connect() as own:
                document = _organization_on(own)
    except Exception:
        return {'workflows': [], 'labels': []}
    return {'workflows': [{'key': item['key'], 'name': item.get('name') or item['key']}
                          for item in document.get('workflows') or () if isinstance(item, dict) and item.get('key')],
            'labels': [label for label in document.get('labels') or () if isinstance(label, str)]}


def prepare(engine, revision, *, actor, destination, pages, size_bytes=0, mailbox=None, workflow=None, labels=(),
            urgent=False, by_call=False, case_packet=False, document_sha256=None, direct_ready=None):
    """Read the facts and decide a preview, before the acceptance lock."""
    from ..accounts import default_sending_key, sending_accounts
    values = revision.values
    accounts = sending_accounts(values)
    principal, kind, key_id = sender_of(actor)
    store, routes = _stores(engine)
    reader = FactsReader(engine, values, routes, alternates=alternate_lookup(engine),
                         direct_ready=direct_ready)
    facts = reader.read(to_number=destination, accounts=accounts, pages=pages, size_bytes=size_bytes,
                        principal_id=principal, sender_kind=kind, key_id=key_id, mailbox_id=mailbox,
                        workflow=workflow, labels=labels, urgent=urgent, by_call=by_call, case_packet=case_packet)
    decision = decide(store.compiled_active(), facts, accounts)
    # A recipient that recognises faxes by their sending number: only its registered trunk (sender_pins, N17).
    from .sender_pins import narrow_for
    decision = narrow_for(engine, decision, facts)
    from . import guard
    dialing = guard.preview(engine, values, destination=facts.destination, decision=decision, accounts=accounts)
    return Prepared(facts, accounts, decision, store, document_sha256, getattr(values, 'time_zone', '') or '',
                    default_sending_key(values), dialing)


def recorder_for(engine, revision, actor, *, job_id, destination, pages, document_path=None, case_packet=False,
                 control=None):
    """Facts, preview and the acceptance-transaction recorder in one call, for faxes Faxbot accepts itself.

    Email and folder connectors (``intake/sources/send.py``) and generated documents (case packets, challenge
    faxes, forms: ``routing/submit.py``) go through the same rules as POST /fax; nothing about the rules is
    skipped. Raises when the rules cannot be read, so the fax is not accepted outside them.
    """
    from .holds import document_sha256
    digest = document_sha256(document_path) if document_path else None
    size = 0
    if document_path:
        try:
            import os
            size = os.path.getsize(document_path)
        except OSError:
            size = 0
    plan = prepare(engine, revision, actor=actor, destination=destination, pages=pages, size_bytes=size,
                   case_packet=case_packet, document_sha256=digest)
    return recorder(plan, job_id, actor, control=control)


def compiled_on(connection, store):
    """The active rules of every scope, read on the acceptance transaction's own connection."""
    revisions, state = store.revisions, store.state
    rows = connection.execute(sa.select(revisions).join(state, state.c.active_revision_id == revisions.c.id)
                              ).mappings().all()
    return compile_scopes({model.scope_name(row['scope_kind'], row['scope_id']): dict(row) for row in rows})


def first_route_is_bound(decision, bound_key):
    """Whether the bound (default sending) account is where the fax goes first, as sending together needs."""
    envelope = decision.envelope
    if decision.outcome != 'route' or bound_key is None or bound_key not in envelope.accounts:
        return False
    return envelope.mode == 'automatic' or envelope.accounts[0] == bound_key


def recorder(prepared, job_id, actor, *, control=None):
    """The acceptance-transaction step that decides, records the decision and writes its holds."""
    def record(connection, now):
        facts = prepared.facts
        if facts.mailbox_id and control is not None and not may_send_from(
                connection, control, actor, facts.mailbox_id, now=now):
            raise RulesAcceptanceError('You can’t send from that mailbox. Choose a mailbox you work in.')
        decision = decide(compiled_on(connection, prepared.store), facts, prepared.accounts)
        # A recipient that recognises faxes by their sending number: only its registered trunk (sender_pins, N17).
        from .sender_pins import narrow_on
        decision = narrow_on(connection, decision, facts)
        principal = getattr(actor, 'principal_id', None)
        decision_id = prepared.store.record_decision_on(connection, job_id=job_id, facts=facts, decision=decision,
                                                        actor_principal_id=principal, now=now)
        pinned = envelopes.Pinned(decision_id, 1, decision, facts)
        t = envelopes.tables(connection)
        _reconcile_dial(connection, job_id, decision)
        digest = hold_store.digest_for(pinned, job_id, facts.destination, prepared.document_sha256) \
            if prepared.document_sha256 else None
        if decision.outcome != 'route':
            hold_store.holds_for_decision_on(connection, t, job_id=job_id, pinned=pinned, now=now,
                                             requested_by=principal, digest=digest, accounts=prepared.accounts,
                                             zone_name=prepared.zone_name or None)
            from ..outbound_store import _event
            _event(connection, sa.table('outbound_events', sa.column('id'), sa.column('job_id'),
                                        sa.column('attempt_id'), sa.column('kind'), sa.column('dedupe_key'),
                                        sa.column('details'), sa.column('created_at')),
                   job_id, 'route_held', now)
        # Where Faxbot may dial (``guard.py``): a number in a class it may not dial waits in Sent for approval.
        if prepared.dialing is not None:
            from . import guard
            dialed = _dialed(prepared.dialing, decision, facts.destination)
            guard.record_on(connection, dialed, job_id=job_id, decision_id=decision_id, now=now,
                            requested_by=principal, digest=digest, also_held=decision.outcome != 'route')
    return record


def _dialed(dialing, decision, destination):
    """The preview again when the decision in the transaction dials the same number, else that number's class."""
    from dataclasses import replace
    from . import guard
    number = decision.envelope.dial.number if decision.envelope.dial is not None else destination
    if number == dialing.number:
        return dialing
    return replace(dialing, number=number, found=guard.dial_class(number, dialing.home_country), rates=None)


def _reconcile_dial(connection, job_id, decision):
    """A rule that never dials the alternate (or a decision with none) clears the alternate chosen on acceptance."""
    if decision.envelope.dial is not None:
        return
    deliveries = sa.table('outbound_deliveries', sa.column('id'), sa.column('alternate_number'),
                          sa.column('alternate_approval'))
    connection.execute(deliveries.update().where(deliveries.c.id == job_id, deliveries.c.alternate_number.is_not(None))
                       .values(alternate_number=None, alternate_approval=None))

