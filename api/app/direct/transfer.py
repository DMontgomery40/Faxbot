"""Resumable transfer of a sealed document to an enrolled partner: preflight, pieces, repair and one commit.

A large document no longer goes to a partner in one request that a dropped
connection wastes. The sealed document (the same signed manifest and
ciphertext as a single delivery, ``crypto.seal``) is cut into pieces: byte
ranges of the ciphertext, each with a sequence number, offset and SHA-256.

1. **Preflight (D17).** The sender signs a transfer statement (message ID,
   size, piece size and every piece's SHA-256) and sends it with the signed
   manifest before any document byte. The receiver checks the sender, the
   recipient, the type, the size, the page count, freshness, room on disk and
   whether it already has this message, and refuses early with a signed reason.
   Equal bytes under another message ID are never refused: the SHA-256 binds
   the document to its message; it does not deduplicate documents (R05 §9.3).
2. **Pieces (D18).** Each piece is uploaded at its offset (like tus's
   ``Upload-Offset``), signed by the sender. The receiver keeps a piece only if
   its SHA-256 is the one the signed statement named, and keeps it once: the
   same piece again is acknowledged and dropped (duplicates suppressed), a
   different piece under the same sequence is refused.
3. **Repair (D8).** After a dropped connection the sender asks the receiver
   what it holds. The receiver answers, signed, with the missing pieces by
   sequence and SHA-256, and only those are sent. Asking never fences the
   message: it is a question about pieces, not about the document.
4. **Commit.** The sender's signed commit asks the receiver to assemble the
   document. Only when every piece and the whole ciphertext match is it handed
   to the ordinary receiving step (``DirectService.receive``), which decrypts,
   checks, stores and records it exactly once per message ID, and answers with
   the same signed receipt or refusal a single delivery gets. A repeated commit
   returns that receipt; it never makes a second document.

Nothing here is a blind resend (C1): a piece goes again only when the receiver
said, signed, that it does not hold it, and the commit is idempotent. Until a
commit is answered, the sender's fax waits for confirmation; the reconciler
resumes the transfer, and falls back to asking (and fencing) only when the
transfer can no longer complete.
"""
import asyncio
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
from uuid import uuid4

import httpx
import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..routing.database import read_connection, reflect, utcnow, write_transaction
from .crypto import (FAX_IMAGE, DirectProtocolError, canonical, check_signed, kind_of, parse_manifest,
                     parse_timestamp, signed, timestamp, verify)


PIECE_SIZE = 1024 * 1024
# A sealed document larger than this goes in pieces; smaller ones go in one request as before.
THRESHOLD = 4 * PIECE_SIZE
MIN_PIECE_SIZE = 1024
MAX_PIECE_SIZE = 8 * 1024 * 1024
MAX_PIECES = 4096
# Open transfers one partner may have here at once, and how long an unfinished one is kept.
OPEN_PER_PARTNER = 8
OPEN_FOR = timedelta(hours=23)  # inside the 24 hours a signed manifest stays fresh (service.FRESHNESS)
REQUEST_SKEW = timedelta(minutes=5)
RESUME_TRIES = 3
TAG_BYTES = 16  # AES-GCM's tag: the ciphertext is the document plus 16 bytes

MESSAGE = re.compile(r'[a-f0-9]{32}')
_HEX64 = re.compile(r'[a-f0-9]{64}')

STATE_TEXT = {
    'open': 'Sending in pieces.',
    'committed': 'Delivered; every piece matched.',
    'refused': 'Refused by the partner.',
    'abandoned': 'Stopped before it was complete.',
}


class TransferUnsupported(RuntimeError):
    """The partner's Faxbot cannot take documents in pieces (it answered 404); nothing was sent."""


def piece_hashes(ciphertext, piece_size):
    return [hashlib.sha256(ciphertext[at:at + piece_size]).hexdigest()
            for at in range(0, len(ciphertext), piece_size)]


def piece_length(size, piece_size, sequence):
    return min(piece_size, size - sequence * piece_size)


def _missing(hashes, held):
    return [{'sequence': number, 'sha256': digest} for number, digest in enumerate(hashes) if number not in held]


def _offset(held, piece_size, size, count):
    """Bytes held contiguously from the start (tus's Upload-Offset)."""
    number = 0
    while number < count and number in held:
        number += 1
    return min(size, number * piece_size)


