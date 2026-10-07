"""Destination scheduling (T13): learned busy hours, send-by times, recipient hours and failed-try billing.

The rules are pure (``routing/schedule.py``) and tested on synthetic call histories; the claim tests run
the real delivery store on SQLite and PostgreSQL, so a held fax is shown to stay ready and never fail.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from api.app.routing import schedule
from api.app.routing.schedule import (
    BusyHours, Decision, Fax, Hours, Observation, Settings, decide, learn, send_by_view,
)
from api.tests.test_capacity import Install
from api.tests.test_schema import database  # noqa: F401 (fixture)


NEW_YORK = 'America/New_York'
NUMBER = '+12025550123'


def at(day, hour, minute=0, zone=NEW_YORK):
    """Naive UTC for a local time in ``zone``."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(zone))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def weekdays(first, last):
    day, found = first, []
    while day <= last:
        if day.weekday() < 5:
            found.append(day)
        day += timedelta(days=1)
    return found


# Monday 19 October 2026, 9:15 in New York.
MONDAY = date(2026, 10, 19)
NOW = at(MONDAY, 9, 15)
# The eight weekdays before it at 9:10: reached twice early on, then busy six times running.
NINE_AM = weekdays(date(2026, 10, 7), date(2026, 10, 16))
assert len(NINE_AM) == 8


def realistic():
    """Busy weekdays at 9 (6 of the last 8), and every weekday afternoon delivered."""
    calls = [Observation(at(day, 9, 10), 'reached' if index < 2 else 'busy') for index, day in enumerate(NINE_AM)]
    calls += [Observation(at(day, 14, 30), 'reached') for day in weekdays(date(2026, 10, 1), date(2026, 10, 16))]
    return calls


SETTINGS = Settings(Hours(zone_name=NEW_YORK), True, NEW_YORK, True)


def sinch(**extra):
    return Fax(NUMBER, pages=2, route='sinch', **extra)


# -- learned busy hours ----------------------------------------------------------------------

def test_a_learned_busy_hour_delays_a_normal_fax_and_never_an_urgent_one():
    busy = learn(realistic(), NOW, NEW_YORK)
    slot = busy.busy_at(NOW)
    assert slot is not None and slot.key == ('weekdays', 9)
    assert slot.sentence() == 'this number was busy at this hour on 6 of the last 8 weekdays Faxbot called it'
    # Afternoon successes never erase the morning's busy hour, and are not busy themselves.
    assert busy.busy_at(at(MONDAY, 14, 15)) is None
    assert [item.label() for item in busy.busy_slots()] == ['Weekdays, 9:00 AM to 10:00 AM']

    held = decide(sinch(), SETTINGS, busy, NOW)
    assert held.hold_until == at(MONDAY, 10, 0) and held.why == 'busy' and held.charge == 'unknown'
    assert decide(sinch(urgent=True), SETTINGS, busy, NOW) == Decision(latest_start=None)


def test_the_sentence_says_why_until_when_and_what_a_failed_try_may_cost(monkeypatch):
    monkeypatch.setattr(schedule, '_clock', lambda moment: f'{moment:%H:%M} UTC')
    busy = learn(realistic(), NOW, NEW_YORK)
    held = decide(sinch(), SETTINGS, busy, NOW)
    assert schedule.reason(held, SETTINGS, route_label='Sinch', price_text='$0.09') == (
        'Waiting until 14:00 UTC: this number was busy at this hour on 6 of the last 8 weekdays Faxbot called it, '
        'and a failed try may cost about $0.09 with Sinch.')
    assert schedule.reason(held, SETTINGS, route_label='Sinch') == (
        'Waiting until 14:00 UTC: this number was busy at this hour on 6 of the last 8 weekdays Faxbot called it, '
        'and Sinch may charge for a failed try.')


def test_a_day_of_the_week_with_enough_calls_of_its_own_decides_for_itself():
    mondays = [date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12)]
    calls = [Observation(at(day, 9, 5), 'no_answer') for day in mondays]
    calls += [Observation(at(day, 9, 5), 'reached') for day in weekdays(date(2026, 10, 13), date(2026, 10, 16))]
    busy = learn(calls, NOW, NEW_YORK)
    slot = busy.busy_at(NOW)
    assert slot.key == ('day', 0, 9)
    assert slot.sentence() == 'nobody answered this number at this hour on 3 of the last 3 Mondays Faxbot called it'
    assert busy.busy_at(at(date(2026, 10, 20), 9, 15)) is None   # Tuesdays at 9 were reached


