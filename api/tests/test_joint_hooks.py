"""The joint optimizer's hooks into other builders' modules, each against the real function: route family
incidents (routing/route_families.py) in route choice and the schedule, and the UPS (power.py) at dispatch.

Synthetic numbers (555) and a local stand-in UPS server only; no call is placed and no provider is contacted.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from api.tests.test_schema import database  # noqa: F401 (fixture)


# Route family incidents (routing/route_families.py, RF) in route choice and the schedule ------------------------

from api.tests.test_engine_learning import data_folder, db, installation, memory_rows  # noqa: F401,E402 (fixtures)


def test_an_incident_on_every_transport_makes_an_account_doubtful_and_t38_alone_leaves_audio(installation):  # noqa: F811
    import sqlalchemy as sa
    import uuid
    from app import engine_learning
    from app.routing import route_families
    from app.routing.plan import RoutePlanner
    from app.routing.policy import RouteCandidate
    from api.tests.test_route_families import replay_broken_t38
    db = installation
    now = engine_learning.utcnow()
    replay_broken_t38(db, now)
    route_families.watch(db, now=now)
    t38 = route_families.t38_problem(db, 'sip')
    assert t38 is not None and route_families.family_incident(db, 'sip', 'audio') is None
    planner = RoutePlanner(SimpleNamespace(engine=db))
    trunk, service = RouteCandidate('sip', 'provider', 'sip'), RouteCandidate('sinch', 'provider', 'sinch')
    assert planner._incidents([trunk, service]) == set()  # its audio fax still goes
    table = sa.Table('route_family_incidents', sa.MetaData(), autoload_with=db)
    row = {key: value for key, value in t38.items() if key in table.c and key != 'id'}
    with db.begin() as connection:
        connection.execute(table.insert().values(**{**row, 'id': uuid.uuid4().hex, 'transport': 'audio'}))
        connection.execute(table.insert().values(**{**row, 'id': uuid.uuid4().hex, 'transport': 'service',
                                                    'account_key': 'sinch', 'provider_id': 'sinch'}))
    assert planner._incidents([trunk, service]) == {'sip', 'sinch'}


def test_failures_in_a_route_family_incident_teach_the_numbers_schedule_nothing(database, tmp_path):  # noqa: F811
    """RF's broken T.38 replay, with D1's failed T.38 call made by a fax the delivery store sent: once the watcher
    opens the incident, that attempt leaves the number's busy-hour observations and its call timings."""
    import sqlalchemy as sa
    from api.app import engine_learning
    from api.app.routing import route_families, schedule as live_schedule
    from api.tests.test_route_families import D1, X, replay_broken_t38
    from api.tests.test_schedule import Scheduled
    now = engine_learning.utcnow()
    install = Scheduled(database, tmp_path)
    when = now - timedelta(hours=20)  # D1's T.38 failure in the replay
    install.history([(when, 'busy')], number=D1)
    replay_broken_t38(database, now)
    with database.begin() as connection:
        attempt, job = connection.execute(sa.text(
            'SELECT a.id, a.job_id FROM outbound_attempts a JOIN fax_jobs j ON j.id = a.job_id '
            'WHERE j.to_number = :number'), {'number': D1}).one()
        connection.execute(sa.text(
            "UPDATE sip_call_records SET attempt_id = :attempt, job_id = :job WHERE called = :number "
            "AND fax_status = 'FAILED'"), {'attempt': attempt, 'job': job, 'number': D1})
    scheduler = live_schedule.Scheduler(database)

    def read():
        with database.connect() as connection:
            return (scheduler.observations(connection, D1, now), scheduler.call_timings(connection, now, D1),
                    scheduler.call_timings(connection, now, X))
    before = read()
    assert len(before[1]) == 1 and before[1][0].failed_after_answer is True
    route_families.watch(database, now=now)
    assert route_families.excluded_attempts(database, [attempt]) == {attempt}
    after = read()
    assert after[1] == [] and len(after[0]) == len(before[0]) - 1
    assert after[2] == before[2]  # X's own failure is a real lesson, kept


