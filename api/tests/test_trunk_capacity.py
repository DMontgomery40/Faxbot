"""Room per trunk and per account, and ``when_busy: next`` at the claim gate (provider-rules design §3.6, WP-T).

The claim gate offers a fax only while the account it will use has room. Before sending rules, the account was
the one the fax was accepted with; with a rule decision it is what the fax's envelope allows: a full first
account holds a ``when_busy: next`` fax only when every other calling account it allows is full too. Faxes that
wait stay ``ready``. Each trunk counts its own lines, calls coming in included; another account with a "faxes at
once" limit counts its own faxes in progress. SQLite and PostgreSQL.
"""
from datetime import datetime
from uuid import uuid4

import pytest

from api.app.capacity import Capacity, limited_accounts, room_sentence
from api.app.config_profiles import ConfigurationDocument, ProviderConfiguration
from api.app.sip_calls import SipCallRecords
from api.tests.test_rules_delivery import BASE, accept, installation, publish, rule
from api.tests.test_schema import database  # noqa: F401 (fixture)


TRUNK = {**BASE, 'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_ROUTES': '', 'SIP_TRUNK_PRESET': 'telnyx',
         'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_CALLER_ID': '+13035550100', 'SIP_TRUNK_DIDS': '+13035550100',
         'SIP_TRUNK_MAX_CALLS': '1'}
LEEDS = {'provider': 'sip', 'label': 'Leeds trunk', 'receives': True, 'numbers': ['+441132000000'],
         'settings': {'preset': 'gamma', 'auth': 'ip', 'host': '192.0.2.40', 'caller_id': '+441132000000'},
         'limits': {'at_once': 1}}


@pytest.fixture
def trunked(database, tmp_path):
    env = installation(database, tmp_path, TRUNK, outbound=ProviderConfiguration('sip', traits={'requires_tiff': True}))
    env.snapshot = env.configuration.apply(env.snapshot, env.snapshot.active.values, restart_required=False,
                                           actor='test', accounts=ConfigurationDocument({'sip-leeds': LEEDS}))
    return env


def call_in(env, trunk=None):
    """A call coming in right now, still on the line (it holds a line of its trunk)."""
    now = datetime.utcnow()
    with env.engine.begin() as connection:
        connection.execute(SipCallRecords(env.engine).table.insert().values(
            id=uuid4().hex, direction='inbound', call_id=uuid4().hex, started_at=now, disposition='answered',
            t38='yes', fax_preference=0, trunk_key=trunk, created_at=now, updated_at=now))


def claim(env, *exclude):
    return env.delivery.claim('worker-test', exclude=tuple(exclude))


def values(env):
    return env.configuration.read().active.values


def test_each_trunk_and_each_limited_account_has_its_own_room(trunked):
    limited = limited_accounts(values(trunked))
    assert {key: (item.trunk, item.at_once) for key, item in limited.items()} == {'sip': (True, 1),
                                                                                  'sip-leeds': (True, 1)}
    call_in(trunked, 'sip-leeds')
    capacity = Capacity(trunked.engine)
    with trunked.engine.connect() as connection:
        rooms = capacity.rooms(connection, values(trunked), datetime.utcnow())
        # The call on the Leeds trunk holds its line, not the first trunk's; NULL is the first trunk.
        assert (rooms['sip-leeds'].full, rooms['sip'].full) == (True, False)
        assert capacity.room(connection, values(trunked), datetime.utcnow()) == rooms['sip']
    call_in(trunked)
    with trunked.engine.connect() as connection:
        assert capacity.room(connection, values(trunked), datetime.utcnow(), trunk='sip').full
    assert room_sentence(rooms['sip-leeds'], several=True) == \
        'Waiting for a free line on Leeds trunk: all 1 line is in use.'


