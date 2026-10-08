"""A call that broke after pages went is never sent again whole by itself (C1).

Each path that can report pages sent before a failure classifies it
``partly_sent``: the built-in engine's FaxResult (FAXPAGES), Documo
(``pagesComplete``) and HumbleFax ("partial success", or an attempt that sent
pages). Such a fax stays failed on its attempt, takes no other route even when
the installation's fallback rule has one (in the observation or in
``routing/fallback.py``), gets an uncertain item for a person, and is offered
its remaining pages where the pages are confirmed. Each case runs beside a
plain failure that does fall back, so the setup is proven to allow it.
"""
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_polling import OutboundPoller
from api.app.outbound_store import OutboundStore
from api.tests.test_continuation import DCS_ECM, synthetic_document
from api.tests.test_outbound_polling import with_profile
from api.tests.test_outbound_store import accept, another_number, installation  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture


DOCUMO = ProviderConfiguration('documo', credentials={'api_key': 'synthetic-key'},
                               settings={'base_url': 'https://original.invalid', 'sandbox': False})
HUMBLEFAX = ProviderConfiguration('humblefax', credentials={'access_key': 'a', 'secret_key': 'b'})
SIP = ProviderConfiguration('sip', traits={'requires_tiff': True})


@pytest.fixture
def another_route(monkeypatch):
    """The installation's fallback rule always has another route for a failed fax."""
    monkeypatch.setattr(OutboundStore, 'fallback_policy', lambda job_id, attempt_id: True)


def on_the_line(installation, profile, sid=None):  # noqa: F811
    """A three-page fax handed to ``profile``'s account and waiting for its result."""
    installation = with_profile(installation, profile)
    configuration, store, snapshot = installation
    job = accept(installation, to_number=another_number())
    claim = store.claim('worker')
    assert claim.job_id == job
    store.begin_submission(claim)
    store.record_receipt(claim, provider_sid=sid or job, status='in_progress')
    return installation, job, claim


def attempt_of(store, attempt_id):
    with store.configuration.engine.connect() as connection:
        return connection.execute(sa.text('SELECT phase, error_category FROM outbound_attempts WHERE id = :id'),
                                  {'id': attempt_id}).mappings().one()


def fell_back(store, job):
    return any(event['kind'] == 'route_fallback' for event in store.history(job))


def keep_document(installation, job, pages=3):  # noqa: F811
    _, _, snapshot = installation
    folder = Path(snapshot.active.values.fax_data_dir or '.')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{job}.pdf').write_bytes(synthetic_document(pages))
    return str(folder)


def item_category(store, job):
    """The uncertain item the certainty feed makes for this fax, by its category; None when it makes none."""
    from api.app.work.certainty import CertaintyStore
    certainty = CertaintyStore(store.configuration.engine)
    certainty.feed(None)
    with store.configuration.engine.connect() as connection:
        return connection.execute(sa.text('SELECT category FROM certainty_items WHERE job_id = :job'),
                                  {'job': job}).scalar()


def offer(store, job, data_dir):
    from api.app.routing.continuation import ContinuationStore, offer_on
    continuations = ContinuationStore(store.configuration.engine)
    with store.configuration.engine.connect() as connection:
        return offer_on(continuations, connection, job, data_dir=data_dir)


def poll(store, job, monkeypatch, answer):
    class Service:
        async def get_fax_status(self, sid):
            return answer(sid)
    monkeypatch.setattr('api.app.outbound_polling.service_from_profile', lambda profile: Service())
    import asyncio
    asyncio.run(OutboundPoller(store).refresh(job))


# -- Documo --------------------------------------------------------------------------------------------

def documo_answer(**fields):
    from api.app import documo_service

    def answer(sid):
        return documo_service._receipt(httpx.Response(200, json={'messageId': sid, 'status': 'failed',
                                                                 'resultCode': '7000', **fields}),
                                       requested_sid=sid)
    return answer


def test_a_documo_failure_with_pages_completed_is_partly_sent_and_never_falls_back(  # noqa: F811
        installation, another_route, monkeypatch):
    from api.app.documo_service import PARTLY_SENT
    installation, job, claim = on_the_line(installation, DOCUMO, str(uuid4()))
    configuration, store, _ = installation
    poll(store, job, monkeypatch, documo_answer(pagesComplete=2, pagesCount=3))
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    installation, plain, _ = on_the_line(installation, DOCUMO, str(uuid4()))
    poll(store, plain, monkeypatch, documo_answer(pagesComplete=0, pagesCount=3))
    assert store.get(plain)['state'] == 'ready' and fell_back(store, plain)  # the setup does fall back
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': 'partly_sent'}
    with configuration.engine.connect() as connection:
        assert connection.execute(sa.text('SELECT error FROM fax_jobs WHERE id = :id'), {'id': job}).scalar() \
            == PARTLY_SENT
    assert item_category(store, job) == 'partly_sent'
    found = offer(store, job, keep_document(installation, job))
    assert found.available and (found.first_page, found.last_page) == (3, 3)


