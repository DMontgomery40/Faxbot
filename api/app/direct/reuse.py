"""Send a partner only the bytes it lacks: a reference to a copy it holds, or the changes (research D9, D10).

Over the direct path the fax call is already gone, so what these save is
bytes, never money (Site 102). They are counted as bytes on Costs → Savings.

The signed manifest never changes: it always describes the whole document
(its SHA-256, size and pages) for its own recipient, and the receipt is the
same signed receipt. Only how the bytes get there differs:

- **Reference (D9).** Before an original goes to a partner that earlier
  accepted the exact same document (SHA-256) from this installation, Faxbot
  asks, signed, whether it still holds it (``POST /direct/holdings``). If it
  does, only the manifest goes (``POST /direct/references``); the partner files
  its kept copy and signs its receipt.
- **Patch (D10).** For a case document whose earlier version (same case,
  source and purpose in the case ledger, ``cases/ledger.py``) this partner
  accepted from us, Faxbot asks whether it still holds that version. If it
  does, it sends a binary delta from that version, encrypted to the partner
  (``POST /direct/patches``). The partner rebuilds the document and files it
  only when the result's SHA-256 and size match the signed manifest.

A partner answers only about documents that same partner delivered to it, so
nobody can learn what another sender sent. A miss is a signed refusal
(``not_held``, ``patch_mismatch``) that records nothing, so the full document
goes at once under the same message ID. Any other outcome follows the direct
route's rules: only a signed refusal allows the fax route, and a lost answer
is asked about, never sent again.

The delta is a simple rsync-style block delta written here (no extra
dependency): the earlier version is indexed in fixed blocks, the new document
is scanned for those blocks, and what does not match goes as literal bytes;
the instructions are then zlib-compressed. A delta that would not save at
least a fifth of the bytes is not sent.
"""
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import zlib
from uuid import uuid4

import httpx
import sqlalchemy as sa
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config_runtime import run_lifecycle_step
from ..routing.database import read_connection, reflect, utcnow, write_transaction
from .crypto import (DirectProtocolError, b64, canonical, check_signed, parse_timestamp, signed, timestamp, unb64,
                     verify)


REFERENCE, PATCH = 'reference', 'patch'
FRESHNESS_SECONDS = 24 * 3600
MAX_ASKED = 10
BLOCK = 64
MAGIC = b'FXBD1'
MAX_PATCH_DOCUMENT = 20 * 1024 * 1024  # larger documents go whole: the delta is computed in this process
WORTH = 0.8  # a delta must be smaller than this share of the document
_HEX64 = re.compile(r'[a-f0-9]{64}')
_ID = re.compile(r'[a-f0-9]{32}')


class DeltaError(ValueError):
    """A delta that cannot be applied to the base it names."""


