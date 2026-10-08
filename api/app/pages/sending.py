"""The attempt-time hook: the one layout the pages of one send take, and the changes that go with it.

Called for each attempt before anything is dialed or uploaded
(``outbound_transport.CapturedTransport.prepare``). The layout chooser
(``conversion.choose_layout``) prices the pages as they are, dense pages and
the experimental encoded pages (``codec/send.py``, only for a number whose
recipient agreed) from the same pages, and keeps exactly one; the pages as
they are and dense pages may also be lightened, trimmed or kept at standard
resolution, encoded pages never are. It is decided again for every attempt,
so a fax that moves to another route is decided for that route.

The fax's own PDF and fax image are never changed: a changed send gets its
own files beside them (``packed-<fax>-<attempt>.tiff`` / ``.pdf``), which the
usual retention cleanup removes. A provider that fetches the document from
Faxbot gets that attempt's PDF at the fetch address (``fetched_pdf``).
Anything that goes wrong here is logged and the fax goes exactly as it would
have; this hook never stops or delays a send. Faxes sent together in one call
are left alone (batching lightens each fax's own image for the shared call).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import math
from pathlib import Path
import re

from . import capability as capabilities

PREFIX = 'packed-'
_HEX32 = re.compile(r'[a-f0-9]{32}')


@dataclass(frozen=True)
class PreparedPages:
    pdf: str | None  # the PDF a cloud provider uploads instead of the fax's own, when changed
    tiff: str | None  # the fax image Faxbot's own engines send instead, when changed
    original_pages: int
    sent_pages: int
    trimmed_pages: int
    # The coding measured smallest for these pages (pages/coding.py), for Faxbot's own engines on the trunk; with
    # no page change, ``pdf`` and ``tiff`` are None and only the coding goes with the call.
    coding: object = None


def unchanged(prepared):
    """Whether an attempt's pages go as they are: no PreparedPages, or one that only carries the coding."""
    return prepared is None or (prepared.pdf is None and prepared.tiff is None)


def how_sent(configuration):
    """'image' (Faxbot's own engines send a fax image), 'pdf_upload' or 'pdf_url' (the provider fetches it)."""
    manifest = configuration.manifest
    if manifest is not None:
        try:
            return 'pdf_upload' if manifest.actions['send_fax'].body_kind == 'multipart' else 'pdf_url'
        except Exception:
            return 'pdf_url'
    if configuration.traits.get('requires_tiff') is True or configuration.provider_id in capabilities.IMAGE_ROUTES:
        return 'image'
    return capabilities.page_model(configuration.provider_id).get('how_sent') or 'pdf_url'


def paths(root, job_id, attempt_id):
    stem = Path(root) / f'{PREFIX}{job_id}-{attempt_id}'
    return stem.with_suffix('.tiff'), stem.with_suffix('.pdf')


def _call_resolution(values):
    """Whether the trunk sends fine pages (the SSL Fax engine re-images a page at any other resolution)."""
    try:
        from ..sip_trunk import fax_options
        return 'fine' if fax_options(values).fine else 'standard'
    except Exception:
        return 'fine'


def _card(engine, route):
    try:
        from ..routing.store import RouteStore
        return RouteStore(engine).card_for(route)
    except Exception:
        return None


def _billing(card):
    """How the route bills, from its rate card: per_page, per_minute, plan or unpriced."""
    if card is None:
        return 'unpriced'
    if card.flat_plan:
        return 'plan'
    return 'per_page' if card.per_page_micros else 'per_minute' if card.per_minute_micros else 'unpriced'


def _trim_seconds(rows, cap, resolution):
    """Seconds the left-out rows would have taken: each costs the machine's minimum scan line time."""
    if not rows or cap.scan_ms is None:
        return None
    per_row = cap.scan_ms * (2 if resolution == 'standard' else 1) / 1000
    return math.floor(rows * per_row)


