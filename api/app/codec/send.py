"""Sending with the experimental payload codec: the per-fax plan at acceptance and the pages a route sends.

Only for a number whose recipient agreed (``codec_numbers``). At acceptance
the fax's original PDF stays exactly as accepted. When the payload saves on
the fax's route:

- a route where Faxbot makes the fax image (the SIP trunk engines) gets the
  payload pages as its fax TIFF, written over the TIFF ``pdf_to_tiff`` made
  from the original moments earlier (the TIFF is derived, never the
  original);
- a provider route gets ``<job>.payload-<provider>.pdf`` beside the original,
  and the transport sends that file when the attempt goes to the same
  provider (``transmitted_pdf``). Any other route sends the original pages.

The plan is recorded in ``codec_sends`` inside the acceptance transaction.
The job's own page count stays the original's (the public fax contract);
the encoded count lives in the send row.
"""
import logging
import os
from pathlib import Path
import re
import tempfile

import sqlalchemy as sa
from PIL import Image

from . import decision
from .store import CodecSettings, CodecStoreError, record_send

log = logging.getLogger(__name__)
FORMAT_VERSION = 1


def payload_pdf_path(root, job_id, provider_id):
    return Path(root) / f'{job_id}.payload-{provider_id}.pdf'


def transmitted_pdf(pdf_path, job_id, provider_id):
    """The PDF to give ``provider_id`` for this fax: its payload pages when they were made for it."""
    if not isinstance(provider_id, str) or re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', provider_id) is None:
        return pdf_path
    candidate = payload_pdf_path(Path(pdf_path).parent, job_id, provider_id)
    if candidate.is_file() and not candidate.is_symlink():
        return type(pdf_path)(candidate) if not isinstance(pdf_path, str) else str(candidate)
    return pdf_path


def _tiff_page_bits(path):
    try:
        from ..routing.predict import tiff_page_bits
        return list(tiff_page_bits(path))
    except ImportError:
        pass
    bits = []
    with Image.open(path) as image:
        index = 0
        while True:
            try:
                image.seek(index)
            except EOFError:
                break
            bits.append(8 * sum(image.tag_v2.get(279, (0,))))
            index += 1
    return bits


def _ecm_and_fine_seen(engine, number):
    """True when the last negotiated outbound call to ``number`` used ECM at fine resolution or better."""
    try:
        metadata = sa.MetaData()
        calls = sa.Table('fax_engine_calls', metadata, autoload_with=engine)
        jobs = sa.Table('fax_jobs', metadata, autoload_with=engine)
        if 'ecm' not in calls.c or 'resolution' not in calls.c:
            return False
        with engine.connect() as connection:
            row = connection.execute(
                sa.select(calls.c.ecm, calls.c.resolution).select_from(calls.join(jobs, calls.c.job_id == jobs.c.id))
                .where(jobs.c.to_number == number, calls.c.direction == 'outbound', calls.c.ecm.is_not(None))
                .order_by(calls.c.created_at.desc()).limit(1)).first()
    except sa.exc.SQLAlchemyError:
        return False
    return row is not None and row[0] == 'on' and row[1] in ('fine', 'superfine')


def _first_page_picture(tiff_path):
    with Image.open(tiff_path) as image:
        return image.convert('L').copy()


