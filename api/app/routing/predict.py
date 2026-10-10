"""One shared pre-dial predictor: what a fax would take and cost on a route, before dialing (M2, M4).

Dense pages (AF), the fax codec (AG), advice (AD), the dry run and Send a fax
all ask the same question, "what would this fax cost on this route?", and get
the same answer from here.

Contract for callers
--------------------
``predict(route_key, destination, shape, *, now=None) -> Prediction``

- ``route_key`` is a ``RouteCandidate.key``: ``sip`` (the installation's own
  fax engine on its SIP trunk, priced by the trunk carrier's card,
  ``sip-<preset>``, as ``RouteStore.card_for`` does), a provider identity such
  as ``sinch``, ``phaxio`` or ``humblefax``, or one of the no-call routes
  ``local`` (one of the installation's own numbers) and ``direct`` (a verified
  partner over the internet), which cost nothing.
- ``destination`` is the number in E.164 (a national number is read for the
  installation's country).
- ``shape`` describes the fax as it will be sent. ``page_bits`` are the
  compressed bits of each page in the prepared fax image (8 x the TIFF's
  strip byte counts; ``tiff_page_bits`` reads them). Pass them whenever the
  image exists: they decide the time on the line far better than any typical
  page.
- ``Prediction.seconds`` is the predicted time on the line, from answer to
  hang-up, unrounded. What the carrier bills follows from the card's increment
  and minimum (``costs.billed_seconds``; ``boundary`` gives both).
- ``Prediction.billed_pages`` is the number of pages charged at a page price:
  the pages sent on a per-page route, the greater-of count under a page-or-time
  rule, pages past a plan's allowance, and 0 on a route that has no page price
  (per minute, per call, or included in a plan).
- ``None`` always means unknown and is never 0. A cost is None when the route
  publishes no price for this kind of number, or when it bills by time that
  cannot be predicted.
- ``marginal`` is True for a monthly plan: the cost is what this one fax adds
  (``Money(0)`` while it is included, overage pages past an allowance). The
  monthly fee is never spread over one fax here.
- ``basis`` is one plain sentence for the administrator, saying how the figure
  was worked out. Every figure is an estimate and is shown as one.

``predict`` gathers the facts (rate card for the number's class, what earlier
calls showed, plan use) through ``predict_facts`` and hands them to
``predict_from``, which is pure: no database, no network, no clock. Importing
this module opens nothing. Without an installation database the facts are
the shipped published prices and the cited defaults below, so other modules'
tests can call ``predict`` in isolation.

How the time is worked out
--------------------------
seconds = setup + (bits on the line / speed) + page handshake x pages

- Bits on the line: the bits of each page measured in the coding the call
  uses (``Shape.measured``, ``pages.coding.measure``: MH, MR and MMR measured
  on the actual pages). Without a measurement for that coding, the page bits
  Faxbot prepared (T.6, MMR) times a fixed factor (``WIRE_FACTOR``), and the
  sentence says it is estimated that way: on AR's synthetic pages the fixed
  factor overestimated a noisy gray scan's MH about threefold.
- Speed: what earlier successful calls to this number on this route reached,
  else what calls on this route usually reach, else the route's typical speed.
- Without page bits, the seconds a page that earlier calls to this number
  took, else a typical page (``TYPICAL_PAGE_BITS``).

How the price is worked out
---------------------------
A call is priced by its expected bill over the predicted duration's spread,
E[ceil(D / increment) x increment], never by rounding the expected duration: a
call of 59 or 61 seconds with equal odds lasts 60 seconds on average but bills
90 seconds on 60-second steps. The spread comes from the destination's own
recorded calls (each call's setup and seconds a page, on the engine that
placed the newest call to it, ``predict_facts.learn``), else the stated
default ``DEFAULT_SPREAD``, which is an estimate. ``Prediction.p90_seconds``
is the time 9 in 10 such calls finish within; unknown stays unknown.

Constants and their sources (see each one): measured Faxbot calls where they
exist, ITU-T T.30 timings where they don't. ``SETUP_SECONDS`` and
``PAGE_SECONDS`` were measured on single calls and should be re-fitted once
enough recorded calls exist; ``predict_facts`` replaces them per number as
soon as it has recorded calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import math
import statistics

from .costs import (Money, RateTerms, billed_seconds, format_amount, greater_of_pages, money_text, plan_fee_text,
                    terms_cost)
from .destinations import CLASS_TEXT, INTERNATIONAL, LOCAL, DestinationClass, country_name


RESOLUTIONS = ('standard', 'fine', 'superfine', '300', '400')
LAYOUTS = ('normal', 'dense', 'codec')
NO_CALL_ROUTES = ('local', 'direct')
CODINGS = ('MH', 'MR', 'MMR', 'JBIG')

# The route's typical speed when no recorded call says otherwise, in bit/s. V.17's top speed, which Faxbot's
# trunk offers over T.38 (``sip_trunk.fax_options``; asterisk/extensions-options.conf: 14,400 over T.38).
TYPICAL_RATE = 14400
# The speed Faxbot offers when the trunk sends fax as audio (the same file: 9,600 on audio).
AUDIO_RATE = 9600
# Seconds from answer to the first page plus the end of the call. ITU-T T.30 phase B and E: answer tone
# 2.6-4 s, DIS, DCS and 1.5 s of training check, CFR (each V.21 frame about 1 s of preamble), then EOP, MCF
# and DCN: about 11 s. Measured: a live engine call on 2026-10-05 was connected 31 s with 22 s of pages
# (9 s); 11 s keeps the estimate on the carrier's side.
SETUP_SECONDS = 11.0
# The handshake around each page (MPS or PPS, MCF, and retraining before the next page), in seconds.
# Measured 2026-10-03 (SSL Fax spike, HylaFAX+ over IAXmodem loopback, 14,400 bit/s with error correction):
# 6 pages took 50 s of transfer at 10,270 bytes a page, so 5.7 s of data and 2.6 s of handshake a page.
PAGE_SECONDS = 2.6
# Bits of a typical text page as Faxbot prepares it (T.6), by resolution, when the image was not read.
# Standard: the spike's 82,160 JBIG bits a page / 0.8 (``WIRE_FACTOR``). Other resolutions scale it by the
# lines and dots a page holds; a default to replace with ``page_bits``, not a measurement.
TYPICAL_PAGE_BITS = {'standard': 103_000, 'fine': 180_000, 'superfine': 330_000, '300': 360_000,
                     '400': 560_000}
# A dense page (AF) holds more marks than a normal one. A default; dense pages should pass ``page_bits``.
LAYOUT_FACTOR = {'normal': 1.0, 'dense': 1.8}
# Bits on the line for each prepared (T.6) bit, by the coding the call uses. From the CCITT test set's
# average compression (MH about 10:1, MR about 14.6:1, MMR about 19.6:1, JBIG about 24:1 on text); MMR and
# JBIG need error correction. Unsure: from published averages, not measured on Faxbot's pages.
WIRE_FACTOR = {'MMR': 1.0, 'MR': 1.35, 'MH': 2.0, 'JBIG': 0.8}
# Assumed until a call to the number shows its coding: 2-D coding, which nearly every fax machine accepts.
DEFAULT_CODING = 'MR'
# Recorded calls needed before a learned figure replaces a default.
MIN_CALLS = 3
# The spread of a call's time after setup when the number has fewer than MIN_CALLS recorded calls: 15% shorter or
# longer a quarter of the time each. An estimate, stated as one wherever it is used; recorded calls replace it.
DEFAULT_SPREAD = ((0.85, 0.25), (1.0, 0.5), (1.15, 0.25))
# The share of calls the completion time covers (9 in 10).
COMPLETION_SHARE = 0.9


class ShapeRefused(ValueError):
    """A documented refusal: a ``Shape`` with an impossible page count, page bits, resolution, layout or coding."""


@dataclass(frozen=True)
class Shape:
    pages: int
    page_bits: tuple[int, ...] | None   # compressed bits per page as they will be sent, if known
    resolution: str                      # 'standard' | 'fine' | 'superfine' | '300' | '400'
    layout: str                          # 'normal' | 'dense' | 'codec'
    # {coding: bits per page} measured on the actual pages (pages.coding.measure); kept as sorted pairs.
    measured: object = None
    coding: str | None = None            # the coding the call uses, when chosen ('MH', 'MR', 'MMR', 'JBIG')

    def __post_init__(self):
        if type(self.pages) is not int or not 1 <= self.pages <= 10_000:
            raise ShapeRefused('A fax has from 1 to 10,000 pages.')
        if self.page_bits is not None and (
                not isinstance(self.page_bits, tuple) or len(self.page_bits) != self.pages
                or any(type(bits) is not int or bits < 0 for bits in self.page_bits)):
            raise ShapeRefused('Give the compressed bits of every page, as whole numbers.')
        if self.resolution not in RESOLUTIONS:
            raise ShapeRefused('Choose standard, fine, superfine, 300 or 400 resolution.')
        if self.layout not in LAYOUTS:
            raise ShapeRefused('Choose a normal, dense or codec layout.')
        if self.coding is not None and self.coding not in CODINGS:
            raise ShapeRefused('Choose the MH, MR, MMR or JBIG coding.')
        if self.measured is not None:
            items = self.measured.items() if hasattr(self.measured, 'items') else self.measured
            try:
                found = {name: tuple(bits) for name, bits in items}
            except (TypeError, ValueError):
                raise ShapeRefused('Give the measured bits of every page for each coding.') from None
            if any(name not in CODINGS or len(bits) != self.pages or any(type(value) is not int or value < 0
                                                                         for value in bits)
                   for name, bits in found.items()):
                raise ShapeRefused('Give the measured bits of every page for each coding.')
            object.__setattr__(self, 'measured', tuple((name, found[name]) for name in CODINGS if name in found)
                               or None)

    def bits_for(self, coding):
        """The measured bits of each page in ``coding``, or None when that coding was not measured."""
        return next((bits for name, bits in self.measured or () if name == coding), None)


@dataclass(frozen=True)
class Prediction:
    billed_pages: int | None
    seconds: float | None
    cost: Money | None                   # the Money type from routing/costs; unknown is None, never 0
    basis: str                           # one plain sentence: how this was worked out
    marginal: bool                       # True when the cost is the marginal cost (flat plans: 0 plus fair-use)
    # The expected billed seconds over the duration's spread, on a route that bills by time; None otherwise.
    expected_billed_seconds: float | None = None
    p90_seconds: float | None = None     # the time 9 in 10 such calls finish within; None when unknown
    increment_seconds: int | None = None  # one billing step, on a route that bills by time
    spread: str | None = None            # how the spread was found, as a clause; None when the time is unknown
    expected_billed_pages: float | None = None  # under a page-or-time rule, the pages expected to be billed


@dataclass(frozen=True)
class ExpectedBill:
    """What a call is expected to bill over its duration's spread (``expected_bill``)."""
    units: float | None                  # expected billing steps (per-minute) or pages (page-or-time); None otherwise
    increment_seconds: int | None        # one billing step in seconds, when the route bills by time
    billed_seconds: float | None         # expected billed seconds, when the route bills by time
    cost: Money | None                   # the expected cost; None when unknown, never 0
    seconds: float | None                # the predicted time on the line
    p90_seconds: float | None            # the time 9 in 10 such calls finish within
    basis: str