class CarriageMiss(RuntimeError):
    """The partner does not hold what a reference or patch needs; ``reason`` is ``not_held`` or ``patch_mismatch``."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


# The delta -------------------------------------------------------------------------------------------------------
def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data, position):
    shift = result = 0
    while True:
        if position >= len(data) or shift > 63:
            raise DeltaError('The changes are not in the expected format.')
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7


def make_delta(base, target, *, worth=WORTH):
    """The compressed delta that rebuilds ``target`` from ``base``, or None when it would not save enough."""
    if not base or not target or len(target) > MAX_PATCH_DOCUMENT or len(base) > MAX_PATCH_DOCUMENT:
        return None
    index = {}
    for offset in range(0, len(base) - BLOCK + 1, BLOCK):
        index.setdefault(base[offset:offset + BLOCK], offset)
    ops = bytearray()
    literal_start = 0
    literal_total = 0
    limit = int(len(target) * worth)
    position = 0
    end = len(target) - BLOCK

    def flush(upto):
        nonlocal literal_total
        if upto > literal_start:
            ops.extend(b'\x02' + _varint(upto - literal_start) + target[literal_start:upto])
            literal_total += upto - literal_start

    while position <= end:
        found = index.get(target[position:position + BLOCK])
        if found is None:
            position += 1
            if position - literal_start + literal_total > limit:
                return None  # mostly new bytes: the whole document is the better choice
            continue
        # Grow the match backwards into the pending literal bytes, then forwards in large steps.
        start, source = position, found
        while start > literal_start and source > 0 and target[start - 1] == base[source - 1]:
            start, source = start - 1, source - 1
        length = position - start + BLOCK
        step = 4096
        while step:
            if (start + length + step <= len(target) and source + length + step <= len(base)
                    and target[start + length:start + length + step] == base[source + length:source + length + step]):
                length += step
            else:
                step //= 2
        flush(start)
        ops.extend(b'\x01' + _varint(source) + _varint(length))
        position = start + length
        literal_start = position
    flush(len(target))
    if literal_total > limit:
        return None
    body = MAGIC + _varint(len(base)) + _varint(len(target)) + bytes(ops)
    delta = zlib.compress(body, 9)
    return delta if len(delta) < len(target) * worth else None


def apply_delta(base, delta, *, max_size):
    """Rebuild a document from ``base`` and a delta made by ``make_delta``; DeltaError when it does not fit."""
    decompressor = zlib.decompressobj()
    try:
        body = decompressor.decompress(delta, max_size * 2 + 4096)
    except zlib.error:
        raise DeltaError('The changes are not in the expected format.') from None
    if decompressor.unconsumed_tail or not decompressor.eof or not body.startswith(MAGIC):
        raise DeltaError('The changes are not in the expected format.')
    base_size, position = _read_varint(body, len(MAGIC))
    target_size, position = _read_varint(body, position)
    if base_size != len(base) or target_size > max_size:
        raise DeltaError('The changes are for another version of the document.')
    out = bytearray()
    while position < len(body):
        op = body[position]
        position += 1
        if op == 1:
            offset, position = _read_varint(body, position)
            length, position = _read_varint(body, position)
            if offset + length > len(base):
                raise DeltaError('The changes are for another version of the document.')
            out += base[offset:offset + length]
        elif op == 2:
            length, position = _read_varint(body, position)
            if position + length > len(body):
                raise DeltaError('The changes are not in the expected format.')
            out += body[position:position + length]
            position += length
        else:
            raise DeltaError('The changes are not in the expected format.')
        if len(out) > target_size:
            raise DeltaError('The changes do not match the document.')
    if len(out) != target_size:
        raise DeltaError('The changes do not match the document.')
    return bytes(out)


# The delta's envelope: encrypted to the partner and bound to its message, base and result -------------------------
def _patch_key(shared, ephemeral, recipient, message_id, base, result):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b'faxbot-direct-patch-v1|' + ephemeral + recipient + f'{message_id}|{base}|{result}'.encode(
                    'ascii')).derive(shared)


def _patch_aad(message_id, base, result):
    return canonical({'message_id': message_id, 'base_sha256': base, 'result_sha256': result})


def seal_delta(peer, *, message_id, base, result, delta):
    """Encrypt ``delta`` to the partner; returns (facts for the signed patch statement, ciphertext)."""
    ephemeral = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ephemeral_public = ephemeral.public_key().public_bytes(*raw)
    recipient = unb64(peer['exchange_key'], length=32)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
    nonce = os.urandom(12)
    ciphertext = AESGCM(_patch_key(shared, ephemeral_public, recipient, message_id, base, result)).encrypt(
        nonce, delta, _patch_aad(message_id, base, result))
    return {'ephemeral_key': b64(ephemeral_public), 'nonce': b64(nonce), 'size': len(delta),
            'sha256': hashlib.sha256(ciphertext).hexdigest()}, ciphertext


def open_delta(identity, facts, ciphertext, *, message_id, base, result):
    if hashlib.sha256(ciphertext).hexdigest() != facts['sha256']:
        raise DeltaError('The changes do not match their signed statement.')
    ephemeral = unb64(facts['ephemeral_key'], length=32)
    own = identity.exchange.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    try:
        shared = identity.exchange.exchange(X25519PublicKey.from_public_bytes(ephemeral))
        delta = AESGCM(_patch_key(shared, ephemeral, own, message_id, base, result)).decrypt(
            unb64(facts['nonce'], length=12), ciphertext, _patch_aad(message_id, base, result))
    except (InvalidTag, ValueError):
        raise DeltaError('The changes could not be decrypted for this recipient.') from None
    if len(delta) != facts['size']:
        raise DeltaError('The changes do not match their signed statement.')
    return delta


# What a partner holds --------------------------------------------------------------------------------------------
class ReuseStore:
    TABLES = ('direct_deliveries', 'direct_byte_savings', 'direct_peers')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.deliveries, self.savings, self.peers = (tables['direct_deliveries'], tables['direct_byte_savings'],
                                                     tables['direct_peers'])

    def held_copy(self, peer_id, digest):
        """The bytes of a document ``peer_id`` delivered here with this SHA-256, still kept and unchanged, or None."""
        d = self.deliveries
        with read_connection(self.engine) as connection:
            paths = connection.execute(sa.select(d.c.document_path).where(
                d.c.direction == 'inbound', d.c.peer_id == peer_id, d.c.digest == digest, d.c.state == 'accepted',
                d.c.document_path.is_not(None)).order_by(d.c.accepted_at.desc())).scalars().all()
        for path in paths:
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() == digest:
                return data
        return None

    def sent_before(self, peer_id, digest, *, exclude=None):
        """Whether ``peer_id`` accepted a document with this SHA-256 from this installation."""
        d = self.deliveries
        query = sa.select(d.c.id).where(d.c.direction == 'outbound', d.c.peer_id == peer_id, d.c.digest == digest,
                                        d.c.state == 'accepted')
        if exclude is not None:
            query = query.where(d.c.message_id != exclude)
        with read_connection(self.engine) as connection:
            return connection.execute(query.limit(1)).first() is not None

    def record_saving(self, *, message_id, peer_id, job_id, send_id, carriage, digest, base, full, sent):
        with write_transaction(self.engine) as connection:
            if connection.execute(sa.select(self.savings.c.id).where(
                    self.savings.c.message_id == message_id)).first() is not None:
                return False
            connection.execute(self.savings.insert().values(
                id=uuid4().hex, message_id=message_id, peer_id=peer_id, job_id=job_id, send_id=send_id,
                carriage=carriage, document_sha256=digest, base_sha256=base, full_bytes=full, sent_bytes=sent,
                created_at=utcnow()))
            return True

    def saving(self, message_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.savings).where(
                self.savings.c.message_id == message_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def totals(self, since):
        s = self.savings
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(s.c.carriage, sa.func.count(), sa.func.sum(s.c.full_bytes),
                                                sa.func.sum(s.c.sent_bytes)).where(s.c.created_at >= since)
                                      .group_by(s.c.carriage)).all()
        return {carriage: {'documents': int(count), 'full_bytes': int(full or 0), 'sent_bytes': int(sent or 0)}
                for carriage, count, full, sent in rows}


def answer_holdings(service, statement, signature, *, now=None):
    """A partner asks, signed, which of up to ten documents it delivered here are still held; signed answer."""
    now = now or utcnow()
    _, identity = service._enabled_identity()
    refused = (400, {'detail': 'This question could not be checked.'})
    try:
        encoded = statement.encode('ascii')
        body = json.loads(encoded)
    except (AttributeError, UnicodeEncodeError, ValueError):
        return refused
    digests = body.get('digests') if isinstance(body, dict) else None
    if (not isinstance(body, dict) or set(body) != {'type', 'recipient', 'said_at', 'signer', 'digests'}
            or body['type'] != 'holdings_query' or body['recipient'] != identity.signing_key
            or not isinstance(digests, list) or not 0 < len(digests) <= MAX_ASKED
            or any(not isinstance(item, str) or not _HEX64.fullmatch(item) for item in digests)):
        return refused
    peer = service.store.peer_by_key(body['signer']) if isinstance(body['signer'], str) else None
    if peer is None or peer['state'] == 'revoked':
        return 403, {'detail': 'This installation does not answer this sender.'}
    try:
        verify(peer['signing_key'], encoded, signature)
        if abs((parse_timestamp(body['said_at']) - now).total_seconds()) > FRESHNESS_SECONDS:
            return refused
    except DirectProtocolError:
        return refused
    store = ReuseStore(service.store.engine)
    # Only documents this partner delivered here: nobody learns what another sender sent.
    held = [digest for digest in dict.fromkeys(digests) if store.held_copy(peer['id'], digest) is not None]
    return 200, signed(identity, {'type': 'holdings', 'recipient': peer['signing_key'], 'digests': held,
                                  'answered_at': timestamp()})


async def ask_holdings(service, identity, peer, digests):
    """Which of ``digests`` the partner says, signed, it still holds from us; None when it cannot say."""
    from .service import PartnerUnreachable
    statement = canonical({'type': 'holdings_query', 'recipient': peer['signing_key'], 'said_at': timestamp(),
                           'signer': identity.signing_key, 'digests': list(digests)[:MAX_ASKED]})
    try:
        status, body = await service.http.request('POST', peer['endpoint_url'] + '/direct/holdings', json={
            'statement': statement.decode('ascii'), 'signature': identity.sign(statement)})
        answer = check_signed(body, peer['signing_key'])
    except (PartnerUnreachable, httpx.HTTPError, DirectProtocolError, OSError):
        return None
    if (status != 200 or answer.get('type') != 'holdings' or answer.get('recipient') != identity.signing_key
            or not isinstance(answer.get('digests'), list)):
        return None
    return {digest for digest in answer['digests'] if isinstance(digest, str) and digest in digests}


# How the bytes arrive at the partner -----------------------------------------------------------------------------
@dataclass
class Reference:
    """The partner's kept copy of the document the manifest names."""
    kind = REFERENCE

    def document(self, service, identity, peer, manifest):
        data = ReuseStore(service.store.engine).held_copy(peer['id'], manifest['document']['sha256'])
        if data is None or len(data) != manifest['document']['size']:
            raise CarriageMiss('not_held', 'This installation no longer holds that document; send the whole '
                                           'document.')
        return data


