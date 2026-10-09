"""Send once to a partner's intake, which files the document for each of its numbers (research D7).

An organization that files its faxes centrally can agree with one enrolled
partner: "send us one copy, our intake files it for each of these numbers."
Faxes to those numbers then go directly to the partner's intake, and the same
document sent to several of them crosses the internet once.

The agreement (signed, both sides)
----------------------------------
- The receiving organization grants it (``distribution_offer``), naming the
  exact numbers its intake files for and the intake's name. Similar numbers or
  a shared name never grant anything (constraint C3): only these signed
  numbers do.
- The sender's administrator accepts it (``distribution_acceptance``, binding
  the offer's SHA-256), because faxes to those numbers will stop going by
  telephone; it is active on both sides once the partner records it.
- Either side withdraws it at once (``distribution_withdrawal``). A fax the
  partner had already accepted stays accepted; a new one to those numbers is
  refused there, signed, so it goes by telephone instead.

Sending
-------
The route planner gives a covered number to the partner
(``RouteStore.verified_peer(..., covered=True)``). Each fax is still its own
direct delivery: its own message, sealed for its own number, with its own
signed receipt, its own Sent row and its own reconciliation. One send is the
faxes of one sender, with the same document (SHA-256), to different numbers
the same agreement covers, accepted within ``WINDOW`` of each other and still
waiting to go. The first to go carries the document and a signed routing
statement naming every recipient of the send (``POST /direct/distributions``);
each of the others then sends only a reference to that copy (``reuse.py``), so
the document crosses once. Two faxes to the same number are never grouped:
an intentional resend stays its own delivery.

Receiving
---------
The intake accepts a document for a number only under an active agreement
with that partner that covers it. It files each recipient's copy when that
recipient's own fax arrives, never before: a fax that ends up going by
telephone instead is never filed twice. Each filing is its own received fax and
Work item, placed by the receiving organization's own number rules; a number
no rule places waits in Received with no mailbox, and says why. The signed
receipt lists where each recipient of the send is filed.
"""
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
from uuid import uuid4

import httpx
import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..routing.database import read_connection, reflect, utcnow, write_transaction
from .crypto import DirectProtocolError, parse_timestamp, signed, timestamp, verify


STATEMENTS = {'distribution_offer': 'offer', 'distribution_acceptance': 'acceptance',
              'distribution_withdrawal': 'withdrawal'}
ROUTING = 'distribution'
FRESHNESS = timedelta(hours=24)
WINDOW = timedelta(minutes=10)
MAX_NUMBERS = 100
MAX_RECIPIENTS = 50
_ID = re.compile(r'[a-f0-9]{32}')
_HEX64 = re.compile(r'[a-f0-9]{64}')
_NUMBER = re.compile(r'\+[1-9][0-9]{7,14}')

STATE_TEXT = {
    ('receiver', 'offered'): 'Offered; waiting for {partner} to accept.',
    ('receiver', 'accepting'): 'Offered; waiting for {partner} to accept.',
    ('receiver', 'active'): 'Active: {partner} sends one copy to your intake, which files it for each number.',
    ('sender', 'offered'): '{partner} offers to file your faxes to these numbers at its intake. Accept only if these '
                           'numbers are theirs.',
    ('sender', 'accepting'): 'Accepted; telling {partner}.',
    ('sender', 'active'): 'Active: faxes to these numbers go once to {partner}\'s intake, with no telephone call.',
}
ENDED_TEXT = {'us': 'Ended by you.', 'partner': 'Ended by {partner}.'}


class DistributionConflict(RuntimeError):
    """One plain sentence about an agreement that cannot change as asked."""


