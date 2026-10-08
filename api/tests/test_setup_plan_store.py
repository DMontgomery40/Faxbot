"""Guided setup's facts and plan records on migrated SQLite and PostgreSQL.

``facts.gather`` reads each module's stored state through its public functions;
these run it against real tables with synthetic history, then keep and read a
plan and its applications.
"""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from api.app.setup_plan import facts as facts_module
from api.app.setup_plan.packs import compile_plan
from api.app.setup_plan.service import basis_token
from api.app.setup_plan.store import PlanStore
from api.tests.test_advice import NOW, CLINIC, place, routes  # noqa: F401 (fixture)
from api.tests.test_outbound_store import installation  # noqa: F401 (fixture)
from api.tests.test_schema import database  # noqa: F401 (fixture: SQLite and PostgreSQL)


def test_facts_are_read_from_stored_history_on_both_databases(installation, routes):  # noqa: F811
    configuration, _, snapshot = installation
    for _ in range(4):
        place(installation, routes, to=CLINIC, pages=12, reported='0.02', call=False)
    place(installation, routes, to='+18005550100', pages=2, reported='0.01', call=False)
    found = facts_module.gather(configuration.engine, snapshot, bound='phaxio', now=NOW)
    assert found.config_revision == snapshot.desired.id and found.pending_restart is False
    assert sorted(found.history) == sorted([(CLINIC, 'sip', 12)] * 4 + [('+18005550100', 'sip', 2)])
    assert [item['number'] for item in found.partners] == [CLINIC]
    assert set(found.rules) == {'organization'} and found.rules['organization']['active'] is None
    assert found.unplaced == () and found.junk == () and found.send_once_offers == () and found.discovered == ()
    plan = compile_plan(found, {'country': 'US'})
    assert [item['key'] for pack in plan['packs'] for item in pack['items'] if pack['key'] == 'partners'] \
        == [f'partners.invite.{CLINIC}']


def test_unplaced_faxes_and_repeated_junk_come_from_their_own_tables(installation):  # noqa: F811
    configuration, _, snapshot = installation
    engine = configuration.engine
    metadata = sa.MetaData()
    faxes = sa.Table('inbound_faxes', metadata, autoload_with=engine)
    callers = sa.Table('screened_callers', metadata, autoload_with=engine)
    with engine.begin() as connection:
        for index in range(3):
            connection.execute(faxes.insert().values(id=f'in-{index}', to_number='+13035550188', status='received',
                                                     backend='phaxio', created_at=NOW, received_at=NOW,
                                                     updated_at=NOW))
        for index, ago in enumerate((200, 100)):  # marked twice; both blocks over
            start = NOW - timedelta(days=ago)
            connection.execute(callers.insert().values(id=f'junk-{index}', number='+13035550999', reason='Ads',
                                                       created_at=start, expires_at=start + timedelta(days=90)))
    found = facts_module.gather(engine, snapshot, now=NOW)
    assert found.unplaced == ({'number': '+13035550188', 'faxes': 3},)
    assert found.junk == ({'number': '+13035550999', 'marks': 2, 'active': False, 'expires_at': None,
                           'rejected': 0},)


def test_a_fax_the_receiving_rules_placed_is_never_called_unplaced(database):  # noqa: F811
    """Companion of the seeded rows above: faxes placed by the real receiving path (access/inbound.py)."""
    from api.tests.test_access_policy import NOW as PLACED_AT
    from api.tests.test_inbound_access import InboundWorld
    world = InboundWorld(database)
    world.fax('placed', '+15550100002')        # Billing's number rule places it
    world.fax('loose-1', '+15550100099')       # no rule for this number
    world.fax('loose-2', '+15550100099')
    assert facts_module.unplaced(database, PLACED_AT) == ({'number': '+15550100099', 'faxes': 2},)


def test_plans_are_numbered_kept_and_their_applications_recorded(installation):  # noqa: F811
    configuration, _, snapshot = installation
    store = PlanStore(configuration.engine)
    found = facts_module.gather(configuration.engine, snapshot, now=NOW)
    plan = compile_plan(found, {})
    first = store.create(basis=basis_token(plan['basis']), context={}, plan=plan, actor_name='Synthetic Admin', now=NOW)
    second = store.create(basis=basis_token(plan['basis']), context={'country': 'GB'}, plan=plan, now=NOW)
    assert (first['number'], second['number']) == (1, 2)
    assert store.get(1)['plan'] == plan and store.get(2)['context'] == {'country': 'GB'}
    assert store.latest()['number'] == 2 and [row['number'] for row in store.recent()] == [2, 1]
    store.record_application(first['id'], outcome='partial', items=['a'],
                             steps=[{'part': 'settings', 'outcome': 'done'}], restart_required=True, now=NOW)
    [application] = store.applications_of(first['id'])
    assert application['outcome'] == 'partial' and application['steps'][0]['part'] == 'settings'
    assert application['restart_required'] == 1 and store.applications_of(second['id']) == []
    with pytest.raises(Exception):
        store.record_application(first['id'], outcome='done', items=[], steps=[], restart_required=False)