@dataclass
class Patch:
    """The changes from a version the partner holds, checked against the manifest before anything is filed."""
    statement: str
    signature: str
    ciphertext: bytes
    kind = PATCH
    delta_size: int = 0
    base: str | None = None

    def document(self, service, identity, peer, manifest):
        try:
            encoded = self.statement.encode('ascii')
            verify(peer['signing_key'], encoded, self.signature)
            body = json.loads(encoded)
        except (AttributeError, ValueError, UnicodeEncodeError, DirectProtocolError):
            raise CarriageMiss('patch_mismatch', 'The changes could not be checked; send the whole document.') from None
        facts = body.get('delta') if isinstance(body, dict) else None
        base = body.get('base_sha256') if isinstance(body, dict) else None
        if (not isinstance(body, dict) or body.get('type') != 'patch' or body.get('signer') != peer['signing_key']
                or body.get('recipient') != identity.signing_key or body.get('message_id') != manifest['message_id']
                or body.get('result_sha256') != manifest['document']['sha256']
                or not isinstance(base, str) or not _HEX64.fullmatch(base) or not isinstance(facts, dict)
                or set(facts) != {'ephemeral_key', 'nonce', 'size', 'sha256'} or type(facts['size']) is not int
                or not isinstance(facts['sha256'], str)):
            raise CarriageMiss('patch_mismatch', 'The changes could not be checked; send the whole document.')
        held = ReuseStore(service.store.engine).held_copy(peer['id'], base)
        if held is None:
            raise CarriageMiss('not_held', 'This installation no longer holds the earlier version; send the whole '
                                           'document.')
        try:
            delta = open_delta(identity, facts, self.ciphertext, message_id=manifest['message_id'], base=base,
                               result=manifest['document']['sha256'])
            data = apply_delta(held, delta, max_size=manifest['document']['size'])
        except (DeltaError, DirectProtocolError):
            raise CarriageMiss('patch_mismatch', 'The changes did not rebuild the document; send the whole '
                                                 'document.') from None
        if len(data) != manifest['document']['size'] or hashlib.sha256(data).hexdigest() != manifest['document']['sha256']:
            raise CarriageMiss('patch_mismatch', 'The changes did not rebuild the document; send the whole '
                                                 'document.')
        self.delta_size, self.base = len(delta), base
        return data