def prepare(engine, values, configuration, claim, job, pdf, tiff, *, rule=None, seal=None, now=None):
    """PreparedPages for this attempt, or None when its pages go as they are. Never raises.

    ``seal`` opens the shared key of a number whose encoded pages are encrypted (``codec.store.KeySeal``)."""
    try:
        return _prepare(engine, values, configuration, claim, job, pdf, tiff, rule=rule, seal=seal, now=now)
    except Exception:
        logging.getLogger(__name__).warning('Fax pages could not be prepared; they go as they are.', exc_info=True)
        return None


def _prepare(engine, values, configuration, claim, job, pdf, tiff, *, rule=None, seal=None, now=None):
    from .. import conversion
    from ..codec.send import setting_for
    from .decision import LINE_BITS_PER_SECOND
    from .resolution import is_standard, standard_frames
    from .trim import rendered_pages, trim_frames
    if engine is None or getattr(claim, 'members', None):
        return None
    job_id, attempt_id = claim.job_id, claim.attempt_id
    if not (_HEX32.fullmatch(str(job_id)) and _HEX32.fullmatch(str(attempt_id))):
        return None
    route, number, mode = configuration.provider_id, job.get('to_number'), how_sent(configuration)
    root = Path(str(pdf)).parent
    # A fax accepted by an earlier build may have encoded pages written over its fax image: made again from the
    # original document, once, so this attempt chooses from the original (codec/send.py).
    from ..codec.send import restore_original_image
    restore_original_image(engine, job_id, pdf, tiff if mode == 'image' else None)
    records = capabilities.records_for(engine)
    # The machine that answers is the dialed number's; the person's page settings are also read for the recipient
    # they chose (an approved toll-free number is dialed instead, routing/alternates.py): the stricter one wins.
    chosen = job.get('recipient_number') or number
    cap = records.capability(number)
    packing_ok, _ = capabilities.long_pages_allowed(engine, route, number, rule=rule)
    if chosen != number and records.recipient_packing(chosen) == 'never':
        packing_ok = False
    trim_ok = (mode == 'image' and cap.ecm is False and records.trim_allowed(number)
               and records.trim_allowed(chosen))
    # A document that is really standard resolution goes at standard (lossless; Faxbot's own engines only).
    match_ok = mode == 'image'
    # Lighten shaded areas and remove specks (pages/friendly.py), decided for this attempt: the setting for your
    # documents, the recipient's own choice, whether this route's rate card bills by time, and whether the
    # receiving machine has error correction.
    from . import friendly as fax_friendly
    route_card = _card(engine, route)
    lighten, _ = fax_friendly.should_lighten(engine, values, route, number, card=route_card, ecm=cap.ecm)
    if lighten and chosen != number and fax_friendly.recipient_choice(engine, chosen) == 'never':
        lighten = False  # never for the recipient the person chose holds on an approved toll-free number too
    friendly = fax_friendly.Request('documents') if lighten else None
    # Encoded pages (experimental): a candidate only when the dialed number, and the recipient the person chose,
    # agreed to them (codec/send.py). This read is cheap; nothing is drawn for a number that did not agree.
    codec_ok = setting_for(engine, number, chosen) is not None
    if not packing_ok and not trim_ok and not match_ok and not codec_ok and friendly is None:
        return None
    out_tiff, out_pdf = paths(root, job_id, attempt_id)
    source = Path(str(tiff)) if mode == 'image' and tiff else None
    raster = None
    try:
        # The lightened pages, made once for the fax and kept with the attempt files (a retry draws nothing again).
        lightened_image = (fax_friendly.lightened_pages(root, job_id, pdf, source, friendly)
                           if friendly is not None else None)
        if lightened_image is not None:
            source = lightened_image
        elif not packing_ok and not trim_ok and not match_ok and not codec_ok:
            return None  # no page changed, and nothing else would change them
        elif source is None:
            # A cloud provider takes a PDF: rasterize the fax's PDF to price and change its pages, then send them as
            # a PDF.
            raster = out_tiff.with_name(out_tiff.stem + '.source.tiff')
            conversion.pdf_to_tiff(str(pdf), str(raster))
            source = raster
        frames = conversion.read_fax_frames(str(source))
        if not frames:
            return None
        matched = standard_frames(frames) if match_ok else None
        if matched is not None:
            frames = matched
        resolution = 'standard' if is_standard(frames) else 'fine'
        # Whether this call carries fine pages: the SSL Fax engine draws a fine page again at standard resolution
        # when the trunk or the receiving machine does not take fine.
        call_fine = cap.fine is not False and (mode != 'image' or _call_resolution(values) == 'fine')
        if mode == 'image' and resolution == 'fine' and not call_fine:
            packing_ok = trim_ok = False  # the pages would be drawn again: leave them alone
        trimmed_pages = trimmed_rows = 0
        if trim_ok:
            flags = rendered_pages(str(pdf))
            if flags is not None and len(flags) == len(frames):
                frames, trimmed_pages, trimmed_rows = trim_frames(frames, flags)
        # The codings this call may use, measured on each candidate's pages (pages/coding.py): Faxbot's own
        # engines on the trunk only, since a fax service codes the pages itself.
        usable = _usable_codings(engine, values, route, mode, number, cap)
        # Exactly one layout: the pages as they are, packed onto long pages, or the experimental encoded pages,
        # whichever the route's billing makes cheapest (conversion.choose_layout), each with its smallest coding.
        from .views import packed_sentence

        def describe_dense(original, sent):
            return packed_sentence({'layout': 'dense', 'original_pages': original, 'sent_pages': sent,
                                    'page_limit': cap.limit, 'limit_learned_at': cap.learned_at})

        def codec(pages):
            # Encoded pages carry the original document; ``pages`` are what they are priced against.
            return conversion.codec_pages(pages, engine=engine, number=number, route=route, capability=cap,
                                          pdf_path=str(pdf), seal=seal, recipient=chosen,
                                          exact_raster=mode == 'image',
                                          resolution='fine' if call_fine else 'standard')
        choice = conversion.choose_layout(frames, route=route, destination=number, limit=cap.limit,
                                          dense_allowed=packing_ok, codec=codec if codec_ok else None,
                                          card=route_card, boundary_seconds=cap.boundary_seconds,
                                          describe_dense=describe_dense, usable=usable,
                                          measure_cache=_coding_cache(out_tiff) if usable is not None else None)
        layout = None if choice['layout'] == 'normal' else choice['layout']
        coded = choice.get('coding')
        if coded is not None:
            _record_coding(engine, job_id, attempt_id, number, route, coded, usable, now)
            if not usable.needs_request(coded.coding):
                coded = None  # what the engines would take anyway: the call goes with its usual settings
        encoded = layout == 'codec'
        if encoded:
            # Encoded pages go exactly as the codec made them: nothing trimmed, kept at standard or lightened.
            trimmed_pages = trimmed_rows = 0
            matched = None
        lightened = (not encoded and friendly is not None and friendly.result is not None
                     and friendly.result.pages_changed > 0)
        if layout is None and not trimmed_pages and matched is None and not lightened:
            # The pages go as they are; the coding measured for them still goes with the call.
            return (PreparedPages(None, None, len(frames), len(frames), 0, coded) if coded is not None else None)
        pages = choice['pages']
        conversion.write_fax_tiff(pages, str(out_tiff))
        if mode != 'image':
            conversion.tiff_to_pdf(str(out_tiff), str(out_pdf))
            out_pdf.chmod(0o600)
            out_tiff.unlink(missing_ok=True)
        seconds = None
        if layout is not None and choice['seconds_saved'] is not None:
            seconds = choice['seconds_saved']
        trim_seconds = _trim_seconds(trimmed_rows, cap, resolution)
        if trim_seconds is not None:
            seconds = (seconds or 0) + trim_seconds
        if matched is not None and layout is None:
            before, after = conversion.fax_page_bits(str(source)), conversion.fax_page_bits(str(out_tiff))
            if before and after:
                seconds = (seconds or 0) + max(0, (sum(before) - sum(after)) // LINE_BITS_PER_SECOND)
        if layout is not None or trimmed_pages or matched is not None:
            records.record_change(
                job_id=job_id, attempt_id=attempt_id, number=number, route=route, original_pages=len(frames),
                sent_pages=len(pages), capability=cap, billing=_billing(route_card) if layout is not None else None,
                trimmed_pages=trimmed_pages or None, trimmed_rows=trimmed_rows or None,
                resolution='standard' if matched is not None else None, seconds_saved=seconds, layout=layout,
                reason=choice['reason'], now=now)
        if encoded:
            _record_codec(engine, job_id, choice['codec'], now)
        if lightened:
            fax_friendly.record_send(engine, job_id=job_id, attempt_id=attempt_id, request=friendly, now=now)
        return PreparedPages(str(out_pdf) if mode != 'image' else None, str(out_tiff) if mode == 'image' else None,
                             len(frames), len(pages), trimmed_pages, coded)
    except BaseException:
        for path in (out_tiff, out_pdf):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if raster is not None:
            try:
                raster.unlink(missing_ok=True)
            except OSError:
                pass


def _usable_codings(engine, values, route, mode, number, cap):
    """The codings a call to ``number`` may request (``pages.coding.usable_for``), or None when Faxbot's own engines
    do not place it or the records it reads cannot be read (logged; the engines then code as they always did)."""
    if route != 'sip' or mode != 'image':
        return None
    import sqlalchemy as sa
    from .. import hylafax_engine
    from ..routing.database import DeliveryStoreError
    from . import coding
    try:
        recipient = hylafax_engine.recipient_limits(engine, number)
        return coding.usable_for(engine, values, number, recipient=recipient, capability=cap)
    except (sa.exc.SQLAlchemyError, DeliveryStoreError):
        logging.getLogger(__name__).warning('The fax codings this number takes could not be read; the call codes its '
                                            'pages as usual.', exc_info=True)
        return None


def _coding_cache(out_tiff):
    from .coding import cache_path
    return cache_path(out_tiff)


def _record_coding(engine, job_id, attempt_id, number, route, choice, usable, now):
    """The coding this attempt requests and what was measured (``fax_coding_choices``), once."""
    import sqlalchemy as sa
    from ..routing.database import DeliveryStoreError
    from .coding import record_choice
    try:
        record_choice(engine, job_id=job_id, attempt_id=attempt_id, number=number, route=route, choice=choice,
                      receiver_known=usable.receiver is not None, now=now)
    except (sa.exc.SQLAlchemyError, DeliveryStoreError):
        logging.getLogger(__name__).warning('The fax coding chosen for this attempt could not be recorded.')


def _record_codec(engine, job_id, details, now):
    """The codec's details for the fax (``codec_sends``), once; the attempt's own record is its page change."""
    import sqlalchemy as sa
    from ..codec.send import record_attempt
    row = getattr(details, 'row', None)
    if not row:
        return
    try:
        record_attempt(engine, job_id, row, now or datetime.utcnow())
    except sa.exc.SQLAlchemyError:
        logging.getLogger(__name__).warning('The details of the encoded pages could not be recorded for this fax.')


def fetched_pdf(pdf_path, job_id, media_url):
    """The PDF a provider fetching ``media_url`` gets: the pages chosen for the attempt that made that link
    (``packed-<fax>-<attempt>.pdf``) when that attempt changed them, else the fax's own PDF (``pdf_path``).

    The attempt is read from the link stored with the fax's current token (``outbound_store.grant_pdf``), never
    from the request, so a token only ever opens its own attempt's pages."""
    from urllib.parse import parse_qs, urlsplit
    try:
        attempt = parse_qs(urlsplit(str(media_url or '')).query).get('attempt', [''])[0]
    except ValueError:
        return pdf_path
    if not (_HEX32.fullmatch(str(job_id or '')) and _HEX32.fullmatch(attempt)):
        return pdf_path
    _, chosen = paths(Path(pdf_path).parent, job_id, attempt)
    if chosen.is_file() and not chosen.is_symlink():
        return chosen
    return pdf_path


def cleanup(root, cutoff):
    """Remove changed-page files older than ``cutoff`` (the retention cleanup's time)."""
    try:
        for path in Path(root).glob(PREFIX + '*'):
            if (path.is_file() and not path.is_symlink()
                    and datetime.utcfromtimestamp(path.stat().st_mtime) < cutoff):
                path.unlink(missing_ok=True)
    except OSError:
        return False
    return True
