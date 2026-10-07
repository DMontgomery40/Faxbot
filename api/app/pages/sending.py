"""The attempt-time hook: the pages one send carries, packed and trimmed when that is allowed and saves.

Called for each attempt before anything is dialed or uploaded
(``outbound_transport.CapturedTransport.prepare``). The fax's own PDF and fax
image are never changed: a changed send gets its own files beside them
(``packed-<fax>-<attempt>.tiff`` / ``.pdf``), which the usual retention
cleanup removes. Anything that goes wrong here is logged and the fax goes
exactly as it would have; this hook never stops or delays a send. Faxes sent
together in one call are left alone for now (batching packs a batch later).
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


def prepare(engine, values, configuration, claim, job, pdf, tiff, *, rule=None, now=None):
    """PreparedPages for this attempt, or None when its pages go as they are. Never raises."""
    try:
        return _prepare(engine, values, configuration, claim, job, pdf, tiff, rule=rule, now=now)
    except Exception:
        logging.getLogger(__name__).warning('Fax pages could not be packed; they go as they are.')
        return None


def _prepare(engine, values, configuration, claim, job, pdf, tiff, *, rule=None, now=None):
    from .. import conversion
    from .decision import LINE_BITS_PER_SECOND
    from .resolution import is_standard, standard_frames
    from .trim import rendered_pages, trim_frames
    if engine is None or getattr(claim, 'members', None):
        return None
    job_id, attempt_id = claim.job_id, claim.attempt_id
    if not (_HEX32.fullmatch(str(job_id)) and _HEX32.fullmatch(str(attempt_id))):
        return None
    route, number, mode = configuration.provider_id, job.get('to_number'), how_sent(configuration)
    if mode == 'pdf_url':
        return None
    records = capabilities.records_for(engine)
    cap = records.capability(number)
    packing_ok, _ = capabilities.long_pages_allowed(engine, route, number, rule=rule)
    trim_ok = mode == 'image' and cap.ecm is False and records.trim_allowed(number)
    # A document that is really standard resolution goes at standard (lossless; Faxbot's own engines only).
    match_ok = mode == 'image'
    # Lighten shaded areas and remove specks (pages/friendly.py), when that setting is on: a cloud provider that
    # takes a PDF gets the lightened pages; Faxbot's own engines send the fax image lightened at acceptance.
    from . import friendly as fax_friendly
    friendly = fax_friendly.Request('documents') if mode != 'image' and fax_friendly.documents_on(values) else None
    if not packing_ok and not trim_ok and not match_ok and friendly is None:
        return None
    root = Path(str(pdf)).parent
    out_tiff, out_pdf = paths(root, job_id, attempt_id)
    source = Path(str(tiff)) if mode == 'image' and tiff else None
    raster = None
    try:
        if source is None:
            # A cloud provider takes a PDF: rasterize the fax's PDF to pack its pages, then send them as a PDF.
            raster = out_tiff.with_name(out_tiff.stem + '.source.tiff')
            conversion.pdf_to_tiff(str(pdf), str(raster), friendly=friendly)
            source = raster
        frames = conversion.read_fax_frames(str(source))
        if not frames:
            return None
        matched = standard_frames(frames) if match_ok else None
        if matched is not None:
            frames = matched
        resolution = 'standard' if is_standard(frames) else 'fine'
        if mode == 'image' and resolution == 'fine' and (_call_resolution(values) != 'fine' or cap.fine is False):
            # The SSL Fax engine would draw these pages again at standard resolution: leave them alone.
            packing_ok = trim_ok = False
        trimmed_pages = trimmed_rows = 0
        if trim_ok:
            flags = rendered_pages(str(pdf))
            if flags is not None and len(flags) == len(frames):
                frames, trimmed_pages, trimmed_rows = trim_frames(frames, flags)
        # Exactly one layout: the pages as they are, packed onto long pages, or the experimental codec,
        # whichever the route's billing makes cheapest (conversion.choose_layout).
        route_card = _card(engine, route)
        from .views import packed_sentence

        def describe_dense(original, sent):
            return packed_sentence({'layout': 'dense', 'original_pages': original, 'sent_pages': sent,
                                    'page_limit': cap.limit, 'limit_learned_at': cap.learned_at})

        def codec(pages):
            return conversion.codec_pages(pages, engine=engine, number=number, route=route, capability=cap)
        choice = conversion.choose_layout(frames, route=route, destination=number, limit=cap.limit,
                                          dense_allowed=packing_ok, codec=codec, card=route_card,
                                          boundary_seconds=cap.boundary_seconds, describe_dense=describe_dense)
        layout = None if choice['layout'] == 'normal' else choice['layout']
        lightened = friendly is not None and friendly.result is not None and friendly.result.pages_changed > 0
        if layout is None and not trimmed_pages and matched is None and not lightened:
            return None
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
        if lightened:
            fax_friendly.record_send(engine, job_id=job_id, attempt_id=attempt_id, request=friendly, now=now)
        return PreparedPages(str(out_pdf) if mode != 'image' else None, str(out_tiff) if mode == 'image' else None,
                             len(frames), len(pages), trimmed_pages)
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
