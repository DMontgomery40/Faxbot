"""Whether a line can go, and moving a number as a checked plan (CE5, international 4), from synthetic records.

Real history rows (received and sent faxes), the real number placement and the real migration 0065 tables; nothing
is stood in for them. Advice only: nothing here ports, cancels or changes a provider account.
"""
from datetime import datetime, timedelta
import json
import math
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.config_profiles import ConfigurationDocument
from api.app.config_values import ConfigurationValues
from api.app.routing import number_moves, number_placement
from api.app.routing.seed import load_cards
from api.app.routing.store import RouteStore, RoutingConflict
from api.app.schema import upgrade_schema, validate_schema
from api.app import schema_fact_advice
from api.tests.test_schema import database  # noqa: F401 (fixture)


NOW = datetime(2026, 10, 8, 12, 0)
BUSY, QUIET, MARCH = '+13035550100', '+13035550101', '+13035550102'


def values(**extra):
    environment = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
                   'SIP_TRUNK_HOST': 'sip.telnyx.com', 'SIP_TRUNK_DIDS': f'{BUSY},{QUIET},{MARCH}',
                   'FAX_DEFAULT_COUNTRY': 'US', 'INBOUND_ENABLED': 'true', **extra}
    return ConfigurationValues.from_environment(environment)


def seeded(engine):
    upgrade_schema(engine)
    routes = RouteStore(engine, sip_preset=lambda: 'telnyx')
    routes.seed_cards(load_cards())
    return routes


def received(engine, number, when, *, sender='+18015550100', backend='sip', pages=1):
    table = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=uuid4().hex, from_number=sender, to_number=number, status='received', backend=backend,
            inbound_backend=backend, pages=pages, created_at=when, received_at=when, updated_at=when))


def sent(engine, number, when, *, route='sip', provider='sip', pages=1):
    with engine.begin() as connection:
        t = {name: sa.Table(name, sa.MetaData(), autoload_with=connection)
             for name in ('fax_jobs', 'outbound_attempts', 'delivery_attempt_costs')}
        job, attempt = uuid4().hex, uuid4().hex
        connection.execute(t['fax_jobs'].insert().values(
            id=job, to_number=number, file_name='test.pdf', tiff_path='', status='SUCCESS', pages=pages,
            backend=provider, created_at=when, updated_at=when))
        connection.execute(t['outbound_attempts'].insert().values(
            id=attempt, job_id=job, sequence=1, phase='success', created_at=when, submitted_at=when,
            completed_at=when))
        connection.execute(t['delivery_attempt_costs'].insert().values(
            id=attempt, job_id=job, destination=number, route=route, route_reason='rule', provider_id=provider,
            billing_checks=0, outcome='success', created_at=when, updated_at=when))


def answer_all(engine, number, answer='no'):
    store = number_moves.DependencyStore(engine)
    for question in number_moves.QUESTIONS:
        store.answer(number, question, answer, by_name='Ada', now=NOW - timedelta(days=1))


def rows_of(result):
    return {row['number']: row for row in result['numbers']}


def test_migration_0065_tables_remain_in_the_current_schema(database):  # noqa: F811
    upgrade_schema(database)
    assert schema_fact_advice.REVISION == '0065_fact_advice'
    with database.connect() as connection:
        validate_schema(connection, require_version=True)
        names = set(sa.inspect(connection).get_table_names())
    assert {'number_dependencies', 'number_moves', 'number_move_events'} <= names


def test_the_quiet_number_bound_is_about_three_over_t_with_its_limit_stated():
    bound = number_placement.quiet_bound(400)
    assert bound['per_day'] == pytest.approx(-math.log(0.05) / 400, abs=1e-6) and bound['per_day'] == pytest.approx(3 / 400,
                                                                                                         rel=0.01)
    assert bound['sentence'].startswith('No fax arrived in the 400 days Faxbot has watched it.')
    assert 'about one every 134 days' in bound['sentence']
    assert bound['limit'].startswith('This says nothing about yearly, seasonal or emergency use')
    assert number_placement.quiet_bound(0) is None