# Facts ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Link:
    """What recorded calls on one route showed about one number (or the route as a whole).

    ``predict_facts`` fills it only from successful calls whose engine reported
    the negotiation; a field is None until there is evidence.
    """
    rate: int | None = None              # the speed calls reached, bit/s
    rate_calls: int = 0
    rate_scope: str | None = None        # 'number': calls to this number; 'route': calls to any number
    coding: str | None = None            # the coding calls to this number used ('MH', 'MR', 'MMR', 'JBIG')
    seconds_per_page: float | None = None  # seconds a page took after setup, on calls to this number
    page_calls: int = 0
    setup_seconds: float | None = None   # seconds outside the pages, on calls to this number
    setup_calls: int = 0
    typical_rate: int = TYPICAL_RATE     # the route's own speed setting, when nothing was learned
    jbig: bool = False                   # a call to this number used JBIG on the SSL Fax engine
    # Each recorded call's (seconds outside the pages or None, seconds a page), for the spread of a call's time,
    # from the calls the engine that placed the newest one made (all engines when it made too few).
    samples: tuple = ()
    # This hour's time a page against the number's typical hour (1.4: 40% more), from learned call hours
    # (routing/schedule.py ``HourTiming.factor_at``), when both have enough calls; None otherwise. ``hour_scope``
    # says whose calls: 'number' (calls to this number) or 'route' (every number on the trunk).
    hour_factor: float | None = None
    hour_scope: str | None = None

    def __post_init__(self):
        if self.coding is not None and self.coding not in CODINGS:
            raise ValueError('Unknown fax coding.')
        if self.rate_scope not in (None, 'number', 'route'):
            raise ValueError('Unknown speed scope.')


