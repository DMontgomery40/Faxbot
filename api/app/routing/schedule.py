"""When a fax to a recipient should start: its send-by time, the recipient's hours and learned busy hours (T13).

``capacity.py`` decides whether a fax *can* start (room on its number and the
trunk); this module decides whether it *should* start now. The claim asks both
(``capacity.Capacity.next_ready`` and ``group_may_start``), so only a new
attempt is ever affected: nothing already dialing is touched, and a fax that
waits stays ``ready`` and never fails for waiting.

The rules, in order:

1. **Urgent** faxes go at once: no learned busy hour and no recipient hours
   hold them.
2. **Send-by time** (``fax_jobs.send_by``): Faxbot never holds a fax past the
   latest moment that still lets it go by then (``latest_start``: the time a
   call takes, twice, plus ``SAFETY``). When the recipient's hours or a busy
   hour would hold it past that, it goes now. A fax whose latest start has
   come while it still waits says it may miss its send-by time.
3. **Recipient hours** (``destination_schedules``, the recipient's own
   request, such as "business hours only") in the recipient's time zone.
4. **Learned busy hours.** From earlier calls to the number: an hour counts as
   busy when on at least ``MIN_DAYS`` distinct days in the last 30 Faxbot
   called it at that hour, those days' weight (a day today weighs 1, one 30
   days old 0) is at least ``MIN_WEIGHT``, and the weighted share of days on
   which the number was busy, unanswered or answered without a fax machine is
   at least ``BUSY_SHARE``. Hours are counted per day of the week ("Mondays at
   9") and, for weekdays, pooled ("weekdays at 9"); a day of the week with
   enough calls of its own decides for itself. Hours are the recipient's own
   (its time zone, else the installation's), so a learned 9:00 stays 9:00
   across a daylight-saving change.

   A successful call during an hour that was busy ends that hour's evidence:
   the line has changed, perhaps to a new owner, and the hour is learned again
   from that call on. Successes at other hours never erase a busy hour.
   Without new calls in it, a busy hour fades out within 30 days.

   Whether waiting out a busy hour is worth it depends on what a failed call
   costs (``FAILED_TRIES``, researched and dated): when the route charges for
   it, or does not say (unknown is never free), the fax waits for the first
   hour that is neither busy nor closed; when a failed call is free, the fax
   does not wait and only goes after other faxes that could use the same room.

Every hold has one sentence for Sent details (``Decision.reason``). Learned
hours are worked out from the delivery records each time and never stored.
Which number: the recipient's (``fax_jobs.to_number``), also when Faxbot dials
its approved toll-free number, because both reach the same fax machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import sqlalchemy as sa


# Evidence older than this counts for nothing; a day's weight falls linearly from 1 (today) to 0.
WINDOW = timedelta(days=30)
# An hour needs calls on this many distinct days before it can count as busy ...
MIN_DAYS = 3
# ... and those days' decayed weight (three calls this week weigh about 2.7; three a month ago, 0).
MIN_WEIGHT = 1.5
# The weighted share of unreachable days that makes an hour busy.
BUSY_SHARE = 0.6
# Attempts read per number, newest first (as ``predict_facts.CALLS_READ`` bounds its reads).
HISTORY_READ = 400
# A closed or busy stretch longer than this is not waited out: the fax goes now.
LOOKAHEAD = timedelta(days=7)
# Room before a send-by time beyond the call itself: enough for one more try.
SAFETY = timedelta(minutes=10)
# Faxes with a send-by time within this go right after urgent ones, earliest first.
DEADLINE_FIRST = timedelta(hours=1)
# The claim looks at a held fax again after at most this long.
RECHECK = timedelta(minutes=1)

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
UNREACHABLE = ('busy', 'no_answer', 'no_fax')
TABLE = 'destination_schedules'


# What a failed call costs ---------------------------------------------------------------------

@dataclass(frozen=True)
class FailedTries:
    """Whether a route charges for a call that fails, by how it failed: 'bills', 'free' or 'unknown'."""
    busy: str
    no_answer: str
    no_fax: str
    note: str
    sources: tuple[str, ...]
    read_on: str

    def charge(self, kind):
        return getattr(self, kind) if kind in UNREACHABLE else 'unknown'


_TRUNK_NOTE = ('carriers bill a trunk call by the connected minute, so a call answered without a fax machine is '
               'charged; none of them says whether a busy or unanswered call is.')
FAILED_TRIES = {
    # Phaxio takes the fax's price before sending and credits it back for each recipient that hit an error
    # (its examples include a busy line); a fax that stops partway ends in error too.
    'phaxio': FailedTries('free', 'free', 'free',
                          'Phaxio credits back the price of a fax that fails, busy lines included.',
                          ('https://www.phaxio.com/docs/billing', 'https://www.phaxio.com/pricing'), '2026-10-07'),
    # A flat monthly plan: "unlimited faxes ... no overage charges"; nothing is charged per fax, page or try.
    'humblefax': FailedTries('free', 'free', 'free',
                             'HumbleFax charges a flat monthly fee and nothing for each fax or try.',
                             ('https://humblefax.com/faq',), '2026-10-07'),
    # Both eFax contracts: "bill you for each attempt ... where any transmission occurs whether or not the
    # transmission is completed, such as instances when someone answers the call"; busy and unanswered
    # calls are not named (no transmission occurs), and eFax retries a busy number 5 times itself.
    'efax': FailedTries('unknown', 'unknown', 'bills',
                        'eFax charges for every try in which a call connects, even when the fax fails; it does '
                        'not say whether a busy or unanswered call is charged.',
                        ('https://enterprise.efax.com/company/efax-developer-customer-agreement',
                         'https://enterprise.efax.com/company/customer-agreement',
                         'https://enterprise.efax.com/resources/faq'), '2026-10-07'),
    'sinch': FailedTries('unknown', 'unknown', 'unknown',
                         'Sinch publishes a price per page and does not say whether a failed try is charged.',
                         ('https://sinch.com/voice/fax-api/',
                          'https://developers.sinch.com/docs/fax/api-reference/fax/faxes'), '2026-10-07'),
    'documo': FailedTries('unknown', 'unknown', 'unknown',
                          'Documo publishes plan pages and overage prices and does not say whether a failed try '
                          'is charged.', ('https://www.documo.com/pricing/',), '2026-10-07'),
    'signalwire': FailedTries('unknown', 'unknown', 'unknown',
                              'SignalWire charges fax by the minute and does not say whether a failed try is '
                              'charged.', ('https://signalwire.com/pricing/fax',), '2026-10-07'),
    # The trunk, by carrier preset. Telnyx: "$0.005/min outbound" for US local; no statement on unanswered calls.
    'sip-telnyx': FailedTries('unknown', 'unknown', 'bills',
                              'Telnyx bills a trunk call by the connected minute, so a call answered without a fax '
                              'machine is charged; it does not say whether a busy or unanswered call is.',
                              ('https://telnyx.com/pricing/elastic-sip',
                               'https://support.telnyx.com/en/articles/4967498-fax-api-error-list'), '2026-10-07'),
    'sip-signalwire': FailedTries('unknown', 'unknown', 'bills',
                                  'SignalWire bills a call by the connected minute, so a call answered without a '
                                  'fax machine is charged; it does not say whether a busy or unanswered call is.',
                                  ('https://signalwire.com/pricing/voice',), '2026-10-07'),
    'sip': FailedTries('unknown', 'unknown', 'bills', 'Phone ' + _TRUNK_NOTE, (), '2026-10-07'),
}
UNKNOWN_TRIES = FailedTries('unknown', 'unknown', 'unknown',
                            'Faxbot has no published statement on whether this route charges for a failed try.',
                            (), '2026-10-07')


def failed_tries(route, preset=''):
    """What a failed call costs on ``route`` (a provider identity, or ``sip`` with the trunk's carrier preset)."""
    route = str(route or '').strip().lower()
    if route in ('sip', 'freeswitch'):
        return FAILED_TRIES.get(f'sip-{preset}') if preset and f'sip-{preset}' in FAILED_TRIES else FAILED_TRIES['sip']
    return FAILED_TRIES.get(route, UNKNOWN_TRIES)


# How earlier calls ended --------------------------------------------------------------------

def _failure_kinds():
    """Each adapter's own failure sentence that says the number could not be reached, and how.

    Exact membership only: a sentence not listed here says nothing about the hour it was sent at.
    """
    from ..hylafax_engine import _REASONS
    from ..humblefax_service import _FAILURES
    from ..sinch_service import CALL_ERRORS
    from ..sip_calls import _DISPOSITION_TEXT
    kinds = {CALL_ERRORS[17]: 'busy', CALL_ERRORS[16]: 'no_answer', CALL_ERRORS[30]: 'no_fax',
             _DISPOSITION_TEXT['busy']: 'busy', _DISPOSITION_TEXT['no_answer']: 'no_answer'}
    humble = {words[0]: sentence for words, sentence in _FAILURES}
    kinds.update({humble['busy']: 'busy', humble['no answer']: 'no_answer', humble['no fax machine']: 'no_fax'})
    # HylaFAX+ (the SSL Fax engine): busy, no answer, then (after "no response to") not a fax machine.
    engine = [sentence for _, sentence in _REASONS]
    kinds.update({engine[0]: 'busy', engine[1]: 'no_answer', engine[3]: 'no_fax'})
    return kinds


_KINDS = None


def failure_kind(sentence):
    """'busy', 'no_answer', 'no_fax' or None for one stored failure sentence."""
    global _KINDS
    if _KINDS is None:
        _KINDS = _failure_kinds()
    return _KINDS.get(sentence) if isinstance(sentence, str) else None


@dataclass(frozen=True)
class Observation:
    """One finished call to the number: when it started (naive UTC) and how it ended."""
    at: datetime
    kind: str   # 'reached', 'busy', 'no_answer' or 'no_fax'


def classify(*, phase, disposition=None, pages=None, sentence=None):
    """How one attempt ended, for learning, or None when it says nothing about the number at that hour.

    A trunk call's own record decides (busy, no answer, or answered and then failed with no page); for
    other routes, a delivered fax was reached and a failure counts only through its adapter's sentence.
    A congested network, a failed call setup or an uncertain ending is about the network, not the number.
    """
    if disposition:
        if disposition in ('busy', 'no_answer'):
            return disposition
        if disposition == 'answered':
            if phase == 'success':
                return 'reached'
            if phase == 'failed' and not pages:
                return 'no_fax'
        return None
    if phase == 'success':
        return 'reached'
    if phase == 'failed':
        return failure_kind(sentence)
    return None


# Hours: the recipient's own, and learned busy hours -------------------------------------------

def _zone(name):
    from ..people_time import zone
    return zone(name)


def _local(moment, zone):
    return moment.replace(tzinfo=timezone.utc).astimezone(zone)


def _utc(local):
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _at(day, minute, zone):
    """Naive UTC for local ``day`` at ``minute`` after midnight; a time skipped by a clock change moves on."""
    local = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=minute)
    # Wall-clock arithmetic: normalize through UTC so a time inside a spring-forward gap lands just after it.
    return _utc(local)


