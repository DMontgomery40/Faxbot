"""Stable identities for accepted fax intent; never retain raw client keys."""
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
    if row['request_fingerprint'] != identity.request_fingerprint:
        raise IdempotencyConflict()
    return row['id']


@dataclass(frozen=True)
class RequestIdentity:
    principal_scope: str
    idempotency_digest: str
    request_fingerprint: str

    def __post_init__(self):
        if (not isinstance(self.principal_scope, str) or not 1 <= len(self.principal_scope) <= 100
                or any(ord(char) < 33 or ord(char) > 126 for char in self.principal_scope)
                or any(not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None
                       for value in (self.idempotency_digest, self.request_fingerprint))):
            raise ValueError('Invalid fax request identity.')

    @classmethod
    def from_key(cls, key, *, principal_scope, fingerprint):
        if (not isinstance(key, str) or not 1 <= len(key) <= 128
                or any(ord(char) < 33 or ord(char) > 126 for char in key)):
            raise ValueError('Idempotency-Key must contain 1 to 128 printable ASCII characters without spaces.')
        return cls(principal_scope, hashlib.sha256(key.encode()).hexdigest(), fingerprint)


async def fingerprint_upload(upload, *, to, queue_only, max_bytes):
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
        intent = json.dumps({'version': 1, 'to': to, 'queue_only': queue_only,
                             'document_sha256': digest.hexdigest()},
                            sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        return hashlib.sha256(intent.encode()).hexdigest()
    finally:
        await upload.seek(0)