def test_keep_can_likely_go_and_the_march_only_number_that_must_be_investigated(database):  # noqa: F811
    routes = seeded(database)
    # Faxbot's record of received faxes starts 400 days ago.
    received(database, BUSY, NOW - timedelta(days=400))
    for day in (2, 5, 9):
        received(database, BUSY, NOW - timedelta(days=day), sender=f'+1801555010{day}')
    # Arrivals only in March: quiet for 90 days, but not for a year.
    for day in (4, 11, 18):
        received(database, MARCH, datetime(2026, 3, day, 10, 0))
    answer_all(database, QUIET)
    answer_all(database, MARCH)
    result = number_placement.line_advice(database, values(), routes=routes, now=NOW)
    rows = rows_of(result)
    busy = rows[BUSY]
    assert busy['verdict'] == 'keep' and busy['evidence']['senders'] == 3
    assert busy['sentence'] == 'Keep +1 303-555-0100: 3 faxes from 3 senders arrived in the last 90 days.'
    assert busy['evidence']['quiet_bound'] is None
    quiet = rows[QUIET]
    assert quiet['verdict'] == 'can_likely_go', quiet['reasons']
    bound = quiet['evidence']['quiet_bound']
    assert bound['days'] == 400 and bound['limit'].startswith('This says nothing about yearly, seasonal')
    # Telnyx publishes $1.00 a month for a number: what giving it up removes.
    assert quiet['evidence']['removed'] == [{'currency': 'USD', 'amount': '1.00'}]
    assert quiet['sentence'].endswith('and giving it up removes about $1.00 a month.')
    march = rows[MARCH]
    assert march['verdict'] == 'investigate'
    assert any('It received faxes in March 2026 and none since: that may be seasonal' in reason
               for reason in march['reasons'])
    months = {item['label']: item['arrivals'] for item in march['evidence']['months']}
    assert months['March 2026'] == 3 and sum(months.values()) == 3
    assert march['evidence']['quiet_bound']['days'] == (NOW - datetime(2026, 3, 18, 10, 0)).days


def test_unanswered_questions_npi_and_short_records_keep_a_quiet_number_from_going(database):  # noqa: F811
    routes = seeded(database)
    received(database, BUSY, NOW - timedelta(days=100))
    result = number_placement.line_advice(database, values(), routes=routes, now=NOW,
                                          published={QUIET: {'kind': 'fax', 'where': 'location', 'npi': '1234567893',
                                                             'read_at': NOW}})
    quiet = rows_of(result)[QUIET]
    assert quiet['verdict'] == 'investigate'
    reasons = ' '.join(quiet['reasons'])
    assert 'covers only 100 days, less than a year' in reasons
    assert 'Not answered yet: Does this line also carry broadband?' in reasons
    assert [row['answer'] for row in quiet['dependencies']] == ['unknown'] * 4
    # Yes to emergency use is a reason too, kept with the note.
    number_moves.DependencyStore(database).answer(QUIET, 'emergency', 'yes', note='Fire panel calls it')
    again = rows_of(number_placement.line_advice(database, values(), routes=routes, now=NOW))[QUIET]
    assert 'You answered yes to: Is this number kept for emergency use? (Fire panel calls it)' in again['reasons']


def test_a_quiet_number_cheaper_elsewhere_moves_its_termination_and_keeps_the_number(database):  # noqa: F811
    routes = seeded(database)
    anveo = {'provider': 'sip', 'label': 'AnveoDirect trunk', 'receives': True, 'numbers': ['+17205550150'],
             'settings': {'preset': 'anveo', 'auth': 'ip', 'host': '192.0.2.50'}}
    two = values().with_provider_accounts(ConfigurationDocument({'sip-anveo': anveo}))
    received(database, BUSY, NOW - timedelta(days=30))
    result = number_placement.line_advice(database, two, routes=routes, now=NOW)
    quiet = rows_of(result)[QUIET]
    assert quiet['verdict'] == 'move_termination' and quiet['verdict_label'] == 'Move the termination'
    assert quiet['sentence'].startswith('Keep +1 303-555-0101 but move it to AnveoDirect trunk')


def test_uk_numbers_carry_the_researched_carrier_facts_with_sources():
    facts = number_placement.carrier_facts('+442079460000')
    ids = {fact['id'] for fact in facts}
    assert ids == {'bt-digital-voice-broadband', 'bt-digital-voice-other-services', 'openreach-pstn-closure'}
    for fact in facts:
        assert fact['source'].startswith('https://') and fact['read_on'] == '2026-10-08'
    assert any('31 January 2027' in fact['sentence'] for fact in facts)
    assert [fact['id'] for fact in number_placement.carrier_facts('+442079460000', broadband=True)] == [
        'bt-digital-voice-broadband']
    assert number_placement.carrier_facts(QUIET) == []


