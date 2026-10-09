"""Direct delivery partners and the ledger of documents sent and received."""
from datetime import timedelta
import hashlib
import hmac
import json
from uuid import uuid4

import sqlalchemy as sa

from ..intake.store import IntakeStore
from .crypto import FAX_IMAGE
from ..routing.database import read_connection, reflect, utcnow, write_transaction


# A document a partner delivered directly is a received fax Faxbot itself delivered (import source ``local``),
# under an account of ``direct:`` and the partner's enrollment id (inbound/acquisition.py, filing.py).
FILING_SOURCE = 'local'
FILING_ACCOUNT = 'direct:'
CHALLENGE_LIFETIME = timedelta(days=7)
MAX_CODE_FAILURES = 5
VERIFIED_LIFETIME = timedelta(days=365)


def _not_relay(deliveries):
    """Rows that are not documents relayed through a partner (``kind`` is NULL for an original document)."""
    return sa.or_(deliveries.c.kind.is_(None), deliveries.c.kind != 'relay')


class DirectConflict(RuntimeError):
    """Plain-sentence refusal of a partner change."""


def accepts_fax_images(peer):
    """Whether this installation accepts fax images from ``peer``: on by default (NULL), off only when turned off."""
    value = peer.get('receive_fax_images')
    return value is None or int(value) != 0


def code_hash(peer_id, code):
    return hashlib.sha256(f'{peer_id}:{code}'.encode('ascii')).hexdigest()


def normalize_code(code):
    digits = ''.join(character for character in str(code or '') if character.isdigit())
    return digits if len(digits) == 8 else None


