"""A person who answers is never dialed again on another route (research N9, 2026-10-08).

A call on which sound came back, no fax message ever did and the far end hung up first was answered by a person or
a voice line (``sip_calls.PERSON_ANSWERED``). On every path that can report it (the built-in engine's FaxResult,
the SSL Fax engine's result with its trunk record, a shared call, and HumbleFax's reason naming a voice answer) the
attempt fails with the category ``person_answered``: the fax takes no other route in the observation or in
``routing/fallback.py``, ends failed with one plain sentence, and gets a Work item for a person to check the
number. Each case runs beside a silent attempt (sound back, the line left open until the timer ran out) that still
falls back, so the setup is proven to allow it. Real store, real fallback query, real result handlers; SQLite and
PostgreSQL. Synthetic numbers only.
"""
import asyncio
import base64
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import sqlalchemy as sa

from api.app import sip_calls
from api.tests.test_outbound_store import another_number, installation  # noqa: F401 - fixture
from api.tests.test_partly_sent import (HUMBLEFAX, SIP, another_route, attempt_of, fell_back,  # noqa: F401
                                        item_category, on_the_line, poll)
from api.tests.test_schema import database  # noqa: F401 - fixture


HUNG_UP, OPEN_LINE = 'The call dropped prematurely', 'Timed out waiting for initial communication'


def fax_result(job, attempt, error, rtp_rx='1500'):
    """The dialplan's FaxResult for an answered audio call that sent no page."""
    return {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': job, 'AttemptID': attempt, 'Status': 'FAILED',
            'Pages': '0', 'Station64': '', 'Answered': '1', 'Mode': 'audio', 'RtpRx': rtp_rx, 'Error': error}


def error_of(configuration, job):
    with configuration.engine.connect() as connection:
        return connection.execute(sa.text('SELECT error FROM fax_jobs WHERE id = :id'), {'id': job}).scalar()


def test_the_verdict_splits_a_person_from_a_silent_line():
    assert sip_calls.verdict(fax_result('a' * 32, 'b' * 32, HUNG_UP)) == sip_calls.PERSON_ANSWERED
    assert sip_calls.verdict(fax_result('a' * 32, 'b' * 32, 'HANGUP', '40')) == sip_calls.PERSON_ANSWERED
    assert sip_calls.verdict(fax_result('a' * 32, 'b' * 32, OPEN_LINE)) == 'no_fax_answer'
    assert sip_calls.verdict(fax_result('a' * 32, 'b' * 32, HUNG_UP, '0')) == 'no_media_back'
    assert sip_calls.result_summary(fax_result('a' * 32, 'b' * 32, HUNG_UP)) == (
        'A person answered, not a fax machine; Faxbot did not call again.')
    assert sip_calls.category_for(sip_calls.PERSON_ANSWERED) == 'person_answered'
    assert sip_calls.category_for('no_fax_answer') is None


def test_a_person_on_the_built_in_engine_is_never_called_again_and_gets_a_work_item(  # noqa: F811
        installation, another_route, monkeypatch):
    from api.app import main
    installation, job, claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    main._handle_fax_result(fax_result(job, claim.attempt_id, HUNG_UP))
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': 'person_answered'}
    assert error_of(configuration, job) == sip_calls.PERSON
    assert item_category(store, job) == 'person_answered'
    # A silent line (sound back, kept open until the timer ran out) may be the route: another route may send it.
    installation, plain, plain_claim = on_the_line(installation, SIP)
    main._handle_fax_result(fax_result(plain, plain_claim.attempt_id, OPEN_LINE))
    assert store.get(plain)['state'] == 'ready' and fell_back(store, plain)
    assert item_category(store, plain) is None


def test_the_fallback_scheduler_never_picks_a_fax_a_person_answered(installation, monkeypatch):  # noqa: F811
    """Without the inline rule a plain failure waits for ``routing/fallback.py``; a person-answered one never does."""
    from api.app import main
    from api.app.routing.fallback import FallbackScheduler
    from api.app.routing.store import RouteStore
    installation, plain, plain_claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    installation, job, claim = on_the_line(installation, SIP)
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    routes = RouteStore(configuration.engine)
    for fax, attempt in ((plain, plain_claim.attempt_id), (job, claim.attempt_id)):
        routes.record_decision(attempt_id=attempt, job_id=fax, destination='+12025550150', route='sip',
                               reason='configured', provider_id='sip')
    main._handle_fax_result(fax_result(plain, plain_claim.attempt_id, OPEN_LINE))
    main._handle_fax_result(fax_result(job, claim.attempt_id, HUNG_UP))
    now = datetime.utcnow() + timedelta(seconds=1)
    candidates = {row.id for row in FallbackScheduler(store, routes)._candidates(now)}
    assert plain in candidates and job not in candidates


def engine_payload(job, attempt, code):
    words = {'E002': 'No carrier detected {E002}', 'E126': 'No receiver protocol (T.30 T1 timeout) {E126}'}[code]
    return {'tag': f'{job}.{attempt}', 'why': 'failed', 'dials': 1, 'pages': 0, 'status_code': code,
            'status_b64': base64.b64encode(words.encode()).decode()}


