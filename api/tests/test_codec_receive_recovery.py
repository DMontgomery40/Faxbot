"""A received original can be recovered after setup changes without replacing its fax image."""
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
import sqlalchemy as sa

from app import codec, main
from app.codec import receive
from app.codec.store import receipt_for, record_receipt
from app.routing.background import installation_engine
from api.tests.test_codec_delivery import ADMIN, NUMBER, _payload_pdf, client
from api.tests.test_schema import database


@pytest.fixture(autouse=True)
def _recovery_database(isolated_installation, monkeypatch, database):
    """Exercise the actual HTTP runtime on each disposable database dialect."""
    url = database.url
    if database.dialect.name == 'postgresql':
        with database.connect() as connection:
            namespace = connection.exec_driver_sql('SELECT current_schema()').scalar_one()
        url = url.update_query_dict({'options': '-csearch_path=' + namespace})
    monkeypatch.setenv('DATABASE_URL', url.render_as_string(hide_password=False))


@pytest.fixture
def received_payload(client, tmp_path):
    original = codec.Document(b'Synthetic original preserved after a missing shared key.\n',
                              'text/plain', 'synthetic.txt')
    data, _ = _payload_pdf(tmp_path, original, layout='grid', secret='synthetic shared recovery key', fec='low')
    engine, runtime = installation_engine(main.app)
    identity = 'synthetic-codec-recovery'
    path = Path(main.settings.fax_data_dir) / f'{identity}.pdf'
    path.write_bytes(data)
    now = datetime.utcnow()
    main.app.state.access_runtime.inbound.accept(dict(
        id=identity, from_number=NUMBER, to_number='+12025550456', status='received', backend='sip',
        pages=1, pdf_path=str(path), created_at=now, received_at=now, updated_at=now))
    return engine, runtime, identity, path, data, original


@pytest.mark.parametrize('entry', ['http', 'document', 'download'])
def test_failed_decode_recovers_after_the_shared_key_is_added(client, received_payload, entry, monkeypatch):
    engine, runtime, identity, path, data, original = received_payload
    from app.codec.store import KeySeal
    seal = KeySeal(runtime.manager.store)
    attempts = []
    decode = codec.decode_images

    def counted(*args, **kwargs):
        attempts.append(True)
        return decode(*args, **kwargs)

    monkeypatch.setattr(codec, 'decode_images', counted)

    def check():
        if entry == 'document':
            return receive.check_document(engine, identity, data, from_number=NUMBER,
                                          folder=path.parent, seal=seal)
        if entry == 'download':
            response = client.get(f'/codec/received/{identity}/document', headers=ADMIN)
            if response.status_code == 200:
                assert response.content == original.data
                return {'state': 'decoded'}
            assert response.status_code == 404, response.text
            return receipt_for(engine, identity)
        response = client.get(f'/codec/received/{identity}', headers=ADMIN)
        assert response.status_code == 200, response.text
        return response.json()

    assert check()['state'] == 'failed'
    failed = receipt_for(engine, identity)
    assert failed['state'] == 'failed' and failed['document_path'] is None
    assert check()['state'] == 'failed' and len(attempts) == 1
    saved = client.put(f'/codec/numbers/{NUMBER}', headers=ADMIN, json={
        'enabled': True, 'recipient_agreed': True, 'shared_key': 'synthetic incorrect recovery key'})
    assert saved.status_code == 200, saved.text
    assert check()['state'] == 'failed' and len(attempts) == 2
    retried = receipt_for(engine, identity)
    assert retried['created_at'] > failed['created_at']
    assert check()['state'] == 'failed' and len(attempts) == 2
    # Decoding also tries other partners' stored keys; their changes must reopen the cache.
    saved = client.put('/codec/numbers/+12025550789', headers=ADMIN, json={
        'enabled': True, 'recipient_agreed': True, 'shared_key': 'synthetic shared recovery key'})
    assert saved.status_code == 200, saved.text
    assert check()['state'] == 'decoded'
    assert len(attempts) == 3
    recovered = receipt_for(engine, identity)
    assert recovered['state'] == 'decoded' and recovered['reason'] is None
    assert recovered['document_sha256'] == original.sha256
    assert Path(recovered['document_path']).read_bytes() == original.data
    downloaded = client.get(f'/codec/received/{identity}/document', headers=ADMIN)
    assert downloaded.status_code == 200 and downloaded.content == original.data
    assert path.read_bytes() == data
    # A later failed checker must not replace a verified result or cause another delivery.
    result = record_receipt(engine, identity, {'state': 'failed', 'reason': 'A stale decode failed.'})
    assert result == recovered
    assert check()['state'] == 'decoded'
    assert len(attempts) == 3


