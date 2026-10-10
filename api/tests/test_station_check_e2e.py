"""End to end through the real built-in engine result path (brief 93 item 4): ``main._handle_fax_result``.

A synthetic FaxResult UserEvent, as the dialplan sends it after asterisk patch 0007, goes through the real handler,
the real outbound store, the real fallback rule (another route always available here), the real station check
records and the real certainty feed with the Work item's real checks:

- ``CsiCheck=refused``: a definite failure with category ``wrong_station`` and its sentence, no fallback attempt,
  the station kept as refused, and a Work item whose script asks a person to confirm the number.
- ``T0Capped=1``: the cap ended the call. It reads as ``no_fax_answer`` (like spandsp's own T0 with the line open:
  not a person who hung up), so another route may still take it and no Work item opens; Sent details say Faxbot
  hung up at 50 seconds, with the billed step kept from the trunk's prices.
- A test fax to a public test line (``public_test_lines.py``) that is refused, or that a person answered, opens no Work
  item, beside an ordinary fax that does.

SQLite and PostgreSQL. Synthetic numbers only.
"""
import base64
from datetime import datetime
from types import SimpleNamespace

import sqlalchemy as sa

from api.app import sip_calls
from api.tests.test_outbound_store import installation  # noqa: F401 - fixture
from api.tests.test_partly_sent import SIP, another_route, attempt_of, fell_back, item_category, on_the_line  # noqa: F401
from api.tests.test_person_answered import HUNG_UP, error_of
from api.tests.test_schema import database  # noqa: F401 - fixture


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def refused_result(job, attempt):
    """The dialplan's FaxResult for a call patch 0007 refused before any page: the far end answered as a station
    that is neither the dialled number nor one it showed before."""
    return {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': job, 'AttemptID': attempt, 'Status': 'FAILED',
            'Pages': '0', 'Station64': _b64('+1 720 555 0199'), 'Answered': '1760000000', 'Mode': 'T38',
            'RtpRx': '0', 'Error': "Far end's ident is not acceptable", 'CsiCheck': 'refused', 'T0Capped': ''}


def capped_result(job, attempt):
    """The dialplan's FaxResult for a call the 50-second cap ended: sound came back, no fax machine answered."""
    return {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': job, 'AttemptID': attempt, 'Status': 'FAILED',
            'Pages': '0', 'Station64': '', 'Answered': '1760000000', 'Mode': 'audio', 'RtpRx': '2400',
            'Error': 'The call dropped prematurely', 'CsiCheck': '', 'T0Capped': '1'}


def _stations(configuration, job):
    with configuration.engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(
            'SELECT outcome, station, engine FROM station_check_results WHERE job_id = :job'), {'job': job}).mappings()]


def _script(store, job):
    """The Work item's checks as a person sees them (certainty_service._checks), from the stored item."""
    from api.app.work.certainty import CertaintyStore
    from api.app.work.certainty_service import CertaintyService
    certainty = CertaintyStore(store.configuration.engine)
    service = CertaintyService(certainty, SimpleNamespace(store=None, control=None),
                               values=lambda: SimpleNamespace(direct_organization='Example Clinic'))
    with store.configuration.engine.connect() as connection:
        item = connection.execute(sa.text('SELECT * FROM certainty_items WHERE job_id = :job'),
                                  {'job': job}).mappings().one()
        jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'), sa.column('pages'),
                        sa.column('tiff_path'), sa.column('created_at', sa.DateTime()))
        fax = connection.execute(sa.select(jobs).where(jobs.c.id == job)).mappings().one()
        row = {**dict(item), 'to_number': fax['to_number'], 'pages': fax['pages'], 'tiff_path': fax['tiff_path'],
               'submitted_at': None, 'fax_created_at': fax['created_at']}
        return service._checks(connection, row, datetime.utcnow(), number=None)