def move_values():
    return values(FAX_INBOUND_BACKEND='humblefax', HUMBLEFAX_ACCESS_KEY='synthetic', HUMBLEFAX_SECRET_KEY='synthetic',
                  HUMBLEFAX_FROM_NUMBER='+17205550199', FAX_OUTBOUND_ROUTES='phaxio', PHAXIO_API_KEY='synthetic',
                  PHAXIO_API_SECRET='synthetic')


def test_a_moves_states_survive_a_restart(database):  # noqa: F811
    seeded(database)
    store = number_moves.MoveStore(database)
    store.start(QUIET, 'sip', 'humblefax', by_name='Ada', now=NOW - timedelta(days=3))
    with pytest.raises(RoutingConflict):
        store.start(QUIET, 'sip', 'humblefax')
    store.record(QUIET, 'new_account_ready', 'done', note='HumbleFax account opened', by_name='Ada',
                 now=NOW - timedelta(days=2))
    store.record(QUIET, 'port_ordered', 'done', by_name='Ada', now=NOW - timedelta(days=2))
    number_moves.DependencyStore(database).answer(QUIET, 'broadband', 'no', by_name='Ada')
    database.dispose()  # a restart: nothing kept in memory survives
    view = number_moves.move_view(database, move_values(), QUIET, now=NOW)
    steps = {step['step']: step for step in view['steps']}
    assert view['state'] == 'open' and view['from_account'] == 'Telnyx' and view['to_account'] == 'HumbleFax'
    assert steps['new_account_ready']['state'] == 'done'
    assert 'HumbleFax account opened' in steps['new_account_ready']['evidence']
    assert steps['port_ordered']['state'] == 'done' and steps['cutover']['state'] == 'waiting'
    assert steps['broadband']['state'] == 'done' and steps['other_lines']['state'] == 'waiting'
    assert steps['receipt_tests']['state'] == 'waiting'
    assert view['note'].startswith('Faxbot never places the port order')


def test_two_origin_receipt_tests_reconcile_one_arrival_at_both_endpoints_once(database):  # noqa: F811
    seeded(database)
    store = number_moves.MoveStore(database)
    start = NOW - timedelta(hours=5)
    store.start(QUIET, 'sip', 'humblefax', now=start)
    store.record(QUIET, 'cutover', 'done', now=start + timedelta(hours=1))
    # Test 1 from the trunk: the fax reaches both the old endpoint and the new one while the move converges.
    first = start + timedelta(hours=2)
    store.record(QUIET, 'receipt_test', 'started', origin='sip', now=first)
    sent(database, QUIET, first + timedelta(minutes=1), route='sip', provider='sip', pages=1)
    received(database, QUIET, first + timedelta(minutes=2), backend='humblefax', sender='+13035550100')
    received(database, QUIET, first + timedelta(minutes=3), backend='sip', sender='+13035550100')
    # Test 2 from Phaxio: it reaches the new endpoint only.
    second = start + timedelta(hours=3)
    store.record(QUIET, 'receipt_test', 'started', origin='phaxio', now=second)
    sent(database, QUIET, second + timedelta(minutes=1), route='phaxio', provider='phaxio', pages=1)
    received(database, QUIET, second + timedelta(minutes=4), backend='humblefax', sender='+18055550100')
    view = number_moves.move_view(database, move_values(), QUIET, now=NOW)
    tests = {test['origin']: test for test in view['tests']}
    assert tests['sip']['state'] == 'arrived_both'
    assert tests['sip']['sentence'] == ('The test fax sent through Telnyx arrived through both accounts; Faxbot '
                                        'counts it once.')
    assert tests['phaxio']['state'] == 'arrived_new'
    # Three arrival records, two faxes: the one that reached both endpoints counts once.
    assert view['arrivals']['new'] == 2 and view['arrivals']['old'] == 0 and view['arrivals']['both'] == 1
    assert view['arrivals']['sentence'] == ('2 faxes arrived through HumbleFax and 0 through Telnyx since the new '
                                            'provider completed the move; 1 arrived through both and is counted '
                                            'once.')
    steps = {step['step']: step for step in view['steps']}
    assert steps['receipt_tests']['state'] == 'done' and steps['reconciled']['state'] == 'done'


