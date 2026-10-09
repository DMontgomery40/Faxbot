"""Idempotent acceptance transaction boundaries, no user-facing route tests."""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import io

from fastapi import UploadFile
import pytest
import sqlalchemy as sa

from api.tests.test_outbound_store import installation
from api.tests.test_schema import database
from api.app.config_profiles import ProviderConfiguration
from api.app.request_identity import RequestIdentity, IdempotentReplay, IdempotencyConflict, fingerprint_upload
from api.app.config_secrets import ConfigurationSecretError
from api.app.documents import UploadPreparationError


def job(identity=None):
    now = datetime.utcnow()
    return {'id': identity or uuid4().hex, 'to_number': '+15555550123', 'file_name': 'synthetic.txt',
            'tiff_path': '', 'status': 'queued', 'pages': 1, 'created_at': now, 'updated_at': now}


def intent(key='same-intent', scope='key:original', fingerprint='a'*64):
    return RequestIdentity.from_key(key, principal_scope=scope, fingerprint=fingerprint)


def test_identical_replay_survives_account_rotation_without_new_records(installation):
    configuration, store, snapshot = installation
    first = job()
    configuration.accept_outbound(snapshot.active, first, request_identity=intent())
    newer = configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'rotated'})})
    assert configuration.find_outbound_replay(intent()) == first['id']
    with pytest.raises(IdempotentReplay) as replay:
        configuration.accept_outbound(snapshot.active, job(), request_identity=intent())
    assert replay.value.job_id == first['id']
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(configuration.jobs)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(store.events)) == 1
    assert configuration.outbound_profile(first['id']).id == snapshot.active.profile_id('outbound')
    assert newer.active.profile_id('outbound') != snapshot.active.profile_id('outbound')


def test_mismatched_key_reuse_is_refused_and_principals_are_independent(installation):
    configuration, store, snapshot = installation
    first = job()
    configuration.accept_outbound(snapshot.active, first, request_identity=intent())
    with pytest.raises(IdempotencyConflict):
        configuration.find_outbound_replay(intent(fingerprint='b'*64))
    with pytest.raises(IdempotencyConflict):
        configuration.accept_outbound(snapshot.active, job(), request_identity=intent(fingerprint='b'*64))
    second = job()
    configuration.accept_outbound(snapshot.active, second, request_identity=intent(scope='key:other'))
    assert configuration.find_outbound_replay(intent(scope='key:other')) == second['id']
    assert store.get(first['id'])['state'] == 'ready'


def test_concurrent_acceptances_share_one_durable_intent(installation):
    configuration, store, snapshot = installation
    ready = Barrier(2)
    def accept_one():
        prepared = job()
        ready.wait(timeout=10)
        try:
            configuration.accept_outbound(snapshot.active, prepared, request_identity=intent())
            return 'accepted', prepared['id']
        except IdempotentReplay as replay:
            return 'replayed', replay.job_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(accept_one) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(kind for kind, _ in results) == ['accepted', 'replayed']
    assert len({identity for _, identity in results}) == 1
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(configuration.jobs)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(store.deliveries)) == 1


@pytest.mark.asyncio
async def test_identical_replay_hashes_under_accepted_upload_bound_after_limit_reduction(installation):
    configuration, _, snapshot = installation
    data = b'x' * (1024 * 1024 + 1)
    first = job()
    original = await fingerprint_upload(UploadFile(io.BytesIO(data), filename='synthetic.txt'),
        to=first['to_number'], queue_only=False,
        max_bytes=snapshot.active.values.max_file_size_mb * 1024 * 1024)
    accepted = intent(fingerprint=original)
    configuration.accept_outbound(snapshot.active, first, request_identity=accepted)
    changed = configuration.apply(snapshot, snapshot.active.values.with_patch({'max_file_size_mb': 1}),
        actor='test', restart_required=False)
    # Header/key validation occurs before lookup or hashing. Fingerprint is
    # computed afterward and still checked exactly before returning the replay.
    validated = intent(fingerprint='0' * 64)
    bound = configuration.outbound_replay_max_bytes(validated)
    assert bound == snapshot.active.values.max_file_size_mb * 1024 * 1024
    replayed_fingerprint = await fingerprint_upload(UploadFile(io.BytesIO(data), filename='renamed.txt'),
        to=first['to_number'], queue_only=False,
        max_bytes=bound if bound is not None else changed.active.values.max_file_size_mb * 1024 * 1024)
    assert configuration.find_outbound_replay(intent(fingerprint=replayed_fingerprint)) == first['id']
    changed_fingerprint = await fingerprint_upload(UploadFile(io.BytesIO(data[:-1] + b'y')),
        to=first['to_number'], queue_only=False, max_bytes=bound)
    with pytest.raises(IdempotencyConflict):
        configuration.find_outbound_replay(intent(fingerprint=changed_fingerprint))