# On battery (power.py, RF): the call that could outlast the UPS goes elsewhere or waits, never cut off ------------

from api.tests.test_trunk_capacity import _routed, claim, trunked  # noqa: F401,E402 (fixture and helpers)
from api.tests.test_rules_delivery import accept, publish, rule  # noqa: E402


@pytest.fixture
def on_battery():
    """A UPS on battery with 65 seconds left and no reserve: room to hand a fax to a service (60 s), not for a
    trunk call from this server."""
    from api.app import power as live_power
    from api.tests.test_power import FakeUpsd
    live_power._cache.clear()
    with FakeUpsd({'office': {'ups.status': 'OB', 'battery.runtime': '65'}}) as ups:
        yield lambda env: live_power.save(env.engine, '127.0.0.1', port=ups.port, reserve_seconds=0)
    live_power._cache.clear()


LONG = {'pages': 20}  # the fax whose call could outlast the battery


def _battery_attempt(env, monkeypatch, routes):
    from api.app.routing import envelope as envelopes
    from api.app.routing.plan import RoutePlanner
    publish(env, {'format': 1, 'routes': [rule('r-power', routes)]})
    job = accept(env)
    found = claim(env)
    revision, _ = env.configuration.outbound_context(job)
    plan = RoutePlanner(env.routes).plan(to_number='+12025550123', bound='sip', values=revision.values, pages=1,
                                         pinned=envelopes.load(env.engine, job), alternates=True)
    return job, found, plan, revision, _routed(env, monkeypatch)


def _hold_reason(env, job):
    import sqlalchemy as sa
    with env.engine.connect() as connection:
        return connection.execute(sa.text("SELECT reason FROM outbound_holds WHERE job_id = :job AND state = 'open'"),
                                  {'job': job}).scalar()


def test_on_battery_a_trunk_call_that_could_outlast_it_goes_by_a_fax_service(trunked, monkeypatch, on_battery):
    job, found, plan, revision, transport = _battery_attempt(trunked, monkeypatch, {'try_in_order': ['sip', 'phaxio']})
    assert [choice.route.key for choice in plan.choices] == ['sip', 'phaxio']
    # Without a UPS nothing is weighed: the trunk takes it.
    assert transport._admission(revision, plan, 0) is None
    on_battery(trunked)
    assert transport._p90(revision, plan, plan.choices[0].route, None, LONG) > 65
    chosen, _ = transport._assign(found, plan, revision, None, LONG)
    assert chosen.route.key == 'phaxio' and ('sip', 'power') in transport._skipped
    assert _hold_reason(trunked, job) is None


def test_on_battery_with_no_other_route_the_fax_waits_in_sent_unsent(trunked, monkeypatch, on_battery):
    from api.app.routing.transport import RouteHeld
    on_battery(trunked)
    job, found, plan, revision, transport = _battery_attempt(trunked, monkeypatch, {'use': 'sip'})
    with pytest.raises(RouteHeld):
        transport._assign(found, plan, revision, None, LONG)
    reason = _hold_reason(trunked, job)
    assert reason.startswith('This office is on battery with about 1 minute left') and 'unsent' in reason


def test_on_battery_a_service_that_is_not_ready_never_sends_the_call_on_the_trunk(trunked, monkeypatch, on_battery):
    from api.app.routing import transport as routed
    from api.app.routing.transport import RouteHeld
    on_battery(trunked)
    job, found, plan, revision, transport = _battery_attempt(trunked, monkeypatch, {'try_in_order': ['sip', 'phaxio']})
    monkeypatch.setattr(routed, 'route_ready', lambda configuration, ami=None: configuration.provider_id != 'phaxio')
    with pytest.raises(RouteHeld):
        transport._assign(found, plan, revision, None, LONG)
    assert 'unsent' in _hold_reason(trunked, job)
    assert ('sip', 'power') in transport._skipped and ('phaxio', 'not_ready') in transport._skipped
