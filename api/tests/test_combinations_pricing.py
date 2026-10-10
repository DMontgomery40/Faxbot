"""Combinations, group 2: encoded layout x account choice x per-page / per-minute / greater-of billing x recipient
capability x negotiated coding (brief 104).

The real codec, the real layout chooser and the real predictor price every candidate. Each account's tariff is its
own (``pages.sending.Account``: the contract the route choice and page preparation share); the account choice runs
through the real outbound worker on two configured SIP accounts. What applied is read back from the attempt's
records (``fax_page_changes``, ``codec_sends``, ``fax_coding_choices``, ``route_selections``).

The invariant:

- the chosen artifact is priced on the chosen account's own card;
- receiving costs count where both ends are ours;
- a recipient's capability and agreement are respected;
- no encoded page goes to a recipient that did not agree;
- every encoded case has an ordinary comparator on the same route.

The rates are synthetic, invented for the arithmetic; they are not any carrier's price. SQLite and PostgreSQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from api.app import codec, conversion
from api.app.codec.store import CodecSettings
from api.app.config_values import ConfigurationValues
from api.app.pages import capability, sending
from api.app.routing import predict
from api.app.routing.costs import RateCard, RateTerms, greater_of_pages, parse_amount
from api.app.routing.destinations import LOCAL, DestinationClass
from api.tests.combinations_support import (  # noqa: F401 - fixtures
    ATTEMPT, FULL_PAGE_POINTS, JOB, NOW, PEER, Case, applied, database, digests, installation, needs_gs, record,
    statement_pdf)
from api.tests.test_joint_optimizer import NUMBER as JOINT_NUMBER, PAGE, joint  # noqa: F401 - fixture

GROUP = '2 encoded x account x billing x capability x coding'
INVARIANT = ('priced on the chosen account\'s own card; receiving end counted when ours; agreement and capability '
             'respected; an ordinary comparator on the same account')
OWN_NUMBER = '+13035550100'  # one of this installation's own trunk numbers (synthetic)
ENVIRONMENT = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
               'SIP_TRUNK_CALLER_ID': OWN_NUMBER, 'SIP_TRUNK_DIDS': OWN_NUMBER, 'FAX_FRIENDLY_DOCUMENTS': 'never',
               'SIP_FAX_FINE': 'true', 'INBOUND_ENABLED': 'true'}
TARIFFS = {
    'per_page': RateTerms(RateCard(None, 'sip', 'outbound', 'Synthetic page account', 'USD', 0, parse_amount('0.03'),
                                   0, 60, 0, None, NOW)),
    'per_minute': RateTerms(RateCard(None, 'sip', 'outbound', 'Synthetic minute account', 'USD',
                                     parse_amount('0.005'), 0, 0, 6, 6, None, NOW)),
    # Pages counted as the greater of the pages sent and each started minute on the line (as Fax.Plus and
    # HumbleFax publish it), at the same page price.
    'greater_of': RateTerms(RateCard(None, 'sip', 'outbound', 'Synthetic greater-of account', 'USD', 0,
                                     parse_amount('0.03'), 0, 60, 0, None, NOW), page_time_seconds=60),
}
# Whole minutes with a one-minute minimum: a short call bills one minute whatever its pages.
WHOLE_MINUTES = RateTerms(RateCard(None, 'sip', 'outbound', 'Synthetic whole-minute account', 'USD',
                                   parse_amount('0.005'), 0, 0, 60, 60, None, NOW))


@pytest.fixture(scope='module')
def documents(tmp_path_factory):
    """Two synthetic documents, drawn once: a two-page statement shaded over the whole page (over three minutes a
    page on the line as an ordinary fax) and a two-page statement of plain text."""
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages, as acceptance does')
    folder = tmp_path_factory.mktemp('documents')
    found = {}
    for name, document in (('slow', statement_pdf(folder / 'slow.pdf', pages=2, box=FULL_PAGE_POINTS)),
                           ('text', statement_pdf(folder / 'text.pdf', pages=2, shaded=False))):
        conversion.pdf_to_tiff(str(document.pdf), str(folder / f'{name}.tiff'))
        found[name] = SimpleNamespace(document=document, tiff=folder / f'{name}.tiff')
    return found


def _filled(table, values):
    filler = {sa.DateTime: NOW, sa.Integer: 1}
    return {**{column.name: next((value for kind, value in filler.items() if isinstance(column.type, kind)), 'x')
               for column in table.c if not column.nullable and column.server_default is None
               and column.name not in values}, **values}


def fax_rows(engine, number, pages):
    """The fax and its attempt, as acceptance and the claim leave them (the codec's record names the fax)."""
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=engine)
    attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(**_filled(jobs, {
            'id': JOB, 'to_number': number, 'status': 'queued', 'created_at': NOW, 'updated_at': NOW,
            'pages': pages, 'backend': 'sip'})))
        connection.execute(attempts.insert().values(**_filled(attempts, {
            'id': ATTEMPT, 'job_id': JOB, 'sequence': 1, 'phase': 'prepared', 'created_at': NOW})))


