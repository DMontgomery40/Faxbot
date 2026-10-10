"""Choosing the account after measuring its best pages (JOINT-OPTIMIZER-AUDIT steps 1-3, brief 84).

Every account, tariff, recipient limit and fax here is synthetic. The two SIP accounts and their rates are the
measured 12-candidate counterexample's (research joint-frontier-next): ``sip`` bills USD 0.005 a minute with a
60-second minimum, ``sip-pages`` bills USD 0.004 a physical page. They are invented for the arithmetic; they are
not any carrier's price. The recipient's limit (unlimited length) and the codings it accepts (MH, MR and MMR with
error correction) are declared here, not learned from a real machine. Nothing is dialed: a stand-in AMI records
the fax image each call would send. SQLite and PostgreSQL.
"""
import asyncio
from datetime import datetime
import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from PIL import Image, ImageDraw

from api.tests.test_rules_delivery import BASE, accept, installation, publish, rule
from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.app import conversion
from api.app.config_profiles import ConfigurationDocument, ProviderConfiguration
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import OutboundWorker
from api.app.pages import capability, coding, unpack
from api.app.routing.costs import RateCard
from api.app.routing.transport import RoutedTransport


NUMBER = '+12025550123'  # a valid US example number; nothing is dialed
NOW = datetime(2026, 10, 9, 12)
TRUNK = {**BASE, 'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_ROUTES': '', 'SIP_TRUNK_PRESET': 'telnyx',
         'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_CALLER_ID': '+13035550100', 'SIP_TRUNK_DIDS': '+13035550100'}
PAGES_ACCOUNT = {'provider': 'sip', 'label': 'Page trunk', 'sends': True,
                 'settings': {'preset': 'telnyx', 'auth': 'ip', 'host': '192.0.2.40', 'caller_id': '+13035550101'}}
MINUTE = RateCard(None, 'sip', 'outbound', 'Synthetic minute account', 'USD', 5000, 0, 0, 60, 60, None, NOW)
PAGE = RateCard(None, 'sip-pages', 'outbound', 'Synthetic page account', 'USD', 0, 4000, 0, 60, 60, None, NOW)


def counterexample_pages():
    """The counterexample's deterministic two-page bilevel document (1728 x 2156 at fine resolution)."""
    found = []
    for number in (1, 2):
        frame = Image.new('1', (1728, 2156), 1)
        frame.info['dpi'] = (204.0, 196.0)
        draw = ImageDraw.Draw(frame)
        draw.rectangle((100, 100, 1628, 200), outline=0, width=3)
        for row in range(14):
            start = 100 + 19 * ((row + number) % 7)
            draw.rectangle((start, 300 + row * 80, 1450 - row * 13, 305 + row * 80), fill=0)
        draw.rectangle((100, 1850, 100 + number * 100, 1900), fill=0)
        found.append(frame)
    return found


def declared_codings():
    """The recipient's declared codings: MH, MR and MMR with error correction (synthetic, not learned)."""
    return coding.usable_codings(ecm=True, far_ecm=True, configured='mmr',
                                 dis={'ecm': True, 'mr': True, 'mmr': True, 'jbig': False})


class Ami:
    """A connected stand-in for Asterisk: it records the fax image each call would send and dials nothing."""

    def __init__(self):
        self._connected = asyncio.Event()
        self._connected.set()
        self.calls = []

    async def originate_sendfax(self, job_id, dest, tiff_path, *, attempt_id=None, call=None, trunk=None):
        frames = conversion.read_fax_frames(tiff_path)
        self.calls.append({'job_id': job_id, 'to': dest, 'tiff': tiff_path, 'attempt_id': attempt_id,
                           'trunk': trunk, 'pages': len(frames), 'pixels': coding.digest(frames),
                           'sha256': hashlib.sha256(Path(tiff_path).read_bytes()).hexdigest()})


