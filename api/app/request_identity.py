"""Stable identities for accepted fax intent; never retain raw client keys.

Fingerprint version 2 binds the canonical destination number. Records accepted
before canonical destinations carry version 1, which bound the number exactly as
it was entered; a replay still matches those records by that exact text.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import sqlalchemy as sa

from .documents import UploadPreparationError


class IdempotencyConflict(ValueError):
    def __init__(self):
        super().__init__('Idempotency-Key already belongs to a different fax request.')


class IdempotentReplay(Exception):
    def __init__(self, job_id):
        self.job_id = job_id
        super().__init__('This fax request has already been accepted.')


def find_scoped_request(connection, deliveries, identity):
    """Read a candidate by validated principal/key; fingerprint is checked later."""
    if not isinstance(identity, RequestIdentity):
        raise ValueError('Invalid fax request identity.')
    return connection.execute(sa.select(deliveries.c.id, deliveries.c.request_fingerprint).where(
        deliveries.c.principal_scope == identity.principal_scope,
        deliveries.c.idempotency_digest == identity.idempotency_digest)).mappings().one_or_none()


def find_replay(connection, deliveries, identity):
    row = find_scoped_request(connection, deliveries, identity)
    if row is None:
        return None
    if not identity.matches(row['request_fingerprint']):
        raise IdempotencyConflict()
    return row['id']


_FINGERPRINT = re.compile('[a-f0-9]{64}')


@dataclass(frozen=True)
class RequestIdentity:
    """A scoped key and the fingerprint stored with a newly accepted request.

    ``legacy_fingerprints`` are earlier-version fingerprints of the same request.
    They only let a replay match a record stored under an older version; they
    are never stored and never widen what counts as the same request.
    """
    principal_scope: str
    idempotency_digest: str
    request_fingerprint: str
    legacy_fingerprints: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.principal_scope, str) or not 1 <= len(self.principal_scope) <= 100
                or any(ord(char) < 33 or ord(char) > 126 for char in self.principal_scope)
                or type(self.legacy_fingerprints) is not tuple
                or any(not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None
                       for value in (self.idempotency_digest, self.request_fingerprint,
                                     *self.legacy_fingerprints))):
            raise ValueError('Invalid fax request identity.')

    def matches(self, stored):
        """True when a stored fingerprint describes this same request."""
        return stored == self.request_fingerprint or stored in self.legacy_fingerprints

    @classmethod
    def from_key(cls, key, *, principal_scope, fingerprint, legacy_fingerprints=()):
        if (not isinstance(key, str) or not 1 <= len(key) <= 128
                or any(ord(char) < 33 or ord(char) > 126 for char in key)):
            raise ValueError('Idempotency-Key must contain 1 to 128 printable ASCII characters without spaces.')
        return cls(principal_scope, hashlib.sha256(key.encode()).hexdigest(), fingerprint,
                   tuple(legacy_fingerprints))


def intent_fingerprint(*, version, to, queue_only, document_sha256):
    """Version 1 binds ``to`` as entered; version 2 binds the canonical destination."""
    if version not in (1, 2) or not isinstance(to, str) or type(queue_only) is not bool:
        raise ValueError('Invalid fax request identity.')
    intent = json.dumps({'version': version, 'to': to, 'queue_only': queue_only,
                         'document_sha256': document_sha256},
                        sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(intent.encode()).hexdigest()


def request_fingerprints(*, entered, destination, queue_only, document_sha256):
    """Return ``(fingerprint, legacy_fingerprints)`` for one request.

    ``destination`` is the canonical number resolved for this request, or None
    when it cannot be resolved; only an exact version-1 replay can then match.
    """
    legacy = intent_fingerprint(version=1, to=entered, queue_only=queue_only,
                                document_sha256=document_sha256)
    if destination is None:
        return legacy, ()
    return intent_fingerprint(version=2, to=destination, queue_only=queue_only,
                              document_sha256=document_sha256), (legacy,)


async def digest_upload(upload, *, max_bytes):
    """Hash the original bounded upload before provider-specific preparation.

    The framework owns the upload spool. Rewind it on every exit so hashing
    does not alter conversion input, and create no additional artifacts.
    """
    digest, total = hashlib.sha256(), 0
    try:
        await upload.seek(0)
        while chunk := await upload.read(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise UploadPreparationError('File exceeds the configured upload limit.', status_code=413)
            digest.update(chunk)
        if total == 0:
            raise UploadPreparationError('Document is empty.', status_code=400)
        return digest.hexdigest()
    finally:
        await upload.seek(0)


async def fingerprint_upload(upload, *, to, queue_only, max_bytes):
    """The version-1 fingerprint, exactly as requests were identified before version 2."""
    return intent_fingerprint(version=1, to=to, queue_only=queue_only,
                              document_sha256=await digest_upload(upload, max_bytes=max_bytes))