def agree(engine, *numbers):
    for number in numbers:
        CodecSettings(engine).save(number, enabled=True, recipient_agreed=True, actor='principal:synthetic', now=NOW)


def tariff_terms(tariff):
    return WHOLE_MINUTES if tariff == 'whole_minutes' else TARIFFS[tariff]


@dataclass
class Priced:
    account: object
    evaluated: object
    prepared: object
    applied: object
    before: dict
    after: dict
    pdf: Path


def price(engine, folder, drawn, *, tariff='per_page', number=PEER, agreed=(PEER,), ecm=1, codings=None,
          negotiated=None, environment=None, recipient=None, provider_tariff=None, key='sip'):
    """One attempt of ``drawn`` to ``number`` on a SIP account priced by ``TARIFFS[tariff]``: every way it may go is
    measured and priced on that account's own facts (``sending.evaluate``), then the chosen pages are published.

    The shared predictor (what ``predict.predict`` reads for the provider, as ``predict_facts.facts_for`` reads the
    first trunk's card) is the account's own tariff, as on an installation with one trunk; ``provider_tariff`` makes
    it another account's, as when the attempt is bound to an extra trunk account."""
    folder = Path(folder)
    folder.mkdir(exist_ok=True)
    pdf, tiff = folder / f'{JOB}.pdf', folder / f'{JOB}.tiff'
    shutil.copyfile(drawn.document.pdf, pdf)
    shutil.copyfile(drawn.tiff, tiff)
    fax_rows(engine, number, drawn.document.pages)
    observed = {'max_length': 'a4', 'ecm': ecm, 'fine': 1}
    if codings is not None:
        observed['codings'] = ','.join(codings)
    capability.records_for(engine).record_observation(number, source='d' * 32, engine='hylafax', values=observed,
                                                      now=NOW)
    if negotiated is not None:
        from api.app import hylafax_records
        earlier = 'e' * 32
        jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(jobs.insert().values(**_filled(jobs, {
                'id': earlier, 'to_number': number, 'status': 'success', 'backend': 'sip', 'pages': 1})))
        hylafax_records.records_for(engine).record_negotiation(
            direction='outbound', call_key='synthetic-earlier-call', engine='hylafax', job_id=earlier, number=number,
            values=negotiated)
    agree(engine, *agreed)
    terms = tariff_terms(tariff)
    facts = predict.RouteFacts('sip', terms.card.label, DestinationClass(LOCAL, 'US', '+1', number), terms)
    account = sending.Account(key, 'sip', 'image', card=terms.card, facts=facts)
    values = ConfigurationValues.from_environment({**ENVIRONMENT, 'FAX_DATA_DIR': str(folder),
                                                   **(environment or {})})
    claim = SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=())
    job = {'to_number': number, **({'recipient_number': recipient} if recipient else {})}
    before = digests(pdf, tiff)
    shared = terms if provider_tariff is None else tariff_terms(provider_tariff)
    shared_facts = predict.RouteFacts('sip', shared.card.label, DestinationClass(LOCAL, 'US', '+1', number), shared)
    with predict.facts_source(lambda route, destination, now=None: shared_facts):
        evaluated = sending.evaluate(engine, values, account, claim, job, pdf, tiff, now=NOW)
    prepared = sending.publish(engine, evaluated, claim, pdf, tiff, now=NOW) if evaluated is not None else None
    return Priced(account, evaluated, prepared, applied(engine, JOB, ATTEMPT), before, digests(pdf, tiff), pdf)