def test_a_test_that_still_lands_on_the_old_account_holds_the_move_open(database):  # noqa: F811
    seeded(database)
    store = number_moves.MoveStore(database)
    start = NOW - timedelta(hours=5)
    store.start(QUIET, 'sip', 'humblefax', now=start)
    store.record(QUIET, 'cutover', 'done', now=start)
    store.record(QUIET, 'receipt_test', 'started', origin='phaxio', now=start + timedelta(hours=1))
    sent(database, QUIET, start + timedelta(hours=1, minutes=1), route='phaxio', provider='phaxio')
    received(database, QUIET, start + timedelta(hours=1, minutes=2), backend='sip')
    store.record(QUIET, 'receipt_test', 'started', origin='sip', now=start + timedelta(hours=2))
    view = number_moves.move_view(database, move_values(), QUIET, now=NOW)
    tests = {test['origin']: test for test in view['tests']}
    assert tests['phaxio']['state'] == 'arrived_old'
    assert 'the move has not reached every network yet' in tests['phaxio']['sentence']
    assert tests['sip']['state'] == 'not_sent'
    steps = {step['step']: step for step in view['steps']}
    assert steps['receipt_tests']['state'] == 'failed' and steps['reconciled']['state'] != 'done'


def test_forgetting_what_was_learned_is_recorded_with_its_count(database):  # noqa: F811
    seeded(database)
    number_moves.MoveStore(database).start(QUIET, 'sip', 'humblefax', now=NOW - timedelta(days=1))
    number_moves.forget_learned(database, QUIET, by_name='Ada', now=NOW)
    view = number_moves.move_view(database, move_values(), QUIET, now=NOW)
    step = next(step for step in view['steps'] if step['step'] == 'facts_expired')
    assert step['state'] == 'done' and step['evidence'][0].startswith('Faxbot forgot 0 learned facts')
    with database.connect() as connection:
        events = sa.Table('number_move_events', sa.MetaData(), autoload_with=connection)
        row = connection.execute(sa.select(events.c.evidence).where(events.c.step == 'facts_expired')).scalar()
    assert json.loads(row) == {'forgotten': 0}
    number_moves.MoveStore(database).record(QUIET, 'move', 'abandoned', note='Staying with Telnyx')
    assert number_moves.move_view(database, move_values(), QUIET, now=NOW)['state'] == 'abandoned'
    with pytest.raises(RoutingConflict):
        number_moves.MoveStore(database).record(QUIET, 'cutover', 'done')


def _memory(engine):
    table = sa.Table('fax_destination_memory', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id='review-memory', number=QUIET, direction='outbound',
            kind='t38_failed', evidence='review-call', epoch_id='review-epoch', learned_at=NOW,
            expires_at=NOW + timedelta(days=30), created_at=NOW))
    return table


def test_move_forget_without_an_open_plan_leaves_learning_untouched(database):
    seeded(database)
    memory = _memory(database)
    with pytest.raises(RoutingConflict):
        number_moves.forget_learned(database, QUIET, by_name='Ada', now=NOW)
    with database.connect() as connection:
        assert connection.execute(sa.select(memory.c.forgotten_at)).scalar_one() is None


