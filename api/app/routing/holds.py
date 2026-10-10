"""Faxes held by sending rules: waiting for approval, for a time window, or for a route the rules allow.

A hold is a row in ``outbound_holds`` (0029). The fax stays ``ready`` and the
claim never offers it while an approval or no-route hold is open, or while a
time window has not opened yet (``blocking``). Approving, refusing and the
claim all run under the configuration lock, so an approval and a claim can
never race into two sends. A held fax never waits in a sending-together group:
the claim takes it out of one first (acceptance can decide a hold after the
preview put it there), so it goes on its own once released.

- **Approval** (``fax:approve``, Owner and Administrator by default). It binds
  to the fax, its document's SHA-256, the destination, the number it dials and
  the rule revisions it was decided under; a change in any of them voids it.
  A rule may require someone other than the sender to decide.
- **Refusing** fails the fax before anything is sent, with the approver's name
  and reason.
- **No route** (owner's answer Q1): nothing the rules allow can send it. It is
  never failed and never sent outside its envelope. "Check again" lets the next
  claim try its accounts again; "send anyway" by an account a cost cap (not a
  mandatory one) left out, or one that was down or busy, writes a new decision
  naming only that account, with the approver's name, and releases the hold.
- **Time windows** open by themselves: the claim reads ``release_at``. The
  routing background task marks them released for the history.

Every decision writes an audit row. Nothing here sends a fax.
"""
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

import sqlalchemy as sa

from ..rules import text
from . import envelope as envelopes


OPERATIONS = {'approve': 'routing.hold_approved', 'refuse': 'routing.hold_refused',
              'anyway': 'routing.hold_sent_anyway', 'check_again': 'routing.hold_checked_again'}
# A fax that has not been claimed: ready to go, or a test fax held while sending is turned off.
WAITING = ('ready', 'held')
SOFT_SKIPS = ('not_ready', 'busy', 'spending_limit', 'unavailable', 'over_cap', 'unknown_cost')


class HoldConflict(RuntimeError):
    """The hold changed or no longer applies; the sentence says what to do."""


class HoldInputError(ValueError):
    """The request cannot be done as asked; one plain sentence."""


class HoldForbidden(PermissionError):
    """This person may not decide on this hold."""


def utcnow():
    return datetime.utcnow().replace(microsecond=0)


def blocking(t, now):
    """Faxes the claim must not offer now: open approval and no-route holds, and windows not yet open."""
    holds = t['outbound_holds']
    return sa.select(holds.c.job_id).where(holds.c.state == 'open', sa.or_(
        holds.c.kind != 'window', holds.c.release_at.is_(None), holds.c.release_at > now))


def document_sha256(path):
    """The SHA-256 of the accepted document, or None when the file is gone."""
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b''):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def bound_digest(*, job_id, document, destination, dial, revisions):
    """What an approval binds to: the fax, its document, where it goes, the number dialed and the rules' revisions."""
    data = {'job': job_id, 'document': document, 'destination': destination, 'dial': dial,
            'revisions': revisions}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def digest_for(pinned, job_id, destination, document):
    dial = pinned.envelope.dial.number if pinned.envelope.dial is not None else None
    return bound_digest(job_id=job_id, document=document, destination=destination, dial=dial,
                        revisions=pinned.decision.revision_ids)


def create_on(connection, t, *, job_id, kind, decision_id, now, rule_id=None, release_at=None, separate=False,
              requested_by=None, digest=None, reason=None):
    holds = t['outbound_holds']
    identity = uuid.uuid4().hex
    connection.execute(holds.insert().values(
        id=identity, job_id=job_id, kind=kind, decision_id=decision_id, rule_id=(rule_id or None) and rule_id[:64],
        release_at=release_at, state='open', separate_approver=int(bool(separate)),
        requested_by_principal_id=requested_by, bound_digest=digest, requested_at=now, reason=(reason or '')[:500] or None,
        version=1, updated_at=now))
    return identity