class _Frame:
    def __init__(self, revision):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def joint(database, tmp_path, monkeypatch):
    from api.app import config
    env = installation(database, tmp_path, TRUNK, outbound=ProviderConfiguration('sip', traits={'requires_tiff': True}))
    env.snapshot = env.configuration.apply(env.snapshot, env.snapshot.active.values, restart_required=False,
                                           actor='test', accounts=ConfigurationDocument({'sip-pages': PAGES_ACCOUNT}))
    env.routes.replace_cards([MINUTE, PAGE])
    # The receiving machine takes pages of any length with error correction (declared, as above).
    capability.records_for(database).record_observation(
        NUMBER, source='d' * 32, engine='hylafax', values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1}, now=NOW)
    monkeypatch.setattr('api.app.pages.sending._usable_codings', lambda *args, **kwargs: declared_codings())
    # Readiness of the second trunk reads the configuration in force: this installation's.
    monkeypatch.setattr(config, 'configuration_values', lambda: env.configuration.read().active.values)
    monkeypatch.setattr('api.app.sip_trunk.trunk_loaded', lambda values, key: True)
    env.ami = Ami()
    env.runtime = type('Runtime', (), {'frame': staticmethod(_Frame)})()
    return env


def write_document(env, job):
    """The fax's own image and PDF, as acceptance leaves them, with their hashes."""
    tiff, pdf = env.tmp / f'{job}.tiff', env.tmp / f'{job}.pdf'
    conversion.write_fax_tiff(counterexample_pages(), str(tiff))
    conversion.tiff_to_pdf(str(tiff), str(pdf))
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (tiff, pdf)}


def transport(env):
    return RoutedTransport(CapturedTransport(env.delivery, env.runtime, ami=env.ami), direct=None,
                           route_store=env.routes)


def page_change(env, attempt_id):
    table = sa.Table('fax_page_changes', sa.MetaData(), autoload_with=env.engine)
    with env.engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.attempt_id == attempt_id)).mappings().one_or_none()
    return dict(row) if row is not None else None


# M1: the account's own tariff reaches page preparation -------------------------------------------------------

@pytest.mark.asyncio
async def test_an_attempt_on_an_extra_account_prepares_its_pages_by_that_accounts_own_tariff(joint):
    """Regression (audit: pages/sending.py read ``configuration.provider_id``): a fax a rule sends by the extra
    account ``sip-pages`` had its pages chosen and recorded under the first trunk's per-minute card."""
    publish(joint, {'format': 1, 'routes': [rule('r-pages', {'use': 'sip-pages'})]})
    job = accept(joint, to=NUMBER, pages=2)
    originals = write_document(joint, job)
    assert await OutboundWorker(joint.delivery, transport(joint)).step() is True
    [call] = joint.ami.calls
    attempt = joint.delivery.get(job)['attempt_id']
    assert joint.routes.decision(attempt)['route'] == 'sip-pages'
    assert call['trunk'] == 'sip-pages' and call['pages'] == 1
    change = page_change(joint, attempt)
    # The page account bills by the page: the one long page was chosen and recorded under that tariff.
    assert (change['layout'], change['original_pages'], change['sent_pages'], change['billing']) == (
        'dense', 2, 1, 'per_page')
    # The fax's own files are unchanged, and the sent page splits back into them pixel for pixel.
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (joint.tmp / f'{job}.tiff', joint.tmp / f'{job}.pdf')} == originals
    recovered = unpack.split_frames(conversion.read_fax_frames(call['tiff']))
    assert [frame.tobytes() for frame in recovered] == [frame.tobytes() for frame in counterexample_pages()]


# M2: the accounts are compared after measuring, then one is bound ---------------------------------------------