def test_days_are_counted_not_calls():
    calls = [Observation(at(date(2026, 10, 16), 9, minute), 'busy') for minute in range(0, 50, 5)]
    assert learn(calls, NOW, NEW_YORK).busy_at(NOW) is None


def test_the_minimum_sample():
    two = [Observation(at(day, 9, 10), 'busy') for day in NINE_AM[-2:]]
    assert learn(two, NOW, NEW_YORK).busy_at(NOW) is None
    three = [Observation(at(day, 9, 10), 'busy') for day in NINE_AM[-3:]]
    assert learn(three, NOW, NEW_YORK).busy_at(NOW) is not None


def test_evidence_decays_over_30_days():
    # The same six busy weekdays, 25 to 30 days old: still inside the window, but too faint to count.
    old = [Observation(at(day, 9, 10), 'busy') for day in weekdays(date(2026, 9, 21), date(2026, 9, 25))]
    busy = learn(old, NOW, NEW_YORK)
    slot = busy.slots[('weekdays', 9)]
    assert slot.days == 5 and slot.weight < schedule.MIN_WEIGHT and busy.busy_at(NOW) is None
    # Older than 30 days counts for nothing at all.
    older = [Observation(at(day, 9, 10), 'busy') for day in weekdays(date(2026, 9, 1), date(2026, 9, 18))]
    assert learn(older, NOW, NEW_YORK).slots == {}
    # The same days ten days later than NOW's history: busy.
    assert learn(old, at(date(2026, 9, 28), 9, 15), NEW_YORK).busy_at(at(date(2026, 9, 28), 9, 15)) is not None


def test_a_successful_call_in_a_busy_hour_resets_it():
    calls = realistic()
    assert learn(calls, NOW, NEW_YORK).busy_at(NOW) is not None
    # The line answered at 9 on the Friday after the busy run: perhaps a new owner. Learned again from there.
    calls.append(Observation(at(date(2026, 10, 16), 9, 40), 'reached'))
    relearned = learn(calls, NOW, NEW_YORK)
    assert relearned.busy_at(NOW) is None and relearned.resets >= 1
    assert relearned.slots[('weekdays', 9)].days == 1


# -- send-by times -------------------------------------------------------------------------

def test_the_send_by_time_beats_the_busy_hour():
    busy = learn(realistic(), NOW, NEW_YORK)
    # Starting at 10:00 would leave too little room before 10:05; it goes now.
    tight = decide(sinch(send_by=at(MONDAY, 10, 5)), SETTINGS, busy, NOW)
    assert tight.hold_until is None and tight.latest_start < at(MONDAY, 10, 0)
    # With room to spare, the busy hour still holds it.
    roomy = decide(sinch(send_by=at(MONDAY, 12, 0)), SETTINGS, busy, NOW)
    assert roomy.hold_until == at(MONDAY, 10, 0)


def test_the_send_by_time_beats_the_recipients_hours_too():
    hours = Settings(Hours(frozenset(range(5)), 9 * 60, 17 * 60, NEW_YORK), True, NEW_YORK, True)
    evening = at(date(2026, 10, 16), 19, 0)   # Friday evening
    assert decide(sinch(), hours, None, evening).hold_until == at(MONDAY, 9, 0)
    assert decide(sinch(send_by=at(date(2026, 10, 17), 8, 0)), hours, None, evening).hold_until is None