def _next_hour(moment, zone):
    local = _local(moment, zone)
    return moment + timedelta(minutes=60 - local.minute, seconds=-local.second, microseconds=-local.microsecond)


@dataclass(frozen=True)
class Hours:
    """The hours a recipient takes faxes, in its own time zone. ``days`` 0 (Monday) to 6; None: every day."""
    days: frozenset | None = None
    start: int | None = None          # minutes after local midnight
    end: int | None = None            # 1 to 1440; before ``start`` when the hours run past midnight
    zone_name: str = ''

    @property
    def always(self):
        return self.days is None and self.start is None

    def open_at(self, moment):
        if self.always:
            return True
        local = _local(moment, _zone(self.zone_name))
        day, minute = local.weekday(), local.hour * 60 + local.minute
        days = self.days if self.days is not None else frozenset(range(7))
        if self.start is None:
            return day in days
        if self.start < self.end:
            return day in days and self.start <= minute < self.end
        return (day in days and minute >= self.start) or ((day - 1) % 7 in days and minute < self.end)

    def next_open(self, moment):
        """The first moment at or after ``moment`` the recipient takes faxes; None when never within a week."""
        if self.open_at(moment):
            return moment
        zone = _zone(self.zone_name)
        today = _local(moment, zone).date()
        days = self.days if self.days is not None else frozenset(range(7))
        for offset in range(9):
            day = today + timedelta(days=offset)
            if day.weekday() not in days:
                continue
            start = _at(day, self.start or 0, zone)
            if start >= moment and self.open_at(start):
                return start
        return None