# The sender ------------------------------------------------------------------------------------------------------
def patch_bases(service, peer, job_id, values):
    """Earlier versions this partner accepted from us of the case documents ``job_id`` carries, newest first.

    From the case ledger: an entry of the same case, source and purpose with another version, carried by a fax
    whose document this partner accepted directly. Returns [(sha256, path)] for the earlier faxes' own documents,
    still kept here unchanged (a packet is composed, so the base is exactly what that fax delivered).
    """
    try:
        tables = reflect(service.store.engine, ('case_entries', 'case_entry_sends', 'direct_deliveries'))
    except Exception:
        return []
    entries, sends, deliveries = tables['case_entries'], tables['case_entry_sends'], tables['direct_deliveries']
    mine, earlier, carried = entries.alias('mine'), entries.alias('earlier'), sends.alias('carried')
    query = (sa.select(deliveries.c.job_id, deliveries.c.digest, earlier.c.created_at)
             .select_from(sends.join(mine, mine.c.id == sends.c.entry_id)
                          .join(earlier, sa.and_(earlier.c.case_id == mine.c.case_id,
                                                 earlier.c.source == mine.c.source,
                                                 earlier.c.purpose == mine.c.purpose,
                                                 earlier.c.version != mine.c.version,
                                                 earlier.c.created_at <= mine.c.created_at))
                          .join(carried, carried.c.entry_id == earlier.c.id)
                          .join(deliveries, deliveries.c.job_id == carried.c.job_id))
             .where(sends.c.job_id == job_id, carried.c.job_id != job_id, deliveries.c.direction == 'outbound',
                    deliveries.c.peer_id == peer['id'], deliveries.c.state == 'accepted',
                    deliveries.c.kind.is_(None))
             .order_by(earlier.c.created_at.desc()))
    with read_connection(service.store.engine) as connection:
        rows = connection.execute(query).all()
    found, seen = [], set()
    folder = Path(values.fax_data_dir)
    for earlier_job, digest, _ in rows:
        if digest in seen or not _ID.fullmatch(earlier_job or ''):
            continue
        seen.add(digest)
        path = folder / f'{earlier_job}.pdf'
        try:
            if not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                found.append((digest, path))
        except OSError:
            continue
        if len(found) >= 3:
            break
    return found


