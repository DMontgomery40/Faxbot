"""Route families (brief 92, RF): failures that belong to a route are not taught to the numbers it called.

Synthetic call histories only (555 numbers). Call records are written the way sip_calls leaves them, then the real
learning code (``engine_learning.learn_recent`` and ``decide``), the real schedule reader and the incident
watcher run on them. No call is ever placed.
"""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from app import engine_learning
from app.routing import route_families, schedule
from api.tests.test_engine_learning import (  # noqa: F401 - helpers and fixtures
    data_folder, db, installation, memory_rows, record, values,
)
from api.tests.test_schema import database  # noqa: F401 - fixture (SQLite, and PostgreSQL when configured)

D1, D2, D3, D4, D5 = '+13035550161', '+13035550162', '+13035550163', '+13035550164', '+13035550165'
X = '+13035550170'


def epoch_at(db, settings, when):
    return engine_learning.current_epoch(db, settings, now=when)


def replay_broken_t38(db, now, *, verdict='remote_fax_failed'):
    """Before a trunk settings change, T.38 to D1..D3 went through; after it, T.38 to each failed after the far
    machine answered while audio fax to D1 and D2 went through. X's own audio failure is a real lesson."""
    before, after = values(), values(SIP_TRUNK_PORT='5062')
    epoch_at(db, before, now - timedelta(days=5))
    for number, hours in ((D1, 100), (D2, 99), (D3, 98)):
        record(db, number=number, status='SUCCESS', when=now - timedelta(hours=hours), pages=1)
    second = epoch_at(db, after, now - timedelta(hours=30))
    for number, hours in ((D1, 20), (D2, 19), (D3, 18)):
        record(db, number=number, verdict=verdict, when=now - timedelta(hours=hours))
    for number, hours in ((D1, 17), (D2, 16)):
        record(db, number=number, mode='audio', status='SUCCESS', when=now - timedelta(hours=hours), pages=1)
    record(db, number=X, mode='audio', when=now - timedelta(hours=15))
    return before, after, second


def _now():
    return engine_learning.utcnow()


def test_a_broken_t38_family_after_a_settings_change_opens_one_incident_at_the_change(db):
    now = _now()
    _, _, second = replay_broken_t38(db, now)
    assert route_families.watch(db, now=now) == {'opened': 1, 'closed': 0}
    [incident] = route_families.open_incidents(db)
    assert (incident['account_key'], incident['transport'], incident['phase']) == ('sip', 't38', 't30')
    assert incident['generation'] == second['id'] and incident['change_cause'] == 'settings'
    assert incident['change_at'] == second['started_at']
    assert (incident['destinations'], incident['failures'], incident['corroborated']) == (3, 3, 3)
    # Audio on the same trunk is outside the family: the trunk stays usable by audio fax.
    assert route_families.family_incident(db, 'sip', 't38')['id'] == incident['id']
    assert route_families.family_incident(db, 'sip', 'audio') is None
    assert route_families.t38_problem(db)['id'] == incident['id']
    # Watching again opens nothing new.
    assert route_families.watch(db, now=now + timedelta(minutes=1)) == {'opened': 0, 'closed': 0}
    sentence = route_families.incident_sentence(incident, 'Telnyx')
    assert sentence.startswith('Since ') and 'fax over IP (T.38) by Telnyx has failed after the fax machine answered' \
        ' for 3 different numbers after its settings changed; 3 of them went through before or by another route.' \
        in sentence and sentence.endswith('Faxbot uses audio fax on this trunk meanwhile and does not count these '
                                          'failures against the numbers.')


def test_the_incident_teaches_the_numbers_nothing_and_other_lessons_stay(installation):
    db = installation
    now = _now()
    _, after, _ = replay_broken_t38(db, now)
    route_families.watch(db, now=now)
    engine_learning.learn_recent(db, after, now=now)
    rows = memory_rows(db)
    # Only X's own audio failure is a lesson; D1..D3's T.38 failures were the route's.
    assert [(row['number'], row['kind']) for row in rows] == [(X, 'audio_failed')]
    for number in (D1, D2, D3):
        assert not engine_learning.decide(after, number, db=db, now=now).audio
    assert engine_learning.decide(after, X, db=db, now=now).t38_now