class RoutingRefused(RuntimeError):
    """A routing statement the intake will not accept; ``reason`` is a stable code, the message one sentence."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def numbers_text(numbers):
    return ', '.join(numbers)


def held_reason(number):
    return f'No receiving rule files faxes to {number}, so it waits in Received with no mailbox.'


def sent_text(partner, recipients):
    """The Sent sentence for a fax that went to a partner's intake (owner's wording for several recipients)."""
    partner = partner or 'the partner'
    if recipients and recipients > 1:
        return f"Sent once to {partner}'s intake, which delivers to {recipients} recipients."
    return f"Sent to {partner}'s intake, which files it for this number."


def received_text(partner, facts):
    """The Received sentence for a document a partner's send filed for one of this installation's numbers."""
    partner = partner or 'a partner'
    recipients = facts.get('recipients')
    if isinstance(recipients, int) and recipients > 1:
        text = (f'Delivered directly by {partner} to your intake, which files one copy for each of {recipients} '
                'numbers; no telephone call.')
    else:
        text = f"Delivered directly by {partner} to your intake for {facts.get('fax_number') or 'this number'}; " \
               'no telephone call.'
    if facts.get('held'):
        text += ' ' + facts['held']
    return text


def _canonical_numbers(raw, *, country):
    from ..routing.numbers import InvalidNumber, normalize_number
    if isinstance(raw, str):
        raw = [part for part in re.split(r'[\s,;]+', raw) if part]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise DistributionConflict('Name at least one of your fax numbers.')
    found = []
    for item in raw:
        try:
            number = normalize_number(str(item), country=country)
        except InvalidNumber:
            raise DistributionConflict(f'{item} is not a fax number; enter it with its country code, such as '
                                       '+15551234567.') from None
        if number not in found:
            found.append(number)
    if len(found) > MAX_NUMBERS:
        raise DistributionConflict(f'Name at most {MAX_NUMBERS} numbers in one agreement.')
    return sorted(found)


def _check_numbers(value):
    """Numbers a partner signed: a short, sorted list of distinct numbers in international form, or None."""
    if (not isinstance(value, list) or not 0 < len(value) <= MAX_NUMBERS
            or any(not isinstance(item, str) or not _NUMBER.fullmatch(item) for item in value)
            or sorted(set(value)) != value):
        return None
    return value


def _clean_intake(value):
    text = ' '.join(str(value or '').split())
    if not text:
        raise DistributionConflict('Name your intake, such as "Central records".')
    if len(text) > 200:
        raise DistributionConflict('Keep the intake name under 200 characters.')
    return text


def digest_of(envelope):
    return hashlib.sha256(envelope['statement'].encode('ascii')).hexdigest()


class DistributionStore:
    TABLES = ('distribution_agreements', 'distribution_statements', 'distribution_sends', 'distribution_members',
              'direct_deliveries', 'direct_peers')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.agreements, self.statements = tables['distribution_agreements'], tables['distribution_statements']
        self.sends, self.members = tables['distribution_sends'], tables['distribution_members']
        self.deliveries, self.peers = tables['direct_deliveries'], tables['direct_peers']

    # Agreements --------------------------------------------------------------------------------------------
    def agreement(self, agreement_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.agreements).where(
                self.agreements.c.id == agreement_id)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def agreements_for(self, *, peer_id=None, role=None):
        query = sa.select(self.agreements).order_by(self.agreements.c.created_at.desc(), self.agreements.c.id)
        if peer_id is not None:
            query = query.where(self.agreements.c.peer_id == peer_id)
        if role is not None:
            query = query.where(self.agreements.c.role == role)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def _keep(self, connection, *, agreement_id, peer_id, direction, kind, envelope, now):
        identity = uuid4().hex
        digest = digest_of(envelope)
        existing = connection.execute(sa.select(self.statements.c.id).where(
            self.statements.c.direction == direction, self.statements.c.digest == digest)).scalar()
        if existing is not None:
            return existing
        connection.execute(self.statements.insert().values(
            id=identity, agreement_id=agreement_id, peer_id=peer_id, direction=direction, kind=kind,
            statement=envelope['statement'], signature=envelope['signature'], digest=digest, created_at=now))
        return identity

    def create(self, *, agreement_id, peer_id, role, state, numbers, intake, envelope, direction, actor_name=None,
               now=None):
        """A new agreement with its signed offer; an offer we sign is kept until the partner has it."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            connection.execute(self.agreements.insert().values(
                id=agreement_id, peer_id=peer_id, role=role, state=state, numbers=json.dumps(numbers),
                intake=intake, offer_digest=digest_of(envelope), pending_statement_id=None, withdrawn_by=None,
                actor_name=(actor_name or '')[:200] or None, version=1, created_at=now, updated_at=now))
            kept = self._keep(connection, agreement_id=agreement_id, peer_id=peer_id, direction=direction,
                              kind='offer', envelope=envelope, now=now)
            if direction == 'sent':
                connection.execute(self.agreements.update().where(self.agreements.c.id == agreement_id).values(
                    pending_statement_id=kept))
            return self.agreement(agreement_id, connection)

    def change(self, agreement_id, *, expected_state=None, envelope=None, kind=None, direction=None, now=None,
               **values):
        """Change an agreement (optionally keeping a signed statement); None when its state is not expected."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = self.agreement(agreement_id, connection)
            if row is None or (expected_state is not None and row['state'] not in expected_state):
                return None
            if envelope is not None:
                kept = self._keep(connection, agreement_id=agreement_id, peer_id=row['peer_id'],
                                  direction=direction, kind=kind, envelope=envelope, now=now)
                if direction == 'sent':
                    values['pending_statement_id'] = kept
            connection.execute(self.agreements.update().where(self.agreements.c.id == agreement_id).values(
                **values, version=row['version'] + 1, updated_at=now))
            return self.agreement(agreement_id, connection)

    def statement(self, statement_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.statements).where(
                self.statements.c.id == statement_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def told(self, agreement_id, statement_id):
        with write_transaction(self.engine) as connection:
            connection.execute(self.agreements.update().where(
                self.agreements.c.id == agreement_id,
                self.agreements.c.pending_statement_id == statement_id).values(pending_statement_id=None))

    def untold(self, *, limit=50):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.agreements).where(
                self.agreements.c.pending_statement_id.is_not(None)).order_by(self.agreements.c.updated_at)
                .limit(limit)).mappings()]

    def covering(self, peer_id, number, *, role, connection=None):
        """The active agreement of ``role`` with this partner whose signed numbers include ``number``, or None."""
        def read(conn):
            rows = conn.execute(sa.select(self.agreements).where(
                self.agreements.c.peer_id == peer_id, self.agreements.c.role == role,
                self.agreements.c.state == 'active')).mappings().all()
            for row in rows:
                if number in json.loads(row['numbers']):
                    return dict(row)
            return None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    # Sends -------------------------------------------------------------------------------------------------
    def send(self, role, message_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.sends).where(
                self.sends.c.role == role, self.sends.c.message_id == message_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def members_of(self, send_id):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.members).where(
                self.members.c.send_id == send_id).order_by(self.members.c.place)).mappings()]

    def record_send(self, *, role, message_id, agreement_id, peer_id, digest, size, envelope, members, now=None):
        """One send on this side, recorded once with its recipients; returns the send row."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            existing = connection.execute(sa.select(self.sends).where(
                self.sends.c.role == role, self.sends.c.message_id == message_id)).mappings().one_or_none()
            if existing is not None:
                return dict(existing)
            identity = uuid4().hex
            connection.execute(self.sends.insert().values(
                id=identity, role=role, message_id=message_id, agreement_id=agreement_id, peer_id=peer_id,
                document_sha256=digest, size_bytes=size, routing_statement=envelope['statement'],
                routing_signature=envelope['signature'], created_at=now))
            for place, member in enumerate(members):
                connection.execute(self.members.insert().values(
                    id=uuid4().hex, send_id=identity, place=place, recipient_number=member['number'],
                    job_id=member.get('job_id'), mailbox_label=member.get('mailbox'),
                    held_reason=member.get('held'), created_at=now))
            return dict(connection.execute(sa.select(self.sends).where(self.sends.c.id == identity)).mappings().one())

    def _delivery_state(self, connection, direction, message_id):
        return connection.execute(sa.select(self.deliveries.c.state).where(
            self.deliveries.c.direction == direction, self.deliveries.c.message_id == message_id)).scalar()

    def live_send_for_job(self, job_id):
        """The sender-side send that names this fax and whose first fax was not refused, or None."""
        s, m = self.sends, self.members
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(s).select_from(s.join(m, m.c.send_id == s.c.id)).where(
                s.c.role == 'sender', m.c.job_id == job_id).order_by(s.c.created_at.desc())).mappings().all()
            for row in rows:
                if self._delivery_state(connection, 'outbound', row['message_id']) != 'refused':
                    return dict(row)
        return None

    def received_send(self, peer_id, digest, number):
        """The receiver-side send from this partner, for this document, that named ``number``; (send, member)."""
        s, m = self.sends, self.members
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(s, m.c.mailbox_label, m.c.held_reason).select_from(
                s.join(m, m.c.send_id == s.c.id)).where(
                s.c.role == 'receiver', s.c.peer_id == peer_id, s.c.document_sha256 == digest,
                m.c.recipient_number == number).order_by(s.c.created_at.desc()).limit(1)).mappings().first()
        return dict(row) if row is not None else None


