"""One shared pre-dial predictor: what a fax would take and cost on a route, before dialing (M2, M4, M7).

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

- Bits on the line: the page bits Faxbot prepared (T.6, MMR) times the factor
  for the coding the call uses (``WIRE_FACTOR``).
- Speed: what earlier successful calls to this number on this route reached,
  else what calls on this route usually reach, else the route's typical speed.
- Without page bits, the seconds a page that earlier calls to this number
  took, else a typical page (``TYPICAL_PAGE_BITS``).

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

from .costs import Money, RateTerms, billed_seconds, format_amount, money_text, plan_fee_text, terms_cost
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
# A halftone page (a scanned photo or grey background) compresses badly with T.6: under 4:1 against a page's
# raw bits. JBIG does much better there (JBIG-KIT: often 2x or more); 0.5 is a cautious default.
HALFTONE_RATIO = 4
JBIG_HALFTONE_FACTOR = 0.5
RAW_PAGE_BITS = {'standard': 1728 * 1143, 'fine': 1728 * 2287, 'superfine': 1728 * 4575, '300': 2592 * 3508,
                 '400': 3456 * 4677}
# Recorded calls needed before a learned figure replaces a default.
MIN_CALLS = 3


@dataclass(frozen=True)
class Shape:
    pages: int
    page_bits: tuple[int, ...] | None   # compressed bits per page as they will be sent, if known
    resolution: str                      # 'standard' | 'fine' | 'superfine' | '300' | '400'
    layout: str                          # 'normal' | 'dense' | 'codec'

    def __post_init__(self):
        if type(self.pages) is not int or not 1 <= self.pages <= 10_000:
            raise ValueError('A fax has from 1 to 10,000 pages.')
        if self.page_bits is not None and (
                not isinstance(self.page_bits, tuple) or len(self.page_bits) != self.pages
                or any(type(bits) is not int or bits < 0 for bits in self.page_bits)):
            raise ValueError('Give the compressed bits of every page, as whole numbers.')
        if self.resolution not in RESOLUTIONS:
            raise ValueError('Choose standard, fine, superfine, 300 or 400 resolution.')
        if self.layout not in LAYOUTS:
            raise ValueError('Choose a normal, dense or codec layout.')


@dataclass(frozen=True)
class Prediction:
    billed_pages: int | None
    seconds: float | None
    cost: Money | None                   # the Money type from routing/costs; unknown is None, never 0
    basis: str                           # one plain sentence: how this was worked out
    marginal: bool                       # True when the cost is the marginal cost (flat plans: 0 plus fair-use)


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


def line_seconds(shape, link, *, coding=None):
    """(seconds, clause): the predicted time on the line and how it was worked out; seconds None when unknown."""
    setup = _setup(link)
    if shape.page_bits is not None:
        rate, how = _speed(link)
        factor = WIRE_FACTOR[coding or link.coding or DEFAULT_CODING]
        data = sum(shape.page_bits) * factor / rate
        seconds = setup + data + PAGE_SECONDS * shape.pages
        return seconds, f'{duration_text(seconds)} on the line, from the size of each page {how}'
    if shape.layout == 'codec':
        return None, "the time on the line is unknown, because a coded page's size depends on its data"
    factor = LAYOUT_FACTOR[shape.layout]
    if link.seconds_per_page is not None and link.page_calls >= MIN_CALLS:
        data = max(0.0, link.seconds_per_page - PAGE_SECONDS)
        seconds = setup + shape.pages * (PAGE_SECONDS + data * factor)
        return seconds, (f'{duration_text(seconds)} on the line, from the time a page took on '
                         f'{_count(link.page_calls, "earlier fax")} to this number')
    rate, how = _speed(link)
    bits = TYPICAL_PAGE_BITS[shape.resolution] * factor * WIRE_FACTOR[coding or link.coding or DEFAULT_CODING]
    seconds = setup + shape.pages * (bits / rate + PAGE_SECONDS)
    return seconds, f'{duration_text(seconds)} on the line, for typical pages {how}'


# The price ----------------------------------------------------------------------------

def _plan(facts, shape, seconds, how):
    """A monthly plan: what this fax adds to the bill (marginal), with the plan's room in the sentence."""
    terms, plan, label = facts.terms, facts.plan or PlanUse(), facts.label
    card = terms.card
    fee = plan_fee_text(card.monthly_fee_micros, card.currency) if card.monthly_fee_micros else None
    named = f'your {label} plan' + (f' ({fee} a month)' if fee else '')
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