async def _held(submission):
    """What the partner says it still holds of this document or its earlier versions: (identity, held, bases), or
    None when there is nothing to ask about or no answer."""
    service, peer = submission.service, submission.peer
    store = ReuseStore(service.store.engine)
    values = await run_lifecycle_step(service.values)
    reference = await run_lifecycle_step(lambda: store.sent_before(peer['id'], submission.digest,
                                                                   exclude=submission.message_id))
    bases = []
    if submission.job_id and submission.document is not None:
        bases = await run_lifecycle_step(lambda: patch_bases(service, peer, submission.job_id, values))
    asked = ([submission.digest] if reference else []) + [digest for digest, _ in bases]
    if not asked:
        return None
    identity = await run_lifecycle_step(service.identity)
    held = await ask_holdings(service, identity, peer, asked)
    return (identity, held, bases) if held else None


async def _patch(submission, identity, digest, path):
    """The signed patch statement and the encrypted delta from the version ``digest`` kept at ``path``, or None."""
    import asyncio
    base = await run_lifecycle_step(path.read_bytes)
    if hashlib.sha256(base).hexdigest() != digest:
        return None
    delta = await asyncio.to_thread(make_delta, base, submission.document)
    if delta is None:
        return None
    peer = submission.peer
    facts, ciphertext = seal_delta(peer, message_id=submission.message_id, base=digest, result=submission.digest,
                                   delta=delta)
    statement = signed(identity, {'type': 'patch', 'recipient': peer['signing_key'],
                                  'message_id': submission.message_id, 'base_sha256': digest,
                                  'result_sha256': submission.digest, 'delta': facts, 'said_at': timestamp()})
    return statement, ciphertext


async def offer(submission):
    """Deliver ``submission``'s original as a reference or a patch when the partner holds what that needs.

    Returns the SubmissionReceipt when the partner accepted it, or None when the whole document should go now
    (nothing was accepted). Raises like the direct route for anything else.
    """
    try:
        planned = await _held(submission)
    except Exception:
        # Nothing was sent: a local problem here only means the whole document goes, as it always did.
        logging.getLogger(__name__).warning('Faxbot could not check what a partner holds; the whole document goes.')
        return None
    if planned is None:
        return None
    identity, held, bases = planned
    if submission.digest in held:
        outcome = await submission.deliver('/direct/references', fallback=('not_held',), json={
            'manifest': submission.manifest.decode('ascii'), 'signature': submission.signature})
        if outcome is not None:
            return outcome
    for digest, path in bases:
        if digest not in held:
            continue
        try:
            built = await _patch(submission, identity, digest, path)
        except Exception:
            logging.getLogger(__name__).warning('The changes to an earlier version could not be prepared; the whole '
                                                'document goes.')
            built = None
        if built is None:
            continue
        statement, ciphertext = built
        outcome = await submission.deliver('/direct/patches', fallback=('not_held', 'patch_mismatch'), files={
            'manifest': (None, submission.manifest, 'application/json'),
            'signature': (None, submission.signature.encode('ascii'), 'text/plain'),
            'patch': (None, statement['statement'].encode('ascii'), 'application/json'),
            'patch_signature': (None, statement['signature'].encode('ascii'), 'text/plain'),
            'delta': ('delta.bin', ciphertext, 'application/octet-stream')})
        if outcome is not None:
            return outcome
    return None


