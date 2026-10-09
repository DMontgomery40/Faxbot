"""Decoded received originals follow their fax's retention, including late decodes."""
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app import codec, main
from app.codec import receive
from app.codec.store import receipt_for
from app.inbound import retention
from app.storage import LocalStorage
from api.tests.test_codec_delivery import ADMIN, client, _payload_pdf
from api.tests.test_schema import database


def received_payload(client, tmp_path, *, retention_until=None, decode=True):
    runtime = main.app.state.configuration_runtime
    engine = runtime.manager.store.engine
    identity = uuid4().hex
    document = codec.Document(b'Synthetic decoded original subject to fax retention.', 'text/plain', 'note.txt')
    data, _ = _payload_pdf(tmp_path, document, layout='grid')
    folder = Path(runtime.manager.store.read().active.values.fax_data_dir)
    pdf = folder / f'{identity}.pdf'
    pdf.write_bytes(data)
    now = datetime.utcnow()
    main.app.state.access_runtime.inbound.accept({
        'id': identity, 'status': 'received', 'backend': 'sip', 'pdf_path': str(pdf),
        'pages': 1, 'size_bytes': len(data), 'retention_until': retention_until,
        'created_at': now, 'updated_at': now, 'received_at': now})
    target = folder / f'{identity}-decoded-{document.sha256[:12]}.txt'
    if decode:
        receipt = receive.check_document(engine, identity, data, folder=folder)
        assert receipt['state'] == 'decoded'
        assert Path(receipt['document_path']) == target
    return engine, identity, pdf, target, document.data


def update(engine, identity, **values):
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(faxes.update().where(faxes.c.id == identity).values(**values))


def source(engine, identity):
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return dict(connection.execute(sa.select(faxes).where(faxes.c.id == identity)).mappings().one())


def expire(engine, identity):
    update(engine, identity, retention_until=datetime.utcnow() - timedelta(days=1))


def cleanup(client):
    with main.app.state.configuration_runtime.frame():
        client.portal.call(main._cleanup_once)


def race_payload(engine, tmp_path):
    """The publication boundary on either supported database, with real encoded pages."""
    from app.schema import upgrade_schema
    upgrade_schema(engine)
    identity = uuid4().hex
    document = codec.Document(b'Synthetic original in a concurrent retention test.', 'text/plain', 'note.txt')
    data, _ = _payload_pdf(tmp_path, document, layout='grid')
    pdf = tmp_path / f'{identity}.pdf'
    pdf.write_bytes(data)
    now = datetime.utcnow()
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(faxes.insert().values(
            id=identity, status='received', backend='sip', pdf_path=str(pdf), pages=1,
            size_bytes=len(data), created_at=now, updated_at=now, received_at=now))
    decoded = tmp_path / f'{identity}-decoded-{document.sha256[:12]}.txt'
    return engine, identity, pdf, decoded, document.data


def test_expired_received_fax_removes_its_decoded_original_and_download(client, tmp_path):
    engine, identity, pdf, decoded, original = received_payload(client, tmp_path)
    assert client.get(f'/codec/received/{identity}/document', headers=ADMIN).content == original
    expire(engine, identity)
    cleanup(client)
    assert not pdf.exists()
    assert not decoded.exists(), 'The verified original survived deletion of its received fax.'
    assert receipt_for(engine, identity)['document_path'] is None
    assert client.get(f'/codec/received/{identity}/document', headers=ADMIN).status_code == 404


@pytest.mark.parametrize('deadline', [None, datetime(2100, 1, 1)])
def test_indefinitely_kept_or_not_yet_due_faxes_keep_both_copies(client, tmp_path, deadline):
    engine, identity, pdf, decoded, original = received_payload(client, tmp_path, retention_until=deadline)
    before = receipt_for(engine, identity)
    assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([], [])
    assert pdf.exists() and decoded.read_bytes() == original
    assert receipt_for(engine, identity) == before