def oracle(env, frames_by_layout):
    """Every allowed account x representation x lossless coding, priced on each account's own facts (the saved
    12-candidate counterexample's brute force): the cheapest known one, as (account, layout, coding, micros)."""
    from api.app.routing.pricing import account_facts
    from api.app.routing.predict import Shape, predict_from
    values = env.configuration.read().active.values
    usable = declared_codings()
    found = []
    for key in ('sip', 'sip-pages'):
        facts = account_facts(env.engine, values, key, NUMBER, provider='sip', now=NOW)
        for layout, frames in frames_by_layout.items():
            measured = coding.measure(frames, codings=usable.codings, check=True)
            for name in coding.CODINGS:
                if name not in usable.codings:
                    continue
                shape = Shape(len(frames), tuple(measured['MMR']), conversion.frames_resolution(frames),
                              layout, measured, name)
                prediction = predict_from(facts, shape)
                if prediction.cost is not None:
                    found.append((prediction.cost.micros, prediction.seconds, len(frames), key, layout, name))
    assert len(found) == 12 and {row[3] for row in found} == {'sip', 'sip-pages'}
    best = min(found)
    return {'account': best[3], 'layout': best[4], 'coding': best[5], 'micros': best[0]}


def selection_spy(monkeypatch):
    """What the route choice handed the transport for each attempt, read where the pages are published."""
    from api.app.pages import sending
    seen = []
    original = sending.prepare

    def spy(*args, **kwargs):
        seen.append(kwargs.get('handoff'))
        return original(*args, **kwargs)
    monkeypatch.setattr(sending, 'prepare', spy)
    return seen


@pytest.mark.asyncio
async def test_packing_reverses_the_route_winner_and_the_worker_binds_the_measured_cheapest_pair(joint, monkeypatch):
    """The saved counterexample through the real worker: by page count the minute account wins ($0.005 against
    $0.008 for two pages); measured, the page account sends one packed page for $0.004, and that is the account
    bound, the artifact published and the price kept with the attempt."""
    from api.app.pages import packing
    from api.app.routing.pricing import prices_for
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', {'cheapest_reliable': ['sip', 'sip-pages']})]})
    job = accept(joint, to=NUMBER, pages=2)
    originals = write_document(joint, job)
    values = joint.configuration.read().active.values
    by_count = prices_for(joint.routes, values, NUMBER, 2, keys=['sip', 'sip-pages'])
    assert (by_count['sip'].micros, by_count['sip-pages'].micros) == (5000, 8000)
    seen = selection_spy(monkeypatch)
    assert await OutboundWorker(joint.delivery, transport(joint)).step() is True
    [call] = joint.ami.calls
    attempt = joint.delivery.get(job)['attempt_id']
    decision = joint.routes.decision(attempt)
    assert (decision['route'], decision['route_reason']) == ('sip-pages', 'cheapest')
    assert call['trunk'] == 'sip-pages' and call['pages'] == 1
    [handoff] = seen
    selected = handoff.selected
    assert handoff.account == 'sip-pages' and selected.account.key == 'sip-pages'
    assert (selected.chosen.layout, selected.chosen.pages, selected.chosen.micros) == ('dense', 1, 4000)
    # The pages sent are exactly the pages priced.
    assert call['pixels'] == selected.chosen.digest
    # The same answer as the brute force over every allowed account x layout x coding.
    pages = counterexample_pages()
    best = oracle(joint, {'normal': pages, 'dense': packing.render(pages, packing.layout_for(pages, 'unlimited'))})
    assert (best['account'], best['layout'], best['micros']) == ('sip-pages', 'dense', 4000)
    assert selected.chosen.coding in declared_codings().codings
    # The fax's own files are unchanged.
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (joint.tmp / f'{job}.tiff', joint.tmp / f'{job}.pdf')} == originals
    # The attempt keeps the account, its pages and their price, recorded before the submission marker, tied to the
    # exact file the call sent; the runner-up is the minute account at its own best pages.
    from api.app.routing import selections
    record = selections.for_attempt(joint.engine, attempt)
    assert (record['account_key'], record['provider_id'], record['layout'], record['original_pages'],
            record['sent_pages'], record['expected_micros'], record['currency']) == (
        'sip-pages', 'sip', 'dense', 2, 1, 4000, 'USD')
    assert (record['runner_key'], record['runner_micros'], record['compared'], record['measured']) == (
        'sip', 5000, 2, True)
    assert record['artifact_sha256'] == call['sha256'] and record['pixels_sha256'] == call['pixels']
    assert record['source_sha256'] == originals[f'{job}.pdf']
    assert len(record['candidates']) >= 4 and sum(item['chosen'] for item in record['candidates']) == 1
    events = [event['kind'] for event in joint.delivery.history(job)]
    assert events.index('route_assigned') < events.index('submission_authorized')
    # Sent details and `faxbot sent show` say the same plan, in one sentence. On the minute account the long page
    # bills the same minute in less time, so that is its own best pages too.
    view = selections.sent_view(joint.engine, job, joint.configuration.read().active.values)
    assert view['sentence'] == ('Going through Page trunk as 1 long page: about $0.004 instead of $0.005 through '
                                'Telnyx as 1 long page.')