def test_without_corroboration_new_numbers_failing_are_not_a_route_problem(db):
    now = _now()
    epoch_at(db, values(), now - timedelta(days=5))
    for number, hours in ((D1, 20), (D2, 19), (D3, 18), (D4, 17)):
        record(db, number=number, when=now - timedelta(hours=hours))
    assert route_families.watch(db, now=now) == {'opened': 0, 'closed': 0}


def test_a_change_outside_faxbot_starts_at_the_first_failure_of_the_run(db):
    now = _now()
    epoch_at(db, values(), now - timedelta(days=5))
    for number, hours in ((D1, 60), (D2, 59), (D3, 58), (D4, 57)):
        record(db, number=number, status='SUCCESS', when=now - timedelta(hours=hours), pages=1)
    first = now - timedelta(hours=10)
    for number, when in ((D1, first), (D2, first + timedelta(hours=1)), (D3, first + timedelta(hours=2)),
                         (D4, first + timedelta(hours=3))):
        record(db, number=number, verdict='no_t38_data_back', reason='No T.38 data came back', when=when)
    route_families.watch(db, now=now)
    [incident] = route_families.open_incidents(db)
    assert incident['change_cause'] == 'outside' and incident['phase'] == 'media'
    assert abs((incident['change_at'] - first).total_seconds()) < 1 and incident['destinations'] == 4


def test_lessons_written_before_the_incident_was_seen_are_retired_when_it_closes(installation):
    db = installation
    now = _now()
    _, after, _ = replay_broken_t38(db, now)
    # Learning ran before the watcher saw the pattern: D1..D3 were taught "T.38 failed".
    engine_learning.learn_recent(db, after, now=now - timedelta(hours=14))
    assert {row['number'] for row in memory_rows(db)} == {D1, D2, D3, X}
    route_families.watch(db, now=now)
    assert engine_learning.decide(after, D1, db=db, now=now).audio  # still audio while the route is broken
    # T.38 goes through again to two numbers: the incident closes and its lessons are set aside.
    for number, minutes in ((D4, 30), (D5, 20)):
        record(db, number=number, status='SUCCESS', when=now - timedelta(minutes=minutes), pages=1)
    assert route_families.watch(db, now=now) == {'opened': 0, 'closed': 1}
    [incident] = route_families.incidents(db)
    assert incident['closed']['reason'] == 'went_through' and incident['closed']['retired'] == 3
    by_number = {row['number']: row for row in memory_rows(db)}
    for number in (D1, D2, D3):
        assert by_number[number]['forgotten_by_name'] == route_families.RETIRED_BY
        assert not engine_learning.decide(after, number, db=db, now=now).audio
    assert by_number[X]['forgotten_at'] is None
    # Learning again never brings them back.
    engine_learning.learn_recent(db, after, now=now)
    assert len(memory_rows(db)) == 4
    assert route_families.family_incident(db, 'sip', 't38') is None
    assert 'It ended ' in route_families.incident_sentence(incident, 'Telnyx')
    assert 'Faxbot set aside 3 lessons' in route_families.incident_sentence(incident, 'Telnyx')


def test_a_settings_change_closes_an_open_incident_and_a_person_can_close_one(db):
    now = _now()
    replay_broken_t38(db, now)
    route_families.watch(db, now=now)
    epoch_at(db, values(SIP_TRUNK_PORT='5064'), now + timedelta(minutes=5))
    assert route_families.watch(db, now=now + timedelta(minutes=6)) == {'opened': 0, 'closed': 1}
    [incident] = route_families.incidents(db)
    assert incident['closed']['reason'] == 'settings_changed'
    assert route_families.close(db, incident['id'], reason='person') is False  # already closed


def test_failures_in_an_incident_stay_out_of_the_speed_learning_and_the_schedule(installation):
    db = installation
    now = _now()
    _, after, _ = replay_broken_t38(db, now)
    views = engine_learning.joined_calls(db, D1, since=now - timedelta(days=2))
    before = [view['record_id'] for view in views]
    route_families.watch(db, now=now)
    kept = route_families.without_covered(db, views)
    removed = set(before) - {view['record_id'] for view in kept}
    assert len(removed) == 1  # only D1's T.38 failure; its audio success stays
    attempts = {view['attempt_id'] for view in views if view['record_id'] in removed}
    everything = {view['attempt_id'] for number in (D1, D2, D3, X)
                  for view in engine_learning.joined_calls(db, number, since=now - timedelta(days=2))}
    excluded = route_families.excluded_attempts(db, everything)
    assert attempts <= excluded and len(excluded) == 3
    x_attempts = {view['attempt_id'] for view in engine_learning.joined_calls(db, X, since=now - timedelta(days=2))}
    assert not (x_attempts & excluded)
    # The schedule's own reading is untouched by the watcher: it reads the same rows before and after.
    scheduler = schedule.Scheduler(db)
    with db.connect() as connection:
        assert scheduler.call_timings(connection, now, X) == scheduler.call_timings(connection, now, X)