def test_finishing_through_the_http_handler_requires_all_evidence(database, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    from api.app.routing import number_http
    seeded(database)
    store = number_moves.MoveStore(database)
    store.start(QUIET, 'sip', 'humblefax', now=NOW)
    monkeypatch.setattr(number_http, '_store', lambda request: SimpleNamespace(engine=database))
    monkeypatch.setattr(number_http, '_values', lambda request: move_values())
    monkeypatch.setattr(number_http, '_number', lambda number, request: number)
    monkeypatch.setattr(number_http, '_actor', lambda engine, identity: (None, 'Ada'))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(number_http.record_move_step(QUIET, 'move', number_http.StepIn(state='finished'),
                                                 SimpleNamespace(), SimpleNamespace()))
    assert failure.value.status_code == 409
    assert number_moves.outcome(store.current(QUIET)[1]) is None


def test_same_provider_receipts_do_not_prove_the_new_account_received_the_tests(database):
    seeded(database)
    other = {'provider': 'sip', 'label': 'Other trunk', 'receives': True, 'numbers': ['+17205550150'],
             'settings': {'preset': 'anveo', 'auth': 'ip', 'host': '192.0.2.50'}}
    configured = move_values().with_provider_accounts(ConfigurationDocument({'sip-other': other}))
    store = number_moves.MoveStore(database)
    start = NOW - timedelta(hours=5)
    store.start(QUIET, 'sip', 'sip-other', now=start)
    store.record(QUIET, 'cutover', 'done', now=start)
    for index, origin in enumerate(('sip', 'phaxio')):
        when = start + timedelta(hours=index + 1)
        store.record(QUIET, 'receipt_test', 'started', origin=origin, now=when)
        sent(database, QUIET, when + timedelta(minutes=1), route=origin, provider=origin)
        received(database, QUIET, when + timedelta(minutes=2), backend='sip')
    view = number_moves.move_view(database, configured, QUIET, now=NOW)
    steps = {step['step']: step for step in view['steps']}
    assert steps['receipt_tests']['state'] != 'done'
    assert steps['reconciled']['state'] != 'done'
    assert all(test['state'] == 'arrived_unattributed' for test in view['tests'])


def test_failed_move_event_rolls_back_the_learning_clear(database):
    seeded(database)
    memory = _memory(database)
    number_moves.MoveStore(database).start(QUIET, 'sip', 'humblefax', now=NOW)
    def refuse_event(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO NUMBER_MOVE_EVENTS'):
            raise RuntimeError('Synthetic event storage failure')
    sa.event.listen(database, 'before_cursor_execute', refuse_event)
    try:
        with pytest.raises(RuntimeError, match='Synthetic event storage failure'):
            number_moves.forget_learned(database, QUIET, now=NOW)
    finally:
        sa.event.remove(database, 'before_cursor_execute', refuse_event)
    with database.connect() as connection:
        assert connection.execute(sa.select(memory.c.forgotten_at)).scalar_one() is None
    assert number_moves.MoveStore(database).current(QUIET)[1] == []


def test_explicit_receiving_accounts_allow_a_fully_evidenced_same_provider_move(database):
    seeded(database)
    other = {'provider': 'sip', 'label': 'Other trunk', 'receives': True, 'numbers': ['+17205550150'],
             'settings': {'preset': 'anveo', 'auth': 'ip', 'host': '192.0.2.50'}}
    configured = move_values().with_provider_accounts(ConfigurationDocument({'sip-other': other}))
    store = number_moves.MoveStore(database)
    start = NOW - timedelta(hours=5)
    store.start(QUIET, 'sip', 'sip-other', now=start)
    answer_all(database, QUIET)
    rules = sa.Table('inbound_rules', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(rules.insert().values(id='move-rule', to_number=QUIET,
                                                mailbox_label='Fax team', created_at=start))
    for step in number_moves.RECORDED:
        store.record(QUIET, step, 'done', now=start + timedelta(minutes=1))
    imports = sa.Table('inbound_imports', sa.MetaData(), autoload_with=database)
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=database)
    for index, origin in enumerate(('sip', 'phaxio')):
        when = start + timedelta(hours=index + 1)
        store.record(QUIET, 'receipt_test', 'started', origin=origin, now=when)
        sent(database, QUIET, when + timedelta(minutes=1), route=origin, provider=origin)
        arrived = when + timedelta(minutes=2)
        received(database, QUIET, arrived, backend='sip')
        with database.begin() as connection:
            fax = connection.execute(sa.select(faxes.c.id).where(faxes.c.received_at == arrived)).scalar_one()
            connection.execute(imports.insert().values(id=uuid4().hex, source='sip', account='synthetic',
                operation_id=fax, revision='', state='received', attempts=1, imported_at=arrived,
                acquired_at=arrived, artifact_digest='a' * 64, artifact_size=12, inbound_fax_id=fax,
                created_at=arrived, updated_at=arrived, account_key='sip-other'))
    number_moves.forget_learned(database, QUIET, now=NOW)
    view = number_moves.move_view(database, configured, QUIET, now=NOW)
    assert all(step['state'] == 'done' for step in view['steps']), view['steps']
    store.record(QUIET, 'move', 'finished', values=configured, now=NOW + timedelta(seconds=1))
    assert number_moves.move_view(database, configured, QUIET, now=NOW)['state'] == 'finished'


def test_receipt_tests_do_not_count_another_account_as_the_primary_route(database):
    seeded(database)
    sent(database, QUIET, NOW, route='sip-other', provider='sip')
    assert number_moves.test_faxes(database, QUIET, 'sip', NOW - timedelta(minutes=1)) == []
    assert number_moves.test_faxes(database, QUIET, 'sip-other', NOW - timedelta(minutes=1)) == [(NOW, 1)]