@pytest.mark.asyncio
async def test_without_packing_the_minute_account_keeps_the_fax(joint, monkeypatch):
    """Long pages forbidden by the rule: both accounts send two pages, and the minute account's $0.005 wins."""
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', {'cheapest_reliable': ['sip', 'sip-pages'],
                                                             'page_layout': 'one_per_sheet'})]})
    job = accept(joint, to=NUMBER, pages=2)
    write_document(joint, job)
    seen = selection_spy(monkeypatch)
    assert await OutboundWorker(joint.delivery, transport(joint)).step() is True
    [call] = joint.ami.calls
    attempt = joint.delivery.get(job)['attempt_id']
    assert joint.routes.decision(attempt)['route'] == 'sip'
    assert call['trunk'] is None and call['pages'] == 2
    [handoff] = seen
    assert handoff.account == 'sip' and (handoff.selected.chosen.layout, handoff.selected.chosen.micros) == (
        'normal', 5000)


def test_the_document_preview_names_the_same_account_and_pages_as_the_worker(joint, tmp_path):
    """POST /routing/predict's core, on the same document, rules, tariffs and recipient as the worker test: the
    page account with one long page at $0.004, the minute account as the runner-up, in one sentence."""
    from types import SimpleNamespace
    from uuid import uuid4
    from api.app.routing.predict_http import measured_plan
    from api.tests.test_rules_delivery import ANNE
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', {'cheapest_reliable': ['sip', 'sip-pages']})]})
    folder = tmp_path / 'preview'
    folder.mkdir()
    claim = SimpleNamespace(job_id=uuid4().hex, attempt_id=uuid4().hex, members=())
    tiff, pdf = folder / f'{claim.job_id}.tiff', folder / f'{claim.job_id}.pdf'
    conversion.write_fax_tiff(counterexample_pages(), str(tiff))
    conversion.tiff_to_pdf(str(tiff), str(pdf))
    revision = joint.configuration.read().active
    views, plan = measured_plan(joint.engine, revision, NUMBER, claim, pdf, tiff, 2, ANNE)
    selected, runner = plan['selected'], plan['runner_up']
    assert (selected['route'], selected['label'], selected['layout'], selected['sent_pages'], selected['cost']) == (
        'sip-pages', 'Page trunk', 'dense', 1, {'currency': 'USD', 'amount': '0.004'})
    assert (runner['route'], runner['cost']) == ('sip', {'currency': 'USD', 'amount': '0.005'})
    assert plan['sentence'] == ('Would go through Page trunk as 1 long page: about $0.004 instead of $0.005 through '
                                'Telnyx as 1 long page.')
    assert [view['route'] for view in views] == ['sip-pages', 'sip'] and plan['held'] is False
    # Nothing is written for a preview: no page change, coding choice or selection record.
    from api.app.routing import selections
    assert selections.for_attempt(joint.engine, claim.attempt_id) is None
    assert page_change(joint, claim.attempt_id) is None


# M1/M2: eligibility by country, and a learned rate where nothing is published (live-campaign dry run, 67d9ad22) --