def plan_for_fax(engine, *, provider_id, needs_tiff, destination, pdf_path, tiff_path, pages, job_id,
                 seal=None, tools=None):
    """(Choice, recorder) for a fax being accepted, or (None, None) when the number has not opted in.

    Writes the payload artifact when the choice is to use it. ``recorder``
    runs inside the acceptance transaction.
    """
    from .. import codec
    try:
        settings = CodecSettings(engine, seal)
        setting = settings.get(destination)
    except CodecStoreError:
        return None, None
    if not setting['enabled']:
        return None, None
    secret = settings.secret(destination) if setting['has_key'] else None
    data = Path(pdf_path).read_bytes()
    document = codec.Document(data, 'application/pdf', 'document.pdf')
    temporary = None
    try:
        measured = tiff_path
        if not measured:
            from ..conversion import pdf_to_tiff
            descriptor, temporary = tempfile.mkstemp(prefix='.codec-', suffix='.tiff', dir=Path(pdf_path).parent)
            os.close(descriptor)
            pdf_to_tiff(pdf_path, temporary)
            measured = temporary
        page_bits = _tiff_page_bits(measured)
        picture = _first_page_picture(measured) if setting['style'] == 'picture' else None
        choice = decision.choose(
            document, route_key=provider_id, destination=destination, pages_original=pages,
            page_bits_original=page_bits, exact_raster=needs_tiff,
            ecm_and_fine_seen=needs_tiff and _ecm_and_fine_seen(engine, destination),
            provider_renders=not needs_tiff, fec=setting['fec'], style=setting['style'], secret=secret,
            picture=picture, tools=tools)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    if not choice.use:
        log.info('Fax %s goes as normal pages: %s', job_id, choice.sentence)
        return choice, None
    _write_artifact(choice, pdf_path=pdf_path, tiff_path=tiff_path, job_id=job_id, provider_id=provider_id,
                    needs_tiff=needs_tiff)
    row = {
        'phone_number': destination, 'provider_id': provider_id, 'layout': choice.layout,
        'resolution': choice.resolution, 'fec': choice.fec, 'pages_original': max(1, int(pages or 1)),
        'pages_encoded': choice.pages_encoded,
        'seconds_original': _whole(getattr(choice.original, 'seconds', None)),
        'seconds_encoded': _whole(getattr(choice.encoded, 'seconds', None)),
        'cost_original_micros': _micros(getattr(choice.original, 'cost', None)),
        'cost_encoded_micros': _micros(getattr(choice.encoded, 'cost', None)),
        'currency': _currency(choice.original), 'basis': (getattr(choice.encoded, 'basis', None) or None),
        'document_sha256': document.sha256, 'encrypted': 1 if secret else 0, 'format_version': FORMAT_VERSION,
    }
    if row['basis'] is not None:
        row['basis'] = str(row['basis'])[:300]

    def record(connection, now):
        record_send(connection, engine, job_id, row, now)
    return choice, record


def _whole(seconds):
    return None if seconds is None else int(round(seconds))


def _micros(cost):
    """Money as integer micros: a Money-like value with ``micros``, an int of micros, or None."""
    if cost is None:
        return None
    micros = getattr(cost, 'micros', cost)
    return micros if isinstance(micros, int) else None


def _currency(prediction):
    cost = getattr(prediction, 'cost', None)
    currency = getattr(cost, 'currency', None)
    return currency if isinstance(currency, str) and len(currency) == 3 else None


def _write_artifact(choice, *, pdf_path, tiff_path, job_id, provider_id, needs_tiff):
    from .. import codec
    from ..conversion import FAX_IMAGE_MODE, tiff_to_pdf
    folder = Path(pdf_path).parent
    descriptor, temporary = tempfile.mkstemp(prefix='.codec-', suffix='.tiff', dir=folder)
    os.close(descriptor)
    try:
        codec.write_tiff(choice.pages.pages, temporary)
        if needs_tiff and tiff_path:
            os.chmod(temporary, FAX_IMAGE_MODE)
            os.replace(temporary, tiff_path)
            temporary = None
        else:
            target = payload_pdf_path(folder, job_id, provider_id)
            tiff_to_pdf(temporary, str(target))
            os.chmod(target, 0o600)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def combine(*steps):
    """One acceptance-transaction step that runs every given step (None steps are skipped)."""
    present = [step for step in steps if step is not None]
    if not present:
        return None

    def run(connection, now):
        for step in present:
            step(connection, now)
    return run


def sentence(row):
    """The fax detail line for a fax sent as payload pages, or None."""
    if not row:
        return None
    count, original = row['pages_encoded'], row['pages_original']
    return f'Sent as {count} encoded page{"s" if count != 1 else ""} instead of {original} (experimental).'