def test_retired_original_keeps_decode_history_but_is_not_available(client, received_payload):
    engine, _, identity, path, data, original = received_payload
    decoded = path.parent / 'already-decoded.txt'
    decoded.write_bytes(original.data)
    record_receipt(engine, identity, {
        'state': 'decoded', 'content_type': 'text/plain', 'document_name': 'synthetic.txt',
        'document_path': str(decoded), 'document_sha256': original.sha256, 'size_bytes': len(original.data)})
    with engine.begin() as connection:
        inbound = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=connection)
        connection.execute(inbound.update().where(inbound.c.id == identity).values(pdf_path=None))
    detail = client.get(f'/codec/received/{identity}', headers=ADMIN)
    assert detail.status_code == 200, detail.text
    assert detail.json()['encoded'] is True and detail.json()['state'] == 'decoded'
    assert detail.json()['document_available'] is False
    assert 'no longer available' in detail.json()['sentence']
    assert client.get(f'/codec/received/{identity}/document', headers=ADMIN).status_code == 404
    assert path.read_bytes() == data


def test_cleanup_after_the_decode_check_does_not_reopen_a_deleted_document(client, received_payload, monkeypatch):
    from app.codec import http
    from app.inbound.retention import remove_expired_documents
    from app.storage import LocalStorage
    engine, _, identity, path, _, original = received_payload
    decoded = path.parent / f'{identity}-decoded-{original.sha256[:12]}.txt'
    decoded.write_bytes(original.data)
    record_receipt(engine, identity, {
        'state': 'decoded', 'content_type': 'text/plain', 'document_name': 'synthetic.txt',
        'document_path': str(decoded), 'document_sha256': original.sha256, 'size_bytes': len(original.data)})
    check = http._check_received

    async def expire_after_check(*args):
        result = await check(*args)
        with engine.begin() as connection:
            faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=connection)
            connection.execute(faxes.update().where(faxes.c.id == identity).values(
                retention_until=datetime.utcnow() - timedelta(days=1)))
        assert remove_expired_documents(engine, LocalStorage(), path.parent) == ([identity], [])
        return result

    monkeypatch.setattr(http, '_check_received', expire_after_check)
    response = client.get(f'/codec/received/{identity}/document', headers=ADMIN)
    assert response.status_code == 404
    assert not decoded.exists()


def test_an_opened_original_finishes_transfer_after_cleanup_unlinks_it(client, received_payload, monkeypatch):
    from app.codec import http
    from app.inbound.retention import remove_expired_documents
    from app.storage import LocalStorage
    engine, _, identity, path, _, original = received_payload
    decoded = path.parent / f'{identity}-decoded-{original.sha256[:12]}.txt'
    decoded.write_bytes(original.data)
    record_receipt(engine, identity, {
        'state': 'decoded', 'content_type': 'text/plain', 'document_name': 'synthetic.txt',
        'document_path': str(decoded), 'document_sha256': original.sha256, 'size_bytes': len(original.data)})
    response_class = http._DecodedResponse
    opened = []

    def expire_after_open(handle, **kwargs):
        opened.append(handle)
        with engine.begin() as connection:
            faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=connection)
            connection.execute(faxes.update().where(faxes.c.id == identity).values(
                retention_until=datetime.utcnow() - timedelta(days=1)))
        assert remove_expired_documents(engine, LocalStorage(), path.parent) == ([identity], [])
        assert not decoded.exists() and not handle.closed
        return response_class(handle, **kwargs)

    monkeypatch.setattr(http, '_DecodedResponse', expire_after_open)
    response = client.get(f'/codec/received/{identity}/document', headers=ADMIN)
    assert response.status_code == 200 and response.content == original.data
    assert opened[0].closed
    assert client.get(f'/codec/received/{identity}/document', headers=ADMIN).status_code == 404


def test_cancelled_decoded_transfer_closes_its_open_handle(tmp_path):
    import asyncio
    from app.codec.http import _DecodedResponse
    path = tmp_path / 'stream.txt'
    path.write_bytes(b'x' * (128 * 1024))
    handle = path.open('rb')
    response = _DecodedResponse(handle, media_type='text/plain')

    async def send(message):
        if message['type'] == 'http.response.body':
            assert len(message['body']) == 64 * 1024
            raise asyncio.CancelledError()

    async def receive():
        raise AssertionError('The streaming response should use its direct ASGI 2.4 send path.')

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(response({'type': 'http', 'method': 'GET', 'asgi': {'spec_version': '2.4'}}, receive, send))
    assert handle.closed


def test_a_late_failed_attempt_cannot_replace_a_newer_failure(client, received_payload):
    engine, _, identity, _, _, _ = received_payload
    first = datetime(2026, 1, 1, 1)
    latest = datetime(2026, 1, 1, 3)
    late = datetime(2026, 1, 1, 2)
    record_receipt(engine, identity, {'state': 'failed', 'reason': 'First failure.'}, now=first)
    record_receipt(engine, identity, {'state': 'failed', 'reason': 'Newest failure.'}, now=latest)
    result = record_receipt(engine, identity, {'state': 'failed', 'reason': 'An older retry failed.'}, now=late)
    assert result['reason'] == 'Newest failure.' and result['created_at'] == latest