def predict_from(facts, shape):
    """The prediction from gathered facts. Pure: the same facts and shape always give the same answer."""
    if not isinstance(shape, Shape):
        raise TypeError('Describe the fax with a Shape.')
    if facts.route_key == 'local':
        return Prediction(0, 0.0, Money(0, facts.currency), _sentence(
            'This is one of your own fax numbers, so the fax goes straight into Received with no phone call and '
            'costs nothing'), False)
    if facts.route_key == 'direct':
        return Prediction(0, 0.0, Money(0, facts.currency), _sentence(
            'Delivered straight to a verified partner over the internet, with no phone call, so it costs nothing'),
            False)
    seconds, how = line_seconds(shape, facts.link)
    terms = facts.terms
    if terms is None:
        if facts.refused:
            return Prediction(None, seconds, None, _sentence(facts.missing), False)
        missing = facts.missing or f'{facts.label} publishes no price for {_what(facts)} to {_where(facts.destination)}'
        return Prediction(None, seconds, None, _sentence(f'{missing}, so the cost is unknown', how), False)
    if terms.max_pages_per_fax and shape.pages > terms.max_pages_per_fax:
        return Prediction(None, seconds, None, _sentence(
            f'{facts.label} takes at most {terms.max_pages_per_fax} pages in one fax, so this fax cannot go this '
            'way as one fax'), False)
    if terms.card.flat_plan or terms.included_pages:
        return _plan(facts, shape, seconds, how)
    billed_pages, micros = terms_cost(terms, seconds=seconds, pages=shape.pages)
    if micros is None:
        reason = (f'{facts.label} bills by time on the line, and the time is unknown' if billed_pages is not None
                  else f'{facts.label} counts pages by time on the line, and the time is unknown')
        return Prediction(billed_pages, seconds, None, _sentence(f'{reason}, so the cost is unknown', how), False)
    cost = Money(micros, terms.card.currency)
    if micros == 0 and facts.destination.kind != LOCAL:
        price = f'{facts.label} charges nothing for {_what(facts)} to {_where(facts.destination)}'
    else:
        price = _price_clause(terms, billed_pages, seconds) or f'{facts.label} charges nothing for this fax'
        if terms.published and facts.destination.kind != LOCAL:
            price += f", {facts.label}'s published price for {_what(facts)} to {_where(facts.destination)}"
    return Prediction(billed_pages, seconds, cost, _sentence(price, how), False)


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


def halftone_pages(shape):
    """How many pages look like halftones (scanned photos, grey backgrounds): T.6 compresses them under 4:1."""
    if shape.page_bits is None:
        return 0
    raw = RAW_PAGE_BITS[shape.resolution]
    return sum(1 for bits in shape.page_bits if bits * HALFTONE_RATIO > raw)


@dataclass(frozen=True)
class EngineChoice:
    """JBIG on the SSL Fax engine against Faxbot's built-in coding for one fax (M7)."""
    standard: Prediction
    jbig: Prediction
    faster: str                          # 'jbig' or 'standard'
    sentence: str


def jbig_choice(facts, shape):
    """Both predictions and which is faster, for a fax with halftone pages to a number known to take JBIG.

    None unless the route is the SSL Fax engine's (``sip``), a recorded call to
    this number used JBIG, and the page sizes show halftone pages: Faxbot makes
    no claim about JBIG otherwise.
    """
    if facts.route_key != 'sip' or not facts.link.jbig or shape.page_bits is None or not halftone_pages(shape):
        return None
    standard = predict_from(facts, shape)
    raw = RAW_PAGE_BITS[shape.resolution]
    jbig_bits = tuple(int(math.ceil(bits * (JBIG_HALFTONE_FACTOR if bits * HALFTONE_RATIO > raw
                                            else WIRE_FACTOR['JBIG']))) for bits in shape.page_bits)
    jbig_shape = Shape(shape.pages, jbig_bits, shape.resolution, shape.layout)
    from dataclasses import replace
    jbig = predict_from(replace(facts, link=replace(facts.link, coding='MMR')), jbig_shape)
    faster = 'jbig' if (jbig.seconds or 0) < (standard.seconds or 0) else 'standard'
    if faster == 'jbig':
        saved = (standard.seconds or 0) - (jbig.seconds or 0)
        sentence = (f"An earlier fax to this number used the coding that suits photos, which only Faxbot's fast fax "
                    f'service sends; for the {_count(halftone_pages(shape), "photo-like page")} here it should save '
                    f'{duration_text(saved)} on the line.')
    else:
        sentence = 'The coding that suits photos would not make this fax shorter, so Faxbot keeps its usual coding.'
    return EngineChoice(standard, jbig, faster, sentence)


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