def test_without_rules_a_fax_waits_for_its_own_full_trunk_as_before(trunked):
    call_in(trunked)
    job = accept(trunked)
    assert claim(trunked) is None
    # With several trunks the sentence names the one that is full.
    assert Capacity(trunked.engine).waiting_sentence(job, values(trunked), datetime.utcnow()) == \
        'Waiting for a free line on Telnyx: all 1 line is in use.'
    # A fax the rules send by another account is never held for the full trunk.
    publish(trunked, {'format': 1, 'routes': [rule('r-phaxio', {'use': 'phaxio'})]})
    other = accept(trunked)
    assert claim(trunked).job_id == other


@pytest.mark.parametrize('then, offered', [
    ({'try_in_order': ['sip', 'phaxio'], 'when_busy': 'next'}, True),
    ({'try_in_order': ['sip', 'phaxio'], 'when_busy': 'wait'}, False),
    ({'try_in_order': ['sip', 'phaxio']}, False),                       # wait is the default
    ({'cheapest_reliable': ['sip', 'phaxio'], 'when_busy': 'next'}, True),
    ({'try_in_order': ['sip', 'sip-leeds'], 'when_busy': 'next'}, True),
    ({'use': 'sip', 'when_busy': 'next'}, False),                       # nothing else allowed: it waits
])
def test_when_busy_next_lets_a_fax_past_its_full_first_account_at_the_claim_gate(trunked, then, offered):
    publish(trunked, {'format': 1, 'routes': [rule('r-busy', then)]})
    call_in(trunked)
    job = accept(trunked)
    found = claim(trunked)
    assert (found is not None and found.job_id == job) is offered
    if not offered:
        # It stays ready, and says which trunk it waits for.
        with trunked.engine.connect() as connection:
            state = connection.exec_driver_sql(
                f"SELECT state FROM outbound_deliveries WHERE id = '{job}'").scalar()
        assert state == 'ready'
        sentence = Capacity(trunked.engine).waiting_sentence(job, values(trunked), datetime.utcnow())
        assert sentence == 'Waiting for a free line on Telnyx: all 1 line is in use.'


def test_a_fax_waits_only_when_every_trunk_its_rule_allows_is_full(trunked):
    publish(trunked, {'format': 1, 'routes': [rule('r-both', {'try_in_order': ['sip-leeds', 'sip'],
                                                              'when_busy': 'next'})]})
    call_in(trunked, 'sip-leeds')
    first = accept(trunked)
    assert claim(trunked).job_id == first        # Leeds is full, the first trunk is not
    call_in(trunked)
    second = accept(trunked, to='+12025550124')
    assert claim(trunked, first) is None          # both trunks are full now
    sentence = Capacity(trunked.engine).waiting_sentence(second, values(trunked), datetime.utcnow())
    assert sentence == 'Waiting for a free line on Leeds trunk: all 1 line is in use.'
    assert Capacity(trunked.engine).waiting_for_line(values(trunked), datetime.utcnow(), waiting=[first]) == 1


def test_an_account_with_a_faxes_at_once_limit_holds_that_many_faxes(database, tmp_path):  # noqa: F811
    env = installation(database, tmp_path, {**BASE, 'FAX_OUTBOUND_ROUTES': ''})
    env.snapshot = env.configuration.apply(env.snapshot, env.snapshot.active.values, restart_required=False,
                                           actor='test',
                                           accounts=ConfigurationDocument({'phaxio': {'limits': {'at_once': 1}}}))
    assert {key: item.at_once for key, item in limited_accounts(values(env)).items()} == {'phaxio': 1}
    first, second = accept(env), accept(env, to='+12025550124')
    started = claim(env)
    assert started.job_id == first and env.delivery.begin_submission(started)
    assert claim(env) is None
    assert Capacity(env.engine).waiting_sentence(second, values(env), datetime.utcnow()) == \
        'Waiting: Phaxio already has 1 fax in progress, its limit at once.'