def test_a_fax_that_cannot_make_its_send_by_time_says_so_before_it_is_late(monkeypatch):
    monkeypatch.setattr(schedule, '_clock', lambda moment: f'{moment:%H:%M} UTC')
    send_by = datetime(2026, 10, 19, 15, 0)
    latest = schedule.latest_start(Fax(NUMBER, pages=2, send_by=send_by))
    assert send_by - timedelta(minutes=30) < latest < send_by - schedule.SAFETY
    early = send_by_view(send_by, 'ready', pages=2, now=latest - timedelta(minutes=1))
    assert early['sentence'] == 'Send by 15:00 UTC.' and not early['at_risk']
    risky = send_by_view(send_by, 'ready', pages=2, now=latest + timedelta(minutes=1))
    assert risky['sentence'] == 'This fax may miss its send-by time of 15:00 UTC: it has not started yet.'
    assert risky['at_risk'] and risky['at'] == '2026-10-19T15:00:00Z'
    missed = send_by_view(send_by, 'ready', pages=2, now=send_by + timedelta(minutes=1))
    assert missed['sentence'] == ('This fax missed its send-by time of 15:00 UTC. Faxbot still sends it as soon as '
                                  'it can.')
    assert send_by_view(send_by, 'success', finished_at=send_by - timedelta(minutes=5),
                        now=send_by)['sentence'] == 'Sent before its send-by time of 15:00 UTC.'
    assert send_by_view(None, 'ready', now=send_by) is None


# -- recipient hours ---------------------------------------------------------------------------

def test_recipient_hours_across_a_time_zone_and_a_daylight_saving_change():
    hours = Hours(frozenset(range(5)), 9 * 60, 17 * 60, NEW_YORK)
    # Friday 30 October 2026, 6 PM in New York (EDT). Clocks go back on Sunday 1 November.
    friday = at(date(2026, 10, 30), 18, 0)
    assert friday == datetime(2026, 10, 30, 22, 0)
    opens = hours.next_open(friday)
    assert opens == datetime(2026, 11, 2, 14, 0)          # Monday 9:00 EST is 14:00 UTC, not 13:00
    assert hours.open_at(opens) and not hours.open_at(opens - timedelta(minutes=1))
    # The same hours seen from Denver: the recipient's zone decides, not the installation's.
    assert not hours.open_at(datetime(2026, 11, 2, 13, 30))   # 8:30 AM in New York, 6:30 AM in Denver
    # Hours that run past midnight: 10 PM to 6 AM, starting Monday to Friday nights.
    nights = Hours(frozenset(range(5)), 22 * 60, 6 * 60, NEW_YORK)
    assert nights.open_at(at(date(2026, 10, 20), 23, 0)) and nights.open_at(at(date(2026, 10, 21), 5, 0))
    assert not nights.open_at(at(date(2026, 10, 18), 23, 0))   # Sunday night
    assert nights.open_at(at(date(2026, 10, 17), 3, 0))        # Friday night, early Saturday
    assert nights.next_open(at(date(2026, 10, 21), 7, 0)) == at(date(2026, 10, 21), 22, 0)


def test_a_learned_busy_hour_stays_at_the_recipients_hour_across_the_change():
    calls = [Observation(at(day, 9, 10), 'busy') for day in weekdays(date(2026, 10, 19), date(2026, 10, 30))]
    monday = date(2026, 11, 2)
    now = at(monday, 9, 15)                    # 9:15 EST, an hour later in UTC than the EDT calls
    busy = learn(calls, now, NEW_YORK)
    assert busy.busy_at(now) is not None
    assert busy.busy_at(now - timedelta(hours=1)) is None   # 8:15 EST: the calls' UTC hour, not theirs
    assert decide(sinch(), SETTINGS, busy, now).hold_until == at(monday, 10, 0)


def test_urgent_faxes_bypass_the_recipients_hours():
    hours = Settings(Hours(frozenset(range(5)), 9 * 60, 17 * 60, NEW_YORK), True, NEW_YORK, True)
    assert decide(sinch(urgent=True), hours, None, at(date(2026, 10, 17), 3, 0)).hold_until is None


def test_learning_can_be_turned_off_per_recipient():
    off = Settings(Hours(zone_name=NEW_YORK), False, NEW_YORK, True)
    assert decide(sinch(), off, learn(realistic(), NOW, NEW_YORK), NOW).hold_until is None


# -- what a failed try costs -------------------------------------------------------------------

