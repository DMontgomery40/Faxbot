"""Bounded upload hashing and stable intent identity, no application routes."""
import hashlib
import io
from datetime import datetime
import pytest
import sqlalchemy as sa
from fastapi import UploadFile

from api.app.request_identity import (
    IdempotencyConflict,
    RequestIdentity,
    digest_upload,
    find_replay,
    fingerprint_upload,
    request_fingerprints,
)
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


@pytest.fixture
def accepted_request():
    """A stored identity without app startup, providers, or installation files."""
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    deliveries = sa.Table('deliveries', metadata,
        sa.Column('id', sa.String, primary_key=True),
        sa.Column('principal_scope', sa.String),
        sa.Column('idempotency_digest', sa.String),
        sa.Column('request_fingerprint', sa.String))
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            yield connection, deliveries
    finally:
        engine.dispose()


@pytest.mark.parametrize('legacy', [False, True], ids=['canonical', 'legacy'])
@pytest.mark.parametrize('change', [
    {'by_call': True},
    {'urgent': True},
    {'send_by': datetime(2026, 10, 29, 12, 30)},
    {'routing': {'mailbox': 'synthetic-mailbox'}},
    {'routing': {'workflow': 'synthetic-workflow'}},
    {'routing': {'labels': ['synthetic-label']}},
    {'routing': {'engine': 'hylafax'}},
    {'routing': {'cover_in_header': True}},
    {'routing': {'court': 'b' * 64}},
    {'routing': {'patient': {'record_number': 'synthetic-record'}}},
], ids=['real-call', 'urgent', 'deadline', 'mailbox', 'workflow', 'labels',
        'engine', 'cover-header', 'court', 'patient'])
def test_changed_send_options_conflict_with_an_accepted_request(accepted_request, legacy, change):
    # Dropping any option from either fingerprint version would incorrectly replay
    # the old fax instead of refusing a different sending instruction.
    connection, deliveries = accepted_request
    request = dict(entered='3035550123', destination='+13035550123',
                   queue_only=False, document_sha256='a' * 64)
    fingerprint, earlier = request_fingerprints(**request)
    original = RequestIdentity.from_key('synthetic-key', principal_scope='key:synthetic',
                                        fingerprint=fingerprint, legacy_fingerprints=earlier)
    connection.execute(deliveries.insert().values(id='accepted-fax',
        principal_scope=original.principal_scope, idempotency_digest=original.idempotency_digest,
        request_fingerprint=earlier[0] if legacy else fingerprint))
    assert find_replay(connection, deliveries, original) == 'accepted-fax'

    changed, changed_earlier = request_fingerprints(**(request | change))
    other = RequestIdentity.from_key('synthetic-key', principal_scope=original.principal_scope,
                                     fingerprint=changed, legacy_fingerprints=changed_earlier)
    with pytest.raises(IdempotencyConflict):
        find_replay(connection, deliveries, other)
    assert connection.scalar(sa.select(sa.func.count()).select_from(deliveries)) == 1


def test_routing_order_does_not_change_replay_identity():
    request = dict(entered='3035550123', destination='+13035550123',
                   queue_only=False, document_sha256='a' * 64)
    first = request_fingerprints(**request, routing={
        'mailbox': 'synthetic', 'labels': ['beta', 'alpha'], 'workflow': 'review'})
    reordered = request_fingerprints(**request, routing={
        'workflow': 'review', 'labels': ('alpha', 'beta'), 'mailbox': 'synthetic'})
    assert first == reordered


def test_deadline_changes_conflict_even_when_both_requests_have_a_deadline():
    request = dict(entered='3035550123', destination='+13035550123',
                   queue_only=False, document_sha256='a' * 64)
    first, first_legacy = request_fingerprints(**request, send_by=datetime(2026, 10, 29, 12))
    later, later_legacy = request_fingerprints(**request, send_by=datetime(2026, 10, 29, 13))
    assert first != later
    assert first_legacy != later_legacy


@pytest.mark.asyncio
async def test_empty_upload_is_rejected_and_rewound():
    upload = UploadFile(io.BytesIO())
    with pytest.raises(UploadPreparationError) as error:
        await digest_upload(upload, max_bytes=100)
    assert error.value.status_code == 400
    assert upload.file.tell() == 0


@pytest.mark.asyncio
async def test_upload_read_failure_propagates_and_rewinds():
    class InterruptedFile(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise OSError('synthetic read failure')
            return super().read(size)

    upload = UploadFile(InterruptedFile(b'x' * (64 * 1024 + 1)))
    with pytest.raises(OSError, match='synthetic read failure'):
        await digest_upload(upload, max_bytes=100000)
    assert upload.file.tell() == 0


@pytest.mark.asyncio
async def test_digest_reads_from_the_start_in_bounded_chunks():
    class BoundedFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 64 * 1024, 'hashing must not read the whole upload at once'
            return super().read(size)

    data = b'synthetic-original\n' * 10000
    upload = UploadFile(BoundedFile(data))
    await upload.seek(123)
    assert await digest_upload(upload, max_bytes=len(data)) == hashlib.sha256(data).hexdigest()
    assert upload.file.tell() == 0
