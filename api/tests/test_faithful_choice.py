"""The most faithful pages at the same expected bill (pages/decision.choose inside conversion.choose_layout).

Each attempt's candidates are the pages as they are, the screened pages (shading kept with a pattern) and, only with
the administrator's opt-in, the whitened pages, each in every layout and with its own coding. The cheapest expected
bill wins; candidates within 0.05 of a billing step of it cost the same, and among those the most faithful wins (the
pages as they are, then screened, then whitened). A faster candidate wins at the same bill only for a named reason:
the administrator's choice (Always), a send-by time within the hour, or faxes waiting for the phone line. Prices
come from the real shared predictor (routing/predict.py) on synthetic facts, never from a stand-in. All synthetic.
"""
from datetime import timedelta
import shutil
from types import SimpleNamespace

import pytest

from api.tests.test_fax_friendly import (ATTEMPT, CARDS, JOB, NOW, PEER, SINCH, TRUNK, attempt, billed,  # noqa: F401
                                         installation, needs_gs)
from api.tests.test_fax_screens import FIXTURES, render
from api.tests.test_schema import database  # noqa: F401 - fixture
from app import conversion
from app.pages import decision, fidelity, friendly, screens, sending
from app.routing import predict
from app.routing.costs import Money, RateCard, RateTerms, parse_amount
from app.routing.destinations import LOCAL, DestinationClass


def candidate(rendering, layout, *, steps=None, pages=None, cost=None, seconds=60.0, faithful=None):
    prediction = decision.Prediction(pages, seconds, Money(cost, 'USD') if cost is not None else None, 'per_minute',
                                     False)
    rank = faithful if faithful is not None else {
        'as_is': fidelity.UNCHANGED.rank, 'screened': (False, 0, 0.01), 'whitened': (True, 2, 0.02)}[rendering]
    return decision.Candidate(rendering, layout, prediction, decision.Shape(1, None, 'fine', layout), rank,
                              decision.Bill(cost, steps, pages))


# The rule, on synthetic bills ----------------------------------------------------------------------------------

def test_at_the_same_bill_the_pages_as_they_are_beat_screened_and_screened_beat_whitened():
    as_is = candidate('as_is', 'normal', steps=1.0, cost=7000, seconds=55)
    screened = candidate('screened', 'normal', steps=1.0, cost=7000, seconds=40)
    whitened = candidate('whitened', 'normal', steps=1.0, cost=7000, seconds=20)
    assert decision.choose([whitened, screened, as_is]) == (as_is, False)
    assert decision.choose([whitened, screened]) == (screened, False)


def test_a_cheaper_bill_wins_whatever_its_fidelity():
    as_is = candidate('as_is', 'normal', steps=2.0, cost=14000, seconds=95)
    screened = candidate('screened', 'normal', steps=1.0, cost=7000, seconds=50)
    whitened = candidate('whitened', 'normal', steps=1.0, cost=7000, seconds=25)
    assert decision.choose([as_is, screened, whitened]) == (screened, False)
    cheapest = candidate('whitened', 'normal', steps=1.0, cost=7000, seconds=25)
    dearer = candidate('screened', 'normal', steps=2.0, cost=14000, seconds=70)
    assert decision.choose([dearer, cheapest])[0] is cheapest


def test_the_same_bill_means_within_five_hundredths_of_a_billing_step():
    as_is = candidate('as_is', 'normal', steps=1.04, seconds=58)
    screened = candidate('screened', 'normal', steps=1.0, seconds=40)
    assert decision.choose([as_is, screened])[0] is as_is  # under a 5% chance of one more billed minute
    as_is = candidate('as_is', 'normal', steps=1.06, seconds=59)
    assert decision.choose([as_is, screened])[0] is screened
    # Pages at a page price count the same way, and priced never ties with unpriced.
    assert decision.same_bill(decision.Bill(30000, None, 1.0), decision.Bill(30000, None, 1.0))
    assert not decision.same_bill(decision.Bill(None, 1.0, None), decision.Bill(7000, 1.0, None))


@pytest.mark.parametrize('reason', decision.NAMED_REASONS)
def test_a_named_reason_takes_the_faster_pages_only_at_the_same_bill(reason):
    as_is = candidate('as_is', 'normal', steps=1.0, seconds=55)
    screened = candidate('screened', 'normal', steps=1.0, seconds=40)
    whitened = candidate('whitened', 'normal', steps=1.0, seconds=20)
    assert decision.choose([as_is, screened], faster=reason) == (screened, True)
    assert decision.choose([as_is, screened, whitened], faster=reason) == (whitened, True)
    dearer = candidate('screened', 'normal', steps=2.0, seconds=30)
    assert decision.choose([as_is, dearer], faster=reason) == (as_is, False)  # never at a higher bill
    with pytest.raises(ValueError):
        decision.choose([as_is], faster='the line was ecm-less')


# The real predictor's expected bill ------------------------------------------------------------------------------

def card(provider, *, page='0', minute='0', increment=60):
    return RateCard(None, provider, 'outbound', provider, 'USD', parse_amount(minute), parse_amount(page),
                    parse_amount('0'), increment, 0, None, NOW, None)