def option(evaluated, layout):
    return next((item for item in evaluated.options if item.layout == layout and item.rendering == 'as_is'), None)


def assert_invariant(priced, *, encoded_expected):
    evaluated = priced.evaluated
    account = priced.account
    # An ordinary comparator on the same account, always.
    normal = option(evaluated, 'normal')
    assert normal is not None and normal.account == account.key
    encoded = option(evaluated, 'codec')
    if encoded is not None:
        assert encoded.account == account.key
    # The chosen pages are priced on this account's own facts: the predictor gives the same price again.
    again = predict.predict_from(account.facts, evaluated.shape)
    assert evaluated.chosen.micros == (again.cost.micros if again.cost is not None else None)
    assert again.cost is None or again.cost.currency == account.card.currency
    # And they are the cheapest of the account's candidates (the most faithful at a tie).
    known = [item.micros for item in evaluated.options if item.micros is not None]
    assert evaluated.chosen.micros == min(known)
    # What went is what was chosen, read back from the records.
    found = priced.applied
    assert ('codec' in found.mechanisms) == (evaluated.chosen.layout == 'codec') == encoded_expected
    if encoded_expected:
        assert found.change['billing'] == sending._billing(account.card)
        assert found.change['sent_pages'] == evaluated.chosen.pages == len(conversion.read_fax_frames(
            priced.prepared.tiff))
        # Encoded pages decode back to exactly the fax's own PDF.
        document, _ = codec.decode_images(conversion.read_fax_frames(priced.prepared.tiff))
        assert document.data == priced.pdf.read_bytes()
        assert found.codec is not None and found.codec['provider_id'] == account.provider_id
        # Encoded pages are never packed, trimmed or shaded on top.
        assert found.mechanisms == {'codec'} and found.friendly is None
    # The coding requested is one the call may use.
    if found.coding is not None and evaluated.permission.usable is not None:
        assert found.coding['requested'] in evaluated.permission.usable.codings
    # The fax's own files never change.
    assert priced.after == priced.before


@needs_gs
@pytest.mark.parametrize('tariff', sorted(TARIFFS))
def test_encoded_pages_and_their_ordinary_comparator_are_priced_on_the_accounts_own_tariff(installation, tmp_path,
                                                                                         documents, tariff):
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff=tariff)
    assert_invariant(priced, encoded_expected=True)
    normal, encoded = option(priced.evaluated, 'normal'), option(priced.evaluated, 'codec')
    if tariff == 'greater_of':
        # Each started minute on the line is a page too: the two slow pages bill as more than two.
        assert normal.billed_pages >= greater_of_pages(2, normal.seconds, 60) > 2
        assert encoded.billed_pages == greater_of_pages(encoded.pages, encoded.seconds, 60)
    elif tariff == 'per_page':
        assert (normal.billed_pages, encoded.billed_pages) == (2, encoded.pages)
    else:
        assert normal.billed_seconds > encoded.billed_seconds
    record(Case(GROUP, f'encoded on a {tariff} account', ('codec', tariff), tuple(sorted(priced.applied.mechanisms)),
                INVARIANT))


@needs_gs
@pytest.mark.parametrize('tariff, expected', [('per_page', 'codec'), ('per_minute', 'codec'),
                                              ('whole_minutes', 'normal')])