def test_the_fallback_scheduler_never_picks_a_partly_sent_fax(installation, monkeypatch):  # noqa: F811
    """Without the inline rule a plain failure waits for ``routing/fallback.py``; a partly sent one never does."""
    from api.app.routing.fallback import FallbackScheduler
    from api.app.routing.store import RouteStore
    installation, plain, plain_claim = on_the_line(installation, DOCUMO, str(uuid4()))
    configuration, store, _ = installation
    installation, job, claim = on_the_line(installation, DOCUMO, str(uuid4()))
    routes = RouteStore(configuration.engine)
    for fax, attempt in ((plain, plain_claim.attempt_id), (job, claim.attempt_id)):
        routes.record_decision(attempt_id=attempt, job_id=fax, destination='+12025550150', route='documo',
                               reason='configured', provider_id='documo')
    poll(store, plain, monkeypatch, documo_answer(pagesComplete=0, pagesCount=3))
    poll(store, job, monkeypatch, documo_answer(pagesComplete=2, pagesCount=3))
    now = datetime.utcnow() + timedelta(seconds=1)
    candidates = {row.id for row in FallbackScheduler(store, routes)._candidates(now)}
    assert plain in candidates and job not in candidates


# -- HumbleFax -----------------------------------------------------------------------------------------

def humblefax_answer(status, attempts):
    from api.app import humblefax_service

    def answer(sid):
        fax = {'id': int(sid), 'status': status, 'recipients': [
            {'failureReason': 'Communication error', 'attempts': attempts}]}
        return humblefax_service._receipt(httpx.Response(200, json={'data': {'sentFax': fax}}), requested_sid=sid)
    return answer


@pytest.mark.parametrize(('status', 'attempts'), [
    ('partial success', [{'numPagesSent': 2}]),
    ('failure', [{'numPagesSent': 1}, {'numPagesSent': 2}]),
])
def test_a_humblefax_partial_success_is_partly_sent_and_never_falls_back(installation, another_route,  # noqa: F811
                                                                         monkeypatch, status, attempts):
    installation, job, claim = on_the_line(installation, HUMBLEFAX, '4712')
    configuration, store, _ = installation
    poll(store, job, monkeypatch, humblefax_answer(status, attempts))
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    installation, plain, _ = on_the_line(installation, HUMBLEFAX, '4711')
    poll(store, plain, monkeypatch, humblefax_answer('failure', [{'numPagesSent': 0,
                                                                  'failureReason': 'Receiver did not pick up'}]))
    assert store.get(plain)['state'] == 'ready' and fell_back(store, plain)  # the setup does fall back
    assert attempt_of(store, claim.attempt_id)['error_category'] == 'partly_sent'
    assert item_category(store, job) == 'partly_sent'
    found = offer(store, job, keep_document(installation, job))
    assert found.available and found.first_page == 3


# -- The built-in engine ---------------------------------------------------------------------------------

def fax_result(job, attempt, pages):
    return {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': job, 'AttemptID': attempt, 'Status': 'FAILED',
            'Pages': str(pages), 'Station64': ''}


def test_a_built_in_engine_call_that_broke_after_pages_is_partly_sent_and_never_falls_back(  # noqa: F811
        installation, another_route, monkeypatch):
    from api.app import main
    installation, job, claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    main._handle_fax_result(fax_result(job, claim.attempt_id, 2))
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    installation, plain, plain_claim = on_the_line(installation, SIP)
    main._handle_fax_result(fax_result(plain, plain_claim.attempt_id, 0))
    assert store.get(plain)['state'] == 'ready' and fell_back(store, plain)  # no page: another route may send it
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': 'partly_sent'}
    with configuration.engine.connect() as connection:
        assert connection.execute(sa.text('SELECT error FROM fax_jobs WHERE id = :id'), {'id': job}).scalar() \
            == 'The call ended after 2 pages; the rest was not confirmed.'
    assert item_category(store, job) == 'partly_sent'
    # Where the call's own record confirms the pages (error correction), only the rest is offered.
    now = datetime.utcnow()
    with configuration.engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO sip_call_records (id, direction, call_id, job_id, attempt_id, started_at, ended_at, "
            "disposition, t38, pages, fax_preference, created_at, updated_at) VALUES ('call-1', 'outbound', 'c-1', "
            ":job, :attempt, :now, :now, 'answered', 'yes', 2, 0, :now, :now)"),
            {'job': job, 'attempt': claim.attempt_id, 'now': now})
        connection.execute(sa.text(
            "INSERT INTO fax_call_frames (id, direction, job_id, attempt_id, dcs_last, trainings, t38_now, created_at) "
            "VALUES (:id, 'out', :job, :attempt, :dcs, 1, 0, :now)"),
            {'id': f'out:{claim.attempt_id}', 'job': job, 'attempt': claim.attempt_id, 'dcs': DCS_ECM, 'now': now})
    found = offer(store, job, keep_document(installation, job))
    assert found.available and found.first_page == 3 and found.confirmed.source == 'builtin'


def test_a_success_and_a_failure_with_no_page_are_left_as_they_were():
    from api.app import main
    assert main._native_partly_sent({'Status': 'SUCCESS', 'Pages': '3'}) is None
    assert main._native_partly_sent({'Status': 'FAILED', 'Pages': '0'}) is None
    assert main._native_partly_sent({'Status': 'FAILED', 'Pages': ''}) is None
    assert main._native_partly_sent({'Status': 'FAILED', 'Pages': '4'}) == 4