def facts(route, rates, *, rate=None, page_time=None):
    link = predict.Link(rate=rate, rate_calls=5, rate_scope='number') if rate else predict.Link()
    return predict.RouteFacts(route, route.title(), DestinationClass(LOCAL, 'US', '+1', PEER),
                              RateTerms(rates, page_time_seconds=page_time), link)


def test_the_bill_reads_the_shared_predictors_expected_billed_seconds_and_pages():
    shape = predict.Shape(1, (873_336,), 'fine', 'normal')
    minute = predict.predict_from(facts('sip', card('sip', minute='0.007')), shape)
    found = decision.bill(minute, route='sip')
    assert found.steps == pytest.approx(minute.expected_billed_seconds / 60) and found.steps > 1
    assert found.cost == minute.cost.micros and found.pages == 0.0
    timed = predict.predict_from(facts('faxplus', card('faxplus', page='0.10'), page_time=60), shape)
    assert decision.bill(timed, route='faxplus').pages == pytest.approx(timed.expected_billed_pages)
    # A phone line the predictor has no price for is still billed by time: compared in whole minutes.
    unpriced = predict.predict_from(predict.RouteFacts('sip', 'Sip', DestinationClass(LOCAL, 'US', '+1', PEER), None),
                                    shape)
    assert unpriced.cost is None and decision.bill(unpriced, route='sip').steps == 2.0


@pytest.fixture(scope='module')
def table(tmp_path_factory):
    """Codex's shaded table as it is, screened and whitened: (frames, renderings)."""
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages')
    folder = tmp_path_factory.mktemp('table')
    gray, today = render(FIXTURES / 'shaded_table.pdf', folder)
    screened = screens.screen_page(gray, today)
    whitened = friendly.friendly_page(gray, today).page
    return [today], {'screened': ([screened.page], fidelity.assess(gray, today, screened.page,
                                                                   frozen=screened.frozen)),
                     'whitened': ([whitened], fidelity.assess(gray, today, whitened))}


def choose(frames, renderings, route, source, **kwargs):
    with predict.facts_source(lambda route_key, destination, now=None: source):
        return conversion.choose_layout(frames, route=route, destination=PEER, limit='a4', dense_allowed=False,
                                        renderings=renderings, **kwargs)


def test_with_the_real_predictor_screened_pages_win_a_billed_minute_and_beat_whitened_in_it(table):
    frames, renderings = table
    trunk = facts('sip', card('sip', minute='0.007'))
    chosen = choose(frames, renderings, 'sip', trunk)
    # As they are about 95 seconds (2 minutes); screened about 50 and whitened about 25: both 1 minute.
    assert chosen['rendering'] == 'screened' and chosen['faster'] is None and chosen['seconds_saved'] >= 30
    assert choose(frames, {'screened': renderings['screened']}, 'sip', trunk)['rendering'] == 'screened'
    # The administrator's choice (or a send-by time, or a busy line) takes the faster pages at that bill.
    assert choose(frames, renderings, 'sip', trunk, faster='administrator')['rendering'] == 'whitened'
    # On a slow line whitening reaches a cheaper minute than the screen: then, and only with the opt-in, it goes.
    slow = facts('sip', card('sip', minute='0.007'), rate=7200)
    assert choose(frames, renderings, 'sip', slow)['rendering'] == 'whitened'
    assert choose(frames, {'screened': renderings['screened']}, 'sip', slow)['rendering'] == 'screened'


def test_with_the_real_predictor_a_per_page_route_keeps_the_pages_as_they_are(table):
    frames, renderings = table
    chosen = choose(frames, renderings, 'sinch', facts('sinch', card('sinch', page='0.045')))
    assert chosen['rendering'] is None and chosen['layout'] == 'normal'
    # A page-or-time route bills a long page as more than one: the screen can save a billed page there.
    timed = choose(frames, renderings, 'faxplus', facts('faxplus', card('faxplus', page='0.10'), page_time=60,
                                                        rate=7200))
    assert timed['rendering'] in ('screened', 'whitened')


# The opt-in, the named reasons and the attempt -------------------------------------------------------------------

def test_whitening_needs_the_opt_in_and_never_applies_under_never():
    assert not friendly.whiten_allowed(SimpleNamespace(fax_friendly_documents='where_it_saves'))
    assert friendly.whiten_allowed(SimpleNamespace(fax_friendly_documents='where_it_saves', fax_friendly_whiten=True))
    assert friendly.whiten_allowed(SimpleNamespace(fax_friendly_documents='always', fax_friendly_whiten=True))
    assert not friendly.whiten_allowed(SimpleNamespace(fax_friendly_documents='never', fax_friendly_whiten=True))
    assert not friendly.whiten_allowed(SimpleNamespace(fax_friendly_documents='always', fax_friendly_whiten='true'))
    from app.config_values import ConfigurationValues
    assert ConfigurationValues.model_fields['fax_friendly_whiten'].default is False
    assert ConfigurationValues(FAX_FRIENDLY_WHITEN='true').fax_friendly_whiten is True