def peer_for_number(connection, number, now, peers_table):
    """Partners whose active "send once" agreement (as the receiving side) covers ``number``: verified, unexpired.

    Used by ``RouteStore.verified_peer(..., covered=True)``: the route planner gives such a number to that partner.
    Returns a list (the caller keeps its "exactly one" rule).
    """
    if not sa.inspect(connection).has_table('distribution_agreements'):
        return []  # before 0049
    metadata = sa.MetaData()
    metadata.reflect(connection, only=['distribution_agreements'])
    agreements = metadata.tables['distribution_agreements']
    p, a = peers_table, agreements
    rows = connection.execute(sa.select(p, a.c.numbers).select_from(p.join(a, a.c.peer_id == p.c.id)).where(
        a.c.role == 'sender', a.c.state == 'active', p.c.state == 'verified',
        sa.or_(p.c.expires_at.is_(None), p.c.expires_at > now))).mappings().all()
    found = {}
    for row in rows:
        if number in json.loads(row['numbers']):
            found[row['id']] = {key: value for key, value in row.items() if key != 'numbers'}
    return list(found.values())


class SendOnce:
    """Agreements, the partner protocol, one send's grouping, and the intake's side, over a direct service."""

    def __init__(self, direct):
        self.direct = direct
        self.store = DistributionStore(direct.store.engine)

    @property
    def engine(self):
        return self.direct.store.engine

    def _values(self):
        return self.direct.values()

    def _peer(self, peer_id):
        peer = self.direct.store.get_peer(peer_id)
        if peer is None or peer['state'] == 'revoked':
            raise DistributionConflict('This partner is not enrolled.')
        return peer

    def _sign(self, peer, kind, **fields):
        identity = self.direct.identity(create=True)
        return signed(identity, {'type': kind, 'recipient': peer['signing_key'], 'said_at': timestamp(), **fields})

    # The receiving organization: granting --------------------------------------------------------------------
    def grant(self, peer_id, numbers, intake, *, actor_name=None, now=None):
        """Offer "send once, we distribute" to a partner for exactly ``numbers``; signed, kept until it has it."""
        values = self._values()
        if not values.direct_delivery_enabled:
            raise DistributionConflict('Turn on direct delivery first.')
        # Receiving needs only an enrolled partner (as every direct delivery does); the partner accepts the offer
        # only from an installation whose number it verified.
        peer = self._peer(peer_id)
        if any(row['state'] in ('offered', 'accepting', 'active')
               for row in self.store.agreements_for(peer_id=peer_id, role='receiver')):
            raise DistributionConflict(f"You already receive this way from {peer['organization']}; end that "
                                       'agreement first to change it.')
        numbers = _canonical_numbers(numbers, country=getattr(values, 'fax_default_country', 'US') or 'US')
        intake = _clean_intake(intake)
        agreement_id = uuid4().hex
        offer = self._sign(peer, 'distribution_offer', agreement=agreement_id, numbers=numbers, intake=intake)
        return self.store.create(agreement_id=agreement_id, peer_id=peer_id, role='receiver', state='offered',
                                 numbers=numbers, intake=intake, envelope=offer, direction='sent',
                                 actor_name=actor_name, now=now)

    # The sender: accepting ------------------------------------------------------------------------------------
    def accept(self, agreement_id, *, actor_name=None, now=None):
        """Accept a partner's offer; faxes to its numbers go to its intake once the partner records this."""
        row = self.store.agreement(agreement_id)
        if row is None or row['role'] != 'sender' or row['state'] not in ('offered', 'accepting'):
            raise DistributionConflict('There is no open offer to accept.')
        peer = self._peer(row['peer_id'])
        statement = self._sign(peer, 'distribution_acceptance', agreement=agreement_id, offer=row['offer_digest'])
        return self.store.change(agreement_id, expected_state=('offered', 'accepting'), envelope=statement,
                                 kind='acceptance', direction='sent', state='accepting',
                                 actor_name=(actor_name or '')[:200] or row['actor_name'], now=now)

    # Either side: withdrawing ---------------------------------------------------------------------------------
    def withdraw(self, agreement_id, *, actor_name=None, now=None):
        row = self.store.agreement(agreement_id)
        if row is None or row['state'] == 'withdrawn':
            raise DistributionConflict('This agreement has already ended.')
        peer = self.direct.store.get_peer(row['peer_id'])
        statement = None
        if peer is not None and peer['state'] != 'revoked':
            statement = self._sign(peer, 'distribution_withdrawal', agreement=agreement_id)
        extra = {} if statement is not None else {'pending_statement_id': None}
        changed = self.store.change(agreement_id, expected_state=('offered', 'accepting', 'active'),
                                    envelope=statement, kind='withdrawal', direction='sent', state='withdrawn',
                                    withdrawn_by='us', actor_name=(actor_name or '')[:200] or row['actor_name'],
                                    now=now, **extra)
        if changed is None:
            raise DistributionConflict('This agreement has already ended.')
        return changed

    # Telling the partner --------------------------------------------------------------------------------------
    async def tell(self, row):
        """Send the agreement's latest signed statement: ``told``, ``refused`` or ``unreachable``."""
        statement_id = row['pending_statement_id']
        if not statement_id:
            return 'told'
        peer = await run_lifecycle_step(lambda: self.direct.store.get_peer(row['peer_id']))
        kept = await run_lifecycle_step(lambda: self.store.statement(statement_id))
        if peer is None or kept is None:
            return 'unreachable'
        from .service import PartnerUnreachable
        try:
            status, body = await self.direct.http.request(
                'POST', peer['endpoint_url'] + '/direct/distribution/statements',
                json={'statement': kept['statement'], 'signature': kept['signature']})
        except (PartnerUnreachable, httpx.HTTPError, OSError):
            return 'unreachable'
        if status == 200 and isinstance(body, dict) and body.get('recorded') is True:
            await run_lifecycle_step(lambda: self.store.told(row['id'], statement_id))
            if kept['kind'] == 'acceptance':
                await run_lifecycle_step(lambda: self.store.change(row['id'], expected_state=('accepting',),
                                                                   state='active'))
            return 'told'
        if 400 <= status < 500 and status not in (404, 405, 429):
            # The partner read it and said no; asking again would not change that.
            await run_lifecycle_step(lambda: self.store.told(row['id'], statement_id))
            if kept['kind'] == 'acceptance':
                await run_lifecycle_step(lambda: self.store.change(row['id'], expected_state=('accepting',),
                                                                   state='offered'))
            return 'refused'
        return 'unreachable'

    async def tell_all(self):
        values = await run_lifecycle_step(self._values)
        if not getattr(values, 'direct_delivery_enabled', False) or not self.direct.ready():
            return False
        for row in await run_lifecycle_step(self.store.untold):
            await self.tell(row)
        return False

    # The partner protocol -------------------------------------------------------------------------------------
    def hear(self, statement, signature, *, now=None):
        """A partner's signed agreement statement; returns (status, body)."""
        now = now or utcnow()
        _, identity = self.direct._enabled_identity()
        refused = (400, {'recorded': False, 'detail': 'This statement could not be checked.'})
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
            if (not isinstance(body, dict) or body.get('type') not in STATEMENTS
                    or body.get('recipient') != identity.signing_key or not isinstance(body.get('signer'), str)):
                return refused
            peer = self.direct.store.peer_by_key(body['signer'])
            if peer is None or peer['state'] == 'revoked':
                return 403, {'recorded': False, 'detail': 'This installation does not accept statements from you.'}
            verify(peer['signing_key'], encoded, signature)
            if abs(parse_timestamp(body.get('said_at')) - now) > FRESHNESS:
                return refused
        except (ValueError, TypeError, UnicodeEncodeError, DirectProtocolError):
            return refused
        agreement_id = body.get('agreement')
        if not isinstance(agreement_id, str) or not _ID.fullmatch(agreement_id):
            return refused
        envelope = {'statement': statement, 'signature': signature}
        row = self.store.agreement(agreement_id)
        if row is not None and row['peer_id'] != peer['id']:
            return 409, {'recorded': False, 'detail': 'This statement names another agreement.'}
        kind = STATEMENTS[body['type']]
        if kind == 'offer':
            numbers, intake = _check_numbers(body.get('numbers')), body.get('intake')
            if (numbers is None or not isinstance(intake, str) or not 0 < len(intake) <= 200
                    or set(body) != {'type', 'recipient', 'said_at', 'signer', 'agreement', 'numbers', 'intake'}):
                return 400, {'recorded': False, 'detail': 'This offer could not be read.'}
            if row is not None:
                return 409, {'recorded': False, 'detail': 'This agreement was already offered.'}
            if peer['state'] != 'verified':
                return 409, {'recorded': False, 'detail': 'This installation has not verified your number yet.'}
            self.store.create(agreement_id=agreement_id, peer_id=peer['id'], role='sender', state='offered',
                              numbers=numbers, intake=intake, envelope=envelope, direction='received', now=now)
            return 200, {'recorded': True}
        if kind == 'acceptance':
            if row is None or row['role'] != 'receiver' or row['state'] not in ('offered', 'active'):
                return 409, {'recorded': False, 'detail': 'There is no open offer from this installation to accept.'}
            if body.get('offer') != row['offer_digest']:
                return 409, {'recorded': False, 'detail': 'The offer changed; accept the new one.'}
            self.store.change(agreement_id, envelope=envelope, kind='acceptance', direction='received',
                              state='active', now=now)
            return 200, {'recorded': True}
        if row is None:
            return 409, {'recorded': False, 'detail': 'There is no such agreement.'}
        self.store.change(agreement_id, expected_state=('offered', 'accepting', 'active'), envelope=envelope,
                          kind='withdrawal', direction='received', state='withdrawn', withdrawn_by='partner',
                          pending_statement_id=None, now=now)
        return 200, {'recorded': True}

    # Views ------------------------------------------------------------------------------------------------------
    def view(self, row, organization=None):
        partner = organization or 'the partner'
        numbers = json.loads(row['numbers'])
        if row['state'] == 'withdrawn':
            status = ENDED_TEXT[row['withdrawn_by'] or 'us'].format(partner=partner)
        else:
            status = STATE_TEXT[(row['role'], row['state'])].format(partner=partner)
        if row['role'] == 'receiver':
            summary = f"Your intake \"{row['intake']}\" files {partner}'s faxes for {len(numbers)} " \
                      f"number{'s' if len(numbers) != 1 else ''}."
        else:
            summary = f"{partner}'s intake \"{row['intake']}\" files your faxes for {len(numbers)} " \
                      f"number{'s' if len(numbers) != 1 else ''}."
        view = {'id': row['id'], 'peer_id': row['peer_id'], 'partner': organization, 'role': row['role'],
                'state': row['state'], 'numbers': numbers, 'intake': row['intake'], 'summary': summary,
                'status': status, 'told': row['pending_statement_id'] is None,
                'created_at': row['created_at'], 'updated_at': row['updated_at']}
        if row['role'] == 'receiver' and row['state'] != 'withdrawn':
            view['placements'] = [{'fax_number': number, **self.placement(number)} for number in numbers]
        return view

    # The intake: where each number is filed --------------------------------------------------------------------
    def placement(self, number, *, from_number=None, now=None):
        """Where this installation's receiving rules file a fax to ``number``: {'mailbox', 'held'}."""
        resources = self.direct.filing.resources()
        if resources is None:
            return {'mailbox': None, 'held': None}
        from ..access.receiving_rules import ReceivedFacts
        values = self._values()
        country = getattr(values, 'fax_default_country', 'US') or 'US'
        facts = ReceivedFacts(to_number=number, from_number=from_number, received_at=now or utcnow(),
                              time_zone=getattr(values, 'time_zone', '') or '')
        try:
            with resources.store.engine.connect() as connection:
                rule = resources._rule_on(connection, facts, country)
        except Exception:
            logging.getLogger(__name__).warning('Receiving rules could not be read for a partner send.')
            return {'mailbox': None, 'held': None}
        if rule is None:
            return {'mailbox': None, 'held': held_reason(number)}
        return {'mailbox': rule['mailbox_label'], 'held': None}

    def covering_received(self, peer, number):
        """The active agreement under which this installation's intake files ``peer``'s faxes to ``number``."""
        return self.store.covering(peer['id'], number, role='receiver')

    def check_routing(self, identity, peer, manifest, routing):
        """The routing statement sent with a send's first document; (agreement, recipients) or RoutingRefused."""
        statement, signature = routing
        try:
            encoded = statement.encode('ascii')
            verify(peer['signing_key'], encoded, signature)
            body = json.loads(encoded)
        except (AttributeError, ValueError, UnicodeEncodeError, DirectProtocolError):
            raise RoutingRefused('routing', 'The list of recipients could not be checked.') from None
        recipients = body.get('recipients') if isinstance(body, dict) else None
        if (not isinstance(body, dict) or body.get('type') != ROUTING or body.get('signer') != peer['signing_key']
                or body.get('recipient') != identity.signing_key
                or body.get('message_id') != manifest['message_id']
                or body.get('document_sha256') != manifest['document']['sha256']
                or not isinstance(recipients, list) or not 0 < len(recipients) <= MAX_RECIPIENTS
                or any(not isinstance(item, str) or not _NUMBER.fullmatch(item) for item in recipients)
                or len(set(recipients)) != len(recipients)
                or recipients[0] != manifest['recipient']['fax_number']):
            raise RoutingRefused('routing', 'The list of recipients could not be checked.')
        agreement = self.store.agreement(body.get('agreement')) if isinstance(body.get('agreement'), str) else None
        if (agreement is None or agreement['peer_id'] != peer['id'] or agreement['role'] != 'receiver'
                or agreement['state'] != 'active'):
            raise RoutingRefused('not_covered', 'This installation has no active agreement to file your faxes '
                                                'at its intake.')
        covered = set(json.loads(agreement['numbers']))
        if any(number not in covered for number in recipients):
            raise RoutingRefused('not_covered', 'Some of these numbers are not in your agreement with this '
                                                'installation.')
        return agreement, recipients

    def record_received(self, *, agreement, peer, manifest, routing, recipients, placements, now=None):
        """The intake's record of one send: every recipient and where its rules file it."""
        return self.store.record_send(
            role='receiver', message_id=manifest['message_id'], agreement_id=agreement['id'], peer_id=peer['id'],
            digest=manifest['document']['sha256'], size=manifest['document']['size'],
            envelope={'statement': routing[0], 'signature': routing[1]},
            members=[{'number': number, **placements[number]} for number in recipients], now=now)

    def receipt_facts(self, peer, manifest, *, recipients=None, placements=None, now=None):
        """What the signed receipt says about a document filed for one of the intake's numbers."""
        number = manifest['recipient']['fax_number']
        own = None
        if recipients is None:
            # A later recipient of a send whose first document came earlier: the list it came with, and where this
            # copy is filed now.
            send = self.store.received_send(peer['id'], manifest['document']['sha256'], number)
            if send is not None:
                members = self.store.members_of(send['id'])
                recipients = [member['recipient_number'] for member in members]
                placements = {member['recipient_number']: {'mailbox': member['mailbox_label'],
                                                           'held': member['held_reason']} for member in members}
        else:
            own = (placements or {}).get(number)
        own = own or self.placement(number, from_number=manifest['sender']['fax_number'], now=now)
        facts = {'placement': {'fax_number': number, **own}}
        if recipients:
            facts['distribution'] = {'recipients': [{'fax_number': item, **(placements or {}).get(
                item, {'mailbox': None, 'held': None})} for item in recipients]}
        return facts

    # The sender: one send ------------------------------------------------------------------------------------
    def gather(self, *, job_id, to_number, agreement, digest, values, now=None):
        """The other faxes of this fax's send, oldest first: [{'job_id', 'number'}] (see the module notes)."""
        now = now or utcnow()
        numbers = set(json.loads(agreement['numbers'])) - {to_number}
        if not numbers:
            return []
        engine = self.engine
        tables = reflect(engine, ('fax_jobs', 'outbound_deliveries', 'access_resources'))
        jobs, deliveries, resources = tables['fax_jobs'], tables['outbound_deliveries'], tables['access_resources']
        with read_connection(engine) as connection:
            leader = connection.execute(sa.select(jobs.c.created_at).where(jobs.c.id == job_id)).scalar()
            owner = connection.execute(sa.select(resources.c.parent_id).where(
                resources.c.kind == 'outbound', resources.c.fax_job_id == job_id)).scalar()
            if leader is None:
                return []
            columns = [jobs.c.id, jobs.c.to_number, jobs.c.created_at]
            if 'send_by_call' in jobs.c:
                columns.append(jobs.c.send_by_call)
            query = (sa.select(*columns, resources.c.parent_id)
                     .select_from(jobs.join(deliveries, deliveries.c.id == jobs.c.id)
                                  .outerjoin(resources, sa.and_(resources.c.kind == 'outbound',
                                                                resources.c.fax_job_id == jobs.c.id)))
                     .where(deliveries.c.state == 'ready', deliveries.c.dispatch_mode == 'normal',
                            jobs.c.id != job_id, jobs.c.to_number.in_(sorted(numbers)),
                            jobs.c.created_at >= leader - WINDOW, jobs.c.created_at <= now)
                     .order_by(jobs.c.created_at, jobs.c.id))
            rows = [dict(row) for row in connection.execute(query).mappings()]
        found, seen = [], set()
        folder = Path(values.fax_data_dir)
        from ..routing.holds import document_sha256
        from ..routing.policy import DIRECT
        from ..routing import envelope as envelopes
        for row in rows:
            if row['to_number'] in seen or row['parent_id'] != owner or row.get('send_by_call'):
                continue
            if self.store.live_send_for_job(row['id']) is not None:
                continue  # already named by another send
            if re.fullmatch('[a-f0-9]{32}', row['id']) is None or document_sha256(folder / f"{row['id']}.pdf") != digest:
                continue
            try:
                pinned = envelopes.load(engine, row['id'])
            except Exception:
                continue
            if pinned is not None and not pinned.allows(DIRECT):
                continue  # its rules keep it off direct delivery, so it never joins a send
            seen.add(row['to_number'])
            found.append({'job_id': row['id'], 'number': row['to_number']})
            if len(found) >= MAX_RECIPIENTS - 1:
                break
        return found

    def start(self, *, identity, peer, agreement, message_id, job_id, to_number, digest, size, others, now=None):
        """Record this fax's send and sign its routing statement; (send, envelope)."""
        recipients = [to_number] + [other['number'] for other in others]
        envelope = signed(identity, {'type': ROUTING, 'recipient': peer['signing_key'], 'said_at': timestamp(),
                                     'agreement': agreement['id'], 'message_id': message_id,
                                     'document_sha256': digest, 'recipients': recipients})
        send = self.store.record_send(
            role='sender', message_id=message_id, agreement_id=agreement['id'], peer_id=peer['id'], digest=digest,
            size=size, envelope=envelope,
            members=[{'number': to_number, 'job_id': job_id}] + [{'number': o['number'], 'job_id': o['job_id']}
                                                                 for o in others], now=now)
        return send, {'statement': send['routing_statement'], 'signature': send['routing_signature']}

    def outbound_texts(self, rows):
        """{message_id: Sent sentence} for outbound direct deliveries that went to a partner's intake."""
        texts = {}
        for row in rows:
            if row.get('direction') != 'outbound' or not row.get('job_id'):
                continue
            send = self.store.live_send_for_job(row['job_id'])
            if send is not None:
                count = len(self.store.members_of(send['id']))
            else:
                # Sealed for one of the partner's other numbers: it went to the partner's intake on its own.
                peer = self.direct.store.get_peer(row['peer_id']) if row.get('peer_id') else None
                if peer is None or row['recipient_number'] == peer['phone_number']:
                    continue
                count = 1
            texts[row['message_id']] = sent_text(row.get('organization'), count)
        return texts