def test_the_accounts_own_bill_decides_whether_a_short_fax_goes_encoded(installation, tmp_path, documents, tariff,
                                                                        expected):
    """A two-page text fax: one encoded page saves a page on a page price and a few seconds on a price by the
    second. On whole minutes both bill the same minute, so the codec's own check (it must cost less) keeps the
    ordinary pages: encoded pages are not even a candidate there."""
    priced = price(installation, tmp_path / 'fax', documents['text'], tariff=tariff)
    assert priced.evaluated.chosen.layout == expected
    assert (option(priced.evaluated, 'codec') is not None) == (expected == 'codec')
    assert_invariant(priced, encoded_expected=expected == 'codec')
    record(Case(GROUP, f'two-page text fax on a {tariff} account', ('codec', tariff),
                tuple(sorted(priced.applied.mechanisms)) or ('ordinary',), INVARIANT))


@needs_gs
def test_no_encoded_page_goes_to_a_recipient_that_did_not_agree(installation, tmp_path, documents):
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff='per_page', agreed=())
    assert option(priced.evaluated, 'codec') is None  # nothing encoded is even made
    assert_invariant(priced, encoded_expected=False)
    assert priced.applied.codec is None and 'codec' not in priced.applied.mechanisms
    record(Case(GROUP, 'recipient did not agree', ('codec',), tuple(sorted(priced.applied.mechanisms)), INVARIANT,
                'impossible', alternate='ordinary pages on the same account, priced on its own card'))


@needs_gs
@pytest.mark.parametrize('agreed', ['dialed only', 'recipient only'])
def test_an_approved_alternate_number_gets_encoded_pages_only_when_both_numbers_agreed(installation, tmp_path,
                                                                                      documents, agreed):
    toll_free = '+18005550199'
    numbers = (toll_free,) if agreed == 'dialed only' else (PEER,)
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff='per_page', number=toll_free,
                   recipient=PEER, agreed=numbers)
    assert option(priced.evaluated, 'codec') is None
    assert_invariant(priced, encoded_expected=False)
    record(Case(GROUP, f'alternate number, {agreed} agreed', ('codec', 'alternate'),
                tuple(sorted(priced.applied.mechanisms)), INVARIANT, 'impossible',
                alternate='ordinary pages to the dialed number'))


@needs_gs
@pytest.mark.parametrize('seen', [None, {'ecm': 'on', 'resolution': 'fine'}, {'ecm': 'off', 'resolution': 'fine'}],
                         ids=['nothing negotiated', 'ecm at fine', 'no ecm'])
def test_run_coded_pages_only_after_a_call_to_the_number_negotiated_ecm_at_fine(installation, tmp_path, documents,
                                                                                seen):
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff='per_page', negotiated=seen)
    assert_invariant(priced, encoded_expected=True)
    layout = priced.applied.codec['layout']
    if seen is None or seen['ecm'] != 'on':
        assert layout == 'grid'  # the sturdy layout: the receiving machine's error correction is not on record
    else:
        assert layout in ('grid', 'runs')  # run-coded pages became a candidate; the bill decides
    record(Case(GROUP, f'recipient capability: {"ecm at fine" if seen and seen["ecm"] == "on" else "not seen"}',
                ('codec',), (f'codec:{layout}',), INVARIANT))


@needs_gs
@pytest.mark.parametrize('declared', [('MH',), ('MH', 'MR'), ('MH', 'MR', 'MMR')], ids='+'.join)
def test_every_candidate_is_priced_and_requested_in_a_coding_the_receiving_machine_declared(installation, tmp_path,
                                                                                            documents, declared):
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff='per_minute', codings=declared)
    usable = priced.evaluated.permission.usable
    assert usable is not None and usable.codings <= set(declared) | {'MH'}
    assert all(item.coding in usable.codings for item in priced.evaluated.options)
    assert priced.applied.coding is not None and priced.applied.coding['requested'] in declared
    assert_invariant(priced, encoded_expected=priced.evaluated.chosen.layout == 'codec')
    record(Case(GROUP, f'recipient declared {"+".join(declared)}', ('codec', 'coding'),
                tuple(sorted(priced.applied.mechanisms | {f'coding:{priced.applied.coding["requested"]}'})),
                INVARIANT))


