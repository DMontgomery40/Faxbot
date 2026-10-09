"""Learned call hours (M26, routing/schedule.py): time a page and failures after answer by hour, per number and
per route, from trunk call records (both engines); the scheduler moves only ordinary faxes, within their send-by
time, and the predictor prices the hour. Synthetic hour-of-day fixtures only."""
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.routing import predict, schedule
from api.app.routing.schedule import CallTiming, Fax, Hours, Settings, decide, learn_timing
from api.tests.test_schedule import MONDAY, NEW_YORK, NOW, NUMBER, Scheduled, at, weekdays
from api.tests.test_schema import database  # noqa: F401 (fixture)

SETTINGS = Settings(Hours(zone_name=NEW_YORK), True, NEW_YORK, True)
# The ten weekdays before Monday 19 October 2026.
DAYS = weekdays(date(2026, 10, 5), date(2026, 10, 16))
assert len(DAYS) == 10


def trunk(**extra):
    return Fax(NUMBER, pages=2, route='sip', **extra)


def slow_mornings():
    """Weekdays at 9: 40 s a page; at 10, 11 and 14: 20 s a page. Every call delivered."""
    calls = [CallTiming(at(day, 9, 5), 40.0, False) for day in DAYS]
    calls += [CallTiming(at(day, hour, 5), 20.0, False) for day in DAYS for hour in (10, 11, 14)]
    return calls


def failing_mornings():
    """Weekdays at 9: the fax machine answers and 3 of 4 calls fail; at 10: all delivered."""
    calls = [CallTiming(at(day, 9, 5), None, index % 4 != 0) for index, day in enumerate(DAYS[:8])]
    calls += [CallTiming(at(day, hour, 5), 22.0, False) for day in DAYS for hour in (10, 11, 14)]
    return calls


# -- learning, with its sample limits --------------------------------------------------------------------------

def test_an_hour_says_something_only_with_three_calls_on_three_days_and_enough_weight():
    two = learn_timing([CallTiming(at(day, 9, 5), 30.0, False) for day in DAYS[-2:]], NOW, NEW_YORK)
    assert two.fact_at(at(MONDAY, 9, 30)) is None and not two.typical.timed
    three = learn_timing([CallTiming(at(day, 9, 5), 30.0, False) for day in DAYS[-3:]], NOW, NEW_YORK)
    fact = three.fact_at(at(MONDAY, 9, 30))
    assert fact.key == ('weekdays', 9) and fact.timed and fact.seconds_per_page == 30.0
    # Three calls on one day are one day.
    same_day = learn_timing([CallTiming(at(DAYS[-1], 9, minute), 30.0, False) for minute in (1, 2, 3)], NOW,
                            NEW_YORK)
    assert same_day.fact_at(at(MONDAY, 9, 30)) is None
    # Calls from the end of the 30 days weigh almost nothing; older ones are not read at all.
    old = learn_timing([CallTiming(NOW - timedelta(days=29, hours=1), 30.0, False) for _ in range(3)]
                       + [CallTiming(NOW - timedelta(days=28, hours=offset), 30.0, False) for offset in (1, 2)],
                       NOW, NEW_YORK)
    assert old.typical is not None and not old.typical.timed
    assert learn_timing([CallTiming(NOW - timedelta(days=31), 30.0, False)], NOW, NEW_YORK).typical is None


def test_time_a_page_and_failures_after_answer_are_learned_per_hour():
    timing = learn_timing(failing_mornings(), NOW, NEW_YORK)
    nine = timing.fact_at(at(MONDAY, 9, 30), 'judged')
    assert (nine.answered, nine.failed) == (8, 6) and nine.failure_share == 0.75 and not nine.timed
    ten = timing.fact_at(at(MONDAY, 10, 30))
    assert ten.timed and ten.seconds_per_page == 22.0 and ten.failed == 0
    assert nine.summary() == '6 of 8 answered calls failed.'
    assert ten.summary() == 'About 22 seconds a page on 10 delivered calls; 0 of 10 answered calls failed.'
    assert ten.label() == 'Weekdays, 10:00 AM to 11:00 AM'
    assert [fact.key for fact in timing.facts()][:3] == [('weekdays', 9), ('weekdays', 10), ('weekdays', 11)]


def test_a_call_says_what_it_can_from_both_engines_records():
    base = {'started_at': NOW, 'direction': 'outbound', 'job_id': 'a' * 32, 'error_cause': None}
    delivered = dict(base, disposition='answered', fax_status='SUCCESS', pages=2, connected_seconds=71)
    # The built-in engine: the connected time less the predictor's setup; the SSL Fax engine: its transfer time.
    assert schedule.call_timing(delivered).seconds_per_page == (71 - predict.SETUP_SECONDS) / 2
    assert schedule.call_timing(dict(delivered, transfer_seconds=50)).seconds_per_page == 25
    failed = dict(base, disposition='answered', fax_status='FAILED', pages=0, connected_seconds=40,
                  error_cause='remote_fax_failed: the fax did not finish')
    assert schedule.call_timing(failed) == CallTiming(NOW, None, True)
    # Busy, unanswered and no fax machine stay busy-hour evidence: nothing here.
    assert schedule.call_timing(dict(base, disposition='busy', fax_status=None, pages=None,
                                     connected_seconds=None)) is None