def test_the_providers_failed_try_billing_changes_the_decision():
    busy = learn(realistic(), NOW, NEW_YORK)
    # Phaxio credits back a fax that fails, and HumbleFax charges nothing per try: only a capacity question.
    for route in ('phaxio', 'humblefax'):
        free = decide(Fax(NUMBER, route=route), SETTINGS, busy, NOW)
        assert free.hold_until is None and free.later, route
    # Sinch does not say: unknown is never free, so the busy hour is worth waiting out.
    assert decide(Fax(NUMBER, route='sinch'), SETTINGS, busy, NOW).charge == 'unknown'
    # eFax charges when a call connects: an hour answered by no fax machine is waited out as billed.
    answered = [Observation(item.at, 'no_fax' if item.kind == 'busy' else item.kind) for item in realistic()]
    efax = decide(Fax(NUMBER, route='efax'), SETTINGS, learn(answered, NOW, NEW_YORK), NOW)
    assert efax.hold_until == at(MONDAY, 10, 0) and efax.charge == 'bills'
    # The trunk: Telnyx bills connected minutes and does not say about busy calls.
    trunk = decide(Fax(NUMBER, route='sip', preset='telnyx'), SETTINGS, busy, NOW)
    assert trunk.charge == 'unknown' and trunk.hold_until == at(MONDAY, 10, 0)
    assert schedule.failed_tries('sip', 'telnyx').no_fax == 'bills'
    assert schedule.failed_tries('a-plugin').busy == 'unknown'


def test_every_billing_entry_is_sourced_and_dated():
    for route, entry in schedule.FAILED_TRIES.items():
        assert entry.read_on == '2026-10-07' and entry.note.endswith('.'), route
        assert {entry.busy, entry.no_answer, entry.no_fax} <= {'bills', 'free', 'unknown'}, route
        assert route == 'sip' or entry.sources, route


def test_failures_are_learned_only_from_each_adapters_own_sentences():
    from api.app.sinch_service import CALL_ERRORS
    assert schedule.classify(phase='failed', sentence=CALL_ERRORS[17]) == 'busy'
    assert schedule.classify(phase='failed', sentence=CALL_ERRORS[30]) == 'no_fax'
    assert schedule.classify(phase='failed', sentence='The fax did not go through: no one answered.') == 'no_answer'
    assert schedule.classify(phase='failed', sentence='The number was busy, sort of.') is None
    assert schedule.classify(phase='failed', sentence=None) is None
    assert schedule.classify(phase='success') == 'reached'
    assert schedule.classify(phase='uncertain') is None
    # A trunk call's own record: answered with no page is no fax machine; a congested network is not the number.
    assert schedule.classify(phase='failed', disposition='answered', pages=0) == 'no_fax'
    assert schedule.classify(phase='failed', disposition='answered', pages=3) is None
    assert schedule.classify(phase='failed', disposition='congestion') is None
    assert schedule.classify(phase='failed', disposition='busy') == 'busy'


# -- the claim, on SQLite and PostgreSQL -------------------------------------------------------

class Scheduled(Install):
    """An installation whose faxes go through Sinch's adapter route name, with a recorded call history."""

    def history(self, calls, *, number=NUMBER):
        """Record finished calls as the delivery store does: urgent faxes, claimed, submitted, then ended."""
        from api.app.sinch_service import CALL_ERRORS
        for when, kind in calls:
            job = self.accept(number, urgent=True, at=when)
            claim = self.store.claim('worker', now=when, lease_seconds=300)
            assert claim is not None and claim.job_id == job
            assert self.store.begin_submission(claim, now=when)
            if kind == 'reached':
                self.store.record_receipt(claim, provider_sid='SID' + job[:8], status='success', now=when)
            else:
                self.store.record_receipt(claim, provider_sid='SID' + job[:8], status='failed', now=when,
                                          error=CALL_ERRORS[17])
        self.store.capacity().forget_holds()

    def normal(self, *, route='sinch', send_by=None, at=None):
        job = self.accept(NUMBER, at=at or self.clock)
        with self.engine.begin() as connection:
            connection.execute(sa.text('UPDATE fax_jobs SET backend = :route, send_by = :send_by WHERE id = :id'),
                               {'route': route, 'send_by': send_by, 'id': job})
        return job

    def state(self, job):
        with self.engine.connect() as connection:
            return connection.execute(sa.text('SELECT state FROM outbound_deliveries WHERE id = :id'),
                                      {'id': job}).scalar()


@pytest.fixture
def scheduled(database, tmp_path):  # noqa: F811
    install = Scheduled(database, tmp_path)
    install.clock = NOW - timedelta(days=20)
    return install


def _realistic_calls():
    return [(item.at, item.kind) for item in realistic()]