def sent_texts(service, rows):
    """{message_id: Sent sentence} for accepted outbound direct deliveries that went to a partner's intake, or
    as a reference or the changes to a version it held (``reuse.py``)."""
    accepted = [row for row in rows if row.get('direction') == 'outbound' and row.get('state') == 'accepted']
    if not accepted:
        return {}
    texts = SendOnce(service).outbound_texts(accepted)
    from .reuse import ReuseStore, sent_text as reuse_text
    store = ReuseStore(service.store.engine)
    for row in accepted:
        if row['message_id'] not in texts:
            saving = store.saving(row['message_id'])
            if saving is not None:
                texts[row['message_id']] = reuse_text(row.get('organization'), saving)
    return texts


def filing_facts(row):
    """What this installation's own signed receipt said about an arrival filed at its intake or sent without its
    full bytes, for the received fax's report: {'fax_number', 'mailbox', 'held', 'recipients', 'carriage'}."""
    try:
        statement = json.loads(json.loads(row.get('receipt') or '{}').get('statement') or '{}')
    except (TypeError, ValueError, AttributeError):
        return None
    placement = statement.get('placement') if isinstance(statement.get('placement'), dict) else None
    carriage = statement.get('carriage') if statement.get('carriage') in ('reference', 'patch') else None
    if placement is None and carriage is None:
        return None
    facts = {'carriage': carriage}
    if placement is not None:
        distribution = statement.get('distribution') if isinstance(statement.get('distribution'), dict) else {}
        recipients = distribution.get('recipients') if isinstance(distribution.get('recipients'), list) else []
        facts.update(fax_number=placement.get('fax_number'), mailbox=placement.get('mailbox'),
                     held=placement.get('held'), recipients=len(recipients) or 1)
    return facts


def received_sentence(partner, facts):
    """The Received sentence for an arrival with ``filing_facts``."""
    if facts.get('fax_number'):
        return received_text(partner, facts)
    from .reuse import received_text as reuse_text
    return reuse_text(partner, facts.get('carriage'))
