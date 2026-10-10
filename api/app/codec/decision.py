"""Whether one fax goes as encoded payload pages: only when the route's bill says it saves.

The route's bill comes from the shared predictor (``routing/predict.py``,
Builder AH): ``predict(route_key, destination, Shape(pages, page_bits,
resolution, layout)) -> Prediction(billed_pages, seconds, cost, basis,
marginal)``, where None always means unknown, never 0. This module compares
the fax's normal pages with each candidate set of payload pages:

- when both costs are known and the route is not a monthly plan, the payload
  must cost less;
- on a monthly plan (``marginal``), or when a cost is unknown, the payload must
  use fewer billed pages, or fewer pages when no page is billed, and no more
  known seconds on the line: it then saves plan allowance, never money that
  cannot be shown;
- anything unknown on both counts keeps the normal pages.

Run-coded pages carry the most per page and per second but need the exact
raster, so they are only a candidate on a route where Faxbot makes the fax
image itself and a previous call to the number negotiated ECM and fine
resolution. Other routes use grid pages, sturdy ones when a provider renders
the PDF itself.

On Faxbot's own engines each candidate, and the normal pages it competes
with, is priced in its smallest usable coding. This happens before filtering
or ranking: a Group-4 estimate must not discard a page that costs less in MH.
Where Faxbot does not choose the coding (a provider draws the pages), every
lossless coding is still measured on the pages, so the predictor prices the
coding it expects from that coding's measured size rather than from a fixed
ratio to MMR, which a payload page does not follow.
"""
from dataclasses import dataclass

RUN_LIMITS = (7, 15, 63)


@dataclass(frozen=True)
class Choice:
    use: bool
    sentence: str
    layout: str = 'grid'
    resolution: str = 'fine'
    fec: str = 'medium'
    run_limit: int = 15
    sturdy: bool = False
    pages_original: int = 0
    pages_encoded: int = 0
    original: object = None
    encoded: object = None
    pages: object = None


def predictor():
    """(predict, Shape) from the shared predictor, or None while it is not installed."""
    try:
        from ..routing.predict import Shape, predict
    except ImportError:
        return None
    return predict, Shape


def _amount(cost):
    """(micros, currency) of a known cost: the predictor's Money, or plain micros (currency None); else None."""
    if cost is None:
        return None
    micros = getattr(cost, 'micros', cost)
    if type(micros) is not int:
        return None
    return micros, getattr(cost, 'currency', None)


def _cheaper(encoded, original):
    """True or False when both costs are known in one currency; None when they cannot be compared."""
    a, b = _amount(encoded), _amount(original)
    if a is None or b is None or a[1] != b[1]:
        return None
    return a[0] < b[0]


def saves(original, encoded, pages_original, pages_encoded):
    """True when ``encoded`` is cheaper by the rules above."""
    if original is None or encoded is None:
        return False
    cheaper = None if original.marginal else _cheaper(encoded.cost, original.cost)
    if cheaper is not None:
        return cheaper
    if original.seconds is not None and encoded.seconds is not None and encoded.seconds > original.seconds:
        return False
    if original.billed_pages is not None and encoded.billed_pages is not None and original.billed_pages > 0:
        return encoded.billed_pages < original.billed_pages
    return (original.seconds is not None and encoded.seconds is not None and pages_encoded < pages_original)


def _rank(prediction):
    unknown = float('inf')
    amount = None if prediction.marginal else _amount(prediction.cost)
    return (amount[0] if amount is not None else unknown,
            prediction.billed_pages if prediction.billed_pages is not None else unknown,
            prediction.seconds if prediction.seconds is not None else unknown)


def _measured_shape(Shape, frames, resolution, layout, usable):
    """The same eligible coding and engine tuning used by the outer layout chooser."""
    from ..pages import coding
    measured = coding.measure(frames, tuning=usable.tuning)
    choice = coding.best_coding(frames, usable.codings, ecm=usable.ecm, measured=measured,
                                negotiate=usable.left_out.get('JBIG') == coding.JBIG_NOT_ON_RECORD)
    return Shape(pages=len(frames), page_bits=tuple(measured['MMR']), resolution=resolution,
                 layout=layout, measured=measured, coding=choice.priced)


