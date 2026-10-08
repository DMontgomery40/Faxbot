"""Whether packing a fax's pages saves anything on the route it goes by.

Both ways of sending, as they are and packed, are put to one predictor:
``predict(route_key, destination, shape) -> Prediction(billed_pages,
seconds, cost, basis, marginal)``. Builder AH's shared ``routing/predict.py``
(``predict(route_key, destination, Shape(pages, page_bits, resolution,
layout), *, now=None)``) is used when it is installed (``predictor``); until
then the stand-in below follows the route's rate card and its page model in
provider_traits.json, with the same meanings: ``billed_pages`` are pages
charged at a page price (0 on a per-minute route), ``marginal`` is True for a
monthly plan, and None is always unknown, never 0:

- per page (Sinch, Phaxio, eFax plan pages): each page sent is billed, so
  fewer pages cost less; eFax and similar routes bill a page that takes more
  than 60 seconds as more than one (``page_rule``);
- per minute (the Telnyx trunk, SignalWire): pages cost nothing, but each
  page boundary is an end-of-page exchange of about 3 seconds, so fewer
  pages make a shorter call; whole-minute billing may still round both to the
  same charge;
- a flat plan (HumbleFax): nothing is saved at the margin, but every page
  saved is room under the plan's fair use and its page cap per fax;
- no rate card: the cost stays unknown, never zero.

Faxbot packs only when it sends fewer pages, the call is not longer, and the
cost (when known) is not higher.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math

from ..routing.costs import ESTIMATE_SETUP_SECONDS, Money, attempt_cost
from .capability import BOUNDARY_SECONDS, page_model

LAYOUTS = ('normal', 'dense', 'codec')
# What the stand-in assumes about the line when nothing better is known: 14,400 bit/s (V.17).
LINE_BITS_PER_SECOND = 14400
# Bits of a page whose compressed size is unknown: about 25 KB, a typical text page in MMR at fine.
DEFAULT_PAGE_BITS = 200_000


@dataclass(frozen=True)
class Shape:
    pages: int
    page_bits: tuple | None = None  # compressed bits of each page, when known
    resolution: str = 'fine'
    layout: str = 'normal'
    boundary_seconds: float | None = None  # measured time between pages for this destination, when known
    measured: dict | None = None  # {coding: bits per page} measured on these pages (pages/coding.py)
    coding: str | None = None  # the coding the call is priced with, when chosen


@dataclass(frozen=True)
class Prediction:
    billed_pages: int | None  # pages charged at a page price; 0 on a per-minute route
    seconds: float | None  # time on the line, not rounded
    cost: Money | None
    basis: str  # 'per_page', 'per_minute', 'plan' or 'unpriced'
    marginal: bool  # True for a monthly plan (no money at the margin while pages are included)


def _bits(shape):
    """The bits of each page as the call sends them: measured in the chosen coding when known (pages/coding.py),
    else the page bits given."""
    measured = (shape.measured or {}).get(shape.coding) if shape.coding else None
    if measured is not None and len(measured) == shape.pages:
        return tuple(measured)
    return shape.page_bits if shape.page_bits and len(shape.page_bits) == shape.pages else None


def _seconds(shape):
    bits = _bits(shape)
    data = (sum(bits) if bits else DEFAULT_PAGE_BITS * shape.pages) / LINE_BITS_PER_SECOND
    boundary = shape.boundary_seconds if shape.boundary_seconds is not None else BOUNDARY_SECONDS
    return ESTIMATE_SETUP_SECONDS + data + max(0, shape.pages - 1) * boundary


def _billed_pages(route_key, shape):
    """Pages the route bills: one each, or a page or 60 seconds, whichever is more (eFax, Fax.Plus)."""
    if page_model(route_key).get('page_rule') != 'page_or_60_seconds':
        return shape.pages
    bits = shape.page_bits if shape.page_bits and len(shape.page_bits) == shape.pages else None
    if bits is None:
        return shape.pages
    return sum(max(1, math.ceil(page / LINE_BITS_PER_SECOND / 60)) for page in bits)


def stand_in_predict(route_key, destination, shape, *, card=None):
    """The stand-in predictor; ``card`` is the route's sending rate card (None: unpriced)."""
    if shape.pages <= 0 or shape.layout not in LAYOUTS:
        raise ValueError('Unsupported page shape')
    seconds = _seconds(shape)
    pages = _billed_pages(route_key, shape)
    if card is None:
        return Prediction(None, seconds, None, 'unpriced', False)
    if card.flat_plan:
        return Prediction(pages, seconds, Money(0, card.currency), 'plan', True)
    basis = 'per_page' if card.per_page_micros else 'per_minute' if card.per_minute_micros else 'per_page'
    cost = attempt_cost(card, seconds=math.ceil(seconds), pages=pages, delivered=True)
    return Prediction(pages if card.per_page_micros else 0, seconds, Money.of(cost, card.currency), basis, False)