@dataclass(frozen=True)
class Slot:
    """What earlier calls showed for one hour: on how many days, how many unreachable, and whether busy."""
    key: tuple                      # ('day', 0-6, hour) or ('weekdays', hour)
    days: int
    unreachable: int
    weight: float
    share: float
    kind: str | None                # the commonest way it was unreachable

    @property
    def enough(self):
        return self.days >= MIN_DAYS and self.weight >= MIN_WEIGHT

    @property
    def busy(self):
        return self.enough and self.share >= BUSY_SHARE and self.kind is not None

    def sentence(self):
        """'this number was busy at this hour on 6 of the last 8 weekdays Faxbot called it'."""
        span = 'weekdays' if self.key[0] == 'weekdays' else f'{DAY_NAMES[self.key[1]]}s'
        counted = f'on {self.unreachable} of the last {self.days} {span} Faxbot called it'
        return {'busy': f'this number was busy at this hour {counted}',
                'no_answer': f'nobody answered this number at this hour {counted}',
                'no_fax': f'no fax machine answered this number at this hour {counted}'}.get(
            self.kind, f'this number could not be reached at this hour {counted}')

    def summary(self):
        """'Busy on 6 of the last 8 weekdays Faxbot called at this hour.' for a list of busy hours."""
        span = 'weekdays' if self.key[0] == 'weekdays' else f'{DAY_NAMES[self.key[1]]}s'
        how = {'busy': 'Busy', 'no_answer': 'Not answered', 'no_fax': 'No fax machine answered'}.get(
            self.kind, 'Not reached')
        return f'{how} on {self.unreachable} of the last {self.days} {span} Faxbot called at this hour.'

    def label(self):
        """'Weekdays, 9:00 AM to 10:00 AM' or 'Mondays, 9:00 AM to 10:00 AM'."""
        span = 'Weekdays' if self.key[0] == 'weekdays' else f'{DAY_NAMES[self.key[1]]}s'
        return f'{span}, {_hour_text(self.key[-1])} to {_hour_text((self.key[-1] + 1) % 24)}'


