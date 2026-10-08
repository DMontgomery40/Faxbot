"""Digital routes in the sending rules and the route planner: named, grouped as "digital", forbidden, priced by the
real predictor, skipped when no longer confirmed, and suggested from NPPES but used only once confirmed
(SQLite and PostgreSQL). Synthetic accounts, numbers and addresses only.
"""
import asyncio

import pytest

from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.tests.test_rules_delivery import Inner, accept, installation, publish, rule, TO, UNLISTED
from api.tests.test_digital_fhir import FakeFhir, BASE
from api.tests.digital_fixtures import synthetic_pdf
from api.app.config_profiles import ConfigurationDocument
from api.app.digital import accounts as digital_accounts
from api.app.digital import fhir, reachability
from api.app.digital.routes import DigitalRoute
from api.app.digital.store import DigitalStore
from api.app.outbound_worker import OutboundWorker
from api.app.routing import envelope as envelopes
from api.app.routing.plan import RoutePlanner, ledger_key
from api.app.routing.transport import RoutedTransport
from api.app.rules import model
from api.app.rules.check import CheckContext, check


def _with_fhir(env, **plan):
    """The installation with a FHIR client (its plan given) and the recipient's confirmed FHIR endpoint."""
    snapshot = env.configuration.read()
    documents = digital_accounts.added(snapshot.desired.values, {
        'key': 'fhir-hospital', 'provider': 'fhir', 'settings': {'client_id': 'faxbot-county-clinic', **plan}})
    documents = digital_accounts.with_signing_key(snapshot.desired.values.with_provider_accounts(
        ConfigurationDocument(documents)), 'fhir-hospital')
    env.configuration.apply(snapshot, snapshot.desired.values, restart_required=False, actor='test',
                            accounts=ConfigurationDocument(documents))
    env.snapshot = env.configuration.read()
    env.digital = DigitalStore(env.engine)
    env.address = env.digital.add_address(number=TO, kind='fhir', address=BASE, source='entered',
                                          action='confirmed', organization='Synthetic Hospital')
    env.key = 'fhir:' + env.address['id']
    env.server = FakeFhir()
    env.server.jwks = fhir.jwks(digital_accounts.digital_account(env.snapshot.active.values, 'fhir-hospital'))
    return env


@pytest.fixture
def ruled(database, tmp_path):  # noqa: F811
    return _with_fhir(installation(database, tmp_path, UNLISTED))


def plan_for(env, job):
    pinned = envelopes.load(env.engine, job)
    revision, _ = env.configuration.outbound_context(job)
    return RoutePlanner(env.routes).plan(to_number=TO, bound='phaxio', values=revision.values, pages=3,
                                         alternates=True, pinned=pinned, current=env.configuration.read().active.values)


def queued(env, **facts):
    job = accept(env, **facts)
    (env.tmp / (job + '.pdf')).write_bytes(synthetic_pdf())
    return job


async def dispatch(env):
    inner = Inner(env.delivery)
    transport = RoutedTransport(inner, direct=None, relay=None, digital=DigitalRoute(
        env.engine, values=lambda: env.configuration.read().active.values,
        fhir_transport=fhir.Transport(send=env.server.send)))
    assert await OutboundWorker(env.delivery, transport).step() is True
    return inner


def test_the_group_key_digital_puts_the_recipients_route_first_and_sends_it(ruled):
    publish(ruled, {'format': 1, 'routes': [rule('r-digital', {'try_in_order': ['digital', 'phaxio']})]})
    job = queued(ruled)
    pinned = envelopes.load(ruled.engine, job)
    assert pinned.envelope.mode == 'ordered' and pinned.envelope.accounts == ('digital', 'phaxio')
    assert pinned.allows(ruled.key) and pinned.allows(ledger_key(ruled.key))
    plan = plan_for(ruled, job)
    assert [(choice.route.key, choice.route.kind, choice.reason) for choice in plan.choices] == [
        (ruled.key, 'digital', 'rule'), ('phaxio', 'provider', 'alternative')]
    inner = asyncio.run(dispatch(ruled))
    assert inner.used == [] and len(ruled.server.posts) == 1
    assert ruled.delivery.get(job)['state'] == 'success'
    with ruled.engine.connect() as connection:
        recorded = envelopes.choice_on(connection, ruled.delivery.get(job)['attempt_id'])
    assert recorded['account_key'] == ruled.key and recorded['rule_id'] == 'r-digital'