@pytest.mark.asyncio
async def test_upload_replay_bound_requires_the_original_scoped_key(installation):
    configuration, _, snapshot = installation
    configuration.accept_outbound(snapshot.active, job(), request_identity=intent())
    changed = configuration.apply(snapshot, snapshot.active.values.with_patch({'max_file_size_mb': 1}),
        actor='test', restart_required=False)
    for other in (intent(scope='key:other'), intent(key='another-key')):
        bound = configuration.outbound_replay_max_bytes(other)
        assert bound is None
        with pytest.raises(UploadPreparationError) as failure:
            await fingerprint_upload(UploadFile(io.BytesIO(b'x' * (1024 * 1024 + 1))),
                to='+15555550123', queue_only=False,
                max_bytes=bound if bound is not None else changed.active.values.max_file_size_mb * 1024 * 1024)
        assert failure.value.status_code == 413


def test_upload_replay_bound_authenticates_the_original_revision_binding(installation):
    configuration, _, snapshot = installation
    first = job()
    configuration.accept_outbound(snapshot.active, first, request_identity=intent())
    newer = configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'rotated'})})
    with configuration._locked() as connection:
        connection.execute(configuration.job_bindings.update().where(
            configuration.job_bindings.c.id == first['id']).values(
                profile_id=newer.active.profile_id('outbound')))
    with pytest.raises(ConfigurationSecretError):
        configuration.outbound_replay_max_bytes(intent())


# Faxes Faxbot makes itself under an ID named in advance (forms, receipt queries) ---------------------------------

class _Outbound:
    """``access.outbound`` as the generated-fax path calls it: the fax and its binding in one transaction."""

    def __init__(self, configuration):
        self.configuration, self.accepted = configuration, []

    def accept(self, actor, revision, job, *, request_identity=None, also=None):
        with self.configuration._locked() as connection:
            self.configuration._accept_outbound_on(connection, revision, job)
            if also is not None:
                also(connection, datetime.utcnow())
        self.accepted.append(job['id'])


def _generated(database, tmp_path):
    from types import SimpleNamespace
    from api.app.schema import upgrade_schema
    from api.app.config_store import ConfigurationStore
    from api.app.config_values import ConfigurationValues
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false',
                                                   'FAX_DATA_DIR': str(tmp_path)})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': ProviderConfiguration(
        'phaxio', credentials={'api_key': 'synthetic-key'}, traits={'requires_tiff': True})})
    runtime = SimpleNamespace(manager=SimpleNamespace(store=configuration))
    return runtime, SimpleNamespace(outbound=_Outbound(configuration)), snapshot


def _pdf(text):
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(72, 720, text)
    page.showPage()
    page.save()
    return buffer.getvalue()


def test_a_generated_fax_named_again_keeps_the_first_fax_and_its_files_untouched(database, tmp_path):
    """A second request naming a fax already queued (a form claim retried, a second click) returns that fax:
    no second fax, and its PDF and TIFF stay byte for byte as they were."""
    from types import SimpleNamespace
    from api.app.routing.submit import accept_generated_fax
    runtime, access, snapshot = _generated(database, tmp_path)
    actor, fixed = SimpleNamespace(principal_id=None, credential=None), uuid4().hex

    def send(text):
        return accept_generated_fax(runtime, access, actor, snapshot.active, to_number='+15555550123',
                                    document=_pdf(text), file_name='form.pdf', pages=1, job_id=fixed)
    assert send('Synthetic form, first request') == fixed
    pdf, tiff = tmp_path / f'{fixed}.pdf', tmp_path / f'{fixed}.tiff'
    before = (pdf.read_bytes(), tiff.read_bytes())
    assert send('Synthetic form, a different second request') == fixed
    assert (pdf.read_bytes(), tiff.read_bytes()) == before
    assert access.outbound.accepted == [fixed]
    configuration = runtime.manager.store
    with configuration.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(configuration.jobs)).scalar() == 1


def test_a_generated_fax_another_request_is_queuing_is_left_alone(database, tmp_path):
    """The document is on disk but its fax is not committed yet: the second request writes and removes nothing.
    A file no fax ever owned, left long ago by an acceptance that never finished, is replaced."""
    import os
    import time
    from types import SimpleNamespace
    from api.app.routing.submit import GeneratedFaxBusy, ORPHAN_SECONDS, accept_generated_fax
    runtime, access, snapshot = _generated(database, tmp_path)
    actor, fixed = SimpleNamespace(principal_id=None, credential=None), uuid4().hex
    pdf = tmp_path / f'{fixed}.pdf'
    pdf.write_bytes(b'%PDF-1.4 being written by the first request')

    def send():
        return accept_generated_fax(runtime, access, actor, snapshot.active, to_number='+15555550123',
                                    document=_pdf('Synthetic form'), file_name='form.pdf', pages=1, job_id=fixed)
    with pytest.raises(GeneratedFaxBusy):
        send()
    assert pdf.read_bytes() == b'%PDF-1.4 being written by the first request'
    assert not (tmp_path / f'{fixed}.tiff').exists() and access.outbound.accepted == []
    stale = time.time() - ORPHAN_SECONDS - 60
    os.utime(pdf, (stale, stale))
    assert send() == fixed and access.outbound.accepted == [fixed]
    assert pdf.read_bytes().startswith(b'%PDF') and pdf.read_bytes() != b'%PDF-1.4 being written by the first request'