def _hour_text(hour):
    return f"{hour % 12 or 12}:00 {'AM' if hour < 12 else 'PM'}"


def _judge(key, entries, reference):
    """A ``Slot`` from (local day, kind, when) entries, weighted by age at ``reference``."""
    kept = [(day, kind, at) for day, kind, at in entries if reference - WINDOW < at <= reference]
    if not kept:
        return Slot(key, 0, 0, 0.0, 0.0, None)
    weights = [max(0.0, 1 - (reference - at) / WINDOW) for _, _, at in kept]
    total = sum(weights)
    bad = sum(weight for weight, (_, kind, _) in zip(weights, kept) if kind != 'reached')
    counts = {}
    for _, kind, _ in kept:
        if kind != 'reached':
            counts[kind] = counts.get(kind, 0) + 1
    kind = max(sorted(counts), key=counts.get) if counts else None
    if kind is not None and len(counts) > 1 and counts[kind] * 2 <= sum(counts.values()):
        kind = 'mixed'
    return Slot(key, len(kept), sum(counts.values()), total, (bad / total) if total else 0.0, kind)


@dataclass(frozen=True)
class BusyHours:
    """Learned busy hours for one number, judged at ``now``, in the recipient's zone."""
    slots: dict = field(default_factory=dict)
    zone_name: str = ''
    resets: int = 0

    def slot_at(self, moment):
        """The deciding ``Slot`` for the hour holding ``moment``: its own day of the week, else weekdays pooled."""
        local = _local(moment, _zone(self.zone_name))
        own = self.slots.get(('day', local.weekday(), local.hour))
        if own is not None and own.enough:
            return own
        if local.weekday() < 5:
            pooled = self.slots.get(('weekdays', local.hour))
            if pooled is not None and pooled.enough:
                return pooled
        return None

    def busy_at(self, moment):
        slot = self.slot_at(moment)
        return slot if slot is not None and slot.busy else None

    def busy_slots(self):
        """Every busy hour, pooled weekdays first, for Recipients details and the CLI."""
        return sorted((slot for slot in self.slots.values() if slot.busy),
                      key=lambda slot: (slot.key[0] != 'weekdays', slot.key[1:]))


def learn(observations, now, zone_name=''):
    """``BusyHours`` from a number's finished calls (any order), judged at ``now``.

    Days are counted, not calls: on one day an hour was reached if any call reached it, otherwise it
    counts once with its commonest failure. Walking forward in time, a success in an hour that was busy
    until then clears that hour's evidence (the pooled weekday hour and its own day's hour alike).
    """
    zone = _zone(zone_name)
    days = {}
    for item in observations:
        if item.at > now or item.at <= now - WINDOW or item.kind not in UNREACHABLE + ('reached',):
            continue
        local = _local(item.at, zone)
        key = (local.date(), local.hour)
        seen = days.setdefault(key, {'at': item.at, 'kinds': []})
        seen['at'] = max(seen['at'], item.at)
        seen['kinds'].append(item.kind)
    entries, resets = {}, 0
    for (day, hour), seen in sorted(days.items()):
        kinds = seen['kinds']
        kind = 'reached' if 'reached' in kinds else max(sorted(set(kinds)), key=kinds.count)
        keys = [('day', day.weekday(), hour)] + ([('weekdays', hour)] if day.weekday() < 5 else [])
        for key in keys:
            current = entries.setdefault(key, [])
            if kind == 'reached' and _judge(key, current, seen['at']).busy:
                current.clear()
                resets += 1
            current.append((day, kind, seen['at']))
    return BusyHours({key: _judge(key, items, now) for key, items in entries.items()}, zone_name, resets)