def test_a_refused_digital_route_moves_a_strict_rule_on_to_its_next_account_not_the_default(ruled):
    """``try_in_order: [digital, signalwire]`` with Phaxio as the default: the FHIR server refuses (nothing stored),
    so the rule's next account sends it, never the default the rule left out, and never as a failure."""
    from api.app.outbound_store import OutboundStore
    from api.app.routing.fallback import FallbackPolicy, FallbackScheduler
    publish(ruled, {'format': 1, 'routes': [rule('r-digital', {'try_in_order': ['digital', 'signalwire']})]})
    ruled.server.mode = 'refuse'
    job = queued(ruled)
    assert envelopes.load(ruled.engine, job).strict
    OutboundStore.fallback_policy = FallbackPolicy(FallbackScheduler(ruled.delivery, ruled.routes))
    try:
        first = asyncio.run(dispatch(ruled))
        assert first.used == [] and len(ruled.server.posts) == 1
        assert ruled.delivery.get(job)['state'] == 'ready'           # back in the queue for the next route
        second = asyncio.run(dispatch(ruled))
    finally:
        OutboundStore.fallback_policy = None
    assert second.used == ['signalwire'] and len(ruled.server.posts) == 1
    from api.app.routing.route_view import fax_route
    view = fax_route(ruled.engine, ruled.configuration, job, rules=ruled.rules)
    labels = [attempt['account_label'] for attempt in view['attempts']]
    assert labels[0].startswith('FHIR') and ruled.address['id'] not in ' '.join(labels)
    assert labels[1] == 'SignalWire'


def test_use_digital_alone_passes_the_submission_marker_though_the_rules_exclude_the_fax_account(ruled):
    publish(ruled, {'format': 1, 'routes': [rule('r-only', {'use': 'digital'})]})
    job = queued(ruled)
    assert not envelopes.load(ruled.engine, job).allows('phaxio')
    inner = asyncio.run(dispatch(ruled))
    assert inner.used == [] and ruled.delivery.get(job)['state'] == 'success'


def test_never_digital_and_direct_only_keep_a_fax_off_every_digital_route(ruled):
    publish(ruled, {'format': 1, 'limits': [rule('l-never', {'never': ['digital']})],
                    'routes': [rule('r-digital', {'try_in_order': ['digital', 'phaxio']})]})
    job = queued(ruled)
    pinned = envelopes.load(ruled.engine, job)
    assert not pinned.allows(ruled.key)
    assert [choice.route.key for choice in plan_for(ruled, job).choices] == ['phaxio']
    assert ('digital', 'never') in {(item.account, item.why) for item in pinned.decision.excluded}
    publish(ruled, {'format': 1, 'limits': [rule('l-direct', {'require_direct': True})], 'routes': []})
    job = queued(ruled)
    assert not envelopes.load(ruled.engine, job).allows(ruled.key)


def test_the_automatic_choice_takes_digital_only_when_it_is_cheaper_by_the_real_predictor(database, tmp_path):  # noqa: F811
    cheap = _with_fhir(installation(database, tmp_path, UNLISTED))
    job = queued(cheap)
    plan = plan_for(cheap, job)
    first = plan.choices[0]
    assert first.route.key == cheap.key and first.estimated_cost_micros == 0
    assert plan.choices[1].route.key == 'phaxio' and plan.choices[1].estimated_cost_micros > 0


def test_a_digital_route_dearer_than_the_fax_goes_after_it(database, tmp_path):  # noqa: F811
    dear = _with_fhir(installation(database, tmp_path, UNLISTED), price_per_message='1.00')
    job = queued(dear)
    plan = plan_for(dear, job)
    # Phaxio: 3 pages at $0.07 = $0.21; the FHIR network's $1.00 a document goes second.
    assert [choice.route.key for choice in plan.choices] == ['phaxio', dear.key]
    assert plan.choices[1].estimated_cost_micros == 1_000_000