@needs_gs
@pytest.mark.parametrize('number', [OWN_NUMBER, PEER], ids=['both ends ours', 'someone else'])
def test_the_receiving_end_is_counted_when_both_ends_are_ours(installation, tmp_path, documents, number):
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff='per_minute', number=number,
                   agreed=(number,))
    assert_invariant(priced, encoded_expected=True)
    values = ConfigurationValues.from_environment({**ENVIRONMENT, 'FAX_DATA_DIR': str(tmp_path / 'fax')})
    leg = predict.receiving_leg(number, engine=installation, values=values)
    basis = priced.applied.codec['basis']
    if number == OWN_NUMBER:
        assert leg.known
        assert 'receiving call on your Telnyx trunk adds about' in basis
        # The choice counted both ends: the encoded pages cost least with the receiving trunk's bill added.
        totals = {item.layout: item.micros + leg.cost(item.seconds).micros
                  for item in priced.evaluated.options if item.micros is not None}
        assert totals[priced.evaluated.chosen.layout] == min(totals.values())
    else:
        assert not leg.known
        assert basis.startswith('What the receiving end pays for the call is unknown.')
    record(Case(GROUP, f'receiving end: {"ours" if number == OWN_NUMBER else "not ours"}', ('codec', 'receiving'),
                tuple(sorted(priced.applied.mechanisms)), INVARIANT))


@needs_gs
def test_encoded_pages_are_never_packed_trimmed_or_shaded_whatever_the_page_settings_say(installation, tmp_path,
                                                                                       documents):
    records = capability.records_for(installation)
    records.set_route_settings('sip', long_pages=True, trim_blank=True)
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff='per_page', ecm=0,
                   environment={'FAX_FRIENDLY_DOCUMENTS': 'always'})
    assert_invariant(priced, encoded_expected=True)
    assert priced.applied.change['trimmed_pages'] is None and priced.applied.friendly is None
    record(Case(GROUP, 'encoded with packing, trimming and shading on', ('codec', 'dense', 'screened', 'trim'),
                tuple(sorted(priced.applied.mechanisms)), INVARIANT, 'impossible',
                alternate='the encoded pages go exactly as the codec made them'))


@needs_gs
@pytest.mark.parametrize('tariff', sorted(TARIFFS))
def test_the_encoded_pages_record_keeps_the_price_they_were_chosen_at(installation, tmp_path, documents, tariff):
    """The codec's record of an encoded send (``codec_sends``) keeps the costs it compared: on an installation with
    one trunk account, the price the attempt was chosen at."""
    priced = price(installation, tmp_path / 'fax', documents['slow'], tariff=tariff)
    assert_invariant(priced, encoded_expected=True)
    row = priced.applied.codec
    normal, encoded = option(priced.evaluated, 'normal'), option(priced.evaluated, 'codec')
    assert (row['cost_original_micros'], row['cost_encoded_micros']) == (normal.micros, encoded.micros)


@needs_gs
@pytest.mark.xfail(strict=True, reason='defect C1: the codec\'s own check prices encoded pages on the provider\'s '
                                       'shared tariff, not the bound account\'s own')
def test_an_extra_account_gets_encoded_pages_when_its_own_tariff_makes_them_cheapest(installation, tmp_path,
                                                                                   documents):
    """An attempt bound to an extra trunk account that bills by the page, while the provider's first trunk bills
    whole minutes. On the page account one encoded page costs half of two ordinary ones, so the encoded page should
    be a candidate and go. The codec's own check (``codec/decision.py`` ``choose`` through ``predictor()``) prices
    it on the first trunk's whole minutes instead, where it saves nothing, so it is never offered; its record would
    also keep that other tariff's costs."""
    priced = price(installation, tmp_path / 'fax', documents['text'], tariff='per_page',
                   provider_tariff='whole_minutes', key='sip-pages')
    assert option(priced.evaluated, 'codec') is not None
    assert priced.evaluated.chosen.layout == 'codec'
    assert_invariant(priced, encoded_expected=True)