@dataclass(frozen=True)
class PlanUse:
    """How much of a monthly plan this month already used; None when Faxbot has no count."""
    pages: int | None = None
    faxes: int | None = None
    page_budget: int | None = None       # a fair-use budget the administrator set, if any
    minutes: int | None = None           # minutes on the line this month, for a minute allowance


@dataclass(frozen=True)
class RouteFacts:
    route_key: str
    label: str                           # the route's name in a sentence ("Telnyx", "Sinch")
    destination: DestinationClass
    terms: RateTerms | None              # the price for this number's class on this route; None: not published
    link: Link = Link()
    plan: PlanUse | None = None
    currency: str = 'USD'                # the currency of a no-call route's nothing
    missing: str | None = None           # why there is no price, as a clause, when ``terms`` is None
    refused: bool = False                # the route does not take this kind of number; ``missing`` says so
    origin: str | None = None            # the origin-rated row that priced the call ('any', a site, 'country:GB')
    # When nothing published prices the number: the clause saying the rate was learned from this account's own carrier
    # records, and when ('the rate 4 of your Telnyx call records to numbers in the United Kingdom showed, ...').
    learned: str | None = None


# Sentences --------------------------------------------------------------------------

def duration_text(seconds):
    """'about 52 seconds', 'about 2 minutes 10 seconds'."""
    whole = max(0, int(round(seconds)))
    if whole < 60:
        return f"about {whole} second{'' if whole == 1 else 's'}"
    minutes, rest = divmod(whole, 60)
    text = f"about {minutes} minute{'' if minutes == 1 else 's'}"
    return text + (f" {rest} second{'' if rest == 1 else 's'}" if rest else '')