def test_a_key_added_during_a_failed_attempt_is_still_seen_on_the_next_check(
        client, received_payload, monkeypatch):
    from app.codec.store import KeySeal
    engine, runtime, identity, path, data, original = received_payload
    decode = codec.decode_images
    first = [True]

    def key_arrives_after_secrets_were_read(*args, **kwargs):
        if first:
            first.pop()
            assert kwargs['secrets'] == []
            saved = client.put(f'/codec/numbers/{NUMBER}', headers=ADMIN, json={
                'enabled': True, 'recipient_agreed': True, 'shared_key': 'synthetic shared recovery key'})
            assert saved.status_code == 200, saved.text
        return decode(*args, **kwargs)

    monkeypatch.setattr(codec, 'decode_images', key_arrives_after_secrets_were_read)
    seal = KeySeal(runtime.manager.store)
    failed = receive.check_document(engine, identity, data, from_number=NUMBER, folder=path.parent, seal=seal)
    assert failed['state'] == 'failed'
    recovered = receive.check_document(engine, identity, data, from_number=NUMBER, folder=path.parent, seal=seal)
    assert recovered['state'] == 'decoded'
    assert Path(recovered['document_path']).read_bytes() == original.data


def test_removing_a_key_retries_when_it_exposes_the_correct_fifty_first_key(client, received_payload):
    from app.codec.store import CodecSettings, KeySeal
    engine, runtime, identity, path, data, original = received_payload
    seal = KeySeal(runtime.manager.store)
    settings = CodecSettings(engine, seal)
    numbers = {f'synthetic candidate key {index:02}': f'+1202555{index:04}' for index in range(51)}
    for key, number in numbers.items():
        settings.save(number, enabled=True, recipient_agreed=True, actor='synthetic', secret=key)
    candidates = settings.secrets(NUMBER)
    assert len(candidates) == 50
    # SQL row order is unspecified. Make the fax use whichever one is currently excluded.
    correct, = set(numbers) - set(candidates)
    data, _ = _payload_pdf(path.parent, original, layout='grid', secret=correct, fec='low')
    path.write_bytes(data)
    failed = receive.check_document(engine, identity, data, from_number=NUMBER, folder=path.parent, seal=seal)
    assert failed['state'] == 'failed'
    settings.save(numbers[candidates[0]], enabled=True, recipient_agreed=True, actor='synthetic', clear_key=True)
    assert len(settings.secrets(NUMBER)) == 50
    assert correct in settings.secrets(NUMBER)
    recovered = receive.check_document(engine, identity, data, from_number=NUMBER, folder=path.parent, seal=seal)
    assert recovered['state'] == 'decoded'
    assert Path(recovered['document_path']).read_bytes() == original.data


def test_a_rolled_back_publication_leaves_no_decoded_original(client, received_payload, monkeypatch):
    from contextlib import contextmanager
    from app.codec.store import KeySeal
    from app.inbound import retention
    engine, runtime, identity, path, data, _ = received_payload
    saved = client.put(f'/codec/numbers/{NUMBER}', headers=ADMIN, json={
        'enabled': True, 'recipient_agreed': True, 'shared_key': 'synthetic shared recovery key'})
    assert saved.status_code == 200, saved.text
    original_lock = retention.locked_document
    fail = [True]

    @contextmanager
    def fail_first_commit(*args):
        with original_lock(*args) as locked:
            yield locked
            if fail:
                fail.pop()
                assert list(path.parent.glob(f'{identity}-decoded-*'))
                raise RuntimeError('Synthetic failure before transaction commit.')

    monkeypatch.setattr(retention, 'locked_document', fail_first_commit)
    with pytest.raises(RuntimeError, match='Synthetic failure'):
        receive.check_document(engine, identity, data, from_number=NUMBER, folder=path.parent,
                               seal=KeySeal(runtime.manager.store))
    assert receipt_for(engine, identity) is None
    assert not list(path.parent.glob(f'{identity}-decoded-*'))
    assert path.read_bytes() == data


def test_a_missing_decoded_attachment_explains_the_problem_and_keeps_the_received_image(
        client, received_payload, tmp_path, caplog):
    engine, _, identity, _, data, original = received_payload
    absent = tmp_path / 'removed-original.txt'
    record_receipt(engine, identity, {
        'state': 'decoded', 'content_type': 'text/plain', 'document_name': 'synthetic.txt',
        'document_path': str(absent), 'document_sha256': original.sha256, 'size_bytes': len(original.data)})
    attachment, note = receive.email_extras(engine, {'inbound_fax_id': identity}, data, folder=tmp_path)
    assert attachment is None
    assert note and 'delivered as received' in note
    assert str(absent) not in note
    message = EmailMessage()
    message.set_content('A fax arrived.\n')
    message.add_attachment(data, maintype='application', subtype='pdf', filename='received.pdf')
    receive.attach(message, attachment, note)
    parts = list(message.iter_attachments())
    assert len(parts) == 1 and parts[0].get_content() == data
    assert note in message.get_body().get_content()
    assert str(absent) not in caplog.text