# Account choice: the real worker on two configured SIP accounts ------------------------------------------------

FIRST_BY_PAGE = RateCard(None, 'sip', 'outbound', 'Synthetic first trunk', 'USD', 0, 6000, 0, 60, 0, None, NOW)


def worker_fax(joint, documents, name='text'):  # noqa: F811 - the fixture
    """Accept a fax to the joint fixture's number and leave its files as acceptance does (the synthetic statement)."""
    from api.tests.test_rules_delivery import accept
    drawn = documents[name]
    job = accept(joint, to=JOINT_NUMBER, pages=drawn.document.pages)
    shutil.copyfile(drawn.document.pdf, joint.tmp / f'{job}.pdf')
    shutil.copyfile(drawn.tiff, joint.tmp / f'{job}.tiff')
    return job, digests(joint.tmp / f'{job}.pdf', joint.tmp / f'{job}.tiff')


async def send_through_worker(joint, monkeypatch, job):  # noqa: F811 - the fixture
    """One worker step through the real route choice, page preparation and stand-in AMI. The shared predictor reads
    the first trunk's own facts, as ``predict_facts.facts_for('sip', ...)`` does on the installation's database.
    Returns (the call, the handoff, the attempt)."""
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing.pricing import account_facts
    from api.tests.test_joint_optimizer import selection_spy, transport
    values = joint.configuration.read().active.values
    first = account_facts(joint.engine, values, 'sip', JOINT_NUMBER, provider='sip', now=NOW)
    seen = selection_spy(monkeypatch)
    with predict.facts_source(lambda route, destination, now=None: first):
        assert await OutboundWorker(joint.delivery, transport(joint)).step() is True
    [call] = joint.ami.calls
    [handoff] = seen
    return call, handoff, joint.delivery.get(job)['attempt_id']


def assert_bound_account_priced_its_own_pages(joint, call, handoff, attempt, originals, job):  # noqa: F811
    from api.app.routing import selections
    from api.app.routing.pricing import account_facts
    values = joint.configuration.read().active.values
    selected = handoff.selected
    key = handoff.account
    assert joint.routes.decision(attempt)['route'] == key == selected.account.key
    # Priced on the bound account's own card: its own facts give the chosen pages the same price again.
    own = account_facts(joint.engine, values, key, JOINT_NUMBER, provider='sip', now=NOW)
    assert predict.predict_from(own, selected.shape).cost.micros == selected.chosen.micros
    found = selections.for_attempt(joint.engine, attempt)
    assert (found['account_key'], found['expected_micros'], found['layout']) == (
        key, selected.chosen.micros, selected.chosen.layout)
    # An ordinary comparator on the same account was priced beside whatever went.
    assert any(item['account'] == key and item['layout'] == 'normal' and item['rendering'] == 'as_is'
               for item in found['candidates'])
    # What the call sent is exactly what was priced, and the fax's own files are unchanged.
    assert call['pixels'] == selected.chosen.digest
    if selected.changes:
        assert call['sha256'] == found['artifact_sha256']
    else:
        # The pages as they are: the call sends the fax's own image, and no file of the attempt's own is made.
        assert found['artifact_sha256'] is None and call['sha256'] == originals[f'{job}.tiff']
    assert digests(joint.tmp / f'{job}.pdf', joint.tmp / f'{job}.tiff') == originals
    return found


ONE_PER_SHEET = {'cheapest_reliable': ['sip', 'sip-pages'], 'page_layout': 'one_per_sheet'}