def test_a_named_key_no_longer_confirmed_is_reported_and_skipped_as_unavailable(ruled):
    document = {'format': 1, 'routes': [rule('r-named', {'try_in_order': [ruled.key, 'phaxio']})]}
    publish(ruled, document)
    job = queued(ruled)
    accounts = tuple(__import__('api.app.accounts', fromlist=['sending_accounts']).sending_accounts(
        ruled.snapshot.active.values))
    confirmed = frozenset(ruled.digital.confirmed_ids())
    assert not [problem for problem in check(model.ORGANIZATION, '', document,
                                             CheckContext(accounts=accounts, digital=confirmed))
                if problem.code == 'unknown_account']
    ruled.digital.record(ruled.address['id'], 'withdrawn')
    problems = check(model.ORGANIZATION, '', document,
                     CheckContext(accounts=accounts, digital=frozenset(ruled.digital.confirmed_ids())))
    (problem,) = [problem for problem in problems if problem.code == 'unknown_account']
    assert 'names a FHIR endpoint that is no longer confirmed' in problem.message
    plan = plan_for(ruled, job)
    assert [choice.route.key for choice in plan.choices] == ['phaxio']
    assert (ruled.key, 'unavailable') in plan.skipped


def test_an_nppes_suggestion_is_never_used_until_confirmed(ruled):
    number = '+13035550188'
    document = {'result_count': 1, 'results': [{
        'number': '1234567893', 'basic': {'organization_name': 'SYNTHETIC REGIONAL HOSPITAL'},
        'addresses': [{'address_purpose': 'LOCATION', 'address_1': '1 Example Way', 'city': 'DENVER',
                       'state': 'CO', 'fax_number': '303-555-0188'}],
        'endpoints': [
            {'endpointType': 'DIRECT', 'endpointTypeDescription': 'Direct Messaging Address',
             'endpoint': 'Intake@Direct.Regional-Hospital.example.net', 'affiliation': 'N', 'use': 'DIRECT'},
            {'endpointType': 'FHIR', 'endpointTypeDescription': 'FHIR URL',
             'endpoint': 'https://fhir.regional-hospital.example.net/r4/', 'affiliation': 'Y',
             'affiliationName': 'Synthetic Health Network', 'use': 'HIE'},
            {'endpointType': 'REST', 'endpoint': 'https://api.regional-hospital.example.net/'}]}]}
    asked = []

    def fetch(params):
        asked.append(params)
        return document
    filed, sentence = reachability.suggest_from_nppes(ruled.digital, ruled.snapshot.active.values, number,
                                                      '1234567893', fetch=fetch)
    assert asked == [{'number': '1234567893'}]
    assert {(item['kind'], item['address'], item['state']) for item in filed} == {
        ('direct', 'intake@direct.regional-hospital.example.net', 'suggested'),
        ('fhir', 'https://fhir.regional-hospital.example.net/r4', 'suggested')}
    assert all(item['source'] == 'nppes' and 'NPPES record NPI 1234567893 lists this fax number' in item['evidence']
               for item in filed)
    assert sentence.startswith('NPPES lists these')
    planner = RoutePlanner(ruled.routes)
    values = ruled.configuration.read().active.values
    plan = planner.plan(to_number=number, bound='phaxio', values=values, pages=1, alternates=True)
    assert [choice.route.kind for choice in plan.choices] == ['provider']
    fhir_row = next(item for item in filed if item['kind'] == 'fhir')
    ruled.digital.record(fhir_row['id'], 'confirmed')
    plan = planner.plan(to_number=number, bound='phaxio', values=values, pages=1, alternates=True)
    assert plan.choices[0].route.key == 'fhir:' + fhir_row['id']
    # A record that does not list this fax number suggests nothing.
    other, sentence = reachability.suggest_from_nppes(ruled.digital, values, '+13035550199', '1234567893',
                                                      fetch=fetch)
    assert other == [] and 'does not list this fax number' in sentence
