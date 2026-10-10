"""When sending together saves money on a number's route, and the one sentence saying why or why not.

Faxbot combines faxes into one call only over its own SIP trunk. It holds a
fax only when that trunk is the route the fax will take and the trunk's rate
card makes one call cheaper than several: a charge for every call, or minutes
billed with a minimum (or in whole-minute steps), so each short call pays for
time it does not use. Per-page and flat-plan routes never hold a fax.
"""
from dataclasses import dataclass

from ..provider_labels import provider_label


DEFAULT_WAIT_SECONDS = 600
DEFAULT_MAX_PAGES = 30
MIN_WAIT_SECONDS, MAX_WAIT_SECONDS = 60, 3600
MIN_PAGES, MAX_PAGES = 2, 200
# A minimum (or billing step) at least this long makes a short call pay for unused time.
SAVING_MINIMUM_SECONDS = 30
AGREEMENT = 'This recipient has agreed to receive several documents in one call.'
# How a shared call marks where each document starts (migration 0025): a separator page before each
# document; one index page listing each document's pages (research D6); or a line Faxbot adds at the top
# of every page, with no page added at all (research M20). Anything but separators needs the recipient's
# agreement to that convention.
LAYOUT_SEPARATORS, LAYOUT_INDEX_PAGE, LAYOUT_PAGE_HEADERS = 'separators', 'index_page', 'page_headers'
LAYOUTS = (LAYOUT_SEPARATORS, LAYOUT_INDEX_PAGE, LAYOUT_PAGE_HEADERS)
LAYOUT_LABELS = {LAYOUT_SEPARATORS: 'A separator page before each document',
                 LAYOUT_INDEX_PAGE: "One index page listing each document's pages",
                 LAYOUT_PAGE_HEADERS: 'A line at the top of every page, with no page added'}
INDEX_PAGE_AGREEMENT = ("This recipient has agreed to one index page listing each document's pages, "
                        'instead of a separator page before each document.')
PAGE_HEADERS_AGREEMENT = ('This recipient has agreed to find where each document starts from a line at the top '
                          'of every page, with no separator or index page.')
AGREEMENTS = {LAYOUT_INDEX_PAGE: INDEX_PAGE_AGREEMENT, LAYOUT_PAGE_HEADERS: PAGE_HEADERS_AGREEMENT}
REFUSALS = {LAYOUT_INDEX_PAGE: 'Record that the recipient agreed to one index page before using it.',
            LAYOUT_PAGE_HEADERS: 'Record that the recipient agreed to marks at the top of every page before using them.'}
# What either convention leaves out, and what it never touches (R07 §3.3: a recipient's own routing pages).
INDEX_PAGE_KEEPS = ("Only Faxbot's separator pages are left out; cover sheets and barcode pages inside "
                    'your documents are always sent.')
# 47 CFR 68.318(d): the date, time, sender and sending number on every page. The fax engine prints that
# header above Faxbot's own line, so marks at the top of every page need both set.
HEADER_NEEDS = ('Faxes to this number use separator pages for now, because marks at the top of every page need '
                'your header text and sending number set in Delivery setup > Sending identity.')
# The most documents one index page lists, each on at most three lines (``image.index_pdf``).
INDEX_PAGE_DOCUMENTS = 15


def header_identifies_sender(values):
    """True when every page's header line will show who sent it and from which number (47 CFR 68.318(d)).

    The built-in fax engine (spandsp 0.0.6, t4_tx.c) prints, above the page's own rows and only when the
    header text is set, the date and time, the header text and the station ID (the sending number: the
    station ID setting, or else the SIP trunk's caller ID) and the page number. The shipped header text
    ("Faxbot") names the software, not the sender, so it does not count, nor does the placeholder station ID.
    """
    from ..config_values import PLACEHOLDER_DEFAULTS, ConfigurationValues
    header = str(getattr(values, 'fax_header', '') or '').strip() if values is not None else ''
    if not header or header == ConfigurationValues.model_fields['fax_header'].default:
        return False
    station = str(getattr(values, 'fax_station_id', '') or '').strip()
    if station and station != PLACEHOLDER_DEFAULTS['fax_station_id']:
        return True
    from .. import sip_trunk
    try:
        return sip_trunk.configured(values) and bool(sip_trunk.effective_trunk(values, for_calls=True).caller_id)
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class RouteVerdict:
    saves: bool
    sentence: str
    route: str | None = None
    reason: str | None = None


