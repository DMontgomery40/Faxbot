"""Sending with the experimental payload codec: the encoded pages one attempt may send.

Only for a number whose recipient agreed (``codec_numbers``). The codec is one
of the layouts the attempt-time chooser prices (``conversion.choose_layout``,
called for each attempt from ``pages/sending.py``), beside the pages as they
are and dense pages, all made from the same original pages. It is decided
again for every attempt, so a fax that moves to another route is decided for
that route, and encoded pages are never packed, trimmed or lightened.

Nothing here writes a file: the chooser writes the one layout it keeps as the
attempt's own files (``packed-<fax>-<attempt>``), and the fax's original PDF
and fax image never change. An attempt that sends encoded pages is recorded
in ``fax_page_changes`` for that attempt (layout 'codec', the pages it sent
and its sentence), which is what its cost and the Sent detail read;
``codec_sends`` keeps the codec's details once per fax, from its first attempt
that sent them. The job's own page count stays the original's (the public fax
contract).
"""
import logging
from pathlib import Path
from typing import NamedTuple

import sqlalchemy as sa

from . import decision
from .store import CodecSettings, CodecStoreError, record_send

log = logging.getLogger(__name__)
FORMAT_VERSION = 1


class AttemptPages(NamedTuple):
    """Encoded pages for one attempt: the pages, the Sent detail's sentence, and the ``codec_sends`` details."""
    pages: list
    sentence: str
    row: dict


def setting_for(engine, number, recipient=None):
    """The codec setting that applies to a call to ``number``, or None when encoded pages are off for it.

    When an approved alternate number is dialed instead of the recipient the person chose, both numbers must
    be on: the machine that answers decodes the pages, and the person agreed for their recipient. The dialed
    number's own setting (style, error correction, shared key) is used."""
    if engine is None or not number:
        return None
    try:
        settings = CodecSettings(engine)
        setting = settings.get(number)
        if not setting['enabled']:
            return None
        if recipient and recipient != number and not settings.get(recipient)['enabled']:
            return None
    except (CodecStoreError, sa.exc.SQLAlchemyError):
        return None
    return setting


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


def _secret(engine, seal, number):
    """The shared key sealed for ``number``, or None when it cannot be read."""
    from ..config_secrets import ConfigurationSecretError
    try:
        return CodecSettings(engine, seal).secret(number)
    except (CodecStoreError, ConfigurationSecretError, sa.exc.SQLAlchemyError, OSError):
        return None


def attempt_pages(engine, setting, *, frames, page_bits, number, route, pdf_path, seal=None, exact_raster=False,
                  resolution='fine', tools=None):
    """``AttemptPages`` when the codec's own check (``decision.choose``) says encoded pages save on ``route``,
    else None. Writes nothing.

    ``frames`` are the pages as this attempt would otherwise send them and ``page_bits`` their coded bits, the
    same pages the chooser prices as normal. ``exact_raster``: Faxbot makes the fax image itself (the phone
    line), so run-coded pages are a candidate once a call to the number negotiated ECM at fine resolution;
    otherwise a provider draws the PDF and only sturdy grid pages are made. A number with a shared key never
    gets an unencrypted document: without the key, the attempt sends other pages."""
    from .. import codec
    secret = None
    if setting['has_key']:
        secret = _secret(engine, seal, number)
        if not secret:
            log.warning('The shared key for encoded pages could not be read, so this attempt sends other pages.')
            return None
    document = codec.Document(Path(pdf_path).read_bytes(), 'application/pdf', 'document.pdf')
    # With a shared key the visible picture is a plain pattern: the first page would show through.
    picture = frames[0].convert('L') if setting['style'] == 'picture' and not secret and frames else None
    choice = decision.choose(
        document, route_key=route, destination=number, pages_original=len(frames), page_bits_original=page_bits,
        exact_raster=exact_raster, ecm_and_fine_seen=exact_raster and _ecm_and_fine_seen(engine, number),
        provider_renders=not exact_raster, fec=setting['fec'], style=setting['style'], secret=secret,
        picture=picture, resolution=resolution, tools=tools)
    if not choice.use:
        log.info('Encoded pages were not chosen for this attempt: %s', choice.sentence)
        return None
    row = {
        'phone_number': number, 'provider_id': route, 'layout': choice.layout,
        'resolution': choice.resolution, 'fec': choice.fec, 'pages_original': max(1, len(frames)),
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
    return AttemptPages(list(choice.pages.pages), choice.sentence, row)


def record_attempt(engine, job_id, row, now):
    """Keep the codec's details for the fax, once: an earlier attempt's row stays as it is."""
    from .store import send_for
    if send_for(engine, job_id) is not None:
        return False
    try:
        with engine.begin() as connection:
            record_send(connection, engine, job_id, row, now)
    except sa.exc.IntegrityError:
        return False  # another attempt recorded it first
    return True


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


def combine(*steps):
    """One acceptance-transaction step that runs every given step (None steps are skipped)."""
    present = [step for step in steps if step is not None]
    if not present:
        return None

    def run(connection, now):
        for step in present:
            step(connection, now)
    return run


def sentence(row, status='success'):
    """The fax detail line for a fax sent (or going) as payload pages, or None."""
    if not row:
        return None
    count, original = row['pages_encoded'], row['pages_original']
    pages = f'{count} encoded page{"s" if count != 1 else ""} instead of {original} (experimental).'
    if status == 'success':
        return 'Sent as ' + pages
    if status in ('failed', 'cancelled'):
        return 'Prepared as ' + pages
    return 'Going as ' + pages
