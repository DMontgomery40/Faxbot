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
"""
from dataclasses import dataclass
import io

from PIL import Image

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


def g4_page_bits(image):
    """Coded bits of one page as the engines' Group 4 TIFF stores it (8 x its strip bytes)."""
    buffer = io.BytesIO()
    image.convert('1').save(buffer, 'TIFF', compression='group4',
                            strip_size=((image.size[0] + 7) // 8) * image.size[1])
    buffer.seek(0)
    with Image.open(buffer) as written:
        return 8 * sum(written.tag_v2.get(279, (len(buffer.getvalue()),)))


def saves(original, encoded, pages_original, pages_encoded):
    """True when ``encoded`` is cheaper by the rules above."""
    if original is None or encoded is None:
        return False
    if not original.marginal and original.cost is not None and encoded.cost is not None:
        return encoded.cost < original.cost
    if original.seconds is not None and encoded.seconds is not None and encoded.seconds > original.seconds:
        return False
    if original.billed_pages is not None and encoded.billed_pages is not None and original.billed_pages > 0:
        return encoded.billed_pages < original.billed_pages
    return (original.seconds is not None and encoded.seconds is not None and pages_encoded < pages_original)


def _rank(prediction):
    unknown = float('inf')
    return (prediction.cost if prediction.cost is not None and not prediction.marginal else unknown,
            prediction.billed_pages if prediction.billed_pages is not None else unknown,
            prediction.seconds if prediction.seconds is not None else unknown)


def choose(document, *, route_key, destination, pages_original, page_bits_original, exact_raster,
           ecm_and_fine_seen, provider_renders, fec='medium', style='dense', secret=None, picture=None,
           resolution='fine', encoder=None, tools=None):
    """The Choice for one fax. ``tools`` is (predict, Shape); tests pass a fake."""
    from .. import codec
    tools = tools or predictor()
    if tools is None:
        return Choice(False, 'Faxbot cannot yet predict what this route charges, so the fax goes as normal pages.')
    predict, Shape = tools
    encode = encoder or codec.encode_document
    original = predict(route_key, destination, Shape(pages=pages_original, page_bits=list(page_bits_original),
                                                     resolution=resolution, layout='normal'))
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
        bits = [g4_page_bits(page) for page in pages.pages]
        prediction = predict(route_key, destination, Shape(pages=pages.page_count, page_bits=bits,
                                                           resolution=resolution, layout='codec'))
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