def holds_for_decision_on(connection, t, *, job_id, pinned, now, requested_by=None, digest=None, accounts=(),
                          zone_name=None):
    """Write the holds a decision asks for: its approval and windows, or one no-route hold when it is blocked."""
    decision, envelope = pinned.decision, pinned.envelope
    approval = next((hold for hold in envelope.holds if hold.kind == 'approval'), None)
    created = []
    if decision.outcome == 'blocked':
        created.append(create_on(connection, t, job_id=job_id, kind='no_route', decision_id=pinned.decision_id,
                                 now=now, rule_id=decision.route.rule_id if decision.route.kind == 'rule' else None,
                                 separate=bool(approval and approval.separate_approver), requested_by=requested_by,
                                 digest=digest, reason=text.blocked_sentence(decision, accounts)))
        return created
    for hold in envelope.holds:
        release = datetime.fromisoformat(hold.release_at) if hold.release_at else None
        created.append(create_on(connection, t, job_id=job_id, kind=hold.kind, decision_id=pinned.decision_id,
                                 now=now, rule_id=hold.source.rule_id, release_at=release,
                                 separate=hold.separate_approver, requested_by=requested_by,
                                 digest=digest if hold.kind == 'approval' else None,
                                 reason=text.hold_sentence(hold, zone_name)))
    return created


def open_on(connection, t, job_id):
    holds = t['outbound_holds']
    return [dict(row) for row in connection.execute(sa.select(holds).where(
        holds.c.job_id == job_id, holds.c.state == 'open').order_by(holds.c.requested_at, holds.c.id)).mappings()]


def no_route_sentence(label_by_key, skipped):
    """Why nothing could take the fax at dispatch, in one sentence."""
    why = {'turned_off': 'is turned off', 'not_ready': 'is not ready', 'busy': 'has no free line',
           'over_cap': "is over the rule's cost cap", 'unknown_cost': "has no known price under the rule's cost cap",
           'spending_limit': 'reached its daily spending limit', 'unavailable': 'is not set up to send',
           'tried': 'was already tried',
           'needs_patient': "needs the patient's details, which this fax does not have"}
    parts = [f'{label_by_key(key)} {why.get(reason, "is not available")}' for key, reason in skipped]
    if not parts:
        return 'No account your rules allow can send this fax now. It waits for you in Sent; nothing was sent.'
    joined = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]
    return f'No account your rules allow can send this fax now: {joined}. It waits for you in Sent; nothing was sent.'


def tried_sentence(labels):
    """Why a fax whose rules chose its route waits after its last allowed try ended before any page, in one
    sentence (strict fallback: at most ``routing.fallback.MAX_FALLBACKS`` more tries, each before any fax data)."""
    labels = [label for index, label in enumerate(labels) if label not in labels[:index]]
    if not labels:
        return 'The call ended before any page was sent. It waits for you in Sent; nothing was sent.'
    if len(labels) == 1:
        return f'Faxbot tried {labels[0]}, and the call ended before any page was sent. It waits for you in Sent; ' \
               'nothing was sent.'
    joined = ', '.join(labels[:-1]) + ' and ' + labels[-1]
    return f'Faxbot tried {joined}, and each call ended before any page was sent. It waits for you in Sent; ' \
           'nothing was sent.'


def hold_after_predata_on(connection, t, *, job_id, pinned, accounts, now):
    """Hold a fax whose last allowed try ended before any page (no fallback is left) instead of failing it.

    The caller has detached the attempt and kept the fax ``ready``; the claim then never offers it until someone
    checks again (its next try never repeats an account already tried), sends it anyway, or refuses it.
    """
    choices = t['delivery_rule_choices']
    attempts = sa.table('outbound_attempts', sa.column('id'), sa.column('sequence'), sa.column('submitted_at'))
    keys = connection.execute(sa.select(choices.c.account_key).select_from(
        choices.join(attempts, attempts.c.id == choices.c.id)).where(
        choices.c.job_id == job_id, attempts.c.submitted_at.is_not(None)).order_by(attempts.c.sequence)
    ).scalars().all()
    reason = tried_sentence([text.account_label(key, accounts) for key in keys])
    if open_on(connection, t, job_id):
        return None
    return create_on(connection, t, job_id=job_id, kind='no_route', decision_id=pinned.decision_id, now=now,
                     reason=reason)