# The decision ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fax:
    """What the scheduler needs to know about one fax (or a group sent in one call)."""
    number: str
    pages: int = 1
    urgent: bool = False
    send_by: datetime | None = None   # naive UTC
    route: str = ''
    preset: str = ''


@dataclass(frozen=True)
class Settings:
    """A recipient's newest schedule row, or the defaults (any time, learning on)."""
    hours: Hours = Hours()
    learn_busy: bool = True
    zone_name: str = ''               # the recipient's zone, else the installation's
    zone_set: bool = False


@dataclass(frozen=True)
class Decision:
    hold_until: datetime | None = None   # naive UTC; None: may start now
    why: str | None = None               # 'hours' or 'busy' while held
    slot: Slot | None = None             # the busy hour that holds it
    charge: str | None = None            # whether a failed try there is charged: 'bills', 'free', 'unknown'
    later: bool = False                  # may start now but goes after faxes that could use the same room
    latest_start: datetime | None = None


def call_time(pages):
    """A typical call for ``pages`` pages, from the predictor's own arithmetic (no recorded calls)."""
    from .predict import Link, Shape, line_seconds
    seconds, _ = line_seconds(Shape(max(1, int(pages or 1)), None, 'fine', 'normal'), Link())
    return timedelta(seconds=seconds or 60 * max(1, int(pages or 1)))


def latest_start(fax):
    """The last moment a fax can start and still go by its send-by time (room for a second try), or None."""
    if fax.send_by is None:
        return None
    return fax.send_by - 2 * call_time(fax.pages) - SAFETY


def decide(fax, settings, busy, now):
    """Whether ``fax`` should start now; never holds an urgent fax or holds one past its latest start."""
    latest = latest_start(fax)
    if fax.urgent:
        return Decision(latest_start=latest)
    start, why = now, None
    hours = settings.hours
    if not hours.always and not hours.open_at(now):
        opens = hours.next_open(now)
        if opens is not None and (latest is None or opens <= latest):
            start, why = opens, 'hours'
    slot = busy.busy_at(start) if (busy is not None and settings.learn_busy) else None
    if slot is None:
        return Decision(start if start > now else None, why, latest_start=latest)
    charge = failed_tries(fax.route, fax.preset).charge(slot.kind if slot.kind in UNREACHABLE else 'busy')
    if charge == 'free':
        # A failed try costs nothing: only a capacity question. It goes after faxes that could use the room.
        return Decision(start if start > now else None, why, later=start <= now, latest_start=latest)
    clear, steps = start, 0
    while steps < 24 * 8 and clear - start <= LOOKAHEAD:
        steps += 1
        if not hours.open_at(clear):
            opened = hours.next_open(clear)
            if opened is None:
                clear = None
                break
            clear = opened
            continue
        if busy.busy_at(clear) is None:
            break
        clear = _next_hour(clear, _zone(busy.zone_name))
    else:
        clear = None
    if clear is None or (latest is not None and clear > latest):
        # Busy for a week, or past what its send-by time allows: the busy hour does not hold it.
        return Decision(start if start > now else None, why, latest_start=latest)
    return Decision(clear, 'busy', slot, charge, latest_start=latest)


# Sentences ---------------------------------------------------------------------------------

def _clock(moment):
    from ..people_time import clock
    return clock(moment)


def _when(moment, now=None):
    """'2:00 PM MDT' today, 'tomorrow at 9:00 AM MDT', 'Monday at 9:00 AM MDT' within a week, else '9 Nov 9:00 AM MST'.

    In the installation's time zone, as every time Faxbot shows.
    """
    from ..people_time import installation_zone_name, short
    now = now or datetime.utcnow()
    zone = _zone(installation_zone_name())
    days = (_local(moment, zone).date() - _local(now, zone).date()).days
    if days == 0:
        return _clock(moment)
    if days == 1:
        return f'tomorrow at {_clock(moment)}'
    if 1 < days < 7:
        return f'{DAY_NAMES[_local(moment, zone).weekday()]} at {_clock(moment)}'
    return short(moment)


def hours_text(hours):
    """'Monday to Friday, 8:00 AM to 6:00 PM' (their time); 'any time' when unset."""
    if hours.always:
        return 'any time'
    days = sorted(hours.days) if hours.days is not None else list(range(7))
    if days == list(range(7)):
        span = 'every day'
    elif days == list(range(5)):
        span = 'Monday to Friday'
    elif days == [5, 6]:
        span = 'Saturday and Sunday'
    elif len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        span = f'{DAY_NAMES[days[0]]} to {DAY_NAMES[days[-1]]}'
    else:
        span = ', '.join(DAY_NAMES[day] for day in days[:-1]) + (f' and {DAY_NAMES[days[-1]]}' if len(days) > 1
                                                                   else DAY_NAMES[days[0]])
    if hours.start is None:
        return span
    return f'{span}, {minute_text(hours.start)} to {minute_text(hours.end)}'


