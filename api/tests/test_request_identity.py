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


@pytest.mark.asyncio
async def test_version_one_fingerprint_is_exactly_the_pre_change_identity():
    # Records stored before canonical destinations must still compare equal.
    import json
    data = b'synthetic-original'
    stored = hashlib.sha256(json.dumps({'version': 1, 'to': '3035550123', 'queue_only': False,
                                        'document_sha256': hashlib.sha256(data).hexdigest()},
                                       sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    upload = UploadFile(io.BytesIO(data), filename='a.txt')
    assert await fingerprint_upload(upload, to='3035550123', queue_only=False, max_bytes=100) == stored


def test_version_two_binds_the_canonical_destination_and_keeps_the_exact_old_form_for_replay():
    from api.app.request_identity import intent_fingerprint, request_fingerprints
    from api.app.routing.numbers import normalize_number
    document = 'a' * 64
    forms = ['01782 684953', '+44 1782 684953', '0044 1782 684953']
    identities = [request_fingerprints(entered=form, destination=normalize_number(form, country='GB'),
                                       queue_only=False, document_sha256=document) for form in forms]
    assert len({fingerprint for fingerprint, _ in identities}) == 1  # one canonical request
    assert [legacy for _, legacy in identities] == [
        (intent_fingerprint(version=1, to=form, queue_only=False, document_sha256=document),) for form in forms]
    assert identities[0][0] == intent_fingerprint(version=2, to='+441782684953', queue_only=False,
                                                  document_sha256=document)
    other = request_fingerprints(entered='01782 684954', destination='+441782684954', queue_only=False,
                                 document_sha256=document)
    assert other[0] != identities[0][0] and other[1] != identities[0][1]
    queued = request_fingerprints(entered=forms[0], destination='+441782684953', queue_only=True,
                                  document_sha256=document)
    assert queued[0] != identities[0][0]
    # An unresolvable number can only match its exact pre-change record.
    assert request_fingerprints(entered='123456', destination=None, queue_only=False,
                                document_sha256=document) == (
        intent_fingerprint(version=1, to='123456', queue_only=False, document_sha256=document), ())


def test_identity_matches_its_fingerprint_or_an_earlier_version_only():
    identity = RequestIdentity.from_key('k', principal_scope='key:abc', fingerprint='a' * 64,
                                        legacy_fingerprints=['b' * 64])
    assert identity.matches('a' * 64) and identity.matches('b' * 64) and not identity.matches('c' * 64)
    assert identity.legacy_fingerprints == ('b' * 64,)
    with pytest.raises(ValueError):
        RequestIdentity('key:abc', 'a' * 64, 'a' * 64, ('not-a-digest',))