UK, AU, JAMAICA, CANADA = '+441132000099', '+61255501234', '+18765550123', '+14165550123'
LIVE = {'FAX_BACKEND': 'humblefax', 'FAX_OUTBOUND_ROUTES': 'sip', 'FAX_DEFAULT_COUNTRY': 'US',
        'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'HUMBLEFAX_ACCESS_KEY': 'synthetic-access',
        'HUMBLEFAX_SECRET_KEY': 'synthetic-secret'}
HUMBLEFAX_PLAN = RateCard(None, 'humblefax', 'outbound', 'HumbleFax plan', 'USD', 0, 0, 0, 60, 0, None, NOW,
                          10_000_000)
TELNYX_US = RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', 5000, 0, 0, 60, 60, None, NOW)


@pytest.fixture
def live(database):
    from api.app.config_values import ConfigurationValues
    from api.app.routing.store import RouteStore
    from api.app.schema import upgrade_schema
    upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.replace_cards([HUMBLEFAX_PLAN, TELNYX_US])
    return routes, ConfigurationValues.from_environment(LIVE)


def carrier_record(engine, called, *, amount, seconds=60, at=datetime(2026, 10, 8, 15), trunk=None):
    """One priced Telnyx detail record for an outbound trunk call to ``called`` (synthetic)."""
    from uuid import uuid4
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=engine)
              for name in ('sip_call_records', 'carrier_charges')}
    call = uuid4().hex
    with engine.begin() as connection:
        connection.execute(tables['sip_call_records'].insert().values(
            id=call, direction='outbound', call_id=call, trunk_preset='telnyx', caller='+13035550100', called=called,
            started_at=at, answered_at=at, ended_at=at, disposition='answered', connected_seconds=seconds - 5,
            t38='yes', fax_preference=0, trunk_key=trunk, created_at=at, updated_at=at))
        connection.execute(tables['carrier_charges'].insert().values(
            id=uuid4().hex, call_record_id=call, provider_id='telnyx', record_id=uuid4().hex, version=1,
            amount_micros=amount, raw_amount=f'{amount / 1_000_000:.4f}', currency='USD', billed_seconds=seconds,
            call_seconds=seconds - 5, match_method='call_id', effective_at=at, observed_at=at, applied=1,
            is_final=1, created_at=at))


def plan_for(live, number):
    from api.app.routing.plan import RoutePlanner
    from api.app.routing.pricing import prices_for
    routes, values = live
    prices = prices_for(routes, values, number, 2, bound='humblefax')
    return RoutePlanner(routes).plan(to_number=number, bound='humblefax', values=values, pages=2, alternates=True,
                                     prices=prices), prices


@pytest.mark.parametrize('number', [UK, AU, JAMAICA])
def test_a_route_that_serves_only_the_us_and_canada_is_never_offered_elsewhere(live, number):
    """HumbleFax's terms limit it to the US and Canada: for a UK, Australian or Jamaican (+1, not the US or Canada)
    number it is ineligible, never ranked as an unknown price ahead of the trunk."""
    plan, prices = plan_for(live, number)
    assert prices['humblefax'].refused and prices['humblefax'].micros is None
    assert [choice.route.key for choice in plan.choices] == ['sip']
    assert ('humblefax', 'not_served') in plan.skipped
    from api.app.humblefax_service import humblefax_destination
    with pytest.raises(ValueError):
        humblefax_destination(number)
    from api.app.routing.holds import no_route_sentence
    assert no_route_sentence(lambda key: 'HumbleFax', [('humblefax', 'not_served')]) == (
        "No account your rules allow can send this fax now: HumbleFax does not send faxes to this number's country. "
        'It waits for you in Sent; nothing was sent.')


@pytest.mark.parametrize('number', [NUMBER, CANADA])
def test_the_us_and_canada_stay_eligible_for_that_route(live, number):
    plan, prices = plan_for(live, number)
    assert not prices['humblefax'].refused and prices['humblefax'].in_plan
    assert 'humblefax' in [choice.route.key for choice in plan.choices]
    from api.app.humblefax_service import humblefax_destination
    assert humblefax_destination(number) == int(number[1:])