def record_from_receipt(service, message_id):
    """Count the bytes a partner's signed receipt says it did not need (a reference or a patch); once."""
    row = service.store.find('outbound', message_id)
    if row is None or row['state'] != 'accepted' or not row.get('receipt'):
        return False
    try:
        statement = json.loads(json.loads(row['receipt'])['statement'])
    except (TypeError, ValueError, KeyError):
        return False
    carriage = statement.get('carriage')
    if carriage not in (REFERENCE, PATCH):
        return False
    sent = statement.get('delta_size') if carriage == PATCH else 0
    base = statement.get('base_sha256') if carriage == PATCH else None
    if type(sent) is not int or sent < 0 or (carriage == PATCH and not isinstance(base, str)):
        return False
    send_id = None
    if row.get('job_id'):
        from .distribute import DistributionStore
        send = DistributionStore(service.store.engine).live_send_for_job(row['job_id'])
        send_id = send['id'] if send is not None else None
    return ReuseStore(service.store.engine).record_saving(
        message_id=message_id, peer_id=row['peer_id'], job_id=row.get('job_id'), send_id=send_id, carriage=carriage,
        digest=row['digest'], base=base, full=max(int(row['size_bytes']), 1), sent=sent)


# Words -----------------------------------------------------------------------------------------------------------
def size_text(count):
    for unit, size in (('GB', 1024 ** 3), ('MB', 1024 ** 2), ('KB', 1024)):
        if count >= size:
            value = count / size
            return f'{value:.1f} {unit}' if value < 10 else f'{value:.0f} {unit}'
    return f'{count} byte{"s" if count != 1 else ""}'


def sent_text(partner, saving):
    """The Sent sentence for a document a partner accepted without its full bytes."""
    partner = partner or 'the partner'
    if saving['carriage'] == REFERENCE:
        return f'Delivered directly to {partner}, which already held this document; only a reference was sent.'
    return (f'Delivered directly to {partner} as the changes to a version it already held; '
            f"{size_text(saving['sent_bytes'])} sent instead of {size_text(saving['full_bytes'])}.")


def received_text(partner, carriage):
    partner = partner or 'a partner'
    if carriage == REFERENCE:
        return (f'Delivered directly by {partner} as a reference to a copy of this document it sent you before; '
                'no telephone call.')
    return (f'Delivered directly by {partner} as the changes to an earlier version it sent you, checked against '
            'the whole document; no telephone call.')


def savings_view(engine, *, since, days):
    """Bytes partners did not need sent again (references and patches), never money."""
    try:
        totals = ReuseStore(engine).totals(since)
    except Exception:
        totals = {}
    reference = totals.get(REFERENCE, {'documents': 0, 'full_bytes': 0, 'sent_bytes': 0})
    patch = totals.get(PATCH, {'documents': 0, 'full_bytes': 0, 'sent_bytes': 0})
    saved = (reference['full_bytes'] - reference['sent_bytes']) + (patch['full_bytes'] - patch['sent_bytes'])
    documents = reference['documents'] + patch['documents']
    if not documents:
        sentence = f'No documents went to partners as references or changes in the last {days} days.'
    else:
        parts = []
        if reference['documents']:
            parts.append(f"{reference['documents']} as a reference to a copy the partner held")
        if patch['documents']:
            parts.append(f"{patch['documents']} as only the changes to an earlier version")
        sentence = (f"{size_text(saved)} not sent in the last {days} days: {' and '.join(parts)}. "
                    'These are bytes over the internet, not money; the calls were already saved.')
    return {'bytes_saved': max(saved, 0), 'documents': documents, 'references': reference['documents'],
            'patches': patch['documents'], 'sentence': sentence}