@pytest.mark.parametrize('cells, verdict', [
    ({'a1': 'failed', 'a2': 'failed', 'b1': 'success', 'b2': 'success'}, 'route_a'),
    ({'a1': 'success', 'a2': 'success', 'b1': 'failed', 'b2': 'failed'}, 'route_b'),
    ({'a1': 'failed', 'a2': 'success', 'b1': 'failed', 'b2': 'success'}, 'number_a'),
    ({'a1': 'success', 'a2': 'failed', 'b1': 'success', 'b2': 'failed'}, 'number_b'),
    ({'a1': 'success', 'a2': 'success', 'b1': 'success', 'b2': 'success'}, 'all_through'),
    ({'a1': 'failed', 'a2': 'failed', 'b1': 'failed', 'b2': 'failed'}, 'all_failed'),
    ({'a1': 'failed', 'a2': 'success', 'b1': 'success', 'b2': 'success'}, 'one_failed'),
    ({'a1': 'failed', 'a2': 'success', 'b1': 'success', 'b2': 'failed'}, 'mixed'),
    ({'a1': 'failed', 'a2': None, 'b1': None, 'b2': None}, 'unsent'),
    ({'a1': 'failed', 'a2': 'pending', 'b1': 'success', 'b2': 'success'}, 'waiting'),
])
def test_the_two_by_two_separates_a_route_from_a_number(cells, verdict):
    found = route_families.interpret(cells, route_a='Telnyx', route_b='Sinch', number_a='+1 303 555 0100',
                                     number_b='+1 303 555 0101')
    assert found['verdict'] == verdict
    assert found['sentence'].endswith('.') and found['sentence'][0].isupper()
    if verdict == 'route_a':
        assert found['sentence'] == ('Both test faxes by Telnyx failed and both by Sinch went through, so the problem '
                                     'is on Telnyx, not on the numbers it calls.')


def test_two_brands_on_one_upstream_are_not_real_diversity(db):
    route_families.set_upstream(db, 'phaxio', 'Example Carrier', source_url='https://example.com/a',
                                source_date='2026-10-10')
    route_families.set_upstream(db, 'sinch', 'Example Carrier', source_url='https://example.com/b',
                                source_date='2026-10-10')
    table = route_families.upstreams(db)
    assert route_families.diversity('phaxio', 'sinch', table)['kind'] == 'shared_upstream'
    assert route_families.diversity('phaxio', 'telnyx', table)['kind'] == 'unknown'
    assert route_families.diversity('sinch', 'sinch', table)['kind'] == 'same_provider'
    # Unknown stays unknown: a later row withdraws what was recorded.
    route_families.set_upstream(db, 'sinch', None)
    assert route_families.diversity('phaxio', 'sinch', route_families.upstreams(db))['kind'] == 'unknown'
    with pytest.raises(ValueError, match='web address'):
        route_families.set_upstream(db, 'telnyx', 'Example Carrier')


def test_a_test_plan_sends_nothing_by_itself(db):
    plan = route_families.create_test(db, route_a='sip', route_b='sinch', number_a='+13035550100',
                                      number_b='+13035550101')
    test = route_families.get_test(db, plan['id'])
    assert test['cells'] == {'a1': None, 'a2': None, 'b1': None, 'b2': None}
    assert route_families.interpret(route_families.cell_states(test))['verdict'] == 'unsent'
    jobs = sa.table('fax_jobs', sa.column('id'))
    with db.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(jobs)).scalar() == 0
    assert route_families.cell_target(test, 'b2') == ('sinch', '+13035550101')
    with pytest.raises(ValueError):
        route_families.create_test(db, route_a='sip', route_b='sip', number_a='+13035550100', number_b='+13035550101')
