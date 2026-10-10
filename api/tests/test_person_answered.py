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
import pytest
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


ANSWERED = datetime(2026, 10, 10, 11, 5, 33)


def epoch(moment):
    return str(int((moment - datetime(1970, 1, 1)).total_seconds()))


def timed(event, seconds):
    """The same FaxResult with the answer and end times the dialplan sends (whole seconds since the epoch)."""
    return {**event, 'Answered': epoch(ANSWERED), 'Ended': epoch(ANSWERED + timedelta(seconds=seconds))}


def test_a_call_cleared_within_two_seconds_of_the_answer_is_not_a_person():
    """Live pilot LC-P004 (2026-10-10): HP's test line answered and cleared 1 s later. Sound back and a far-end hang-up
    within 2 whole seconds (under 3 s of real time) is a service turning the call away; from 3 s it may be a person.
    Without both times the duration is unknown and the earlier rule stands."""
    job, attempt = 'a' * 32, 'b' * 32
    for seconds, expected in ((0, sip_calls.CLEARED_AT_ONCE), (1, sip_calls.CLEARED_AT_ONCE),
                              (2, sip_calls.CLEARED_AT_ONCE), (3, sip_calls.PERSON_ANSWERED),
                              (9, sip_calls.PERSON_ANSWERED)):
        assert sip_calls.verdict(timed(fax_result(job, attempt, HUNG_UP), seconds)) == expected, seconds
    assert sip_calls.verdict(fax_result(job, attempt, HUNG_UP)) == sip_calls.PERSON_ANSWERED  # duration unknown
    # No sound in one second says nothing about the network either; the other machine answering still decides.
    assert sip_calls.verdict(timed(fax_result(job, attempt, HUNG_UP, '0'), 1)) == sip_calls.CLEARED_AT_ONCE
    assert sip_calls.verdict(timed({**fax_result(job, attempt, HUNG_UP), 'Pages': '1'}, 1)) == 'remote_fax_failed'
    # A line Faxbot kept open until its timer ran out was not cleared by the far end.
    assert sip_calls.verdict(timed(fax_result(job, attempt, OPEN_LINE), 1)) == 'no_fax_answer'
    assert sip_calls.category_for(sip_calls.CLEARED_AT_ONCE) is None
    assert sip_calls.result_summary(timed(fax_result(job, attempt, HUNG_UP), 1)) == sip_calls.CLEARED
    assert len(sip_calls.CLEARED) <= 80


def test_a_call_cleared_at_once_on_the_built_in_engine_takes_the_next_route_with_no_work_item(  # noqa: F811
        installation, another_route, monkeypatch):
    from api.app import main
    # A person who stayed on the line long enough to be a voice is still a person.
    installation, person, person_claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    main._handle_fax_result(timed(fax_result(person, person_claim.attempt_id, HUNG_UP), 6))
    assert store.get(person)['state'] == 'failed' and not fell_back(store, person)
    assert attempt_of(store, person_claim.attempt_id)['error_category'] == 'person_answered'
    installation, job, claim = on_the_line(installation, SIP)
    main._handle_fax_result(timed(fax_result(job, claim.attempt_id, HUNG_UP), 1))
    assert store.get(job)['state'] == 'ready' and fell_back(store, job)
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': None}
    assert item_category(store, job) is None


def _engine_call(calls, job, attempt, *, seconds, rtp_rx='412'):
    """The SSL Fax engine's call as Asterisk reports it: the engine side with its answer and end times, and the
    trunk side with the sound that came back (LC-P004: answered 11:05:33, ended 11:05:34)."""
    calls.record_submission({'JobID': job, 'AttemptID': attempt, 'Called': '+18005550199'})
    calls.record_engine_call({'Direction': 'out', 'Side': 'engine', 'JobID': job, 'AttemptID': attempt,
                              'T38Session': '0', 'Cause': '16', 'Started': epoch(ANSWERED - timedelta(seconds=4)),
                              'Answered': epoch(ANSWERED), 'Ended': epoch(ANSWERED + timedelta(seconds=seconds))})
    calls.record_engine_call({'Direction': 'out', 'Side': 'trunk', 'JobID': job, 'AttemptID': attempt,
                              'T38': 'DISABLED', 'Cause': '16', 'RtpRx': rtp_rx})


def real_engine_result(monkeypatch, configuration, store, payload):
    """The SSL Fax engine's real result handler with the real store and the real call records: the verdict comes
    from ``SipCallRecords`` itself, never a stand-in."""
    from api.app import audit, hylafax_http
    from api.app.routing import background
    monkeypatch.setattr(hylafax_http, '_require_engine', lambda secret: None)
    monkeypatch.setattr(hylafax_http, '_store', lambda request: store)
    monkeypatch.setattr(background, 'installation_engine', lambda app: (configuration.engine, None))
    monkeypatch.setattr(audit, 'audit_event', lambda *args, **kwargs: None)
    return asyncio.run(hylafax_http.engine_result(SimpleNamespace(app=None), payload, x_internal_secret='x'))