@needs_gs
@pytest.mark.asyncio
async def test_the_worker_binds_the_account_whose_own_card_makes_the_encoded_page_cheapest(joint, monkeypatch,
                                                                                         documents):
    """Both trunk accounts bill by the page (USD 0.006 and USD 0.004, synthetic); the recipient agreed to encoded
    pages; packing is off by the rule. Each account prices one encoded page and the two ordinary pages on its own
    card; the page account's encoded page is the cheapest, and that is the account bound, the page sent and the
    price kept with the attempt."""
    from api.tests.test_rules_delivery import publish, rule
    joint.routes.replace_cards([FIRST_BY_PAGE, PAGE])
    agree(joint.engine, JOINT_NUMBER)
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', ONE_PER_SHEET)]})
    job, originals = worker_fax(joint, documents)
    call, handoff, attempt = await send_through_worker(joint, monkeypatch, job)
    found = assert_bound_account_priced_its_own_pages(joint, call, handoff, attempt, originals, job)
    assert (found['account_key'], found['layout'], found['expected_micros']) == ('sip-pages', 'codec', 4000)
    assert call['trunk'] == 'sip-pages' and call['pages'] == 1
    document, _ = codec.decode_images(conversion.read_fax_frames(call['tiff']))
    assert document.data == (joint.tmp / f'{job}.pdf').read_bytes()
    assert applied(joint.engine, job, attempt).mechanisms == {'codec'}
    record(Case(GROUP, 'account choice x encoded (worker)', ('codec', 'sip', 'sip-pages', 'per_page'),
                ('codec', 'sip-pages'), INVARIANT))


@needs_gs
@pytest.mark.asyncio
async def test_no_encoded_page_goes_through_the_worker_when_the_recipient_did_not_agree(joint, monkeypatch,
                                                                                      documents):
    from api.tests.test_rules_delivery import publish, rule
    joint.routes.replace_cards([FIRST_BY_PAGE, PAGE])
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', ONE_PER_SHEET)]})
    job, originals = worker_fax(joint, documents)
    call, handoff, attempt = await send_through_worker(joint, monkeypatch, job)
    found = assert_bound_account_priced_its_own_pages(joint, call, handoff, attempt, originals, job)
    assert not any(item['layout'] == 'codec' for item in found['candidates'])
    assert (found['account_key'], found['layout'], found['expected_micros']) == ('sip-pages', 'normal', 8000)
    assert 'codec' not in applied(joint.engine, job, attempt).mechanisms
    record(Case(GROUP, 'account choice, recipient did not agree (worker)', ('codec', 'sip', 'sip-pages'),
                ('ordinary', 'sip-pages'), INVARIANT, 'impossible',
                alternate='ordinary pages on the account whose own card is cheapest'))


@needs_gs
@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="defect C1: the codec's own check prices encoded pages on the provider's "
                                       "shared tariff, not the bound account's own")
async def test_the_worker_offers_the_page_account_its_encoded_page_when_the_first_trunk_bills_whole_minutes(
        joint, monkeypatch, documents):
    """The joint fixture's own tariffs: the first trunk bills whole minutes (USD 0.005, 60-second minimum), the page
    account USD 0.004 a page; packing is off. One encoded page on the page account (USD 0.004) is the cheapest way to
    send the two pages; the first trunk sends them for USD 0.005 in one minute. The codec's own check runs on the
    first trunk's whole minutes for both accounts, finds no saving there, and the encoded page is never offered."""
    from api.tests.test_rules_delivery import publish, rule
    agree(joint.engine, JOINT_NUMBER)
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', ONE_PER_SHEET)]})
    job, originals = worker_fax(joint, documents)
    call, handoff, attempt = await send_through_worker(joint, monkeypatch, job)
    found = assert_bound_account_priced_its_own_pages(joint, call, handoff, attempt, originals, job)
    assert (found['account_key'], found['layout'], found['expected_micros']) == ('sip-pages', 'codec', 4000)