class DirectStore:
    TABLES = ('direct_peers', 'direct_deliveries', 'delivery_destinations', 'outbound_deliveries',
              'outbound_attempts', 'intake_items', 'inbound_imports')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.peers = tables['direct_peers']
        self.deliveries = tables['direct_deliveries']
        self.destinations = tables['delivery_destinations']
        self.outbound = tables['outbound_deliveries']
        self.attempts = tables['outbound_attempts']
        self.items, self.imports = tables['intake_items'], tables['inbound_imports']
        self.intake = IntakeStore(engine, None)

    # Partners -------------------------------------------------------------
    def list_peers(self):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.peers).order_by(
                self.peers.c.organization, self.peers.c.phone_number)).mappings()]

    def get_peer(self, peer_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.peers).where(self.peers.c.id == peer_id)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def peer_by_key(self, signing_key, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.peers).where(
                self.peers.c.signing_key == signing_key)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def verified_peer_for(self, number):
        """The verified partner that owns a fax number, or None."""
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.peers).where(
                self.peers.c.phone_number == number, self.peers.c.state == 'verified')).mappings().first()
        return dict(row) if row is not None else None

    def add_peer(self, card, *, own_signing_key):
        if card['signing_key'] == own_signing_key:
            raise DirectConflict("This card belongs to this installation.")
        now = utcnow()
        values = dict(organization=card['organization'].strip(), phone_number=card['fax_number'],
                      endpoint_url=card['endpoint'].rstrip('/'), exchange_key=card['exchange_key'])
        with write_transaction(self.engine) as connection:
            existing = self.peer_by_key(card['signing_key'], connection)
            if existing is not None and existing['state'] != 'revoked':
                raise DirectConflict('This partner is already enrolled.')
            if existing is not None:
                connection.execute(self.peers.update().where(self.peers.c.id == existing['id']).values(
                    **values, state='pending', challenge_hash=None, challenge_job_id=None,
                    challenge_expires_at=None, challenge_failures=0, verified_at=None, expires_at=None,
                    receive_fax_images=None, receive_peer_calls=None, partner_receives_fax_images=None,
                    partner_peer_calls=None, partner_said_at=None, peer_call_address=None, told_partner_at=None,
                    version=existing['version'] + 1, updated_at=now))
                return self.get_peer(existing['id'], connection)
            identity = uuid4().hex
            connection.execute(self.peers.insert().values(
                id=identity, signing_key=card['signing_key'], state='pending', challenge_failures=0, version=1,
                created_at=now, updated_at=now, **values))
            return self.get_peer(identity, connection)

    def start_challenge(self, peer_id, *, code, job_id, now=None):
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            peer = self.get_peer(peer_id, connection)
            if peer is None or peer['state'] == 'revoked':
                raise DirectConflict('This partner is not enrolled.')
            connection.execute(self.peers.update().where(self.peers.c.id == peer_id).values(
                challenge_hash=code_hash(peer_id, code), challenge_job_id=job_id,
                challenge_expires_at=now + CHALLENGE_LIFETIME, challenge_failures=0,
                version=peer['version'] + 1, updated_at=now))
            return self.get_peer(peer_id, connection)

    def confirm_code(self, signing_key, code, *, now=None):
        """A partner proves it received our challenge fax; ``verified``, ``mismatch`` or ``unavailable``."""
        now = now or utcnow()
        code = normalize_code(code)
        with write_transaction(self.engine) as connection:
            peer = self.peer_by_key(signing_key, connection)
            if (peer is None or peer['state'] == 'revoked' or peer['challenge_hash'] is None
                    or peer['challenge_expires_at'] is None or peer['challenge_expires_at'] <= now):
                return 'unavailable', peer
            if code is not None and hmac.compare_digest(peer['challenge_hash'], code_hash(peer['id'], code)):
                connection.execute(self.peers.update().where(self.peers.c.id == peer['id']).values(
                    state='verified', verified_at=now, expires_at=now + VERIFIED_LIFETIME, challenge_hash=None,
                    challenge_expires_at=None, challenge_failures=0, version=peer['version'] + 1, updated_at=now))
                self._link_destination(connection, peer, now)
                return 'verified', self.get_peer(peer['id'], connection)
            failures = peer['challenge_failures'] + 1
            exhausted = failures >= MAX_CODE_FAILURES
            connection.execute(self.peers.update().where(self.peers.c.id == peer['id']).values(
                challenge_failures=failures, challenge_hash=None if exhausted else peer['challenge_hash'],
                challenge_expires_at=None if exhausted else peer['challenge_expires_at'],
                version=peer['version'] + 1, updated_at=now))
            return 'mismatch', peer

    def _link_destination(self, connection, peer, now):
        row = connection.execute(sa.select(self.destinations).where(
            self.destinations.c.phone_number == peer['phone_number'])).mappings().one_or_none()
        if row is None:
            connection.execute(self.destinations.insert().values(
                id=uuid4().hex, phone_number=peer['phone_number'], display_name=peer['organization'],
                direct_peer_id=peer['id'], accepts_references=0, version=1, created_at=now, updated_at=now))
        else:
            connection.execute(self.destinations.update().where(self.destinations.c.id == row['id']).values(
                direct_peer_id=peer['id'], version=row['version'] + 1, updated_at=now))

    def set_receive_fax_images(self, peer_id, accept):
        """This installation's choice to accept fax images from a partner (on by default); the partner is told again."""
        now = utcnow()
        with write_transaction(self.engine) as connection:
            peer = self.get_peer(peer_id, connection)
            if peer is None or peer['state'] == 'revoked':
                raise DirectConflict('This partner is not enrolled.')
            connection.execute(self.peers.update().where(self.peers.c.id == peer_id).values(
                receive_fax_images=1 if accept else 0, told_partner_at=None, version=peer['version'] + 1,
                updated_at=now))
            return self.get_peer(peer_id, connection)

    def set_peer_calls(self, peer_id, *, accept, address):
        """Whether you take peer fax calls from a partner, and its address inside the tunnel (a private address,
        checked by the caller); the partner is told your choice again. None clears the address."""
        now = utcnow()
        with write_transaction(self.engine) as connection:
            peer = self.get_peer(peer_id, connection)
            if peer is None or peer['state'] == 'revoked':
                raise DirectConflict('This partner is not enrolled.')
            connection.execute(self.peers.update().where(self.peers.c.id == peer_id).values(
                receive_peer_calls=1 if accept else None, peer_call_address=address, told_partner_at=None,
                version=peer['version'] + 1, updated_at=now))
            return self.get_peer(peer_id, connection)

    def untold(self, *, limit=50):
        """Enrolled partners that still have to be told, signed, what we accept from them."""
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.peers).where(
                self.peers.c.state != 'revoked', self.peers.c.told_partner_at.is_(None))
                .order_by(self.peers.c.created_at, self.peers.c.id).limit(limit)).mappings()]

    def mark_told(self, peer_id, *, fax_images, peer_calls):
        """The partner has our statement; kept only if our choices are still the ones it was told."""
        with write_transaction(self.engine) as connection:
            peer = self.get_peer(peer_id, connection)
            if (peer is None or accepts_fax_images(peer) != fax_images
                    or (peer['receive_peer_calls'] is not None and int(peer['receive_peer_calls']) == 1) != peer_calls):
                return False
            connection.execute(self.peers.update().where(self.peers.c.id == peer_id).values(told_partner_at=utcnow()))
            return True

    def note_capabilities(self, peer_id, *, fax_images, peer_calls, said_at):
        """Keep what a partner said it accepts from us, unless a newer signed statement is already kept.

        Signed times have one-second resolution; of two statements signed in
        the same second, the one that arrives later is kept.
        """
        with write_transaction(self.engine) as connection:
            peer = self.get_peer(peer_id, connection)
            if peer is None or (peer['partner_said_at'] is not None and peer['partner_said_at'] > said_at):
                return False
            connection.execute(self.peers.update().where(self.peers.c.id == peer_id).values(
                partner_receives_fax_images=1 if fax_images else None, partner_peer_calls=1 if peer_calls else None,
                partner_said_at=said_at, version=peer['version'] + 1, updated_at=utcnow()))
            return True

    def _notices(self):
        if not hasattr(self, '_notice_table'):
            try:
                self._notice_table = reflect(self.engine, ('direct_notices',))['direct_notices']
            except Exception:
                self._notice_table = None
        return self._notice_table

    # Pinned certificates -------------------------------------------------------
    def _pins(self):
        """Builder AT's ``direct_certificate_pins`` (0045), or None while that revision is not installed here."""
        if not hasattr(self, '_pin_table'):
            try:
                self._pin_table = reflect(self.engine, ('direct_certificate_pins',))['direct_certificate_pins']
            except Exception:
                self._pin_table = None
        return self._pin_table

    def certificate_pin(self, host):
        """The SHA-256 of the one certificate accepted for ``host``: the newest pin of an enrolled partner whose
        address is that host, or None (verified normally)."""
        pins = self._pins()
        if pins is None or not host:
            return None
        from urllib.parse import urlsplit
        host = host.lower()
        with read_connection(self.engine) as connection:
            rows = connection.execute(
                sa.select(pins.c.peer_id, pins.c.certificate_sha256, self.peers.c.endpoint_url)
                .select_from(pins.join(self.peers, self.peers.c.id == pins.c.peer_id))
                .where(sa.func.lower(pins.c.host) == host, self.peers.c.state != 'revoked')
                .order_by(pins.c.created_at.desc(), pins.c.id.desc())).all()
        for _, certificate, endpoint in rows:
            if (urlsplit(endpoint).hostname or '').lower() == host:
                return str(certificate).lower()
        return None

    def note_certificate(self, host, seen, *, now=None):
        """Keep, on each enrolled partner at ``host``, the certificate it presented when it was not its pinned
        one (``seen``), or clear it once the pinned certificate is presented again (``seen`` None)."""
        from urllib.parse import urlsplit
        now = now or utcnow()
        host = (host or '').lower()
        with write_transaction(self.engine) as connection:
            for peer in connection.execute(sa.select(self.peers).where(self.peers.c.state != 'revoked')).mappings():
                if (urlsplit(peer['endpoint_url']).hostname or '').lower() != host:
                    continue
                if seen is None and peer['certificate_changed_at'] is None:
                    continue
                if seen is not None and peer['certificate_changed_sha256'] == seen:
                    continue
                connection.execute(self.peers.update().where(self.peers.c.id == peer['id']).values(
                    certificate_changed_sha256=seen, certificate_changed_at=now if seen is not None else None,
                    version=peer['version'] + 1, updated_at=now))

    def set_notice_fax(self, peer_id, on):
        """Pair every original sent to this partner with a one-page notice fax (on), or not (off)."""
        now = utcnow()
        with write_transaction(self.engine) as connection:
            peer = self.get_peer(peer_id, connection)
            if peer is None or peer['state'] == 'revoked':
                raise DirectConflict('This partner is not enrolled.')
            connection.execute(self.peers.update().where(self.peers.c.id == peer_id).values(
                notice_fax=1 if on else None, version=peer['version'] + 1, updated_at=now))
            return self.get_peer(peer_id, connection)

    def revoke(self, peer_id):
        now = utcnow()
        with write_transaction(self.engine) as connection:
            peer = self.get_peer(peer_id, connection)
            if peer is None:
                raise DirectConflict('This partner is not enrolled.')
            connection.execute(self.peers.update().where(self.peers.c.id == peer_id).values(
                state='revoked', challenge_hash=None, challenge_expires_at=None, version=peer['version'] + 1,
                updated_at=now))
            return self.get_peer(peer_id, connection)

    # Ledger ---------------------------------------------------------------
    def find(self, direction, message_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.deliveries).where(
                self.deliveries.c.direction == direction,
                self.deliveries.c.message_id == message_id)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def record_outbound(self, *, message_id, peer_id, job_id, attempt_id, recipient_number, digest, size, manifest,
                        kind=None):
        now = utcnow()
        with write_transaction(self.engine) as connection:
            existing = self.find('outbound', message_id, connection)
            if existing is not None:
                return existing
            connection.execute(self.deliveries.insert().values(
                id=uuid4().hex, direction='outbound', message_id=message_id, peer_id=peer_id, job_id=job_id,
                attempt_id=attempt_id, recipient_number=recipient_number, digest=digest, size_bytes=size,
                manifest=manifest, state='sending', kind=kind, created_at=now, updated_at=now))
            return self.find('outbound', message_id, connection)

    def mark_outbound(self, message_id, state, *, receipt=None):
        now = utcnow()
        with write_transaction(self.engine) as connection:
            values = {'state': state, 'updated_at': now}
            if receipt is not None:
                values.update(receipt=json.dumps(receipt, sort_keys=True), accepted_at=now)
            connection.execute(self.deliveries.update().where(
                self.deliveries.c.direction == 'outbound', self.deliveries.c.message_id == message_id).values(**values))

    def accept_inbound(self, *, message_id, peer, manifest, receipt_for, document_path, now=None):
        """Record a verified document; idempotent on message id.

        Every arrival accepted since 0034 is then filed as a received fax
        (``filing.py``), which gives it email delivery, mailbox rules and a Work
        item. Arrivals accepted before kept their intake item and are never filed
        again, so nothing is emailed twice.
        """
        now = now or utcnow()
        parsed = json.loads(manifest)
        with write_transaction(self.engine) as connection:
            existing = self.find('inbound', message_id, connection)
            if existing is not None:
                return existing, False
            identity = uuid4().hex
            receipt = receipt_for(identity)
            connection.execute(self.deliveries.insert().values(
                id=identity, direction='inbound', message_id=message_id, peer_id=peer['id'],
                recipient_number=parsed['recipient']['fax_number'], digest=parsed['document']['sha256'],
                size_bytes=parsed['document']['size'], manifest=manifest.decode('ascii'), state='accepted',
                receipt=json.dumps(receipt, sort_keys=True), document_path=document_path, accepted_at=now,
                kind=FAX_IMAGE if parsed.get('kind') == FAX_IMAGE else None, created_at=now, updated_at=now))
            return self.find('inbound', message_id, connection), True

    def unfiled(self, *, limit=20):
        """Accepted arrivals not yet filed as received faxes; fences, earlier arrivals and stopped filings excluded."""
        d, items, imports = self.deliveries, self.items, self.imports
        legacy = sa.exists(sa.select(1).where(items.c.direct_delivery_id == d.c.id))
        settled = sa.exists(sa.select(1).where(imports.c.source == FILING_SOURCE,
                                               imports.c.account == sa.func.coalesce(FILING_ACCOUNT + d.c.peer_id,
                                                                                     FILING_ACCOUNT + 'unknown'),
                                               imports.c.operation_id == d.c.message_id,
                                               imports.c.state.in_(('received', 'conflict', 'failed'))))
        # A document a partner sent for relaying is sent on as a fax here, never filed as received (relay.py).
        # A document held for its notice fax is filed when the notice is paired (notice.py). Only a notice still
        # waiting holds it: a cancelled one never holds a document forever.
        notices = self._notices()
        held = (sa.exists(sa.select(1).where(notices.c.role == 'receiver', notices.c.message_id == d.c.message_id,
                                             notices.c.peer_id == d.c.peer_id, notices.c.state == 'waiting'))
                if notices is not None else sa.false())
        query = (sa.select(d).where(d.c.direction == 'inbound', d.c.state == 'accepted', d.c.manifest != '',
                                    d.c.document_path.is_not(None), ~legacy, ~settled, _not_relay(d), ~held)
                 .order_by(d.c.accepted_at, d.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def answer_or_fence(self, message_id, peer, *, now=None):
        """The received record for a sender's question, or a fence so it can never be accepted later.

        A "not received" answer lets the sender fall back to fax, so a delayed
        upload of the same message must not be accepted afterwards. The fence and
        an acceptance share the message id's unique record: whichever commits
        first wins, and the other sees it.
        """
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            existing = self.find('inbound', message_id, connection)
            if existing is not None:
                return existing
            connection.execute(self.deliveries.insert().values(
                id=uuid4().hex, direction='inbound', message_id=message_id, peer_id=peer['id'], recipient_number='',
                digest='0' * 64, size_bytes=0, manifest='', state='refused', created_at=now, updated_at=now))
            return None

    def awaiting_partner(self, *, limit=20):
        """Sent documents whose answer was lost while the fax waits for confirmation."""
        d, o = self.deliveries, self.outbound
        # Relayed documents are reconciled by the relay's own reconciler: accepted there is not delivered.
        query = (sa.select(d).join(o, o.c.id == d.c.job_id)
                 .where(d.c.direction == 'outbound', d.c.state.in_(('sending', 'uncertain')),
                        o.c.state == 'reconciliation_required', o.c.attempt_id == d.c.attempt_id, _not_relay(d))
                 .order_by(d.c.updated_at, d.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def recent(self, *, limit=100):
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.deliveries, self.peers.c.organization)
                                      .select_from(self.deliveries.outerjoin(self.peers,
                                                                             self.peers.c.id == self.deliveries.c.peer_id))
                                      .where(self.deliveries.c.manifest != '')
                                      .order_by(self.deliveries.c.created_at.desc()).limit(limit)).mappings()
            return [dict(row) for row in rows]