def _duration(seconds):
    if seconds % 60 == 0:
        minutes = seconds // 60
        return '1-minute' if minutes == 1 else f'{minutes}-minute'
    return f'{seconds}-second'


def _trunk_label(preset):
    from ..routing.carriers import carrier_label
    preset = (preset or '').strip()
    return f'your {carrier_label(preset)} SIP trunk' if preset and preset != 'custom' else 'your SIP trunk'


def card_saves(card):
    """True when one call carrying several faxes costs less than separate calls under ``card``."""
    if card is None or card.flat_plan:
        return False
    if card.per_call_micros > 0:
        return True
    floor = max(card.minimum_seconds, card.billing_increment_seconds)
    return card.per_minute_micros > 0 and floor >= SAVING_MINIMUM_SECONDS


def card_sentence(card, label):
    """One sentence for a SIP trunk route's rate card."""
    from ..routing.costs import money_text
    if card is None:
        return f'Faxbot has no prices for {label}, so it cannot tell whether sending together saves money; faxes go straight away.'
    if card.flat_plan:
        return f"On {label}'s flat plan, sending together saves nothing, so faxes go straight away."
    if card.per_call_micros > 0:
        return f'{label[0].upper() + label[1:]} charges {money_text(card.per_call_micros, card.currency)} for each call, so faxes sent together cost less.'
    floor = max(card.minimum_seconds, card.billing_increment_seconds)
    if card.per_minute_micros > 0 and floor >= SAVING_MINIMUM_SECONDS:
        return (f'{label[0].upper() + label[1:]} bills each call with a {_duration(floor)} minimum, '
                'so faxes sent together cost less.')
    if card.per_page_micros > 0 and card.per_minute_micros == 0:
        return f'{label[0].upper() + label[1:]} charges by the page, so sending together saves nothing and faxes go straight away.'
    return f'{label[0].upper() + label[1:]} bills by the second, so sending together saves almost nothing and faxes go straight away.'


def verdict(choice, card, *, preset=None):
    """Whether the route this number's faxes take (``choice``, the plan's first choice) saves by sending together."""
    if choice is None:
        return RouteVerdict(False, 'Faxbot has no route for this number yet, so faxes go straight away.')
    route = choice.route
    if route.kind == 'direct':
        return RouteVerdict(False, 'Faxes to this number go to a verified partner without a fax call, so they go straight away.',
                            route.key, choice.reason)
    if route.provider_id != 'sip':
        label = provider_label(route.provider_id)
        if card is not None and card.flat_plan:
            return RouteVerdict(False, f"On {label}'s flat plan, sending together saves nothing, so faxes go straight away.",
                                route.key, choice.reason)
        if card is not None and card.per_page_micros > 0 and card.per_minute_micros == 0 and card.per_call_micros == 0:
            return RouteVerdict(False, f'{label} charges by the page, so sending together saves nothing and faxes go straight away.',
                                route.key, choice.reason)
        return RouteVerdict(False, f'Faxbot sends faxes together only over its own SIP trunk; faxes to this number go '
                                   f'through {label}, so they go straight away.', route.key, choice.reason)
    label = _trunk_label(preset)
    return RouteVerdict(card_saves(card), card_sentence(card, label), route.key, choice.reason)


def evaluate(routes, number, values, bound, *, pages=1):
    """The verdict for faxes to ``number`` under ``values`` with outbound provider ``bound``.

    ``routes`` is a ``RouteStore``; only its read functions are used.
    """
    from ..routing.plan import RoutePlanner
    if not bound:
        return verdict(None, None)
    plan = RoutePlanner(routes, direct_ready=lambda: True).plan(
        to_number=number, bound=bound, values=values, pages=pages, alternates=True)
    choice = plan.first
    card = choice.route.card if choice.route.kind != 'direct' else None
    return verdict(choice, card, preset=getattr(values, 'sip_trunk_preset', None))


def wait_text(seconds):
    minutes = max(1, round(seconds / 60))
    return '1 minute' if minutes == 1 else f'{minutes} minutes'