# Measured when Faxbot does not choose the call's coding: the one-dimensional and two-dimensional codings every
# engine and provider can use (JBIG, which needs its encoder and the machine's agreement, is left to the estimate).
UNCHOSEN_CODINGS = ('MH', 'MR', 'MMR')


def _unchosen_shape(Shape, frames, resolution, layout):
    """A shape for pages whose coding Faxbot does not choose (a provider draws them, or the codings a number takes
    could not be read): each lossless coding measured on the pages themselves, so the predictor prices whichever
    coding it expects the call to use from that coding's real size. A fixed ratio to MMR would price a payload page
    (drawn so that its MH code is the payload, and larger in MMR than in MH or MR) far above what it costs."""
    from ..pages import coding
    measured = coding.measure(frames, codings=UNCHOSEN_CODINGS)
    return Shape(pages=len(frames), page_bits=tuple(measured['MMR']), resolution=resolution, layout=layout,
                 measured=measured)


def choose(document, *, route_key, destination, pages_original, page_bits_original, exact_raster,
           ecm_and_fine_seen, provider_renders, fec='medium', style='dense', secret=None, picture=None,
           resolution='fine', encoder=None, tools=None, usable=None, frames_original=None):
    """The Choice for one fax. ``tools`` is (predict, Shape); tests pass a fake.

    ``usable`` and ``frames_original`` let Faxbot's own engines price every layout in the coding the call can
    use. Where Faxbot does not choose the coding (a provider draws the pages), each layout is priced from its
    pages' measured sizes in the coding the predictor expects for the call.
    """
    from .. import codec
    tools = tools or predictor()
    if tools is None:
        return Choice(False, 'Faxbot cannot yet predict what this route charges, so the fax goes as normal pages.')
    predict, Shape = tools
    encode = encoder or codec.encode_document
    usable = usable if exact_raster and not provider_renders else None
    if usable is not None:
        if frames_original is None or len(frames_original) != pages_original:
            raise ValueError('The original pages are required to compare measured fax codings.')
        shape = _measured_shape(Shape, frames_original, resolution, 'normal', usable)
    elif frames_original is not None and len(frames_original) == pages_original:
        shape = _unchosen_shape(Shape, frames_original, resolution, 'normal')
    else:
        shape = Shape(pages=pages_original, page_bits=tuple(page_bits_original),
                      resolution=resolution, layout='normal')
    original = predict(route_key, destination, shape)
    if style == 'picture':
        candidates = [dict(layout='picture')]
    elif exact_raster and ecm_and_fine_seen:
        candidates = [dict(layout='runs', run_limit=limit) for limit in RUN_LIMITS] + [dict(layout='grid')]
    else:
        candidates = [dict(layout='grid', sturdy=provider_renders)]
    best = None
    for options in candidates:
        try:
            pages = encode(document, resolution=resolution, fec=fec, secret=secret,
                           picture=picture if options['layout'] == 'picture' else None, **options)
        except codec.CodecError:
            continue
        if usable is not None:
            shape = _measured_shape(Shape, pages.pages, resolution, 'codec', usable)
        else:
            shape = _unchosen_shape(Shape, pages.pages, resolution, 'codec')
        prediction = predict(route_key, destination, shape)
        if not saves(original, prediction, pages_original, pages.page_count):
            continue
        if best is None or _rank(prediction) < _rank(best[1]):
            best = (options, prediction, pages)
    if best is None:
        return Choice(False, 'Encoded pages would not save on this route, so the fax goes as normal pages.',
                      pages_original=pages_original, original=original)
    options, prediction, pages = best
    count = pages.page_count
    sentence = (f'Sent as {count} encoded page{"s" if count != 1 else ""} instead of {pages_original} '
                '(experimental).')
    return Choice(True, sentence, layout=options['layout'], resolution=resolution, fec=fec,
                  run_limit=options.get('run_limit', 15), sturdy=options.get('sturdy', False),
                  pages_original=pages_original, pages_encoded=count, original=original, encoded=prediction,
                  pages=pages)