@needs_gs
def test_whitened_pages_are_made_only_with_the_opt_in_and_lose_to_screened_at_the_same_bill(installation, tmp_path,
                                                                                         billed):
    chosen = attempt(installation, tmp_path, TRUNK)
    assert not list(tmp_path.glob(f'{friendly.CACHE}*-whitened.*'))  # never made without the opt-in
    assert friendly.run_for(installation, JOB)['method'] == 'screened' and chosen.tiff
    again = attempt(installation, tmp_path, TRUNK, whiten=True, attempt_id='d' * 32, now=NOW + timedelta(minutes=1))
    assert sorted(path.name for path in tmp_path.glob(f'{friendly.CACHE}*-whitened.*')) == [
        f'{friendly.CACHE}{JOB}-whitened.json', f'{friendly.CACHE}{JOB}-whitened.tiff']
    run = friendly.run_for(installation, JOB)
    assert run['attempt_id'] == 'd' * 32 and run['method'] == 'screened'  # same minute: the more faithful pages
    assert conversion.read_fax_frames(again.tiff)[0].tobytes() == conversion.read_fax_frames(chosen.tiff)[0].tobytes()
    # The administrator's Always takes the faster whitened pages at that bill, and the Sent detail says so.
    attempt(installation, tmp_path, TRUNK, whiten=True, attempt_id='e' * 32, choice='always',
            now=NOW + timedelta(minutes=2))
    run = friendly.run_for(installation, JOB)
    assert run['method'] == 'whitened'
    assert friendly.sent_sentence(run).startswith('Light areas on the page were made white and specks removed')


def test_the_named_reasons(installation, monkeypatch):
    values = SimpleNamespace()
    assert friendly.named_reason(installation, values, {}, 'sip', 'always', now=NOW) == 'administrator'
    assert friendly.named_reason(installation, values, {}, 'sinch', 'recipient', now=NOW) == 'administrator'
    soon = {'send_by': NOW + timedelta(minutes=40)}
    assert friendly.named_reason(installation, values, soon, 'sinch', 'time', now=NOW) == 'deadline'
    later = {'send_by': NOW + timedelta(hours=3)}
    assert friendly.named_reason(installation, values, later, 'sinch', 'time', now=NOW) is None
    from app import capacity
    waiting = SimpleNamespace(waiting_for_line=lambda values, now: 2)
    monkeypatch.setattr(capacity, 'for_engine', lambda engine: waiting)
    assert friendly.named_reason(installation, values, later, 'sip', 'time', now=NOW) == 'capacity'
    assert friendly.named_reason(installation, values, later, 'sinch', 'ecm', now=NOW) is None  # not a phone line


@needs_gs
def test_a_send_by_time_within_the_hour_takes_the_faster_pages_in_the_same_minute(installation, tmp_path, billed):
    assert sending.unchanged(attempt(installation, tmp_path, TRUNK, small=True,
                                     job={'send_by': NOW + timedelta(hours=5)}))
    chosen = attempt(installation, tmp_path, TRUNK, small=True, attempt_id='d' * 32,
                     job={'send_by': NOW + timedelta(minutes=20)})
    assert not sending.unchanged(chosen) and friendly.run_for(installation, JOB)['method'] == 'screened'


# One source of the setting's words ------------------------------------------------------------------------------

def test_describe_setting_keeps_guided_setups_keys_and_adds_the_whitening_opt_in():
    described = friendly.describe_setting()
    assert set(described) == {'setting', 'label', 'default', 'off', 'choices', 'whiten'}
    assert (described['setting'], described['default'], described['off']) == (
        'fax_friendly_documents', 'where_it_saves', 'never')
    assert list(described['choices']) == list(friendly.CHOICES)
    assert {value: label for value, (label, _) in described['choices'].items()} == friendly.CHOICE_LABELS
    assert all(sentence.endswith('.') for _, sentence in described['choices'].values())
    assert described['whiten'] == {'setting': 'fax_friendly_whiten', 'label': 'Also make light areas white',
                                   'default': False, 'warning': friendly.WHITEN_WARNING}
    assert 'pale text' in friendly.WHITEN_WARNING and 'light marks' in friendly.WHITEN_WARNING
    from app.cli.commands.pages import shading_text
    assert shading_text('where_it_saves') == 'where it saves time' and shading_text(None) == 'where it saves time'


def test_the_console_and_the_command_line_say_the_same_words_as_the_server():
    import json
    from pathlib import Path
    from app.cli.commands.delivery import RECOMMENDATION_SECTIONS
    copy = Path(__file__).resolve().parents[2] / friendly.CONSOLE_WORDS
    words = json.loads(json.dumps(friendly.console_words()))
    assert json.loads(copy.read_text(encoding='utf-8')) == words, (
        f'Update {friendly.CONSOLE_WORDS} from friendly.console_words().')
    assert dict((key, heading) for key, heading, _, _ in RECOMMENDATION_SECTIONS)['pages'] == \
        friendly.RECOMMENDATION_HEADING