def engine_result(monkeypatch, store, payload, verdict):
    """The SSL Fax engine's real result handler with the real store; the trunk record says what it heard."""
    from api.app import audit, hylafax_http
    row = {'verdict': verdict, 'ended_at': '2026-10-08T12:00:40Z', 't38': 'no', 'error_cause': None}
    monkeypatch.setattr(hylafax_http, '_require_engine', lambda secret: None)
    monkeypatch.setattr(hylafax_http, '_store', lambda request: store)
    monkeypatch.setattr(hylafax_http, '_record', lambda *args: row)
    monkeypatch.setattr(audit, 'audit_event', lambda *args, **kwargs: None)
    return asyncio.run(hylafax_http.engine_result(SimpleNamespace(app=None), payload, x_internal_secret='x'))


def test_a_person_on_the_ssl_fax_engine_is_never_called_again(installation, another_route,  # noqa: F811
                                                              monkeypatch):
    installation, job, claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    engine_result(monkeypatch, store, engine_payload(job, claim.attempt_id, 'E002'), sip_calls.PERSON_ANSWERED)
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': 'person_answered'}
    assert error_of(configuration, job) == sip_calls.PERSON
    assert item_category(store, job) == 'person_answered'
    installation, plain, plain_claim = on_the_line(installation, SIP)
    engine_result(monkeypatch, store, engine_payload(plain, plain_claim.attempt_id, 'E126'), 'no_fax_answer')
    assert store.get(plain)['state'] == 'ready' and fell_back(store, plain)


def humblefax_failure(reason):
    from api.app import humblefax_service

    def answer(sid):
        fax = {'id': int(sid), 'status': 'failure', 'recipients': [
            {'failureReason': reason, 'attempts': [{'numPagesSent': 0, 'failureReason': reason}]}]}
        return humblefax_service._receipt(httpx.Response(200, json={'data': {'sentFax': fax}}), requested_sid=sid)
    return answer


def test_humblefax_naming_a_voice_answer_is_never_sent_again(installation, another_route,  # noqa: F811
                                                             monkeypatch):
    from api.app.humblefax_service import PERSON_ANSWERED
    installation, job, claim = on_the_line(installation, HUMBLEFAX, '4721')
    configuration, store, _ = installation
    poll(store, job, monkeypatch, humblefax_failure('Voice answered'))
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    assert attempt_of(store, claim.attempt_id)['error_category'] == 'person_answered'
    assert error_of(configuration, job) == PERSON_ANSWERED
    assert item_category(store, job) == 'person_answered'
    # HumbleFax's documented example does not say who answered: a plain failure another route may send.
    installation, plain, _ = on_the_line(installation, HUMBLEFAX, '4722')
    poll(store, plain, monkeypatch, humblefax_failure('No fax machine detected at destination'))
    assert store.get(plain)['state'] == 'ready' and fell_back(store, plain)


def test_a_shared_call_a_person_answered_fails_every_fax_in_it_with_the_category():
    from api.app.batching.outcomes import map_call
    members = [{'id': 'j1', 'attempt_id': 'a1', 'first_page': 1, 'last_page': 2, 'layout': 'separators'},
               {'id': 'j2', 'attempt_id': 'a2', 'first_page': 3, 'last_page': 5, 'layout': 'separators'}]
    outcomes = map_call(members, succeeded=False, confirmed_pages=0, failure_sentence=sip_calls.PERSON,
                        failure_category='person_answered')
    assert [(item.status, item.category, item.sentence) for item in outcomes] == [
        ('failed', 'person_answered', sip_calls.PERSON)] * 2
    plain = map_call(members, succeeded=False, confirmed_pages=0, failure_sentence='Busy.')
    assert [item.category for item in plain] == [None, None]


def test_the_work_item_checks_the_number_and_offers_the_npi_registry_for_a_provider(installation):  # noqa: F811
    from api.app.routing.nppes import NppesStore
    from api.app.work import certainty_checks as checks
    configuration, _, _ = installation
    item = {'job_id': uuid4().hex, 'to_number': another_number(), 'reference': 'K7Q4MX'}
    assert checks.npi_checks(configuration.engine, item) == []  # nothing says the recipient is a provider
    NppesStore(configuration.engine).add('1234567893', 'Synthetic Clinic', by_name='Test')
    (found,) = checks.npi_checks(configuration.engine, item)
    assert found['kind'] == 'npi_lookup' and found['source_url'].startswith('https://npiregistry.cms.hhs.gov/')
    call = checks.number_check(item, organization='County Clinic', number='+1 202-555-0150', when_text='Oct 8')
    assert call['script'][0] == 'Call the recipient. The number Faxbot faxed, +1 202-555-0150, may be a voice line.'
    assert checks.suggestion([checks.person_answered_check(), call, found]) == 'not_delivered'