class HoldStore:
    """Read and decide holds. ``delivery`` is the OutboundStore; ``access_store`` writes the audit rows."""

    def __init__(self, delivery, *, access_store=None, values=None):
        self.delivery = delivery
        self.configuration = delivery.configuration
        self.engine = self.configuration.engine
        self.access_store = access_store
        self._values = values

    def _t(self, connection):
        t = envelopes.tables(connection)
        if t is None:
            raise HoldConflict('Sending rules are not set up in this database yet.')
        return t

    def _audit(self, connection, actor, operation, hold, details):
        if self.access_store is None:
            return
        version = self.access_store.lock_on(connection)
        credential = getattr(actor, 'credential', None)
        connection.execute(self.access_store.tables['access_audit'].insert().values(
            id=uuid.uuid4().hex, actor_principal_id=getattr(actor, 'principal_id', None),
            actor_key_binding_id=getattr(credential, 'binding_id', None),
            actor_session_id=getattr(credential, 'session_id', None), operation=OPERATIONS[operation],
            target_kind='installation', target_id=f'fax:{hold["job_id"]}'[:100], policy_version_before=version,
            policy_version_after=version, outcome='allowed',
            details=json.dumps({'hold': hold['id'], 'kind': hold['kind'], **details}, sort_keys=True,
                               separators=(',', ':'), ensure_ascii=True), created_at=utcnow()))

    # Reading -------------------------------------------------------------------------------------------------

    def _names(self, connection, principal_ids):
        principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
        ids = [item for item in set(principal_ids) if item]
        if not ids:
            return {}
        return dict(connection.execute(sa.select(principals.c.id, principals.c.display_name)
                                       .where(principals.c.id.in_(ids))).all())

    def holds(self, *, state='open', principal_id=None, every=True, job_id=None, limit=500):
        """Hold rows with the fax's number and pages, newest last; ``every`` False keeps one sender's own."""
        with self.engine.connect() as connection:
            t = self._t(connection)
            holds, jobs = t['outbound_holds'], self.configuration.jobs
            query = (sa.select(holds, jobs.c.to_number, jobs.c.pages)
                     .select_from(holds.join(jobs, jobs.c.id == holds.c.job_id)))
            if state:
                query = query.where(holds.c.state == state)
            if job_id:
                query = query.where(holds.c.job_id == job_id)
            if not every:
                query = query.where(holds.c.requested_by_principal_id == principal_id)
            rows = [dict(row) for row in connection.execute(
                query.order_by(holds.c.requested_at, holds.c.id).limit(limit)).mappings()]
            names = self._names(connection, [row['requested_by_principal_id'] for row in rows])
        for row in rows:
            row['sender_name'] = names.get(row['requested_by_principal_id'])
        return rows

    def pinned_for(self, job_id):
        return envelopes.load(self.engine, job_id)

    # Deciding --------------------------------------------------------------------------------------------------

    def _hold_on(self, connection, t, hold_id, version):
        holds = t['outbound_holds']
        row = connection.execute(sa.select(holds).where(holds.c.id == hold_id)).mappings().one_or_none()
        if row is None:
            raise HoldInputError('There is no such held fax.')
        row = dict(row)
        if row['state'] != 'open':
            raise HoldConflict('This fax is no longer held. Reload Sent to see what happened to it.')
        if type(version) is not int or version != row['version']:
            raise HoldConflict('Someone else decided on this fax since you opened it. Reload Sent and try again.')
        return row

    def _decided(self, connection, t, row, state, actor, actor_name, now, reason=None):
        holds = t['outbound_holds']
        changed = connection.execute(holds.update().where(holds.c.id == row['id'], holds.c.version == row['version'])
                                     .values(state=state, decided_by_principal_id=getattr(actor, 'principal_id', None),
                                             decided_by_name=(actor_name or None) and actor_name[:200],
                                             decided_at=now, reason=(reason or row['reason'] or '')[:500] or None,
                                             version=row['version'] + 1, updated_at=now)).rowcount
        if changed != 1:
            raise HoldConflict('Someone else decided on this fax since you opened it. Reload Sent and try again.')

    @staticmethod
    def may_decide(row, actor, *, approver):
        """Whether this person may approve, refuse or send anyway the fax this hold keeps."""
        if not approver:
            return False
        if row['separate_approver'] and row['requested_by_principal_id'] and \
                row['requested_by_principal_id'] == getattr(actor, 'principal_id', None):
            return False
        return True

    def _delivery_row(self, connection, job_id):
        deliveries = self.delivery.deliveries
        return connection.execute(sa.select(deliveries).where(deliveries.c.id == job_id)).mappings().one_or_none()

    def _document(self, job_id):
        try:
            revision, _ = self.configuration.outbound_context(job_id)
            root = revision.values.fax_data_dir
        except Exception:
            return None
        return document_sha256(Path(root) / f'{job_id}.pdf')

    def approve(self, hold_id, *, version, actor, actor_name=None, account=None, approver=True, now=None):
        """Approve a fax waiting for approval, or send a fax with no allowed route by ``account`` anyway."""
        from ..outbound_store import _event
        document = None
        with self.engine.connect() as connection:
            t = self._t(connection)
            preview = connection.execute(sa.select(t['outbound_holds']).where(
                t['outbound_holds'].c.id == hold_id)).mappings().one_or_none()
        if preview is not None and preview['kind'] == 'approval':
            # Read the document before the lock: hashing a large file must not hold up every claim.
            document = self._document(preview['job_id'])
        with self.configuration._locked() as connection:
            now = now or utcnow()
            t = self._t(connection)
            row = self._hold_on(connection, t, hold_id, version)
            if not self.may_decide(row, actor, approver=approver):
                raise HoldForbidden('Someone other than the sender must decide on this fax.')
            delivery = self._delivery_row(connection, row['job_id'])
            if delivery is None or delivery['state'] not in WAITING:
                raise HoldConflict('This fax is no longer waiting. Reload Sent to see what happened to it.')
            pinned = envelopes.load_on(connection, row['job_id'])
            if row['kind'] == 'window':
                raise HoldInputError('This fax waits for its time window, not for approval.')
            if row['kind'] == 'approval':
                if account:
                    raise HoldInputError('Choose an account only for a fax with no route your rules allow.')
                # Where Faxbot may dial (guard.py): a premium-rate, special-service or satellite number is never
                # dialed by approving one fax while its class is not allowed.
                from .guard import refuse_release_on
                refuse_release_on(connection, row)
                jobs = self.configuration.jobs
                destination = connection.scalar(sa.select(jobs.c.to_number).where(jobs.c.id == row['job_id']))
                if row['bound_digest'] and (pinned is None or document is None or digest_for(
                        pinned, row['job_id'], destination, document) != row['bound_digest']):
                    raise HoldConflict('The fax changed after it was held, so this approval cannot apply. Refuse it '
                                       'and send it again.')
                self._decided(connection, t, row, 'released', actor, actor_name, now)
                _event(connection, self.delivery.events, row['job_id'], 'route_released', now)
                self._audit(connection, actor, 'approve', row, {})
                sentence = 'Approved. The fax is no longer held.'
            else:
                options = {item['account']: item for item in self.options(pinned, row, connection=connection)}
                if not account or account not in options:
                    raise HoldInputError('Choose one of the accounts offered for this fax.')
                if pinned is None:
                    raise HoldConflict('This fax has no routing decision to change.')
                decision_id = self._send_anyway_on(connection, t, pinned, row, account, actor, actor_name, now)
                self._decided(connection, t, row, 'released', actor, actor_name, now,
                              reason=f'Sent by {options[account]["label"]} anyway.')
                self._audit(connection, actor, 'anyway', row, {'account': account, 'decision': decision_id})
                sentence = f'Approved. The fax goes by {options[account]["label"]}.'
            # A held fax waiting for approval or a window may still have other holds; it goes once none is open.
            remaining = open_on(connection, t, row['job_id'])
            if not remaining:
                from ..outbound_wake import wake
                wake.notify()
        return {**self._view_row(row, state='released'), 'sentence': sentence}

    def _send_anyway_on(self, connection, t, pinned, row, account, actor, actor_name, now):
        """A new decision naming only ``account``: the approver's explicit choice, kept with the fax."""
        decisions = t['fax_job_rule_decisions']
        sequence = connection.scalar(sa.select(sa.func.max(decisions.c.sequence)).where(
            decisions.c.job_id == row['job_id'])) or 0
        envelope = replace(pinned.envelope, mode='one', accounts=(account,), caps=(), holds=(), strict_fallback=True)
        decision = replace(pinned.decision, outcome='route', reason=None, envelope=envelope)
        identity = uuid.uuid4().hex
        name = actor_name or 'someone with Approve faxes'
        connection.execute(decisions.insert().values(
            id=identity, job_id=row['job_id'], sequence=sequence + 1, revisions=json.dumps(
                decision.revision_ids, sort_keys=True, separators=(',', ':')),
            facts=pinned.facts.to_json(), facts_digest=decision.facts_digest, decision=decision.compact().to_json(),
            dial_number=envelope.dial.number if envelope.dial else None,
            approval_id=envelope.dial.approval_id if envelope.dial else None, page_layout=envelope.page_layout,
            outcome='route', reason=f'Sent by {account} anyway, approved by {name}.'[:200],
            actor_principal_id=getattr(actor, 'principal_id', None), created_at=now))
        return identity

    def refuse(self, hold_id, *, version, actor, actor_name=None, reason, approver=True, now=None):
        """Fail a held fax before anything is sent, with the approver's name and reason."""
        from ..outbound_store import _event
        reason = (reason or '').strip()
        if not reason or len(reason) > 500:
            raise HoldInputError('Give a reason of up to 500 characters, such as "wrong recipient".')
        with self.configuration._locked() as connection:
            now = now or utcnow()
            t = self._t(connection)
            row = self._hold_on(connection, t, hold_id, version)
            if not self.may_decide(row, actor, approver=approver):
                raise HoldForbidden('Someone other than the sender must decide on this fax.')
            delivery = self._delivery_row(connection, row['job_id'])
            if delivery is None or delivery['state'] not in WAITING:
                raise HoldConflict('This fax is no longer waiting. Reload Sent to see what happened to it.')
            name = actor_name or 'someone with Approve faxes'
            sentence = f'Refused by {name}: {reason}'
            self._decided(connection, t, row, 'refused', actor, actor_name, now, reason=sentence)
            # Every other hold on the fax ends with it.
            holds = t['outbound_holds']
            connection.execute(holds.update().where(holds.c.job_id == row['job_id'], holds.c.state == 'open').values(
                state='refused', decided_by_principal_id=getattr(actor, 'principal_id', None),
                decided_by_name=(actor_name or None) and actor_name[:200], decided_at=now,
                version=holds.c.version + 1, updated_at=now))
            deliveries = self.delivery.deliveries
            connection.execute(deliveries.update().where(deliveries.c.id == row['job_id']).values(
                state='failed', version=delivery['version'] + 1, updated_at=now))
            connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == row['job_id'])
                               .values(status='failed', error=sentence[:500], updated_at=now))
            _event(connection, self.delivery.events, row['job_id'], 'route_refused', now)
            # Nothing waits to go together with a refused fax.
            try:
                from ..batching.store import separate_on
                separate_on(connection, self.delivery._batching(connection), row['job_id'], now)
            except Exception:
                pass
            self._audit(connection, actor, 'refuse', row, {'reason': reason})
        return {**self._view_row(row, state='refused'), 'sentence': f'Refused. Nothing was sent. {sentence}'}

    def check_again(self, hold_id, *, version, actor, now=None):
        """Let the next claim try a no-route fax's accounts again; it is held again if none can take it."""
        with self.configuration._locked() as connection:
            now = now or utcnow()
            t = self._t(connection)
            row = self._hold_on(connection, t, hold_id, version)
            if row['kind'] != 'no_route':
                raise HoldInputError('Only a fax with no route your rules allow can be checked again.')
            self._decided(connection, t, row, 'released', actor, None, now,
                          reason='Checked again: Faxbot tries the accounts your rules allow.')
            self._audit(connection, actor, 'check_again', row, {})
        from ..outbound_wake import wake
        wake.notify()
        return {**self._view_row(row, state='released'),
                'sentence': 'Faxbot is trying the accounts your rules allow again. If none can take the fax, it waits '
                            'for you here again.'}

    def release_due(self, now=None):
        """Mark time windows that have opened as released, for the history; the claim already goes by the time."""
        now = now or utcnow()
        with self.engine.connect() as connection:
            t = envelopes.tables(connection)
            if t is None:
                return 0
            holds = t['outbound_holds']
            due = connection.execute(sa.select(holds.c.id).where(
                holds.c.state == 'open', holds.c.kind == 'window', holds.c.release_at <= now).limit(100)).first()
        if due is None:
            return 0
        with self.configuration._locked() as connection:
            t = self._t(connection)
            holds = t['outbound_holds']
            released = connection.execute(holds.update().where(
                holds.c.state == 'open', holds.c.kind == 'window', holds.c.release_at <= now).values(
                state='released', decided_at=now, version=holds.c.version + 1, updated_at=now)).rowcount
        if released:
            from ..outbound_wake import wake
            wake.notify()
        return released

    # Views -----------------------------------------------------------------------------------------------------

    def options(self, pinned, row, *, connection=None, accounts=None):
        """Accounts a no-route fax may be sent by anyway: left out only by a cost cap that is not mandatory, or by
        being down, busy or at a spending limit when Faxbot tried them. ``[{'account', 'label', 'reason'}]``."""
        if pinned is None or row['kind'] != 'no_route':
            return []
        accounts = accounts if accounts is not None else self._accounts_for(row['job_id'])
        known = {account.key for account in accounts}
        label = lambda key: text.account_label(key, accounts)  # noqa: E731
        found, seen = [], set()
        for item in pinned.decision.excluded:
            if not item.soft or item.account in seen or item.account not in known:
                continue
            seen.add(item.account)
            found.append({'account': item.account, 'label': label(item.account),
                          'reason': text.excluded_sentence(item, accounts, pinned.facts, pinned.envelope.caps)})
        skipped = self._skipped(row['job_id'], connection)
        for key, why in skipped:
            if key in seen or key not in known or why not in SOFT_SKIPS or not pinned.allows(key):
                continue
            seen.add(key)
            reason = {'not_ready': 'It was not ready when Faxbot tried it.', 'busy': 'It had no free line.',
                      'spending_limit': 'It reached its daily spending limit.',
                      'unavailable': 'It could not take the fax when Faxbot tried it.',
                      'over_cap': "Its price today is over the rule's cost cap.",
                      'unknown_cost': "Its price is unknown, and a rule caps the cost."}[why]
            found.append({'account': key, 'label': label(key), 'reason': reason})
        return found

    def not_offered(self, pinned, row, accounts=None):
        """Why the other accounts are not offered, one sentence each."""
        if pinned is None or row['kind'] != 'no_route':
            return []
        accounts = accounts if accounts is not None else self._accounts_for(row['job_id'])
        sentences = []
        for item in pinned.decision.excluded:
            if item.soft:
                continue
            sentence = text.excluded_sentence(item, accounts, pinned.facts, pinned.envelope.caps)
            if item.why in ('over_cap', 'unknown_cost'):
                sentence = sentence[:-1] + ', which can’t be approved around.'
            if sentence not in sentences:
                sentences.append(sentence)
        return sentences

    def _skipped(self, job_id, connection=None):
        """What the latest attempt skipped (``delivery_rule_choices``), or the no-route hold's own record."""
        def read(conn):
            t = envelopes.tables(conn)
            if t is None:
                return []
            choices = t['delivery_rule_choices']
            row = conn.execute(sa.select(choices).where(choices.c.job_id == job_id)
                               .order_by(choices.c.created_at.desc()).limit(1)).mappings().one_or_none()
            return envelopes.skipped_of(dict(row) if row else None)[0]
        if connection is not None:
            return read(connection)
        with self.engine.connect() as conn:
            return read(conn)

    def _accounts_for(self, job_id):
        from ..accounts import sending_accounts
        try:
            revision, _ = self.configuration.outbound_context(job_id)
        except Exception:
            return ()
        return sending_accounts(revision.values)

    @staticmethod
    def _view_row(row, *, state=None):
        return {'id': row['id'], 'job_id': row['job_id'], 'kind': row['kind'], 'state': state or row['state'],
                'version': row['version'] + (1 if state and state != row['state'] else 0)}

    def view(self, row, actor, *, approver, zone_name=None):
        """RD's Hold: the fax, who sent it, why it waits, and what this person may do."""
        pinned = None
        try:
            pinned = self.pinned_for(row['job_id'])
        except envelopes.UnreadableDecision:
            pinned = None
        accounts = self._accounts_for(row['job_id'])
        view = {'id': row['id'], 'job_id': row['job_id'], 'kind': row['kind'], 'to_number': row['to_number'],
                'pages': row.get('pages'), 'sender_name': row.get('sender_name'),
                'requested_at': _when(row['requested_at']),
                'until': _when(row['release_at']) if row['kind'] == 'window' else None,
                'reason': row['reason'] or text.WAITS, 'can_decide': self.may_decide(row, actor, approver=approver),
                'version': row['version'], 'state': row['state']}
        if row['kind'] == 'no_route':
            view['options'] = self.options(pinned, row, accounts=accounts)
            view['not_offered'] = self.not_offered(pinned, row, accounts=accounts)
        return view


def _when(value):
    return value.isoformat(timespec='seconds') if isinstance(value, datetime) else value
