"""Bounded upload hashing and stable intent identity, no application routes."""
import hashlib
import io
import pytest
from fastapi import UploadFile

from api.app.request_identity import RequestIdentity, fingerprint_upload
from api.app.documents import UploadPreparationError


@pytest.mark.asyncio
async def test_fingerprint_streams_original_bytes_and_rewinds_for_conversion():
    data = b'synthetic-original\n' * 10000
    upload = UploadFile(io.BytesIO(data), filename='first.txt')
    first = await fingerprint_upload(upload, to='+15555550123', queue_only=False, max_bytes=len(data))
    assert await upload.read() == data
    await upload.seek(0)
    upload.filename = 'different-display-name.txt'
    assert first == await fingerprint_upload(upload, to='+15555550123', queue_only=False, max_bytes=len(data))
    assert first != await fingerprint_upload(upload, to='+15555550123', queue_only=True, max_bytes=len(data))
    assert first != await fingerprint_upload(upload, to='+15555550124', queue_only=False, max_bytes=len(data))


@pytest.mark.asyncio
async def test_hashing_rejects_oversize_and_rewinds_without_publishing_files():
    upload = UploadFile(io.BytesIO(b'abcd'))
    with pytest.raises(UploadPreparationError) as error:
        await fingerprint_upload(upload, to='15555550123', queue_only=False, max_bytes=3)
    assert error.value.status_code == 413
    assert await upload.read() == b'abcd'


def test_idempotency_key_is_bounded_and_only_digest_is_retained():
    identity = RequestIdentity.from_key('synthetic-client-intent', principal_scope='key:abc', fingerprint='a'*64)
    assert identity.idempotency_digest == hashlib.sha256(b'synthetic-client-intent').hexdigest()
    assert 'synthetic-client-intent' not in repr(identity)
    for invalid in ('', ' ', 'a\n', 'ü', 'a'*129):
        with pytest.raises(ValueError):
            RequestIdentity.from_key(invalid, principal_scope='key:abc', fingerprint='a'*64)
    with pytest.raises(ValueError):
        RequestIdentity.from_key('valid', principal_scope='', fingerprint='a'*64)


def test_openapi_declares_the_optional_idempotency_header():
    # Schema generation only; no client, route invocation or lifespan startup.
    from api.app.main import app
    parameters = app.openapi()['paths']['/fax']['post']['parameters']
    headers = [entry for entry in parameters if entry['in'] == 'header'
               and entry['name'] == 'Idempotency-Key']
    assert len(headers) == 1
    assert headers[0]['required'] is False
    assert headers[0].get('description')