def test_lc_p004_on_the_ssl_fax_engine_is_a_definite_failure_before_any_data(  # noqa: F811
        installation, another_route, monkeypatch):
    """HP's toll-free test line answered 4 s after dialling and cleared 1 s later, with sound back and HylaFAX's
    E002 "No carrier detected" 5 s after dialling. It was written as a person answering (no fallback, a Work item);
    it is a definite failure before any fax data that another route, or a later try, may take."""
    # The same call kept open 8 s before the far end hung up is still a person answering.
    installation, person, person_claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    calls = sip_calls.SipCallRecords(configuration.engine)
    _engine_call(calls, person, person_claim.attempt_id, seconds=8)
    real_engine_result(monkeypatch, configuration, store, engine_payload(person, person_claim.attempt_id, 'E002'))
    assert calls.for_attempt(person_claim.attempt_id)[-1]['verdict'] == sip_calls.PERSON_ANSWERED
    assert store.get(person)['state'] == 'failed' and not fell_back(store, person)
    assert attempt_of(store, person_claim.attempt_id)['error_category'] == 'person_answered'
    assert error_of(configuration, person) == sip_calls.PERSON
    assert item_category(store, person) == 'person_answered'
    installation, job, claim = on_the_line(installation, SIP)
    _engine_call(calls, job, claim.attempt_id, seconds=1)
    real_engine_result(monkeypatch, configuration, store, engine_payload(job, claim.attempt_id, 'E002'))
    [row] = calls.for_attempt(claim.attempt_id)
    assert (row['connected_seconds'], row['verdict']) == (1, sip_calls.CLEARED_AT_ONCE)
    assert store.get(job)['state'] == 'ready' and fell_back(store, job)
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': None}
    assert item_category(store, job) is None


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
    # A shared call the far end cleared at once (LC-P004): every fax in it fails with no category, so each may take
    # another route; the category comes from the call's verdict as the built-in engine's handler reads it.
    event = timed(fax_result('a' * 32, 'b' * 32, HUNG_UP), 1)
    cleared = map_call(members, succeeded=False, confirmed_pages=0, failure_sentence=sip_calls.result_summary(event),
                       failure_category=sip_calls.category_for(sip_calls.verdict(event)))
    assert [(item.status, item.category, item.sentence) for item in cleared] == [
        ('failed', None, sip_calls.CLEARED)] * 2


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


def freeswitch_installation(installation, monkeypatch):  # noqa: F811
    from api.app import main
    from api.app.config_profiles import ProviderConfiguration
    configuration, store, snapshot = installation
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({'asterisk_inbound_secret': 'sekret'}),
                                   actor='test', restart_required=False,
                                   providers={'outbound': ProviderConfiguration('freeswitch')})
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    return (configuration, store, snapshot)


def freeswitch_result(job, attempt, text, audio_in):
    """mod_spandsp's channel variables after txfax, plus FreeSWITCH's own count of audio packets back."""
    from api.app import main
    return main.freeswitch_outbound_result(main.FSOutboundResultIn(
        job_id=job, attempt_id=attempt, fax_status='0', fax_result_code='49', fax_result_text=text,
        fax_document_transferred_pages=0, fax_document_total_pages=3, uuid='synthetic-channel',
        rtp_audio_in_packet_count=audio_in), x_internal_secret='sekret')


def test_a_person_on_freeswitch_is_never_called_again_when_the_hook_says_sound_came_back(  # noqa: F811
        installation, another_route, monkeypatch):
    """A call dropped by the far end before any fax message, with sound back, is a person."""
    from api.app.config_profiles import ProviderConfiguration
    installation = freeswitch_installation(installation, monkeypatch)
    configuration, store, _ = installation
    installation, job, claim = on_the_line(installation, ProviderConfiguration('freeswitch'), str(uuid4()))
    assert freeswitch_result(job, claim.attempt_id, HUNG_UP, 512)['applied'] is True
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': 'person_answered'}
    assert error_of(configuration, job) == sip_calls.PERSON
    assert item_category(store, job) == 'person_answered'


@pytest.mark.parametrize('text, audio_in', [(OPEN_LINE, 512), (HUNG_UP, 0), (HUNG_UP, None)])
def test_a_silent_or_timed_out_freeswitch_call_still_takes_the_next_route(  # noqa: F811
        installation, another_route, monkeypatch, text, audio_in):
    """A timeout, or a drop with no sound (or no count from the hook), may be the route."""
    from api.app.config_profiles import ProviderConfiguration
    installation = freeswitch_installation(installation, monkeypatch)
    _, store, _ = installation
    installation, plain, claim = on_the_line(installation, ProviderConfiguration('freeswitch'), str(uuid4()))
    freeswitch_result(plain, claim.attempt_id, text, audio_in)
    assert store.get(plain)['state'] == 'ready' and fell_back(store, plain)
    assert attempt_of(store, claim.attempt_id)['error_category'] is None