def test_the_claim_holds_a_normal_fax_in_a_busy_hour_and_sends_an_urgent_one(scheduled):
    scheduled.history(_realistic_calls())
    held = scheduled.normal(at=NOW)
    assert scheduled.store.claim('worker', now=NOW) is None
    sentence = scheduled.waiting(held, NOW)
    assert sentence.startswith('Waiting until ') and (
        ': this number was busy at this hour on 6 of the last 8 weekdays Faxbot called it, and ') in sentence
    assert 'Sinch' in sentence
    # An urgent fax goes at once; the line is busy for it too.
    urgent = scheduled.accept(NUMBER, urgent=True, at=NOW)
    claim = scheduled.store.claim('worker', now=NOW + timedelta(seconds=1))
    assert claim is not None and claim.job_id == urgent
    scheduled.store.begin_submission(claim, now=NOW + timedelta(seconds=1))
    from api.app.sinch_service import CALL_ERRORS
    scheduled.store.record_receipt(claim, provider_sid='SIDurgent', status='failed', now=NOW + timedelta(seconds=2),
                                   error=CALL_ERRORS[17])
    # Nothing else goes while it is held, it never fails for waiting, and it goes when the hour ends.
    for minutes in (5, 20, 44):
        scheduled.store.recover_expired(now=NOW + timedelta(minutes=minutes))
        assert scheduled.store.claim('worker', now=NOW + timedelta(minutes=minutes)) is None
        assert scheduled.state(held) == 'ready'
    claim = scheduled.store.claim('worker', now=at(MONDAY, 10, 0, NEW_YORK) + timedelta(seconds=30))
    assert claim is not None and claim.job_id == held


def test_a_success_inside_the_busy_hour_lets_held_faxes_go(scheduled):
    scheduled.history(_realistic_calls())
    held = scheduled.normal(at=NOW)
    assert scheduled.store.claim('worker', now=NOW) is None
    urgent = scheduled.accept(NUMBER, urgent=True, at=NOW)
    claim = scheduled.store.claim('worker', now=NOW + timedelta(seconds=1))
    assert claim.job_id == urgent
    scheduled.store.begin_submission(claim, now=NOW + timedelta(seconds=1))
    scheduled.store.record_receipt(claim, provider_sid='SIDurgent', status='success', now=NOW + timedelta(seconds=40))
    # The line answered at this hour: the busy hour's evidence ends, and the held fax goes at the next look.
    assert scheduled.waiting(held, NOW + timedelta(minutes=1)) is None
    claim = scheduled.store.claim('worker', now=NOW + timedelta(minutes=2))
    assert claim is not None and claim.job_id == held


def test_the_claim_lets_a_fax_go_when_its_send_by_time_beats_the_busy_hour(scheduled):
    scheduled.history(_realistic_calls())
    job = scheduled.normal(at=NOW, send_by=at(MONDAY, 10, 5))
    claim = scheduled.store.claim('worker', now=NOW)
    assert claim is not None and claim.job_id == job


def test_the_claim_sends_at_once_when_a_failed_try_is_free(scheduled):
    scheduled.history(_realistic_calls())
    phaxio = scheduled.normal(route='phaxio', at=NOW)
    claim = scheduled.store.claim('worker', now=NOW)
    assert claim is not None and claim.job_id == phaxio


def test_a_free_busy_hour_fax_goes_after_faxes_that_could_use_the_room(scheduled):
    scheduled.history(_realistic_calls())
    phaxio = scheduled.normal(route='phaxio', at=NOW)
    elsewhere = scheduled.accept('+12025550199', at=NOW + timedelta(seconds=1))
    assert scheduled.store.claim('worker', now=NOW + timedelta(seconds=2)).job_id == elsewhere
    assert scheduled.store.claim('worker', now=NOW + timedelta(seconds=3)).job_id == phaxio


def test_faxes_whose_send_by_time_is_near_go_first_after_urgent_ones(scheduled):
    plain = scheduled.accept('+12025550198', at=NOW)
    later_deadline = scheduled.normal(route='phaxio', at=NOW + timedelta(seconds=1),
                                      send_by=NOW + timedelta(minutes=50))
    first_deadline = scheduled.accept('+12025550197', at=NOW + timedelta(seconds=2))
    with scheduled.engine.begin() as connection:
        connection.execute(sa.text('UPDATE fax_jobs SET send_by = :at WHERE id = :id'),
                           {'at': NOW + timedelta(minutes=40), 'id': first_deadline})
    order = [scheduled.store.claim('worker', now=NOW + timedelta(seconds=10 + index)).job_id for index in range(3)]
    assert order == [first_deadline, later_deadline, plain]