def parse_offer(text):
    """A signed transfer statement's content (shape only; the signature is checked separately), or None."""
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return None
    if (not isinstance(body, dict)
            or set(body) != {'type', 'message_id', 'size', 'piece_size', 'pieces', 'signer', 'recipient'}
            or body['type'] != 'transfer' or not isinstance(body['message_id'], str)
            or MESSAGE.fullmatch(body['message_id']) is None or type(body['size']) is not int
            or type(body['piece_size']) is not int or not isinstance(body['pieces'], list)):
        return None
    if not MIN_PIECE_SIZE <= body['piece_size'] <= MAX_PIECE_SIZE or not 0 < len(body['pieces']) <= MAX_PIECES:
        return None
    if body['size'] <= 0 or -(-body['size'] // body['piece_size']) != len(body['pieces']):
        return None
    if not all(isinstance(digest, str) and _HEX64.fullmatch(digest) for digest in body['pieces']):
        return None
    return body


class TransferStore:
    """``direct_transfers`` and ``direct_transfer_pieces`` (0046)."""

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, ('direct_transfers', 'direct_transfer_pieces'))
        self.transfers, self.pieces = tables['direct_transfers'], tables['direct_transfer_pieces']

    def find(self, role, message_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.transfers).where(
                self.transfers.c.role == role, self.transfers.c.message_id == message_id)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def held(self, transfer_id, connection=None):
        def read(conn):
            return {row.sequence: row.sha256 for row in conn.execute(sa.select(
                self.pieces.c.sequence, self.pieces.c.sha256).where(self.pieces.c.transfer_id == transfer_id))}
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def open_count(self, peer_id):
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(sa.func.count()).select_from(self.transfers).where(
                self.transfers.c.role == 'receiver', self.transfers.c.peer_id == peer_id,
                self.transfers.c.state == 'open')).scalar()

    def create(self, *, role, message_id, peer_id, manifest, signature, offer, offer_signature, size, piece_size,
               pieces, folder, now=None):
        """The transfer for ``message_id`` on this side, created once; returns (row, created)."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            existing = self.find(role, message_id, connection)
            if existing is not None:
                return existing, False
            connection.execute(self.transfers.insert().values(
                id=uuid4().hex, role=role, message_id=message_id, peer_id=peer_id, manifest=manifest,
                signature=signature, offer=offer, offer_signature=offer_signature, size=size, piece_size=piece_size,
                pieces=pieces, confirmed=0, state='open', folder=folder, created_at=now, updated_at=now))
            return self.find(role, message_id, connection), True

    def keep_piece(self, transfer_id, sequence, digest, size, *, now=None):
        """Record a held piece once; False when it was already held (a duplicate)."""
        now = now or utcnow()
        try:
            with write_transaction(self.engine) as connection:
                if connection.execute(sa.select(self.pieces.c.id).where(
                        self.pieces.c.transfer_id == transfer_id, self.pieces.c.sequence == sequence)).first():
                    return False
                connection.execute(self.pieces.insert().values(
                    id=uuid4().hex, transfer_id=transfer_id, sequence=sequence, sha256=digest, size=size,
                    received_at=now))
                count = connection.execute(sa.select(sa.func.count()).select_from(self.pieces).where(
                    self.pieces.c.transfer_id == transfer_id)).scalar()
                connection.execute(self.transfers.update().where(self.transfers.c.id == transfer_id).values(
                    confirmed=count, updated_at=now))
                return True
        except sa.exc.IntegrityError:
            return False

    def forget_piece(self, transfer_id, sequence):
        """A held piece whose stored bytes no longer match: forgotten, so the receiver asks for it again."""
        with write_transaction(self.engine) as connection:
            connection.execute(self.pieces.delete().where(self.pieces.c.transfer_id == transfer_id,
                                                          self.pieces.c.sequence == sequence))

    def finish(self, transfer_id, state, *, detail=None, now=None):
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            connection.execute(self.transfers.update().where(
                self.transfers.c.id == transfer_id, self.transfers.c.state == 'open').values(
                state=state, detail=(detail or None) and str(detail)[:300], updated_at=now, finished_at=now))

    def note_confirmed(self, transfer_id, confirmed):
        with write_transaction(self.engine) as connection:
            connection.execute(self.transfers.update().where(self.transfers.c.id == transfer_id).values(
                confirmed=max(0, int(confirmed)), updated_at=utcnow()))

    def stale(self, *, before, limit=50):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.transfers).where(
                self.transfers.c.state == 'open', self.transfers.c.created_at < before)
                .order_by(self.transfers.c.created_at).limit(limit)).mappings()]

    def recent(self, *, limit=100):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.transfers).order_by(
                self.transfers.c.created_at.desc()).limit(limit)).mappings()]


def _remove(folder):
    if folder:
        shutil.rmtree(folder, ignore_errors=True) if os.path.isdir(folder) else Path(folder).unlink(missing_ok=True)


def _write_private(path, data):
    """Write ``data`` at ``path`` whole or not at all (a temporary file renamed over it), mode 0600."""
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# Receiving ---------------------------------------------------------------------------------------

class TransferReceiver:
    """The partner protocol on the receiving side; each method returns (HTTP status, body)."""

    def __init__(self, service):
        self.service = service
        self.store = TransferStore(service.store.engine)

    def _folder(self, values, message_id):
        return Path(values.fax_data_dir) / 'direct' / 'transfers' / message_id

    def _answer(self, identity, row, status='open', **extra):
        hashes = json.loads(row['offer'])['pieces']
        held = self.store.held(row['id'])
        statement = {'type': 'transfer', 'message_id': row['message_id'], 'status': status,
                     'have': sorted(held), 'missing': _missing(hashes, held),
                     'offset': _offset(held, row['piece_size'], row['size'], row['pieces']), **extra}
        return signed(identity, statement)

    def _refusal(self, identity, message_id, reason, text, peer=None):
        return self.service._refusal(identity, message_id, reason, text, peer)

    def _settled(self, identity, row, peer):
        """The answer for a transfer that already finished: its receipt, or why it was refused."""
        delivery = self.service.store.find('inbound', row['message_id'])
        if row['state'] == 'committed' and delivery is not None and delivery['state'] == 'accepted':
            return 200, json.loads(delivery['receipt'])
        if delivery is not None and delivery['state'] == 'refused':
            return 409, self.service._withdrawn(identity, row['message_id'], peer)
        return 409, self._refusal(identity, row['message_id'], 'transfer_' + row['state'],
                                  row['detail'] or 'This transfer is no longer open; send the document again.', peer)

    def open(self, body, *, now=None):
        """Preflight: check everything about the document before any of its bytes, then open the transfer."""
        now = now or utcnow()
        service = self.service
        values, identity = service._enabled_identity()
        if not isinstance(body, dict) or not all(isinstance(body.get(name), str)
                                                 for name in ('manifest', 'signature', 'offer', 'offer_signature')):
            return 400, self._refusal(identity, None, 'malformed', 'The message is not in the direct delivery format.')
        try:
            manifest_bytes = body['manifest'].encode('ascii')
            manifest = parse_manifest(manifest_bytes)
        except (UnicodeEncodeError, DirectProtocolError):
            return 400, self._refusal(identity, None, 'malformed', 'The message is not in the direct delivery format.')
        message_id = manifest['message_id']
        peer = service.store.peer_by_key(manifest['sender']['signing_key'])
        if peer is None or peer['state'] == 'revoked':
            return 403, self._refusal(identity, message_id, 'unknown_sender',
                                      'This installation does not accept documents from this sender.')
        offer = parse_offer(body['offer'])
        try:
            verify(peer['signing_key'], manifest_bytes, body['signature'])
            verify(peer['signing_key'], body['offer'].encode('ascii'), body['offer_signature'])
        except (DirectProtocolError, UnicodeEncodeError):
            return 403, self._refusal(identity, message_id, 'signature', 'The signature does not match the sender.')
        if (offer is None or offer['signer'] != peer['signing_key'] or offer['recipient'] != identity.signing_key
                or offer['message_id'] != message_id
                or offer['size'] != manifest['document']['size'] + TAG_BYTES):
            return 400, self._refusal(identity, message_id, 'malformed',
                                      'The transfer does not describe this document.', peer)
        from ..routing.numbers import InvalidNumber, normalize_number
        try:
            own_number = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except InvalidNumber:
            own_number = None
        if manifest['recipient']['signing_key'] != identity.signing_key or manifest['recipient']['fax_number'] != own_number:
            return 403, self._refusal(identity, message_id, 'wrong_recipient',
                                      'This document is addressed to another recipient.', peer)
        existing = service.store.find('inbound', message_id)
        if existing is not None and existing['state'] == 'refused':
            return 409, service._withdrawn(identity, message_id, peer)
        if existing is not None:
            if existing['manifest'].encode('ascii') != manifest_bytes:
                return 409, self._refusal(identity, message_id, 'replay',
                                          'This message id was already used for a different document.', peer)
            # Already delivered: nothing to send again (a duplicate is answered with the receipt it has).
            return 200, json.loads(existing['receipt'])
        from .service import FRESHNESS, MAX_DOCUMENT_BYTES
        if abs(parse_timestamp(manifest['created_at']) - now) > FRESHNESS:
            return 403, self._refusal(identity, message_id, 'stale',
                                      'This document was signed too long ago; send it again.', peer)
        if manifest['document']['size'] > MAX_DOCUMENT_BYTES:
            return 413, self._refusal(identity, message_id, 'too_large', 'This document is too large.', peer)
        from ..conversion import MAX_DOCUMENT_PAGES
        pages = manifest['document']['pages']
        if pages is not None and pages > MAX_DOCUMENT_PAGES:
            return 413, self._refusal(identity, message_id, 'too_many_pages',
                                      f'This document has more than {MAX_DOCUMENT_PAGES} pages, more than this '
                                      'installation accepts.', peer)
        from .store import accepts_fax_images
        if kind_of(manifest) == FAX_IMAGE and not accepts_fax_images(peer):
            return 409, self._refusal(identity, message_id, 'fax_images_off',
                                      'This installation does not accept fax images from you; send the original '
                                      'document instead.', peer)
        row = self.store.find('receiver', message_id)
        if row is not None:
            if row['manifest'] != body['manifest'] or row['offer'] != body['offer'] or row['peer_id'] != peer['id']:
                return 409, self._refusal(identity, message_id, 'replay',
                                          'This message id was already used for a different document.', peer)
            if row['state'] != 'open':
                return self._settled(identity, row, peer)
            return 200, self._answer(identity, row)
        if self.store.open_count(peer['id']) >= OPEN_PER_PARTNER:
            return 429, self._refusal(identity, message_id, 'busy',
                                      'This installation already has several unfinished transfers from you; finish '
                                      'or wait for those first.', peer)
        folder = self._folder(values, message_id)
        folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            free = shutil.disk_usage(folder.parent).free
        except OSError:
            free = 0
        if free < 2 * offer['size'] + 64 * 1024 * 1024:
            return 507, self._refusal(identity, message_id, 'no_space',
                                      'This installation does not have room for this document right now.', peer)
        folder.mkdir(exist_ok=True, mode=0o700)
        row, _ = self.store.create(role='receiver', message_id=message_id, peer_id=peer['id'],
                                   manifest=body['manifest'], signature=body['signature'], offer=body['offer'],
                                   offer_signature=body['offer_signature'], size=offer['size'],
                                   piece_size=offer['piece_size'], pieces=len(offer['pieces']), folder=str(folder),
                                   now=now)
        if row['manifest'] != body['manifest'] or row['offer'] != body['offer']:
            return 409, self._refusal(identity, message_id, 'replay',
                                      'This message id was already used for a different document.', peer)
        return 200, self._answer(identity, row)

    def _peer_for(self, row, signer):
        peer = self.service.store.get_peer(row['peer_id']) if row is not None else None
        if peer is None or peer['state'] == 'revoked' or peer['signing_key'] != signer:
            return None
        return peer

    def _checked(self, message_id, signer, request_time, signature, text, now):
        """The open transfer and its partner when the request is signed by that partner within the skew."""
        if not isinstance(signer, str) or MESSAGE.fullmatch(message_id or '') is None:
            return None, None
        row = self.store.find('receiver', message_id)
        peer = self._peer_for(row, signer)
        if peer is None:
            return None, None
        try:
            verify(peer['signing_key'], text.encode('ascii'), signature)
            if abs(parse_timestamp(request_time) - now) > REQUEST_SKEW:
                return None, None
        except (DirectProtocolError, UnicodeEncodeError, TypeError):
            return None, None
        return row, peer

    def piece(self, message_id, sequence, data, *, signer, request_time, signature, offset, digest, now=None):
        """Keep one piece at its offset when its SHA-256 is the signed one; a piece held already is a duplicate."""
        now = now or utcnow()
        _, identity = self.service._enabled_identity()
        text = f'PUT /direct/transfers/{message_id}/pieces/{sequence} {digest} {offset} {request_time}'
        row, peer = self._checked(message_id, signer, request_time, signature, text, now)
        if row is None:
            return 403, {'detail': 'This request is not from the partner that opened this transfer.'}
        if row['state'] != 'open':
            return self._settled(identity, row, peer)
        hashes = json.loads(row['offer'])['pieces']
        if type(sequence) is not int or not 0 <= sequence < len(hashes) or offset != sequence * row['piece_size']:
            return 400, self._refusal(identity, message_id, 'wrong_offset', 'This piece is not at its offset.', peer)
        if (len(data) != piece_length(row['size'], row['piece_size'], sequence) or digest != hashes[sequence]
                or hashlib.sha256(data).hexdigest() != hashes[sequence]):
            # Not kept: the receiver asks for this piece again by its sequence and SHA-256.
            return 422, self._refusal(identity, message_id, 'piece_mismatch',
                                      'This piece does not match its SHA-256; send it again.', peer)
        if sequence in self.store.held(row['id']):
            return 200, signed(identity, {'type': 'piece', 'message_id': message_id, 'sequence': sequence,
                                          'status': 'duplicate'})
        folder = Path(row['folder'])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private(folder / f'{sequence:06d}.piece', data)
        stored = self.store.keep_piece(row['id'], sequence, hashes[sequence], len(data), now=now)
        return 200, signed(identity, {'type': 'piece', 'message_id': message_id, 'sequence': sequence,
                                      'status': 'stored' if stored else 'duplicate'})

    def state(self, message_id, *, signer, request_time, signature, now=None):
        """What this installation holds of a transfer: never a fence, only an answer about pieces."""
        now = now or utcnow()
        _, identity = self.service._enabled_identity()
        row, peer = self._checked(message_id, signer, request_time, signature,
                                  f'GET /direct/transfers/{message_id} {request_time}', now)
        if row is None:
            peer = self.service.store.peer_by_key(signer) if isinstance(signer, str) else None
            if peer is None or peer['state'] == 'revoked' or MESSAGE.fullmatch(message_id or '') is None:
                return 403, {'detail': 'This request is not from an enrolled partner.'}
            try:
                verify(peer['signing_key'], f'GET /direct/transfers/{message_id} {request_time}'.encode('ascii'),
                       signature)
                if abs(parse_timestamp(request_time) - now) > REQUEST_SKEW:
                    raise DirectProtocolError('stale', 'The request time does not match.')
            except (DirectProtocolError, UnicodeEncodeError, TypeError):
                return 403, {'detail': 'This request is not from an enrolled partner.'}
            if self.store.find('receiver', message_id) is not None:
                return 403, {'detail': 'This request is not from the partner that opened this transfer.'}
            return 200, signed(identity, {'type': 'transfer', 'message_id': message_id, 'status': 'unknown'})
        if row['state'] == 'open':
            return 200, self._answer(identity, row)
        delivery = self.service.store.find('inbound', message_id)
        extra = {}
        if row['state'] == 'committed' and delivery is not None and delivery['state'] == 'accepted':
            extra['receipt'] = json.loads(delivery['receipt'])
        return 200, signed(identity, {'type': 'transfer', 'message_id': message_id, 'status': row['state'], **extra})

    def commit(self, message_id, statement, signature, *, now=None):
        """Assemble and accept the document once every piece and the whole match; the receipt of a single delivery."""
        now = now or utcnow()
        service = self.service
        _, identity = service._enabled_identity()
        refused = (400, {'detail': 'This commit could not be checked.'})
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
        except (AttributeError, UnicodeEncodeError, ValueError):
            return refused
        if (not isinstance(body, dict) or set(body) != {'type', 'message_id', 'ciphertext_sha256', 'signer', 'recipient'}
                or body['type'] != 'commit' or body['message_id'] != message_id
                or body['recipient'] != identity.signing_key):
            return refused
        row = self.store.find('receiver', message_id)
        peer = self._peer_for(row, body['signer'])
        if peer is None:
            return 403, {'detail': 'This request is not from the partner that opened this transfer.'}
        try:
            verify(peer['signing_key'], encoded, signature)
        except DirectProtocolError:
            return 403, {'detail': 'This request is not from the partner that opened this transfer.'}
        if row['state'] != 'open':
            return self._settled(identity, row, peer)
        manifest = json.loads(row['manifest'])
        if body['ciphertext_sha256'] != manifest['encryption']['ciphertext_sha256']:
            return 400, self._refusal(identity, message_id, 'malformed',
                                      'The commit does not describe this document.', peer)
        hashes = json.loads(row['offer'])['pieces']
        held = self.store.held(row['id'])
        folder = Path(row['folder'])
        ciphertext = bytearray()
        for sequence, digest in enumerate(hashes):
            if sequence not in held:
                continue
            path = folder / f'{sequence:06d}.piece'
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else b''
            if hashlib.sha256(data).hexdigest() != digest:
                # The stored bytes no longer match: forgotten here, and asked for again below.
                self.store.forget_piece(row['id'], sequence)
                path.unlink(missing_ok=True)
        held = self.store.held(row['id'])
        if len(held) != len(hashes):
            return 409, self._answer(identity, row, status='incomplete')
        for sequence in range(len(hashes)):
            ciphertext += (folder / f'{sequence:06d}.piece').read_bytes()
        if hashlib.sha256(ciphertext).hexdigest() != manifest['encryption']['ciphertext_sha256']:
            self.store.finish(row['id'], 'refused', detail='The pieces do not make up the signed document.', now=now)
            _remove(row['folder'])
            return 400, self._refusal(identity, message_id, 'tampered', 'The document does not match its manifest.',
                                      peer)
        status, answer = service.receive(row['manifest'].encode('ascii'), row['signature'], bytes(ciphertext), now=now)
        if status == 200:
            self.store.finish(row['id'], 'committed', now=now)
        else:
            detail = None
            try:
                detail = json.loads(answer['statement']).get('detail')
            except (KeyError, TypeError, ValueError):
                detail = None
            self.store.finish(row['id'], 'refused', detail=detail, now=now)
        _remove(row['folder'])
        return status, answer

    def expire(self, *, now=None):
        """Stop transfers left open too long; a fence makes sure their message can never be accepted later."""
        now = now or utcnow()
        for row in self.store.stale(before=now - OPEN_FOR):
            if row['role'] == 'receiver':
                peer = self.service.store.get_peer(row['peer_id'])
                if peer is not None:
                    self.service.store.answer_or_fence(row['message_id'], peer, now=now)
            # The receiver's pieces, or the sender's sealed copy: nothing encrypted is left behind.
            _remove(row['folder'])
            self.store.finish(row['id'], 'abandoned', detail='Stopped: the transfer was not finished in time.', now=now)
        return False


# Sending -----------------------------------------------------------------------------------------

class TransferSender:
    """The sending side: stage the sealed document, preflight, send what is missing and commit."""

    def __init__(self, service, *, pause=None):
        self.service = service
        self.store = TransferStore(service.store.engine)
        # ``transfer_pause`` replaces the wait between tries (tests only).
        self.pause = pause or getattr(service, 'transfer_pause', None) or (lambda seconds: asyncio.sleep(seconds))
        self.piece_size = getattr(service, 'transfer_piece_size', PIECE_SIZE)

    @staticmethod
    def wanted(service, ciphertext):
        return len(ciphertext) > getattr(service, 'transfer_threshold', THRESHOLD)

    def _sealed_path(self, values, message_id):
        return Path(values.fax_data_dir) / 'direct' / 'outbound' / f'{message_id}.sealed'

    def staged(self, message_id):
        """The sealed document kept for a transfer still open: (manifest, signature, ciphertext), or None.

        A resumed transfer must send the same bytes its partner already holds pieces of, so an attempt prepared
        again reuses them instead of sealing the document anew."""
        row = self.store.find('sender', message_id)
        if row is None or row['state'] != 'open' or not row['folder']:
            return None
        path = Path(row['folder'])
        if not path.is_file() or path.is_symlink():
            return None
        return row['manifest'].encode('ascii'), row['signature'], path.read_bytes()

    def stage(self, identity, peer, message_id, manifest, signature, ciphertext):
        values = self.service.values()
        path = self._sealed_path(values, message_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_file():
            _write_private(path, ciphertext)
        hashes = piece_hashes(ciphertext, self.piece_size)
        offer = canonical({'type': 'transfer', 'message_id': message_id, 'size': len(ciphertext),
                           'piece_size': self.piece_size, 'pieces': hashes, 'signer': identity.signing_key,
                           'recipient': peer['signing_key']})
        row, _ = self.store.create(role='sender', message_id=message_id, peer_id=peer['id'],
                                   manifest=manifest.decode('ascii'), signature=signature, offer=offer.decode('ascii'),
                                   offer_signature=identity.sign(offer), size=len(ciphertext),
                                   piece_size=self.piece_size, pieces=len(hashes), folder=str(path))
        return row

    def _done(self, row, state, detail=None):
        self.store.finish(row['id'], state, detail=detail)
        _remove(row['folder'])

    def _signed_headers(self, identity, text, moment):
        return {'X-Faxbot-Direct-Key': identity.signing_key, 'X-Faxbot-Direct-Time': moment,
                'X-Faxbot-Direct-Signature': identity.sign(text.encode('ascii'))}

    async def _upload(self, identity, peer, row, ciphertext, missing):
        """Send the pieces the partner said it is missing; only those whose SHA-256 is ours."""
        hashes = json.loads(row['offer'])['pieces']
        for entry in missing:
            sequence = entry.get('sequence') if isinstance(entry, dict) else None
            if type(sequence) is not int or not 0 <= sequence < len(hashes) or entry.get('sha256') != hashes[sequence]:
                raise DirectProtocolError('malformed', 'The partner asked for a piece this transfer does not have.')
            data = ciphertext[sequence * row['piece_size']:(sequence + 1) * row['piece_size']]
            offset = sequence * row['piece_size']
            moment = timestamp()
            path = f"/direct/transfers/{row['message_id']}/pieces/{sequence}"
            headers = {**self._signed_headers(identity, f'PUT {path} {hashes[sequence]} {offset} {moment}', moment),
                       'Upload-Offset': str(offset), 'X-Faxbot-Piece-Sha256': hashes[sequence],
                       'Content-Type': 'application/offset+octet-stream'}
            status, body = await self.service.http.request('PUT', peer['endpoint_url'] + path, content=data,
                                                           headers=headers)
            if status != 200:
                try:
                    statement = check_signed(body, peer['signing_key'])
                except DirectProtocolError:
                    statement = None
                if statement is not None and statement.get('type') == 'refusal' and \
                        statement.get('reason') not in ('piece_mismatch',):
                    return status, statement
                raise RuntimeError('A piece was not kept; Faxbot asks the partner what it holds.')
        return None

    async def _ask(self, identity, peer, message_id):
        moment = timestamp()
        path = f'/direct/transfers/{message_id}'
        status, body = await self.service.http.request(
            'GET', peer['endpoint_url'] + path, headers=self._signed_headers(identity, f'GET {path} {moment}', moment))
        statement = check_signed(body, peer['signing_key'])
        if status != 200 or statement.get('type') != 'transfer' or statement.get('message_id') != message_id:
            raise DirectProtocolError('malformed', 'The partner did not answer about this transfer.')
        return statement

    async def _commit(self, identity, peer, row):
        manifest = json.loads(row['manifest'])
        statement = canonical({'type': 'commit', 'message_id': row['message_id'],
                               'ciphertext_sha256': manifest['encryption']['ciphertext_sha256'],
                               'signer': identity.signing_key, 'recipient': peer['signing_key']})
        return await self.service.http.request(
            'POST', peer['endpoint_url'] + f"/direct/transfers/{row['message_id']}/commit",
            json={'statement': statement.decode('ascii'), 'signature': identity.sign(statement)})

    def _receipt(self, peer, row, body, digest):
        """The partner's signed receipt for this document, or None."""
        try:
            receipt = check_signed(body, peer['signing_key'])
        except DirectProtocolError:
            return None
        if (receipt.get('type') == 'receipt' and receipt.get('status') == 'accepted'
                and receipt.get('message_id') == row['message_id'] and receipt.get('document_sha256') == digest):
            return body
        return None

    async def _finish(self, identity, peer, row, ciphertext, digest, missing):
        """Send what is missing and commit, repairing from the partner's answers; ('accepted', receipt),
        ('refused', detail) or raises (the outcome is not known yet)."""
        for _ in range(RESUME_TRIES + 1):
            refusal = await self._upload(identity, peer, row, ciphertext, missing)
            if refusal is not None:
                return 'refused', refusal.get('detail') or 'The partner refused the document.'
            status, body = await self._commit(identity, peer, row)
            receipt = self._receipt(peer, row, body, digest)
            if status == 200 and receipt is not None:
                return 'accepted', receipt
            try:
                statement = check_signed(body, peer['signing_key'])
            except DirectProtocolError:
                statement = None
            if statement is not None and statement.get('message_id') == row['message_id']:
                if statement.get('type') == 'transfer' and statement.get('status') == 'incomplete':
                    missing = statement.get('missing') or []
                    self.store.note_confirmed(row['id'], len(statement.get('have') or []))
                    continue
                if statement.get('type') == 'refusal' and 400 <= status < 500:
                    return 'refused', statement.get('detail') or 'The partner refused the document.'
            raise RuntimeError('The answer from the partner could not be confirmed.')
        raise RuntimeError('The partner kept missing pieces of this document.')

    async def deliver(self, identity, peer, message_id, manifest, signature, ciphertext, digest):
        """Send one sealed document in pieces; ('accepted', receipt) or ('refused', detail).

        Raises TransferUnsupported (nothing was sent; send it whole instead), the address errors of
        ``HttpClient`` (nothing was sent), or another error when the outcome is not known yet: the fax then
        waits for confirmation and the reconciler resumes the transfer."""
        from .service import PartnerUnreachable
        row = await run_lifecycle_step(lambda: self.stage(identity, peer, message_id, manifest, signature, ciphertext))
        if row['state'] != 'open' or row['manifest'] != manifest.decode('ascii'):
            # A transfer of this message already finished here: the single delivery answers authoritatively (its
            # receipt, or the partner's fence), because the partner records each message once.
            raise TransferUnsupported()
        try:
            status, body = await self.service.http.request('POST', peer['endpoint_url'] + '/direct/transfers', json={
                'manifest': row['manifest'], 'signature': row['signature'], 'offer': row['offer'],
                'offer_signature': row['offer_signature']})
        except PartnerUnreachable:
            await run_lifecycle_step(lambda: self._done(row, 'abandoned', 'The partner could not be reached.'))
            raise
        if status in (404, 405):
            await run_lifecycle_step(lambda: self._done(row, 'abandoned', "The partner's Faxbot cannot take documents "
                                                                         'in pieces yet.'))
            raise TransferUnsupported()
        receipt = self._receipt(peer, row, body, digest)
        if status == 200 and receipt is not None:
            await run_lifecycle_step(lambda: self._done(row, 'committed'))
            return 'accepted', receipt
        try:
            statement = check_signed(body, peer['signing_key'])
        except DirectProtocolError:
            statement = None
        if statement is not None and statement.get('message_id') == message_id:
            await _heard(self.service, peer, statement)
            if statement.get('type') == 'refusal' and 400 <= status < 500:
                # Refused at the preflight: no document byte was sent.
                detail = statement.get('detail') or 'The partner refused the document.'
                await run_lifecycle_step(lambda: self._done(row, 'refused', detail))
                return 'refused', detail
            if status == 200 and statement.get('type') == 'transfer' and statement.get('status') == 'open':
                await run_lifecycle_step(lambda: self.store.note_confirmed(row['id'], len(statement.get('have') or [])))
                return await self._resume(identity, peer, row, ciphertext, digest, statement.get('missing') or [])
        raise RuntimeError('The answer from the partner could not be confirmed.')

    async def _resume(self, identity, peer, row, ciphertext, digest, missing):
        from .service import PartnerUnreachable
        last = None
        for attempt in range(RESUME_TRIES):
            try:
                outcome, detail = await self._finish(identity, peer, row, ciphertext, digest, missing)
            except (PartnerUnreachable, httpx.HTTPError, RuntimeError, DirectProtocolError) as error:
                last = error
                await self.pause(2 ** attempt)
                try:
                    statement = await self._ask(identity, peer, row['message_id'])
                except (PartnerUnreachable, httpx.HTTPError, DirectProtocolError) as error:
                    last = error
                    continue
                if statement.get('status') == 'committed' and self._receipt(peer, row, statement.get('receipt'),
                                                                            digest) is not None:
                    outcome, detail = 'accepted', statement['receipt']
                elif statement.get('status') == 'open':
                    missing = statement.get('missing') or []
                    await run_lifecycle_step(lambda: self.store.note_confirmed(row['id'],
                                                                               len(statement.get('have') or [])))
                    continue
                else:
                    raise RuntimeError('The partner no longer holds this transfer; it is asked instead.') from None
            await run_lifecycle_step(lambda: self._done(row, 'committed' if outcome == 'accepted' else 'refused',
                                                        None if outcome == 'accepted' else detail))
            return outcome, detail
        raise RuntimeError('The connection to the partner kept dropping; Faxbot resumes the transfer later.') from last

    async def resume(self, delivery):
        """For the reconciler: finish a transfer whose answer was lost.

        Returns ('accepted', receipt), 'waiting' (ask again later) or None when the transfer can no longer
        complete (the reconciler then asks about the document, which fences it)."""
        from .service import FRESHNESS, PartnerUnreachable
        row = await run_lifecycle_step(lambda: self.store.find('sender', delivery['message_id']))
        if row is None or row['state'] not in ('open', 'committed'):
            return None
        peer = await run_lifecycle_step(lambda: self.service.store.get_peer(row['peer_id']))
        if peer is None or peer['state'] == 'revoked':
            return None
        identity = await run_lifecycle_step(self.service.identity)
        manifest = json.loads(row['manifest'])
        digest = manifest['document']['sha256']
        try:
            statement = await self._ask(identity, peer, row['message_id'])
        except (PartnerUnreachable, httpx.HTTPError, DirectProtocolError):
            return 'waiting'
        if statement.get('status') == 'committed':
            receipt = self._receipt(peer, row, statement.get('receipt'), digest)
            if receipt is None:
                return None
            await run_lifecycle_step(lambda: self._done(row, 'committed'))
            return 'accepted', receipt
        if statement.get('status') != 'open' or row['state'] != 'open':
            return None
        if abs(parse_timestamp(manifest['created_at']) - utcnow()) > FRESHNESS - timedelta(minutes=30):
            return None  # Too old to commit now; asking settles it.
        sealed = await run_lifecycle_step(lambda: self.staged(row['message_id']))
        if sealed is None:
            return None
        try:
            outcome, detail = await self._resume(identity, peer, row, sealed[2], digest,
                                                 statement.get('missing') or [])
        except (PartnerUnreachable, httpx.HTTPError, RuntimeError, DirectProtocolError):
            return 'waiting'
        return (outcome, detail) if outcome == 'accepted' else None


async def _heard(service, peer, statement):
    from .service import _hear
    await _hear(service, peer, statement)


def transfer_view(row, organization=None):
    """One transfer for the console and the command line, in plain words."""
    held = row['confirmed']
    progress = f"{held} of {row['pieces']} pieces confirmed" if row['state'] == 'open' else None
    status = STATE_TEXT.get(row['state'], '')
    if row['state'] == 'open':
        status = f'Sending in pieces: {progress}.' if row['role'] == 'sender' else f"Receiving in pieces: {held} of {row['pieces']} held."
    elif row['detail']:
        status = f"{status} {row['detail']}"
    return {'message_id': row['message_id'], 'direction': 'outbound' if row['role'] == 'sender' else 'inbound',
            'partner': organization, 'state': row['state'], 'status': status, 'pieces': row['pieces'],
            'confirmed': held, 'size_bytes': row['size'], 'created_at': row['created_at'],
            'finished_at': row['finished_at']}


def background_step(service):
    """The receiver's cleanup of transfers left unfinished, run by the direct delivery background tasks."""
    def step():
        try:
            return TransferReceiver(service).expire()
        except Exception:
            logging.getLogger(__name__).warning('Unfinished direct transfers could not be cleaned up yet.')
            return False
    return step