def test_object_stored_fax_expires_its_local_decoded_copy_and_retains_receipt_metadata(client, tmp_path):
    engine, identity, pdf, decoded, _ = received_payload(client, tmp_path)
    before = receipt_for(engine, identity)
    uri = f's3://synthetic-bucket/received/{pdf.name}'
    update(engine, identity, pdf_path=uri)
    pdf.unlink()  # Acquisition removes its local PDF after a successful upload.
    expire(engine, identity)

    class Objects:
        bucket, prefix = 'synthetic-bucket', 'received/'
        deleted = []

        def delete(self, location):
            self.deleted.append(location)

    objects = Objects()
    assert retention.remove_expired_documents(engine, objects, pdf.parent) == ([identity], [])
    assert objects.deleted == [uri] and not decoded.exists()
    assert receipt_for(engine, identity) == {**before, 'document_path': None}
    assert source(engine, identity)['pdf_path'] is None


def test_already_missing_files_clear_references_and_retained_tiff_is_removed(client, tmp_path):
    engine, identity, pdf, decoded, _ = received_payload(client, tmp_path)
    tiff = pdf.parent / 'inbound' / '12345.6.tiff'
    tiff.parent.mkdir(exist_ok=True)
    tiff.write_bytes(b'synthetic retained TIFF bytes')
    update(engine, identity, tiff_path=str(tiff))
    expire(engine, identity)
    pdf.unlink()
    decoded.unlink()
    assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([identity], [])
    assert not tiff.exists()
    assert source(engine, identity)['tiff_path'] is None
    assert receipt_for(engine, identity)['document_path'] is None