def test_the_recipients_hours_hold_a_fax_through_the_claim(scheduled):
    scheduler = schedule.for_engine(scheduled.engine)
    scheduler.save(NUMBER, time_zone=NEW_YORK, days=frozenset(range(5)), start=9 * 60, end=17 * 60,
                   learn_busy=True, now=NOW - timedelta(days=1))
    saturday = at(date(2026, 10, 17), 10, 0)
    job = scheduled.normal(at=saturday)
    assert scheduled.store.claim('worker', now=saturday) is None
    assert scheduled.waiting(job, saturday).endswith(
        ': this recipient takes faxes only Monday to Friday, 9:00 AM to 5:00 PM in their time zone.')
    assert scheduled.state(job) == 'ready'
    urgent = scheduled.accept(NUMBER, urgent=True, at=saturday)
    assert scheduled.store.claim('worker', now=saturday + timedelta(seconds=1)).job_id == urgent


def test_the_newest_schedule_row_counts_and_no_row_is_ever_changed(scheduled):
    scheduler = schedule.for_engine(scheduled.engine)
    scheduler.save(NUMBER, time_zone=NEW_YORK, days=frozenset(range(5)), start=9 * 60, end=17 * 60,
                   learn_busy=True, now=NOW - timedelta(days=2))
    scheduler.save(NUMBER, time_zone='', days=None, start=None, end=None, learn_busy=False, now=NOW)
    with scheduled.engine.connect() as connection:
        settings = scheduler.settings(connection, NUMBER, scheduled.values)
        rows = connection.execute(sa.text('SELECT days, learn_busy FROM destination_schedules ORDER BY created_at')
                                  ).all()
    assert settings.hours.always and not settings.learn_busy and not settings.zone_set
    assert [tuple(row) for row in rows] == [('mon,tue,wed,thu,fri', 1), (None, 0)]
    with pytest.raises(ValueError):
        scheduler.save(NUMBER, time_zone='Mars/Olympus', days=None, start=None, end=None, learn_busy=True)
    with pytest.raises(ValueError):
        scheduler.save(NUMBER, time_zone='', days=None, start=480, end=None, learn_busy=True)


# -- send-by times as people give them ---------------------------------------------------------

def test_send_by_times_are_read_in_the_installations_zone_and_refused_when_past_or_far():
    now = datetime(2026, 10, 7, 15, 0)                       # 9:00 AM in Denver
    assert schedule.parse_send_by(None, now) is None and schedule.parse_send_by('  ', now) is None
    assert schedule.parse_send_by('2026-10-07T17:00-04:00', now) == datetime(2026, 10, 7, 21, 0)
    assert schedule.parse_send_by('2026-10-07T21:00Z', now) == datetime(2026, 10, 7, 21, 0)
    assert schedule.parse_send_by('2026-10-07 17:00', now, 'America/Denver') == datetime(2026, 10, 7, 23, 0)
    assert schedule.parse_send_by('17:00', now, 'America/Denver') == datetime(2026, 10, 7, 23, 0)
    assert schedule.parse_send_by('08:00', now, 'America/Denver') == datetime(2026, 10, 8, 14, 0)  # tomorrow
    for value, message in (('2026-10-07 08:00', 'The send-by time has already passed.'),
                           ('2026-12-01 08:00', 'Choose a send-by time within the next 31 days.'),
                           ('tomorrow', 'Give the send-by time as a date and time')):
        with pytest.raises(ValueError, match=message):
            schedule.parse_send_by(value, now, 'America/Denver')


def test_a_close_send_by_time_does_not_wait_to_go_with_other_faxes():
    now = datetime(2026, 10, 7, 15, 0)
    assert schedule.too_soon_to_wait(now + timedelta(minutes=40), 1, now)
    assert not schedule.too_soon_to_wait(now + timedelta(hours=3), 1, now)