def test_a_refused_station_fails_for_certain_takes_no_other_route_and_asks_a_person_to_confirm_the_number(  # noqa: F811
        installation, another_route, monkeypatch):
    from api.app import main
    installation, job, claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    main._handle_fax_result(refused_result(job, claim.attempt_id))
    assert store.get(job)['state'] == 'failed' and not fell_back(store, job)
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': 'wrong_station'}
    assert error_of(configuration, job) == sip_calls.STATION
    assert _stations(configuration, job) == [{'outcome': 'refused', 'station': '17205550199', 'engine': 'builtin'}]
    assert item_category(store, job) == 'wrong_station'
    checks = _script(store, job)
    assert [(check['kind'], check['result']) for check in checks][:2] == [('call_record', 'not_delivered'),
                                                                          ('phone_call', 'not_done')]
    assert checks[0]['text'].startswith('The number answered as a fax machine Faxbot did not expect there')
    script = ' '.join(checks[1]['script'])
    assert 'answered as another fax machine' in script and 'What is your fax number?' in script
    assert 'This is Example Clinic.' in script


def test_a_call_the_cap_ended_reads_as_no_fax_answer_and_sent_details_say_so(  # noqa: F811
        installation, another_route, monkeypatch):
    from api.app import main
    from api.app.routing import stations
    installation, job, claim = on_the_line(installation, SIP)
    # A second call in progress at the same time, whose trunk prices cannot be read when its result arrives.
    installation, plain, plain_claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    # The trunk's prices as read when the result arrives: by the minute in 60-second steps.
    from api.tests.test_answer_cap import card
    monkeypatch.setattr(stations, 'trunk_card', lambda values, engine: card())
    event = capped_result(job, claim.attempt_id)
    assert sip_calls.verdict(event) == 'no_fax_answer'
    main._handle_fax_result(event)
    # Not a person who hung up and not a wrong number: another route may still take it, and no Work item opens.
    assert attempt_of(store, claim.attempt_id) == {'phase': 'failed', 'error_category': None}
    assert fell_back(store, job) and store.get(job)['state'] == 'ready'
    assert item_category(store, job) is None
    assert stations.fax_sentences(configuration.engine, job) == [
        'Faxbot hung up 50 seconds after the call was answered because no fax machine answered, so it is billed as '
        'one minute instead of two.']
    # The same capped call on a trunk whose prices cannot be read: kept without the billed step.
    monkeypatch.setattr(stations, 'trunk_card', lambda values, engine: None)
    main._handle_fax_result(capped_result(plain, plain_claim.attempt_id))
    assert stations.fax_sentences(configuration.engine, plain) == [
        'Faxbot hung up 50 seconds after the call was answered because no fax machine answered.']


def test_a_test_line_fax_refused_or_answered_by_a_person_opens_no_work_item(  # noqa: F811
        installation, another_route, monkeypatch):
    from api.app import main, public_test_lines as test_lines
    from api.tests.test_person_answered import fax_result
    installation, ordinary, ordinary_claim = on_the_line(installation, SIP)
    configuration, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    marked = []
    for line_id in ('faxbeep-us', 'hp-us'):
        installation, job, claim = on_the_line(installation, SIP)
        # The acceptance step a real test send runs in its acceptance transaction, run here on the accepted fax.
        with configuration.engine.begin() as connection:
            test_lines.record_step(test_lines.BY_ID[line_id], reply_number='+13035550100', actor_principal_id=None,
                                   actor_name='Test', holder=[])(connection, datetime.utcnow(), job)
        marked.append((job, claim))
    (refused_job, refused_claim), (person_job, person_claim) = marked
    main._handle_fax_result(refused_result(refused_job, refused_claim.attempt_id))
    main._handle_fax_result(fax_result(person_job, person_claim.attempt_id, HUNG_UP))
    main._handle_fax_result(refused_result(ordinary, ordinary_claim.attempt_id))
    # Both test faxes failed with their category and took no other route, but nobody is asked to settle them.
    assert attempt_of(store, refused_claim.attempt_id)['error_category'] == 'wrong_station'
    assert attempt_of(store, person_claim.attempt_id)['error_category'] == 'person_answered'
    assert not fell_back(store, refused_job) and not fell_back(store, person_job)
    assert item_category(store, refused_job) is None and item_category(store, person_job) is None
    assert item_category(store, ordinary) == 'wrong_station'