def test_an_unknown_price_never_ranks_ahead_of_a_known_one():
    """Even a plan past its normal-use budget (a known price) goes before a route whose price is unknown."""
    from api.app.routing.policy import RouteCandidate, RoutePolicy
    from api.app.routing.pricing import Price
    plan_route = RouteCandidate('humblefax', 'provider', 'humblefax', HUMBLEFAX_PLAN, bound=True)
    trunk = RouteCandidate('sip', 'provider', 'sip', TELNYX_US)
    prices = {'humblefax': Price('humblefax', 0, 'USD', in_plan=True, over_budget=True, uses_budget=True),
              'sip': Price('sip', None, None)}
    order = RoutePolicy().order([plan_route, trunk], prices=prices, pages=2)
    assert [(choice.route.key, choice.reason) for choice in order] == [('humblefax', 'included'),
                                                                      ('sip', 'alternative')]


def test_a_trunk_with_no_published_price_abroad_is_priced_by_its_own_carrier_records(live):
    """Telnyx publishes no international fax price: the rate this trunk's carrier records showed for the UK
    ($0.0043 a minute) and for Australia ($0.0151 a minute) prices the call, said as learned and dated; a country
    with no records stays unknown, never free."""
    from api.app.routing.predict import Shape, predict_from
    from api.app.routing.predict_facts import facts_for
    routes, values = live
    engine = routes.engine
    carrier_record(engine, '+441132000000', amount=4300, at=datetime(2026, 10, 7, 9))
    carrier_record(engine, '+441132000001', amount=4300, at=datetime(2026, 10, 8, 15))
    carrier_record(engine, '+61255501230', amount=15100, at=datetime(2026, 10, 8, 16))
    carrier_record(engine, '+61255501231', amount=30200, seconds=120, at=datetime(2026, 10, 8, 17))
    shape = Shape(2, None, 'standard', 'normal')
    uk = facts_for('sip', UK, engine=engine, values=values, now=NOW)
    assert (uk.terms.card.per_minute_micros, uk.terms.card.billing_increment_seconds, uk.terms.published) == (
        4300, 60, False)
    found = predict_from(uk, shape)
    assert found.cost.micros == 4300
    assert found.basis.startswith('Billed as 1 minute at $0.0043 a minute, the rate 2 of your ')
    assert 'call records to numbers in the United Kingdom showed, the newest on 8 October 2026' in found.basis
    au = facts_for('sip', AU, engine=engine, values=values, now=NOW)
    assert au.terms.card.per_minute_micros == 15100
    assert 'to numbers in Australia showed, the newest on 8 October 2026' in predict_from(au, shape).basis
    japan = facts_for('sip', '+81312345678', engine=engine, values=values, now=NOW)
    assert japan.terms is None and predict_from(japan, shape).cost is None
    # A published (or saved) price for the country always comes before a learned one.
    from api.app.routing.predict_facts import shipped
    data = shipped()
    published = {**data, 'classes': {**data['classes'], 'international': [
        *data['classes']['international'],
        ('sip-telnyx', 'own', __import__('api.app.routing.costs', fromlist=['RateTerms']).RateTerms(
            RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx UK', 'USD', 7000, 0, 0, 60, 60, None, NOW),
            'international', ('+44',), published=True), ('+44',), {'route': 'sip-telnyx', 'prefixes': ['+44']})]}}
    assert facts_for('sip', UK, engine=engine, values=values, now=NOW, data=published).terms.card.per_minute_micros \
        == 7000
    # With learned prices the trunk ranks by money: it is the one eligible account for the UK, at a known price.
    plan, prices = plan_for(live, UK)
    assert prices['sip'].micros is not None and [choice.route.key for choice in plan.choices] == ['sip']
    assert plan.choices[0].reason in ('cheapest', 'configured', 'known_cheapest')