def test_the_send_by_time_is_bound_into_the_request_only_when_given():
    from api.app.request_identity import intent_fingerprint
    plain = intent_fingerprint(version=2, to=NUMBER, queue_only=False, document_sha256='0' * 64)
    assert intent_fingerprint(version=2, to=NUMBER, queue_only=False, document_sha256='0' * 64,
                              send_by=None) == plain
    assert intent_fingerprint(version=2, to=NUMBER, queue_only=False, document_sha256='0' * 64,
                              send_by=datetime(2026, 10, 7, 21, 0)) != plain


# -- the API and `faxbot` ------------------------------------------------------------------------

@pytest.fixture
def schedule_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, TZ='America/Denver'):
        yield Cli(client)


def test_cli_and_api_show_and_set_a_recipients_hours_and_a_send_by_time(schedule_cli, tmp_path):
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = schedule_cli, {'X-API-Key': BOOTSTRAP}
    route = '/routing/destinations/%2B12025550123/schedule'
    shown = cli.client.get(route, headers=admin).json()
    assert shown['hours_sentence'] == 'Faxbot sends to this recipient at any time.' and shown['learn_busy'] is True
    assert shown['busy_hours'] == [] and shown['busy_sentence'] == (
        'Faxbot has not seen a busy hour for this number in the last 30 days.')
    assert shown['failed_try']['sentence'] == 'Phaxio credits back the price of a fax that fails, busy lines included.'
    assert shown['failed_try']['read_on'] == '2026-10-07' and shown['failed_try']['sources']

    saved = cli.json('recipients', 'schedule', NUMBER, '--days', 'mon,tue,wed,thu,fri', '--from', '09:00',
                     '--until', '17:00', '--time-zone', NEW_YORK)
    assert (saved['days'], saved['start'], saved['end'], saved['time_zone']) == (
        ['mon', 'tue', 'wed', 'thu', 'fri'], '09:00', '17:00', NEW_YORK)
    assert saved['hours_sentence'] == ('This recipient takes faxes only Monday to Friday, 9:00 AM to 5:00 PM in '
                                       'their time zone (America/New_York).')
    printed = ' '.join(cli('recipients', 'schedule', NUMBER).stdout.split())
    assert 'Hours This recipient takes faxes only Monday to Friday, 9:00 AM to 5:00 PM' in printed
    assert cli.json('recipients', 'schedule', NUMBER, '--no-learn')['learn_busy'] is False
    cleared = cli.json('recipients', 'schedule', NUMBER, '--any-time', '--time-zone', 'default', '--learn')
    assert cleared['days'] is None and cleared['start'] is None and cleared['time_zone'] == ''
    assert cli('recipients', 'schedule', NUMBER, '--days', 'someday').exit_code != 0
    bad = cli.client.put(route, headers=admin, json={'start': '09:00', 'end': None, 'learn_busy': True})
    assert bad.status_code == 400
    assert cli.client.put(route, headers=admin, json={'time_zone': 'Mars/Olympus'}).status_code == 400
    sender = cli.client.post('/admin/api-keys', headers=admin, json={'name': 'synthetic', 'scopes': ['fax:send']})
    key = {'X-API-Key': sender.json()['token']}
    assert cli.client.get(route, headers=key).status_code == 403
    assert cli.client.put(route, headers=key, json={'learn_busy': False}).status_code == 403

    # A send-by time: refused when past, stored, shown and bound into the request.
    def post(send_by, idempotency):
        return cli.client.post('/fax', headers={**admin, 'Idempotency-Key': idempotency},
                               data={'to': NUMBER, 'queue_only': 'true', **({'send_by': send_by} if send_by else {})},
                               files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    assert post('2020-01-01 09:00', 'schedule-past').status_code == 400
    later = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0)
    sent = post(later.isoformat() + 'Z', 'schedule-key-1')
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    detail = cli.client.get(f'/admin/fax-jobs/{job}', headers=admin).json()
    assert detail['send_by']['at'] == later.isoformat() + 'Z' and detail['send_by']['sentence'].startswith('Send by ')
    assert post(None, 'schedule-key-1').status_code == 409
    assert 'Send by Send by ' in ' '.join(cli('sent', 'show', job).stdout.split())
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic page\n')
    accepted = cli('send', NUMBER, note, '--queue', '--by', '17:00')
    assert accepted.exit_code == 0, accepted.stdout