def minute_text(minute):
    minute = minute % 1440 if minute != 1440 else 0
    hour, rest = divmod(minute, 60)
    return f"{hour % 12 or 12}:{rest:02d} {'AM' if hour < 12 else 'PM'}"


def charge_clause(charge, route_label, price_text):
    """', and a failed try costs about $0.05 with eFax' and its unknown variants."""
    if charge == 'bills':
        return (f', and a failed try costs about {price_text} with {route_label}' if price_text
                else f', and {route_label} charges for a failed try')
    return (f', and a failed try may cost about {price_text} with {route_label}' if price_text
            else f', and {route_label} may charge for a failed try')


def reason(decision, settings, *, route_label='this route', price_text=None, now=None):
    """One sentence for Sent details while the scheduler holds a fax, else None."""
    if decision.hold_until is None:
        return None
    until = _when(decision.hold_until, now)
    if decision.why == 'hours':
        # Their hours in their own zone, named by its abbreviation then ("EST"); none when it is the installation's.
        zone = f' {_local(decision.hold_until, _zone(settings.zone_name)).tzname()}' if settings.zone_set else ''
        return f'Waiting until {until}: this recipient takes faxes only {hours_text(settings.hours)}{zone}.'
    if decision.why == 'busy' and decision.slot is not None:
        return (f'Waiting until {until}: {decision.slot.sentence()}'
                f'{charge_clause(decision.charge, route_label, price_text)}.')
    return None


def send_by_view(send_by, state, *, pages=1, finished_at=None, now):
    """The send-by time and one sentence for Sent details and ``faxbot sent show``; None without one."""
    if send_by is None:
        return None
    at = _when(send_by, now)
    latest = latest_start(Fax('', pages=pages or 1, send_by=send_by))
    if state == 'success':
        late = finished_at is not None and finished_at > send_by
        sentence = f"Sent {'after' if late else 'before'} its send-by time of {at}."
    elif state in ('failed', 'cancelled'):
        sentence = f'Send by {at}.'
    elif now >= send_by:
        sentence = f'This fax missed its send-by time of {at}. Faxbot still sends it as soon as it can.'
    elif state == 'ready' and now >= latest:
        sentence = f'This fax may miss its send-by time of {at}: it has not started yet.'
    else:
        sentence = f'Send by {at}.'
    return {'at': send_by.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z'), 'sentence': sentence,
            'at_risk': state not in ('success', 'failed', 'cancelled') and now >= latest}


# A send-by time from people ------------------------------------------------------------------

# The furthest ahead a send-by time may be.
SEND_BY_HORIZON = timedelta(days=31)


def parse_send_by(value, now, zone_name='', *, check=True):
    """Naive UTC from a send-by time, or None for none; raises ValueError with a sentence for people.

    Accepts a date and time with an offset ('2026-10-08T17:00-04:00', '...Z'), a date and time without
    one ('2026-10-08 17:00', read in ``zone_name``, the installation's zone), or a time alone ('17:00':
    its next occurrence there). ``check`` refuses a time that has passed or is over 31 days away; a replay
    of an accepted request is read without it, so it still finds its original fax.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    zone = _zone(zone_name)
    try:
        if len(text) <= 5 and ':' in text:
            minute = parse_clock(text)
            local = _local(now, zone)
            moment = _at(local.date(), minute, zone)
            if moment <= now:
                moment = _at(local.date() + timedelta(days=1), minute, zone)
        else:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00').replace(' ', 'T', 1))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
            moment = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        raise ValueError('Give the send-by time as a date and time, such as 2026-10-08 17:00, or a time '
                         'such as 17:00.') from None
    if check:
        check_send_by(moment, now)
    return moment


def check_send_by(moment, now):
    """Refuse a send-by time that has passed or is more than 31 days away."""
    if moment is None:
        return
    if moment <= now:
        raise ValueError('The send-by time has already passed.')
    if moment > now + SEND_BY_HORIZON:
        raise ValueError('Choose a send-by time within the next 31 days.')


def too_soon_to_wait(send_by, pages, now, wait=timedelta(hours=1)):
    """Whether a fax with this send-by time must not wait to go with other faxes (that wait can last ``wait``)."""
    latest = latest_start(Fax('', pages=pages or 1, send_by=send_by))
    return latest is not None and latest - now < wait


# Reading and saving --------------------------------------------------------------------------

def parse_days(value):
    """'mon,tue' or a list of day names -> frozenset of 0-6; None or empty: every day."""
    if value is None:
        return None
    names = value.split(',') if isinstance(value, str) else list(value)
    names = [str(name).strip().lower()[:3] for name in names if str(name).strip()]
    if not names:
        return None
    unknown = [name for name in names if name not in DAYS]
    if unknown:
        raise ValueError('Name days as Monday to Sunday.')
    return frozenset(DAYS.index(name) for name in names)


def parse_clock(value, *, end=False):
    """'08:00' -> 480; '24:00' only as an end time."""
    if value is None or value == '':
        return None
    try:
        hour, minute = (int(part) for part in str(value).strip().split(':'))
    except ValueError:
        raise ValueError('Give times as hours and minutes, such as 08:00.') from None
    total = hour * 60 + minute
    if not 0 <= minute < 60 or not 0 <= total <= (1440 if end else 1439):
        raise ValueError('Give times as hours and minutes, such as 08:00.')
    return total


def usable_zone(name):
    """An IANA zone name Faxbot can use, or '' for none; raises for a name it cannot."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = str(name or '').strip()
    if not name:
        return ''
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError('Choose a time zone from the list, such as America/New_York.') from None
    return name