@pytest.mark.parametrize('bad_path', ['outside', 'wrong_name', 'symlink', 'parent_symlink'])
def test_unowned_decoded_path_preserves_all_files_and_references(client, tmp_path, bad_path):
    engine, identity, pdf, decoded, original = received_payload(client, tmp_path)
    outsider = tmp_path / decoded.name
    outsider.write_bytes(b'unrelated original')
    if bad_path == 'outside':
        value = outsider
    elif bad_path == 'wrong_name':
        value = pdf.parent / 'unrelated.txt'
        value.write_bytes(b'unrelated original')
    elif bad_path == 'symlink':
        decoded.unlink()
        decoded.symlink_to(outsider)
        value = decoded
    else:
        linked = pdf.parent / 'linked'
        linked.symlink_to(tmp_path, target_is_directory=True)
        value = linked / decoded.name
    receipts = sa.Table('codec_receipts', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(receipts.update().where(receipts.c.inbound_fax_id == identity).values(
            document_path=str(value)))
    expire(engine, identity)
    before = receipt_for(engine, identity)
    assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([], [identity])
    assert pdf.exists() and value.exists() and outsider.read_bytes() == b'unrelated original'
    assert receipt_for(engine, identity) == before
    assert source(engine, identity)['pdf_path'] == str(pdf)


@pytest.mark.parametrize('field', ['pdf_path', 'tiff_path'])
def test_outside_source_pointer_is_not_deleted(client, tmp_path, field):
    engine, identity, pdf, decoded, original = received_payload(client, tmp_path)
    outsider = tmp_path / (pdf.name if field == 'pdf_path' else 'unrelated.tiff')
    outsider.write_bytes(b'unrelated source')
    update(engine, identity, **{field: str(outsider)})
    expire(engine, identity)
    assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([], [identity])
    assert outsider.exists() and pdf.exists() and decoded.read_bytes() == original
    assert source(engine, identity)[field] == str(outsider)


def test_failed_decoded_delete_preserves_references_and_reports_attention_then_retries(client, tmp_path, monkeypatch):
    engine, identity, pdf, decoded, _ = received_payload(client, tmp_path)
    expire(engine, identity)
    before = receipt_for(engine, identity)
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == decoded:
            raise PermissionError('synthetic denied deletion')
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', denied)
        events = []
        patch.setattr(main, 'audit_event', lambda name, **values: events.append((name, values)))
        cleanup(client)
    assert ('inbound_retention_requires_attention', {'job_id': identity}) in events
    assert pdf.exists() and decoded.exists()
    assert receipt_for(engine, identity) == before
    assert source(engine, identity)['pdf_path'] == str(pdf)
    assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([identity], [])


def test_failed_storage_delete_keeps_references_for_retry(client, tmp_path):
    engine, identity, pdf, decoded, _ = received_payload(client, tmp_path)
    before = receipt_for(engine, identity)
    expire(engine, identity)

    class Unavailable(LocalStorage):
        def delete(self, location):
            raise OSError('synthetic storage outage')

    assert retention.remove_expired_documents(engine, Unavailable(), pdf.parent) == ([], [identity])
    assert pdf.exists() and source(engine, identity)['pdf_path'] == str(pdf)
    assert receipt_for(engine, identity) == before
    # A physical deletion cannot roll back; its retained reference is safe to retry.
    assert not decoded.exists()
    assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([identity], [])
    assert receipt_for(engine, identity)['document_path'] is None


def test_retention_change_after_selection_is_rechecked_under_the_document_lock(client, tmp_path, monkeypatch):
    engine, identity, pdf, decoded, original = received_payload(client, tmp_path)
    expire(engine, identity)
    lock = retention.locked_document

    @contextmanager
    def preserve_before_lock(engine, inbound_fax_id):
        update(engine, inbound_fax_id, retention_until=None)
        with lock(engine, inbound_fax_id) as value:
            yield value

    monkeypatch.setattr(retention, 'locked_document', preserve_before_lock)
    assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([], [])
    assert pdf.exists() and decoded.read_bytes() == original
    assert receipt_for(engine, identity)['document_path'] == str(decoded)


@pytest.mark.parametrize('previous_failure', [False, True])
def test_retention_wins_while_decode_is_in_progress_and_late_result_cannot_resurrect(
        database, tmp_path, monkeypatch, previous_failure):
    engine, identity, pdf, decoded, _ = race_payload(database, tmp_path)
    if previous_failure:
        receive.record_receipt(engine, identity, {'state': 'failed', 'reason': 'Synthetic previous failure.'})
        monkeypatch.setattr(receive.CodecSettings, 'keys_changed_since', lambda *args: True)
    before = receipt_for(engine, identity)
    data = pdf.read_bytes()
    ready, resume = Event(), Event()
    decode = codec.decode_images

    def wait_before_publication(*args, **kwargs):
        result = decode(*args, **kwargs)
        ready.set()
        assert resume.wait(10)
        return result

    monkeypatch.setattr(codec, 'decode_images', wait_before_publication)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(receive.check_document, engine, identity, data, folder=pdf.parent)
        try:
            assert ready.wait(10)
            expire(engine, identity)
            assert retention.remove_expired_documents(engine, LocalStorage(), pdf.parent) == ([identity], [])
        finally:
            resume.set()
        assert pending.result(timeout=10) is None
    assert not pdf.exists() and not decoded.exists()
    assert receipt_for(engine, identity) == before
    assert not list(pdf.parent.glob('.codec-*'))


def test_publication_wins_lock_then_retention_removes_the_committed_original(database, tmp_path, monkeypatch):
    engine, identity, pdf, decoded, _ = race_payload(database, tmp_path)
    expire(engine, identity)
    publishing, resume, expiring = Event(), Event(), Event()
    record = receive.record_receipt
    lock = retention.locked_document

    def wait_inside_publication(*args, **kwargs):
        publishing.set()
        assert resume.wait(10)
        return record(*args, **kwargs)

    @contextmanager
    def observed_lock(*args, **kwargs):
        if publishing.is_set():
            expiring.set()
        with lock(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(receive, 'record_receipt', wait_inside_publication)
    monkeypatch.setattr(retention, 'locked_document', observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(receive.check_document, engine, identity, pdf.read_bytes(), folder=pdf.parent)
        try:
            assert publishing.wait(10)
            cleaning = pool.submit(retention.remove_expired_documents, engine, LocalStorage(), pdf.parent)
            assert expiring.wait(10)
            assert not cleaning.done()
        finally:
            resume.set()
        assert checking.result(timeout=10)['state'] == 'decoded'
        assert cleaning.result(timeout=10) == ([identity], [])
    assert not pdf.exists() and not decoded.exists()
    assert source(engine, identity)['pdf_path'] is None
    assert receipt_for(engine, identity)['document_path'] is None