def predictor():
    """AH's shared predictor behind this module's signature once it is installed; None means use the stand-in."""
    try:
        from ..routing import predict as shared  # type: ignore[attr-defined]
    except ImportError:
        return None

    def adapted(route_key, destination, shape):
        return shared.predict(route_key, destination,
                              shared.Shape(shape.pages, shape.page_bits, shape.resolution, shape.layout,
                                           getattr(shape, 'measured', None), getattr(shape, 'coding', None)))
    return adapted


@dataclass(frozen=True)
class Decision:
    pack: bool
    normal: Prediction
    dense: Prediction
    pages_saved: int
    seconds_saved: int | None
    billing: str


def price_all(route_key, destination, shapes, *, card=None, predict=None):
    """One Prediction per shape, all from the same predictor: the shared one when it prices every shape,
    else the stand-in for all of them (never a mix)."""
    shared = predict or predictor()
    if shared is not None:
        try:
            return [shared(route_key, destination, shape) for shape in shapes]
        except (ValueError, TypeError):
            # Said in the log, never quietly: a shape the shared predictor refuses is a bug to fix.
            logging.getLogger(__name__).warning('The shared predictor refused these pages; the rate card prices '
                                                'them instead.', exc_info=True)
    return [stand_in_predict(route_key, destination, shape, card=card) for shape in shapes]


ORDER = {'normal': 0, 'dense': 1, 'codec': 2}


def rank(prediction, shape):
    """Sort key: the cheapest expected bill first (the shared predictor prices a call over its time's spread);
    when that is the same (or unknown), fewer expected billing steps (minutes inside an allowance), then fewer
    billed pages, then less expected time on the line, unrounded, so a measured coding's seconds count; only a
    full tie keeps the simpler layout (normal, then dense, then codec)."""
    cost = prediction.cost
    billed = prediction.billed_pages if prediction.billed_pages is not None else shape.pages
    steps = getattr(prediction, 'expected_billed_seconds', None)
    seconds = prediction.seconds if prediction.seconds is not None else 0.0
    return (cost is None, cost.micros if cost is not None else 0, steps if steps is not None else 0.0, billed,
            seconds, ORDER[shape.layout])


def decide(route_key, destination, normal, dense, *, card=None, predict=None):
    """Pack only when it sends fewer pages, the call is no longer and the cost (when known) is no higher."""
    shared = predict or predictor()
    before = after = None
    if shared is not None:
        try:
            before, after = shared(route_key, destination, normal), shared(route_key, destination, dense)
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning('The shared predictor refused these pages; the rate card prices '
                                                'them instead.', exc_info=True)
            before = after = None
    if before is None or after is None:
        before = stand_in_predict(route_key, destination, normal, card=card)
        after = stand_in_predict(route_key, destination, dense, card=card)
    pages_saved = normal.pages - dense.pages
    seconds_saved = (math.floor(before.seconds - after.seconds)
                     if before.seconds is not None and after.seconds is not None else None)
    worth = pages_saved > 0
    if seconds_saved is not None and seconds_saved < 0:
        worth = False
    if before.billed_pages is not None and after.billed_pages is not None and after.billed_pages > before.billed_pages:
        worth = False
    if before.cost is not None and after.cost is not None and after.cost.micros > before.cost.micros:
        worth = False
    return Decision(worth, before, after, pages_saved, max(0, seconds_saved) if seconds_saved is not None else None,
                    before.basis)