def _count(number, noun):
    """'1 page', '3 pages', '2 earlier faxes'."""
    if number == 1:
        return f'{number} {noun}'
    return f"{number} {noun}{'es' if noun.endswith(('x', 's')) else 's'}"


def _price_clause(terms, billed_pages, seconds):
    """How the bill is made up, as a clause: 'billed as 1 minute at $0.005 a minute'."""
    card = terms.card
    parts = []
    if card.per_minute_micros and seconds is not None:
        billed = billed_seconds(card, seconds)
        unit = (_count(billed // 60, 'minute') if billed % 60 == 0 else _count(billed, 'second'))
        parts.append(f'{unit} at {money_text(card.per_minute_micros, card.currency)} a minute')
    if terms.page_time_seconds and billed_pages is not None:
        unit = 'minute' if terms.page_time_seconds == 60 else f'{terms.page_time_seconds} seconds'
        parts.append(f'{_count(billed_pages, "page")} at {money_text(card.per_page_micros, card.currency)} a page '
                     f'(the greater of the pages sent and each started {unit} on the line)')
    elif card.per_page_micros and billed_pages:
        parts.append(f'{_count(billed_pages, "page")} at {money_text(card.per_page_micros, card.currency)} a page')
    if card.per_call_micros:
        parts.append(f'{money_text(card.per_call_micros, card.currency)} for the call')
    return 'billed as ' + ' plus '.join(parts) if parts else None


def _where(destination):
    if destination.kind == INTERNATIONAL:
        return f'numbers in {country_name(destination.region)}'
    return CLASS_TEXT[destination.kind]


def _what(facts):
    """A trunk places calls; a fax service sends faxes."""
    return 'calls' if facts.route_key == 'sip' else 'faxes'


def _sentence(*clauses):
    text = '; '.join(clause for clause in clauses if clause)
    return text[:1].upper() + text[1:] + '.'


# The time on the line ------------------------------------------------------------------

def _speed(link):
    """(bit/s, clause) the call is expected to reach."""
    if link.rate and link.rate_scope == 'number':
        return link.rate, f'at the speed {_count(link.rate_calls, "earlier fax")} to this number reached'
    if link.rate and link.rate_scope == 'route':
        return link.rate, f'at the usual speed of {_count(link.rate_calls, "earlier fax")} on this route'
    return link.typical_rate, 'at a typical fax speed'


def _setup(link):
    if link.setup_seconds is not None and link.setup_calls >= MIN_CALLS:
        return link.setup_seconds
    return SETUP_SECONDS


def call_coding(shape, link, coding=None):
    """The coding the call is priced with: the one asked for, the shape's, what calls to the number used, else
    the default."""
    return coding or shape.coding or link.coding or DEFAULT_CODING


def line_seconds(shape, link, *, coding=None):
    """(seconds, clause): the predicted time on the line and how it was worked out; seconds None when unknown."""
    setup = _setup(link)
    coding = call_coding(shape, link, coding)
    measured = shape.bits_for(coding)
    if measured is not None:
        rate, how = _speed(link)
        seconds = setup + sum(measured) / rate + PAGE_SECONDS * shape.pages
        return seconds, f'{duration_text(seconds)} on the line, from the measured size of each page in {coding} {how}'
    if shape.page_bits is not None:
        rate, how = _speed(link)
        data = sum(shape.page_bits) * WIRE_FACTOR[coding] / rate
        seconds = setup + data + PAGE_SECONDS * shape.pages
        estimate = '' if coding == 'MMR' else f', with {coding} estimated from a fixed ratio to MMR'
        return seconds, f'{duration_text(seconds)} on the line, from the size of each page {how}{estimate}'
    if shape.layout == 'codec':
        return None, "the time on the line is unknown, because a coded page's size depends on its data"
    factor = LAYOUT_FACTOR[shape.layout]
    if link.seconds_per_page is not None and link.page_calls >= MIN_CALLS:
        data = max(0.0, link.seconds_per_page - PAGE_SECONDS)
        seconds = setup + shape.pages * (PAGE_SECONDS + data * factor)
        return seconds, (f'{duration_text(seconds)} on the line, from the time a page took on '
                         f'{_count(link.page_calls, "earlier fax")} to this number')
    rate, how = _speed(link)
    bits = TYPICAL_PAGE_BITS[shape.resolution] * factor * WIRE_FACTOR[coding]
    seconds = setup + shape.pages * (bits / rate + PAGE_SECONDS)
    return seconds, f'{duration_text(seconds)} on the line, for typical pages {how}'


# The spread of a call's time ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Spread:
    """The predicted time on the line as a distribution: ((seconds, share), ...), shares adding up to 1."""
    points: tuple
    learned: bool                        # from the number's own recorded calls; False: ``DEFAULT_SPREAD``
    calls: int = 0
    learns: bool = True                  # the route learns from its calls (Faxbot's own trunk); a fax service does not

    def percentile(self, share=COMPLETION_SHARE):
        """The shortest time that this share of calls finishes within."""
        total = 0.0
        ordered = sorted(self.points)
        for seconds, weight in ordered:
            total += weight
            if total >= share - 1e-9:
                return seconds
        return ordered[-1][0]

    def clause(self):
        if self.learned:
            return f'the spread of {_count(self.calls, "earlier fax")} to this number'
        if not self.learns:
            return 'an assumed spread of 15% either way'
        return f'an assumed spread of 15% either way until this number has {MIN_CALLS} faxes of its own'


# The hour of the call (M26) -------------------------------------------------------------------------------

# An hour within this share of the typical hour changes nothing (and says nothing).
HOUR_EFFECT_FLOOR = 0.1


def hour_effect(seconds, how, link):
    """(seconds, clause) with the time after setup scaled by this hour's learned time a page (``Link.hour_factor``)
    when it differs from the number's typical hour by at least ``HOUR_EFFECT_FLOOR``; unchanged otherwise."""
    factor = link.hour_factor
    if seconds is None or not factor or abs(factor - 1) < HOUR_EFFECT_FLOOR:
        return seconds, how
    setup = min(_setup(link), seconds)
    adjusted = setup + (seconds - setup) * factor
    before = duration_text(seconds)
    if how.startswith(before):
        how = duration_text(adjusted) + how[len(before):]
    whose = 'calls to this number' if link.hour_scope != 'route' else 'calls on this phone line'
    percent = round(abs(factor - 1) * 100)
    change = 'more' if factor > 1 else 'less'
    return adjusted, f'{how}, and {whose} take about {percent}% {change} time a page at this hour'


def spread_for(seconds, link, *, learns=True):
    """The spread of a call predicted at ``seconds``: each recorded call to the number (``Link.samples``) scales
    the time after setup by its seconds a page against their median, with its own setup when known; with fewer
    than ``MIN_CALLS``, ``DEFAULT_SPREAD``. None when the time is unknown."""
    if seconds is None:
        return None
    setup = min(_setup(link), seconds)
    after = max(0.0, seconds - setup)
    samples = [(start, page) for start, page in link.samples if page is not None and page > 0]
    if len(samples) >= MIN_CALLS:
        middle = statistics.median(page for _, page in samples)
        weight = 1 / len(samples)
        points = tuple((max(0.0, (setup if start is None else start) + after * page / middle), weight)
                       for start, page in samples)
        return Spread(points, True, len(samples))
    return Spread(tuple((setup + after * factor, weight) for factor, weight in DEFAULT_SPREAD), False, learns=learns)


def _expected(points, value):
    """Sum of ``value(seconds) x share`` over the spread; None when any point's value is unknown."""
    total = 0.0
    for seconds, weight in points:
        found = value(seconds)
        if found is None:
            return None
        total += found * weight
    return total


def _billed_by_time(terms):
    return bool(terms.card.per_minute_micros or terms.page_time_seconds)


def expected_terms_cost(terms, spread, pages):
    """(expected micros as a whole number rounded up, or None; expected billed seconds or None) of one delivered
    fax under ``terms`` over ``spread``: E[cost(D)], never the cost of the expected D."""
    if spread is None:
        _, micros = terms_cost(terms, seconds=None, pages=pages)
        return micros, None
    micros = _expected(spread.points, lambda seconds: terms_cost(terms, seconds=seconds, pages=pages)[1])
    billed = (_expected(spread.points, lambda seconds: billed_seconds(terms.card, seconds))
              if terms.card.per_minute_micros else None)
    return (None if micros is None else math.ceil(micros - 1e-6)), billed


def expected_pages(terms, spread, pages):
    """Under a page-or-time rule, the pages expected to be billed over ``spread``; None otherwise."""
    if spread is None or not terms.page_time_seconds:
        return None
    return _expected(spread.points, lambda seconds: greater_of_pages(pages, seconds, terms.page_time_seconds))


# The price ----------------------------------------------------------------------------

def _plan(facts, shape, seconds, how, spread=None):
    """A monthly plan: what this fax adds to the bill (marginal), with the plan's room in the sentence."""
    terms, plan, label = facts.terms, facts.plan or PlanUse(), facts.label
    card = terms.card
    fee = plan_fee_text(card.monthly_fee_micros, card.currency) if card.monthly_fee_micros else None
    named = f'your {label} plan' + (f' ({fee} a month)' if fee else '')
    if terms.included_minutes and not terms.included_pages:
        return _minutes(terms, plan, named, seconds, how, spread)
    if terms.included_pages:
        if plan.pages is None:
            return Prediction(None, seconds, None, _sentence(
                f'{named} includes {terms.included_pages} pages a month, and Faxbot has no count of the pages sent '
                'on it this month, so whether this fax costs extra is unknown', how), True)
        before = max(0, plan.pages - terms.included_pages)
        over = max(0, plan.pages + shape.pages - terms.included_pages) - before
        room = f'{plan.pages} of its {terms.included_pages} included pages used this month'
        if not over:
            return Prediction(0, seconds, Money(0, card.currency),
                              _sentence(f'Included in {named}, so this fax adds nothing to the bill', room, how), True)
        if terms.overage_page_micros is None:
            return Prediction(over, seconds, None, _sentence(
                f'{_count(over, "page")} of this fax would go past what {named} includes, and the price of extra '
                'pages is not published', room, how), True)
        return Prediction(over, seconds, Money(over * terms.overage_page_micros, card.currency), _sentence(
            f'{_count(over, "page")} past what {named} includes, at '
            f'{money_text(terms.overage_page_micros, card.currency)} a page', room, how), True)
    room = None
    if plan.pages is not None:
        room = f'{_count(plan.pages, "page")} sent on it this month'
        if plan.page_budget:
            room = f'{plan.pages} of the {plan.page_budget} pages you allow on it a month used so far'
            if plan.pages + shape.pages > plan.page_budget:
                room += ', and this fax would go past that'
    return Prediction(0, seconds, Money(0, card.currency),
                      _sentence(f'Included in {named}, so this fax adds nothing to the bill', room, how), True)


def _minutes(terms, plan, named, seconds, how, spread=None):
    """A minute allowance: this fax is free while its minutes fit, and minutes past it cost the per-minute price,
    expected over the call's spread."""
    card = terms.card
    if seconds is None:
        return Prediction(None, None, None, _sentence(
            f'{named} includes {terms.included_minutes} minutes a month, and the time on the line is unknown, so '
            'whether this fax costs extra is unknown', how), True)
    if plan.minutes is None:
        return Prediction(None, seconds, None, _sentence(
            f'{named} includes {terms.included_minutes} minutes a month, and Faxbot has no count of the minutes '
            'used this month, so whether this fax costs extra is unknown', how), True)
    before = max(0, plan.minutes - terms.included_minutes)

    def over_at(duration):
        return max(0, plan.minutes + math.ceil(duration / 60) - terms.included_minutes) - before
    over = over_at(seconds)
    expected = _expected(spread.points, over_at) if spread is not None else over
    micros = math.ceil(expected * card.per_minute_micros - 1e-6)
    room = f'{plan.minutes} of its {terms.included_minutes} included minutes used this month'
    if not micros:
        return Prediction(0, seconds, Money(0, card.currency),
                          _sentence(f'Included in {named}, so this fax adds nothing to the bill', room, how), True)
    if not over:
        return Prediction(0, seconds, Money(micros, card.currency), _sentence(
            f'Included in {named} unless the call runs past its minutes, so about '
            f'{money_text(micros, card.currency)} is expected', room, how), True)
    clause = f'{_count(over, "minute")} past what {named} includes, at {money_text(card.per_minute_micros, card.currency)} a minute'
    if micros != over * card.per_minute_micros:
        clause += f', so about {money_text(micros, card.currency)} is expected'
    return Prediction(0, seconds, Money(micros, card.currency), _sentence(clause, room, how), True)


def _spread_price(terms, spread, pages, central, expected):
    """', or more about 25% of the time, so about $0.00625 is expected' when the spread crosses a billing step."""
    if spread is None or expected is None or expected == central:
        return ''
    costs = [(terms_cost(terms, seconds=seconds, pages=pages)[1], weight) for seconds, weight in spread.points]
    higher = sum(weight for micros, weight in costs if micros is not None and micros > central)
    lower = sum(weight for micros, weight in costs if micros is not None and micros < central)
    parts = ([f'or more about {round(higher * 100)}% of the time'] if higher else []) + (
        [f'or less about {round(lower * 100)}% of the time'] if lower else [])
    return (', ' + ' and '.join(parts) if parts else '') + (
        f', so about {money_text(expected, terms.card.currency)} is expected')


def _with_spread(prediction, terms, spread, billed=None, pages=None):
    """The prediction with its completion time, its billing step and how the spread was found."""
    if spread is None:
        return prediction
    from dataclasses import replace
    step = None
    if terms is not None and terms.card.per_minute_micros:
        step = terms.card.billing_increment_seconds
    elif terms is not None and terms.page_time_seconds:
        step = terms.page_time_seconds
    return replace(prediction, expected_billed_seconds=billed, p90_seconds=spread.percentile(),
                   increment_seconds=step, spread=spread.clause(), expected_billed_pages=pages)


# A Direct message or FHIR document (``digital/``): no call, priced per message by its account's plan.
DIGITAL_PREFIXES = ('dsm:', 'fhir:')


def _digital(facts, shape):
    """What one message adds on a digital route's plan. ``terms.card.per_call_micros`` is the price of a message,
    ``per_page_micros`` of a page, ``included_pages`` the messages a monthly fee includes, ``overage_page_micros``
    the price of each message past them, and ``plan.faxes`` the messages sent this month. No terms: unknown."""
    terms, label = facts.terms, facts.label
    if terms is None:
        return Prediction(None, 0.0, None, _sentence(
            facts.missing or f'{label} has no price on file, so the cost is unknown'), False)
    card = terms.card
    plan = facts.plan or PlanUse()
    fee = plan_fee_text(card.monthly_fee_micros, card.currency) if card.monthly_fee_micros else None
    named = f'your {label} plan' + (f' ({fee} a month)' if fee else '')
    if terms.included_pages:
        if plan.faxes is None:
            return Prediction(0, 0.0, None, _sentence(
                f'{named} includes {terms.included_pages} messages a month, and Faxbot has no count of the messages '
                'sent this month, so whether this one costs extra is unknown'), True)
        room = f'{plan.faxes} of its {terms.included_pages} included messages used this month'
        if plan.faxes + 1 <= terms.included_pages:
            return Prediction(0, 0.0, Money(0, card.currency), _sentence(
                f'Included in {named}, so this message adds nothing to the bill', room), True)
        if terms.overage_page_micros is None:
            return Prediction(0, 0.0, None, _sentence(
                f'This message would go past what {named} includes, and the price of extra messages is not on file',
                room), True)
        return Prediction(0, 0.0, Money(terms.overage_page_micros, card.currency), _sentence(
            f'One message past what {named} includes, at {money_text(terms.overage_page_micros, card.currency)}',
            room), True)
    if card.flat_plan:
        return Prediction(0, 0.0, Money(0, card.currency), _sentence(
            f'Included in {named}, so this message adds nothing to the bill'), True)
    pages = shape.pages if card.per_page_micros else 0
    micros = card.per_call_micros + pages * card.per_page_micros
    if micros == 0:
        sentence = f'{label} charges nothing for a message'
    else:
        parts = ([f'{money_text(card.per_call_micros, card.currency)} a message'] if card.per_call_micros else []) + (
            [f'{money_text(card.per_page_micros, card.currency)} a page'] if card.per_page_micros else [])
        sentence = f'{label} charges ' + ' and '.join(parts)
    return Prediction(pages, 0.0, Money(micros, card.currency), _sentence(sentence), False)


def predict_from(facts, shape):
    """The prediction from gathered facts. Pure: the same facts and shape always give the same answer."""
    if not isinstance(shape, Shape):
        raise TypeError('Describe the fax with a Shape.')
    if facts.route_key == 'local':
        return Prediction(0, 0.0, Money(0, facts.currency), _sentence(
            'This is one of your own fax numbers, so the fax goes straight into Received with no phone call and '
            'costs nothing'), False, p90_seconds=0.0)
    if facts.route_key == 'direct':
        return Prediction(0, 0.0, Money(0, facts.currency), _sentence(
            'Delivered straight to a verified partner over the internet, with no phone call, so it costs nothing'),
            False, p90_seconds=0.0)
    if facts.route_key.startswith(DIGITAL_PREFIXES):
        return _digital(facts, shape)
    seconds, how = line_seconds(shape, facts.link)
    seconds, how = hour_effect(seconds, how, facts.link)
    # Only Faxbot's own trunk learns each number's calls (predict_facts.learn); a fax service keeps the assumed one.
    spread = spread_for(seconds, facts.link, learns=facts.route_key == 'sip')
    terms = facts.terms
    if terms is None:
        if facts.refused:
            return _with_spread(Prediction(None, seconds, None, _sentence(facts.missing), False), None, spread)
        missing = facts.missing or f'{facts.label} publishes no price for {_what(facts)} to {_where(facts.destination)}'
        return _with_spread(Prediction(None, seconds, None, _sentence(f'{missing}, so the cost is unknown', how),
                                       False), None, spread)
    if terms.max_pages_per_fax and shape.pages > terms.max_pages_per_fax:
        return _with_spread(Prediction(None, seconds, None, _sentence(
            f'{facts.label} takes at most {terms.max_pages_per_fax} pages in one fax, so this fax cannot go this '
            'way as one fax'), False), terms, spread)
    if terms.card.flat_plan or terms.included_pages or terms.included_minutes:
        billed = (_expected(spread.points, lambda duration: billed_seconds(terms.card, duration))
                  if spread is not None and terms.card.per_minute_micros else None)
        return _with_spread(_plan(facts, shape, seconds, how, spread), terms, spread, billed)
    billed_pages, central = terms_cost(terms, seconds=seconds, pages=shape.pages)
    if central is None:
        reason = (f'{facts.label} bills by time on the line, and the time is unknown' if billed_pages is not None
                  else f'{facts.label} counts pages by time on the line, and the time is unknown')
        return Prediction(billed_pages, seconds, None, _sentence(f'{reason}, so the cost is unknown', how), False)
    micros, billed = expected_terms_cost(terms, spread, shape.pages)
    cost = Money(micros, terms.card.currency)
    if micros == 0 and facts.destination.kind != LOCAL:
        price = f'{facts.label} charges nothing for {_what(facts)} to {_where(facts.destination)}'
    else:
        price = _price_clause(terms, billed_pages, seconds) or f'{facts.label} charges nothing for this fax'
        if terms.published and facts.destination.kind != LOCAL:
            price += f", {facts.label}'s published price for {_what(facts)} to {_where(facts.destination)}"
        elif facts.learned:
            price += f', {facts.learned}'
        price += _spread_price(terms, spread, shape.pages, central, micros)
    return _with_spread(Prediction(billed_pages, seconds, cost, _sentence(price, how), False), terms, spread, billed,
                        expected_pages(terms, spread, shape.pages))


# Gathering the facts --------------------------------------------------------------------

_SOURCE = []


@contextmanager
def facts_source(source):
    """Use ``source(route_key, destination, now=...)`` for facts inside this block (tests, the dry run)."""
    _SOURCE.append(source)
    try:
        yield source
    finally:
        _SOURCE.remove(source)


def predict(route_key: str, destination: str, shape: Shape, *, now=None) -> Prediction:
    """What this fax would take and cost on ``route_key``; see the module's contract."""
    if not isinstance(shape, Shape):
        raise TypeError('Describe the fax with a Shape.')
    if _SOURCE:
        facts = _SOURCE[-1](route_key, destination, now=now)
    else:
        from .predict_facts import facts_for
        facts = facts_for(route_key, destination, now=now)
    return predict_from(facts, shape)


def bill_of(prediction):
    """The ``ExpectedBill`` a prediction carries: billing steps (or pages under a page-or-time rule) expected."""
    units = None
    step = prediction.increment_seconds
    if prediction.expected_billed_seconds is not None and step:
        units = prediction.expected_billed_seconds / step
    elif prediction.expected_billed_pages is not None:
        units = prediction.expected_billed_pages
    return ExpectedBill(units, step, prediction.expected_billed_seconds, prediction.cost, prediction.seconds,
                        prediction.p90_seconds, prediction.basis)


def expected_bill(route_key: str, destination: str, shape: Shape, *, now=None) -> ExpectedBill:
    """What a fax on ``route_key`` to ``destination`` is expected to bill over its duration's spread.

    ``units`` are E[ceil(D / increment)] billing steps on a route that bills by
    time (None otherwise), ``cost`` the expected cost (``predict``'s), and
    ``p90_seconds`` the time 9 in 10 such calls finish within. Refusals:
    ``ShapeRefused`` (a ValueError) for an impossible shape, TypeError for
    something that is not a ``Shape``. An unknown route or price is not an
    error: ``cost`` is None and ``basis`` says why.
    """
    return bill_of(predict(route_key, destination, shape, now=now))


# Helpers for callers -------------------------------------------------------------------

def tiff_page_bits(path):
    """The compressed bits of each page of a prepared fax image (8 x its strip byte counts), or None.

    Faxbot prepares T.6 (MMR) images (``conversion.pdf_to_tiff``), so these are
    the bits the call sends with MMR; ``WIRE_FACTOR`` converts them for other
    codings. None when the file cannot be read as a TIFF.
    """
    try:
        from PIL import Image, ImageSequence
        with Image.open(path) as image:
            bits = []
            for frame in ImageSequence.Iterator(image):
                counts = frame.tag_v2.get(279)  # StripByteCounts
                if counts is None:
                    return None
                counts = counts if isinstance(counts, tuple) else (counts,)
                bits.append(8 * sum(int(count) for count in counts))
            return tuple(bits) or None
    except (OSError, ValueError, ImportError):
        return None


def boundary(card, seconds):
    """(billed seconds, seconds left before the next billing step) for a predicted call; (None, None) unknown."""
    if card is None or seconds is None:
        return None, None
    billed = billed_seconds(card, seconds)
    return billed, max(0.0, billed - seconds)


def amount_text(prediction):
    """The cost as text for a sentence, or None when unknown: '$0.005'."""
    if prediction.cost is None:
        return None
    return money_text(prediction.cost.micros, prediction.cost.currency)


def amount_value(prediction):
    """The cost as exact decimal text and currency for an API answer, or None."""
    if prediction.cost is None:
        return None
    return {'amount': format_amount(prediction.cost.micros), 'currency': prediction.cost.currency}