# -- the decision ---------------------------------------------------------------------------------------------

def test_a_slow_hour_moves_an_ordinary_fax_to_a_faster_one_and_never_an_urgent_one():
    timing = learn_timing(slow_mornings(), NOW, NEW_YORK)
    decision = decide(trunk(), SETTINGS, None, NOW, timing)
    assert decision.why == 'faster' and decision.hold_until == at(MONDAY, 10, 0)
    assert decision.better.percent == 50
    sentence = schedule.reason(decision, SETTINGS, now=NOW)
    assert sentence.startswith('Waiting until ') and sentence.endswith(
        ': calls to this number take about 50% less time a page before 3:00 PM UTC.')  # 11:00 AM in New York
    assert decide(trunk(urgent=True), SETTINGS, None, NOW, timing).hold_until is None
    # At the hour it waited for, the hour is no longer worse than typical: it goes, and is never held again.
    assert decide(trunk(), SETTINGS, None, at(MONDAY, 10, 0), timing).hold_until is None


def test_a_faster_hour_matters_only_on_a_route_billed_by_the_minute():
    timing = learn_timing(slow_mornings(), NOW, NEW_YORK)
    assert decide(Fax(NUMBER, pages=2, route='sinch'), SETTINGS, None, NOW, timing).hold_until is None
    assert decide(Fax(NUMBER, pages=2, route='phaxio'), SETTINGS, None, NOW, timing).hold_until is None


def test_an_hour_where_answered_calls_fail_moves_an_ordinary_fax_to_a_reliable_one():
    timing = learn_timing(failing_mornings(), NOW, NEW_YORK)
    decision = decide(trunk(), SETTINGS, None, NOW, timing)
    assert decision.why == 'reliable' and decision.hold_until == at(MONDAY, 10, 0)
    assert schedule.reason(decision, SETTINGS, now=NOW).endswith(
        ': fewer calls to this number fail after the fax machine answers before 3:00 PM UTC (0 of 10, against 6 of '
        '8 at this hour).')


def test_the_send_by_time_the_wait_limit_the_switch_and_closed_hours_bound_the_move():
    timing = learn_timing(slow_mornings(), NOW, NEW_YORK)
    # A send-by time that 10:00 would miss: it goes now.
    assert decide(trunk(send_by=at(MONDAY, 10, 0)), SETTINGS, None, NOW, timing).hold_until is None
    # Learning off for this number: it goes now.
    off = Settings(Hours(zone_name=NEW_YORK), False, NEW_YORK, True)
    assert decide(trunk(), off, None, NOW, timing).hold_until is None
    # The recipient takes faxes only until 10:00: the faster 10:00 hour is closed, 11:00 too: it goes now.
    early = Settings(Hours(None, 8 * 60, 10 * 60, NEW_YORK), True, NEW_YORK, True)
    assert decide(trunk(), early, None, NOW, timing).hold_until is None
    # Only slower hours within the 12 hours: it goes now.
    slow_all_day = [CallTiming(at(day, hour, 5), 40.0 if hour < 22 else 20.0, False) for day in DAYS
                    for hour in (8, 9, 10, 22)]
    assert decide(trunk(), SETTINGS, None, NOW, learn_timing(slow_all_day, NOW, NEW_YORK)).hold_until is None


def test_every_number_on_the_line_together_prices_an_hour_but_never_holds_a_fax():
    route_wide = learn_timing(slow_mornings(), NOW, NEW_YORK, scope='route')
    assert route_wide.factor_at(at(MONDAY, 9, 30)) > 1.5
    assert decide(trunk(), SETTINGS, None, NOW, route_wide).hold_until is None


def test_too_few_calls_change_nothing():
    few = learn_timing(slow_mornings()[:2] + slow_mornings()[-2:], NOW, NEW_YORK)
    assert decide(trunk(), SETTINGS, None, NOW, few).hold_until is None
    assert decide(trunk(), SETTINGS, None, NOW, None).hold_until is None


# -- the predictor ----------------------------------------------------------------------------------------------

def test_the_predictor_prices_the_hours_time_a_page_when_the_sample_supports_it():
    timing = learn_timing(slow_mornings(), NOW, NEW_YORK)
    factor = timing.factor_at(at(MONDAY, 9, 30))
    assert factor == 40.0 / timing.typical.seconds_per_page and factor > 1.5
    assert timing.factor_at(at(MONDAY, 3, 0)) is None  # no calls at 3 AM
    shape = predict.Shape(2, None, 'fine', 'normal')
    plain = predict.line_seconds(shape, predict.Link())
    link = predict.Link(hour_factor=factor, hour_scope='number')
    seconds, how = predict.hour_effect(*plain, link)
    assert seconds == pytest.approx(predict.SETUP_SECONDS + (plain[0] - predict.SETUP_SECONDS) * factor)
    assert how.startswith(predict.duration_text(seconds)) and how.endswith(
        f'and calls to this number take about {round((factor - 1) * 100)}% more time a page at this hour')
    # Within 10% of typical: nothing changes and nothing is said.
    assert predict.hour_effect(*plain, predict.Link(hour_factor=1.05)) == plain