@pytest.mark.parametrize('when_busy, offered', [('next', True), ('wait', False)])
def test_a_sites_accounts_in_order_pass_a_full_site_trunk_only_when_the_rule_says_next(trunked, when_busy, offered):
    """Sites (X13): "use the Leeds office's accounts, in order" with the Leeds trunk full."""
    publish(trunked, {'format': 1,
                      'sites': [{'key': 'leeds', 'name': 'Leeds office', 'country': 'GB',
                                 'time_zone': 'Europe/London', 'accounts': ['sip-leeds', 'phaxio']}],
                      'routes': [rule('r-leeds', {'site_accounts': 'leeds', 'mode': 'ordered',
                                                  'when_busy': when_busy})]})
    call_in(trunked, 'sip-leeds')
    job = accept(trunked)
    from api.app.routing import envelope as envelopes
    assert envelopes.load(trunked.engine, job).envelope.accounts == ('sip-leeds', 'phaxio')
    found = claim(trunked)
    assert (found is not None and found.job_id == job) is offered


def test_a_trunk_never_calls_its_own_numbers_but_another_trunk_may(trunked):
    """The self-call guard per trunk: a fax to one of the Leeds trunk's numbers never goes out over Leeds; with
    "place a real call" it may use the first trunk, and a fax to the first trunk's number may use Leeds."""
    from api.app.routing import envelope as envelopes
    from api.app.routing.plan import RoutePlanner, _own_trunks
    publish(trunked, {'format': 1, 'routes': [rule('r-two', {'cheapest_reliable': ['sip-leeds', 'phaxio']})]})
    values_now = values(trunked)
    assert _own_trunks(values_now, '+441132000000') == {'sip-leeds'}
    assert _own_trunks(values_now, '+13035550100') == {'sip'}
    assert _own_trunks(values_now, '+12025550123') == set()
    for to, kept in (('+441132000000', {'phaxio'}), ('+12025550123', {'phaxio', 'sip-leeds'})):
        job = accept(trunked, to=to, by_call=True)
        pinned = envelopes.load(trunked.engine, job)
        plan = RoutePlanner(trunked.routes).plan(to_number=to, bound='sip', values=values_now, pages=1,
                                                 pinned=pinned, by_call=True, alternates=True)
        assert {choice.route.key for choice in plan.choices} - {"sip"} == kept, (to, plan.choices, plan.skipped, pinned.envelope)


def test_a_fax_whose_allowed_trunks_are_all_busy_waits_instead_of_being_held(trunked):
    """At dispatch each trunk's own room is read (the route's account key); when everything the rule allows is
    busy the fax waits for a line (CapacityWait), and is never held in Sent for a person."""
    from api.app.outbound_worker import CapacityWait
    from api.app.routing import envelope as envelopes
    from api.app.routing.plan import RoutePlan
    from api.app.routing.policy import RouteCandidate, RouteChoice
    from api.app.routing.transport import RoutedTransport
    from api.tests.test_rules_delivery import TO, Inner
    publish(trunked, {'format': 1, 'routes': [rule('r-leeds', {'try_in_order': ['sip-leeds', 'phaxio'],
                                                               'when_busy': 'next'})]})
    job = accept(trunked)
    found = claim(trunked)
    assert found.job_id == job
    call_in(trunked, 'sip-leeds')
    pinned = envelopes.load(trunked.engine, job)
    revision, _ = trunked.configuration.outbound_context(job)
    transport = RoutedTransport(Inner(trunked.delivery), direct=None)
    leeds = RouteChoice(RouteCandidate('sip-leeds', 'provider', 'sip', None), 'rule', None)
    assert transport._trunk_has_room(found, revision, 'sip-leeds') is False
    assert transport._trunk_has_room(found, revision, 'sip') is True
    with pytest.raises(CapacityWait):
        transport._assign(found, RoutePlan(TO, (leeds,), None, pinned=pinned), revision)
    assert transport._skipped == (('sip-leeds', 'busy'),)