class Scheduler:
    """Reads what decisions need: a recipient's newest schedule row and its finished calls."""

    def __init__(self, engine, connection=None):
        """Inside an open transaction pass its ``connection``: reflection then reads through it."""
        bind = connection if connection is not None else engine
        inspector = sa.inspect(bind)
        present = set(inspector.get_table_names())
        metadata = sa.MetaData()
        wanted = [name for name in ('outbound_attempts', 'fax_jobs', 'outbound_deliveries', 'sip_call_records',
                                    'outbound_events', TABLE) if name in present]
        metadata.reflect(bind, only=wanted)
        self.engine = engine
        self.t = {name: metadata.tables[name] for name in wanted}
        self.has_send_by = 'fax_jobs' in self.t and 'send_by' in self.t['fax_jobs'].c

    # Settings -----------------------------------------------------------------------------
    def settings(self, connection, number, values=None):
        from ..people_time import installation_zone_name
        fallback = getattr(values, 'time_zone', None) if values is not None else None
        fallback = installation_zone_name() if fallback is None else (fallback or '')
        table = self.t.get(TABLE)
        row = None
        if table is not None:
            row = connection.execute(sa.select(table).where(table.c.phone_number == number)
                                     .order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)
                                     ).mappings().one_or_none()
        if row is None:
            return Settings(Hours(zone_name=fallback), True, fallback, False)
        zone_name = row['time_zone'] or fallback
        days = parse_days(row['days']) if row['days'] else None
        hours = Hours(days, row['start_minute'], row['end_minute'], zone_name)
        if hours.start is not None and hours.end is None:
            hours = Hours(days, hours.start, 1440, zone_name)
        return Settings(hours, bool(row['learn_busy']), zone_name, bool(row['time_zone']))

    def save(self, number, *, time_zone, days, start, end, learn_busy, actor=None, actor_name=None, now=None):
        table = self.t.get(TABLE)
        if table is None:
            raise RuntimeError('Recipient schedules are not available until the database is upgraded.')
        if (start is None) != (end is None):
            raise ValueError('Give both the time the recipient starts taking faxes and the time it stops.')
        if start is not None and start == end % 1440:
            raise ValueError('The start and end times must differ.')
        row = {'id': uuid4().hex, 'phone_number': number, 'time_zone': usable_zone(time_zone) or None,
               'days': ','.join(DAYS[day] for day in sorted(days)) if days is not None and len(days) < 7 else None,
               'start_minute': start, 'end_minute': end, 'learn_busy': 1 if learn_busy else 0,
               'recorded_by': (str(actor)[:40] if actor else None),
               'recorded_by_name': (str(actor_name)[:200] if actor_name else None),
               'created_at': now or datetime.utcnow()}
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**row))
        return row

    # Learning ----------------------------------------------------------------------------
    def observations(self, connection, number, now):
        a, j = self.t['outbound_attempts'], self.t['fax_jobs']
        d, s, e = self.t.get('outbound_deliveries'), self.t.get('sip_call_records'), self.t.get('outbound_events')
        columns = [a.c.id, a.c.phase, a.c.submitted_at, j.c.error]
        source = a.join(j, j.c.id == a.c.job_id)
        if d is not None:
            columns.append(d.c.attempt_id.label('current'))
            source = source.outerjoin(d, d.c.id == a.c.job_id)
        if s is not None:
            columns += [s.c.disposition, s.c.started_at, s.c.pages]
            source = source.outerjoin(s, sa.and_(s.c.attempt_id == a.c.id, s.c.direction == 'outbound'))
        if e is not None:
            columns.append(e.c.details)
            source = source.outerjoin(e, sa.and_(e.c.attempt_id == a.c.id, e.c.kind == 'route_fallback'))
        rows = connection.execute(
            sa.select(*columns).select_from(source)
            .where(j.c.to_number == number, a.c.submitted_at.is_not(None), a.c.submitted_at > now - WINDOW,
                   a.c.submitted_at <= now)
            .order_by(a.c.submitted_at.desc(), a.c.id).limit(HISTORY_READ)).mappings().all()
        seen, found = set(), []
        for row in rows:
            if row['id'] in seen:
                continue
            seen.add(row['id'])
            sentence = None
            if row.get('details'):
                try:
                    sentence = json.loads(row['details']).get('reason')
                except (ValueError, AttributeError):
                    sentence = None
            if sentence is None and row.get('current') == row['id']:
                sentence = row['error']
            kind = classify(phase=row['phase'], disposition=row.get('disposition'), pages=row.get('pages'),
                            sentence=sentence)
            if kind is not None:
                found.append(Observation(row.get('started_at') or row['submitted_at'], kind))
        return found

    def busy_hours(self, connection, number, settings, now):
        if 'outbound_attempts' not in self.t or 'fax_jobs' not in self.t:
            return BusyHours({}, settings.zone_name)
        return learn(self.observations(connection, number, now), now, settings.zone_name)

    # Decisions ----------------------------------------------------------------------------
    def fax_on(self, connection, job_ids, values):
        """A ``Fax`` for one fax or for faxes sent together in one call (any urgent: urgent; earliest send-by)."""
        j = self.t['fax_jobs']
        columns = [j.c.to_number, j.c.pages, j.c.backend]
        columns += [j.c.urgent] if 'urgent' in j.c else []
        columns += [j.c.send_by] if self.has_send_by else []
        rows = connection.execute(sa.select(*columns).where(j.c.id.in_(list(job_ids)))).mappings().all()
        if not rows:
            return None
        deadlines = [row['send_by'] for row in rows if row.get('send_by') is not None]
        return Fax(rows[0]['to_number'], pages=sum(int(row['pages'] or 1) for row in rows),
                   urgent=any(row.get('urgent') for row in rows), send_by=min(deadlines) if deadlines else None,
                   route=rows[0]['backend'] or '', preset=getattr(values, 'sip_trunk_preset', '') or '')

    def decide_on(self, connection, fax, values, now, memo=None):
        """(``Decision``, ``Settings``); ``memo`` keeps each number's settings and busy hours for one claim."""
        memo = {} if memo is None else memo
        if fax.number not in memo:
            memo[fax.number] = [self.settings(connection, fax.number, values), None]
        settings = memo[fax.number][0]
        if fax.urgent:
            return decide(fax, settings, None, now), settings
        if settings.learn_busy and memo[fax.number][1] is None:
            memo[fax.number][1] = self.busy_hours(connection, fax.number, settings, now)
        return decide(fax, settings, memo[fax.number][1] if settings.learn_busy else None, now), settings


_CACHE = {}


def for_engine(engine, connection=None):
    """One ``Scheduler`` per engine, reflected once (through ``connection`` inside an open transaction)."""
    cached = _CACHE.get(id(engine))
    if cached is not None and cached.engine is engine:
        return cached
    scheduler = Scheduler(engine, connection)
    if len(_CACHE) > 16:
        _CACHE.clear()
    _CACHE[id(engine)] = scheduler
    return scheduler


def attempt_price(route, number, pages, *, now=None):
    """The predictor's price of one try on ``route`` as text ('$0.05'), or None when unknown (never 0).

    Only the predictor's own refusal (ValueError) or an unreadable database makes it unknown, and the log says
    why; any other error is a bug and is raised."""
    from .predict import Shape, amount_text, predict
    try:
        prediction = predict(route, number, Shape(max(1, int(pages or 1)), None, 'fine', 'normal'), now=now)
    except (ValueError, sa.exc.SQLAlchemyError):
        import logging
        logging.getLogger(__name__).warning('The price of a try could not be predicted.', exc_info=True)
        return None
    return amount_text(prediction) if prediction.cost is not None and prediction.cost.micros > 0 else None