# -- the claim, on SQLite and PostgreSQL ------------------------------------------------------------------------

class Trunk(Scheduled):
    def calls(self, calls):
        """Finished trunk calls as the delivery store and the call records keep them: (when, seconds a page,
        failed after answer)."""
        table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=self.engine)
        for when, per_page, failed in calls:
            job = self.accept(NUMBER, urgent=True, at=when)
            claim = self.store.claim('worker', now=when, lease_seconds=300)
            assert claim is not None and claim.job_id == job
            assert self.store.begin_submission(claim, now=when)
            self.store.record_receipt(claim, provider_sid='SID' + job[:8], status='failed' if failed else 'success',
                                      now=when)
            row = {'id': uuid4().hex, 'direction': 'outbound', 'call_id': claim.attempt_id, 'job_id': job,
                   'attempt_id': claim.attempt_id, 'called': NUMBER, 'started_at': when,
                   'answered_at': when + timedelta(seconds=3), 'ended_at': when + timedelta(seconds=90),
                   'disposition': 'answered', 't38': 'yes', 'fax_preference': 0, 'created_at': when,
                   'updated_at': when, 'pages': 0 if failed else 2, 'fax_status': 'FAILED' if failed else 'SUCCESS',
                   'connected_seconds': int(predict.SETUP_SECONDS + 2 * (per_page or 20)),
                   'error_cause': 'remote_fax_failed: the fax did not finish' if failed else None}
            with self.engine.begin() as connection:
                connection.execute(table.insert().values(**row))
        self.store.capacity().forget_holds()


@pytest.fixture
def trunk_install(database, tmp_path):  # noqa: F811
    install = Trunk(database, tmp_path)
    install.clock = NOW - timedelta(days=20)
    return install


def test_the_claim_holds_an_ordinary_trunk_fax_for_the_faster_hour_and_sends_an_urgent_one(trunk_install):
    trunk_install.calls([(item.at, item.seconds_per_page, False) for item in slow_mornings()])
    with trunk_install.engine.connect() as connection:
        timing = schedule.for_engine(trunk_install.engine).timing(connection, NUMBER, SETTINGS, NOW)
    assert timing.scope == 'number' and timing.fact_at(at(MONDAY, 9, 30)).seconds_per_page == pytest.approx(40, 0.01)
    held = trunk_install.accept(NUMBER, trunk=True, at=NOW)
    assert trunk_install.store.claim('worker', now=NOW) is None
    sentence = trunk_install.waiting(held, NOW)
    assert sentence.startswith('Waiting until 2:00 PM UTC') and 'less time a page before 3:00 PM UTC' in sentence
    urgent = trunk_install.accept(NUMBER, urgent=True, trunk=True, at=NOW)
    claim = trunk_install.store.claim('worker', now=NOW + timedelta(seconds=1))
    assert claim is not None and claim.job_id == urgent
    trunk_install.store.begin_submission(claim, now=NOW + timedelta(seconds=1))
    trunk_install.store.record_receipt(claim, provider_sid='SIDurgent', status='success',
                                       now=NOW + timedelta(seconds=2))
    assert trunk_install.state(held) == 'ready'
    claim = trunk_install.store.claim('worker', now=at(MONDAY, 10, 0) + timedelta(seconds=30))
    assert claim is not None and claim.job_id == held


def test_the_view_shows_the_learned_call_hours(trunk_install):
    from api.app.routing.schedule_http import view
    trunk_install.calls([(item.at, item.seconds_per_page, False) for item in slow_mornings()])
    schedule.for_engine(trunk_install.engine).save(NUMBER, time_zone=NEW_YORK, days=None, start=None, end=None,
                                                   learn_busy=True, now=NOW)
    shown = view(trunk_install.engine, NUMBER, trunk_install.values, 'sip', NOW)
    assert shown['call_hours'][0] == {'label': 'Weekdays, 9:00 AM to 10:00 AM',
                                      'sentence': 'About 40 seconds a page on 10 delivered calls; 0 of 10 answered '
                                                  'calls failed.'}
    assert shown['typical_hour'].startswith('About 20 seconds a page on 40 delivered calls')
    assert shown['call_hours_sentence'].startswith('An ordinary fax may wait up to 12 hours')
    empty = view(trunk_install.engine, '+12025550199', trunk_install.values, 'sip', NOW)
    assert empty['call_hours'] == [] and empty['call_hours_sentence'] == (
        'Faxbot has too few calls to this number to compare its hours yet.')
