"""Which lossless fax coding a fax goes with, measured on its own pages before dialing.

Faxbot used to assume a fixed order (JBIG, MMR, MR, MH, from most to least
compact) and fixed ratios (MH twice MMR's bits). Measured on AR's synthetic
pages with libtiff 4.7.1 (research/faxbot-next-experiments-2026-10-08), MH is
20% smaller than MMR on a shaded table, 30% on a tinted form and 34% on a
noisy gray scan, while MMR is 94% smaller than MH on black text. So each
usable coding is measured on the actual pages, and the attempt's single layout
chooser (``conversion.choose_layout``) takes the measurements. Every coding is
lossless: the receiving machine prints exactly the same pixels.

Measuring (``measure``)
-----------------------
MH (T.4 one-dimensional), MR (T.4 two-dimensional, K = 2 at standard and 4 at
fine resolution, as T.4 4.2.1.3.4 sets it) and MMR (T.6) through libtiff in
Pillow, with the paper coded as white runs (``conversion._g4_data``'s
convention), so the measured MMR bits are exactly ``conversion.frame_bits``.
JBIG (T.85) only when jbigkit's ``pbmtojbg85`` is installed; without it JBIG
is not measured and is left out of the result (neither this computer nor the
API image has it today). Strip lengths leave out ECM framing, fill bits,
retransmissions and negotiation: the bits are the page data, not the call.

Requesting a coding (``best_coding``)
--------------------------------------
Both of Faxbot's engines treat the coding they are given as the most compact
one they may use, never as a force, so a request never makes a machine get a
coding it lacks:

- HylaFAX+ 7.0.11 (the SSL Fax engine): ``JPARM DATAFORMAT`` (hfaxd
  Jobs.c++ ``dataVals``: G31D, G32D, G4, JBIG) sets the job's ``desireddf``
  (sendq(5): "The desired data format to use for page data transmissions");
  faxsend uses ``fxmin(modem best, desireddf)`` (faxd/FaxSend.c++) and the
  queue prepares MMR only with error correction (faxd/faxQueueApp.c++).
- spandsp 0.0.6 (the built-in engine): ``t30_set_supported_compressions`` is
  the set Faxbot offers; the session takes T.6 only with error correction and
  the far end's T.6 bit, then 2-D when both have it, else 1-D (t30.c,
  ``process_rx_dis_dtc``). Faxbot sets it per call from FAXBOT_COMPRESSION.

So the request is the measured smallest coding among the usable ones:

- MH always (T.30 requires it of every machine);
- MR, MMR and JBIG only when the receiving machine's own capabilities (its
  DIS, ``engine_frames.decode_dis``) list them; with no DIS on record they may
  still be requested, and the engine falls back to what the machine has;
- MMR and JBIG only with error correction on this call, and error correction
  is never turned off to make one possible;
- never a coding that failed to this number (``failing``: engine learning's
  rule, two failures in a row after the machine answered), and never past
  your compression setting or what engine learning chose for the number;
- JBIG that could not be measured is kept as the request only when MMR, its
  two-dimensional relative, measured smallest; the time is then priced at
  MMR's measured size.

A tie keeps the more widely supported coding (MH, then MR, then MMR).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import weakref

CODINGS = ('MH', 'MR', 'MMR', 'JBIG')
# The codings that need error correction (T.30: T.6 and T.85 are used only in ECM).
NEEDS_ECM = frozenset({'MMR', 'JBIG'})
# The compression setting's values (sip_trunk.COMPRESSIONS) and HylaFAX's job data formats (hfaxd Jobs.c++).
SETTING = {'MH': 'mh', 'MR': 'mr', 'MMR': 'mmr', 'JBIG': 'jbig'}
FROM_SETTING = {value: key for key, value in SETTING.items()}
JBIG_ENCODER = 'pbmtojbg85'
JBIG_DECODER = 'jbgtopbm85'
JBIG_TIMEOUT_SECONDS = 60
FINE_DPI = (204.0, 196.0)
_INVERT = bytes(255 - value for value in range(256))
_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')


class CodingRefused(ValueError):
    """A documented refusal: no pages, a page that is not one-bit (mode "1"), or an unknown coding name."""


def _rank(coding):
    return CODINGS.index(coding)


def _known(codings):
    names = tuple(codings)
    unknown = [name for name in names if name not in CODINGS]
    if unknown:
        raise CodingRefused('Unknown fax coding: ' + ', '.join(sorted(str(name) for name in unknown)) + '.')
    return names


def _pages(raster_pages):
    pages = list(raster_pages or ())
    if not pages:
        raise CodingRefused('There are no pages to measure.')
    if any(getattr(page, 'mode', None) != '1' for page in pages):
        raise CodingRefused('Only one-bit fax pages can be measured.')
    return pages


# Measuring ----------------------------------------------------------------------------------------------------

def jbig_encoder():
    """jbigkit's T.85 encoder and decoder, when both are installed; None otherwise."""
    encoder, decoder = shutil.which(JBIG_ENCODER), shutil.which(JBIG_DECODER)
    return (encoder, decoder) if encoder and decoder else None


def _dpi(page):
    try:
        x, y = (float(value) for value in (page.info.get('dpi') or FINE_DPI))
    except (TypeError, ValueError):
        return FINE_DPI
    return (x, y) if x > 0 and y > 0 else FINE_DPI


def _tiff_bits(page, coding, *, check=False):
    """Bits of one page in MH, MR or MMR: libtiff's strip, the paper coded as white runs."""
    from PIL import Image, features
    if not features.check('libtiff'):
        from ..conversion import DocumentConversionError
        raise DocumentConversionError('Fax image writing is unavailable.', operational=True)
    inverted = Image.frombytes('1', page.size, page.tobytes().translate(_INVERT))
    options = {'MH': {'compression': 'group3'},
               # T4Options bit 0: two-dimensional coding. libtiff sets K from the vertical resolution.
               'MR': {'compression': 'group3', 'tiffinfo': {292: 1}},
               'MMR': {'compression': 'group4'}}[coding]
    stream = io.BytesIO()
    inverted.save(stream, 'TIFF', dpi=_dpi(page), strip_size=math.ceil(page.width / 8) * page.height, **options)
    stream.seek(0)
    with Image.open(stream) as written:
        counts = written.tag_v2.get(279)
        counts = counts if isinstance(counts, tuple) else (counts,)
        bits = 8 * sum(int(count) for count in counts)
        if check and written.tobytes() != inverted.tobytes():
            raise CodingRefused(f'{coding} did not decode to the same page.')
    return bits


def _jbig_bits(page, tools, *, check=False):
    """Bits of one page in JBIG (T.85), through jbigkit; the page as a PBM (1 is black, as PBM has it)."""
    from PIL import Image
    encoder, decoder = tools
    pbm = io.BytesIO()
    page.save(pbm, 'PPM')
    encoded = subprocess.run([encoder, '-', '-'], input=pbm.getvalue(), capture_output=True, check=True,
                             timeout=JBIG_TIMEOUT_SECONDS).stdout
    if check:
        decoded = subprocess.run([decoder, '-', '-'], input=encoded, capture_output=True, check=True,
                                 timeout=JBIG_TIMEOUT_SECONDS).stdout
        with Image.open(io.BytesIO(decoded)) as back:
            if back.convert('1').tobytes() != page.tobytes():
                raise CodingRefused('JBIG did not decode to the same page.')
    return 8 * len(encoded)


def measure(raster_pages, *, codings=None, check=False) -> dict:
    """{coding: bits of each page} for MH, MR and MMR, and JBIG when its encoder is installed.

    ``codings`` limits what is measured (JBIG is still left out without an
    encoder). ``check`` decodes every page again and refuses one that comes
    back different (the tests do; the engines encode the call themselves).
    Raises ``CodingRefused`` for no pages, a page that is not one-bit, or an
    unknown coding name.
    """
    pages = _pages(raster_pages)
    wanted = _known(CODINGS if codings is None else codings)
    result = {}
    for coding in CODINGS:
        if coding not in wanted:
            continue
        if coding == 'JBIG':
            tools = jbig_encoder()
            if tools is None:
                continue
            try:
                result[coding] = tuple(_jbig_bits(page, tools, check=check) for page in pages)
            except (OSError, subprocess.SubprocessError):
                logging.getLogger(__name__).warning('JBIG could not be measured on these pages.', exc_info=True)
            continue
        result[coding] = tuple(_tiff_bits(page, coding, check=check) for page in pages)
    return result


def digest(raster_pages) -> str:
    """One fingerprint of these pages' pixels and sizes, for the measurement cache."""
    found = hashlib.sha256()
    for page in _pages(raster_pages):
        found.update(f'{page.width}x{page.height}@{_dpi(page)[1]:.0f};'.encode('ascii'))
        found.update(page.tobytes())
    return found.hexdigest()


def cache_path(tiff_path):
    """The measurement cache beside an attempt's page files (``packed-<fax>-<attempt>.coding.json``), so the
    retention cleanup that removes the attempt's files (``pages.sending.cleanup``) removes it too."""
    path = Path(tiff_path)
    return path.with_name(path.stem + '.coding.json')


def measure_cached(raster_pages, cache, *, codings=None):
    """``measure``, kept in ``cache`` (a JSON file) by the pages' fingerprint; a later call on the same pages
    reads it back. An unreadable or foreign cache is measured again; a cache that cannot be written is only
    logged (the measurement itself is returned either way)."""
    key = digest(raster_pages)
    wanted = tuple(_known(CODINGS if codings is None else codings))
    path = Path(cache)
    kept = {}
    try:
        if path.is_file() and not path.is_symlink():
            kept = json.loads(path.read_text(encoding='utf-8'))
            kept = kept if isinstance(kept, dict) else {}
    except (OSError, ValueError):
        kept = {}
    found = kept.get(key)
    if isinstance(found, dict) and all(name in found or name == 'JBIG' for name in wanted):
        try:
            return {name: tuple(int(bits) for bits in found[name]) for name in wanted if name in found}
        except (TypeError, ValueError):
            pass
    measured = measure(raster_pages, codings=wanted)
    kept[key] = {name: list(bits) for name, bits in measured.items()}
    try:
        temporary = path.with_name(path.name + f'.{uuid.uuid4().hex[:8]}.tmp')
        temporary.write_text(json.dumps(kept), encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        logging.getLogger(__name__).warning('The measured fax codings could not be kept with the attempt files.')
    return measured


# Choosing -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CodingChoice:
    coding: str                          # the coding to request: 'MH', 'MR', 'MMR' or 'JBIG'
    bits_per_page: tuple                 # its measured bits per page (MMR's for a JBIG that was not measured)
    all_measured: dict                   # {coding: bits per page} for every coding measured
    reason: str                          # one sentence: "MH: 20% shorter than MMR for these pages."
    measured: bool = True                # False only for a JBIG request that could not be measured
    compared: str | None = None          # the coding the sentence compares with, when there is one

    @property
    def bits(self) -> int:
        return sum(self.bits_per_page)


def _shorter(chosen, other, measured):
    mine, theirs = sum(measured[chosen]), sum(measured[other])
    if theirs <= 0 or mine >= theirs:
        return f'{chosen}: the same size as {other} for these pages.'
    percent = round((theirs - mine) * 100 / theirs)
    if percent < 1:
        return f'{chosen}: about the same size as {other} for these pages.'
    return f'{chosen}: {percent}% shorter than {other} for these pages.'


def best_coding(frames, allowed, *, ecm, measured=None) -> CodingChoice:
    """The coding to request for these pages: the usable coding with the fewest measured bits.

    ``allowed`` names the codings this call may use (MH is always added);
    ``ecm`` False drops MMR and JBIG, and error correction is never turned off
    for them. ``measured`` reuses an earlier ``measure()`` of the same pages.
    Raises ``CodingRefused`` for no pages, a page that is not one-bit, or an
    unknown coding name in ``allowed``.
    """
    _pages(frames)
    usable = set(_known(allowed or ())) | {'MH'}
    if not ecm:
        usable -= NEEDS_ECM
    measured = dict(measure(frames, codings=[name for name in CODINGS if name in usable]) if measured is None
                    else measured)
    _known(measured)
    candidates = [name for name in CODINGS if name in usable and name in measured]
    if 'MH' not in candidates:
        measured.update(measure(frames, codings=('MH',)))
        candidates.insert(0, 'MH')
    best = min(candidates, key=lambda name: (sum(measured[name]), _rank(name)))
    others = sorted((name for name in candidates if name != best), key=lambda name: (sum(measured[name]),
                                                                                     _rank(name)))
    if best == 'MMR' and 'JBIG' in usable and 'JBIG' not in measured:
        # JBIG is MMR's two-dimensional relative and was not measured: kept where the machine takes it.
        return CodingChoice('JBIG', tuple(measured['MMR']), measured,
                            'JBIG where the receiving machine takes it (not measured here), otherwise MMR, '
                            'the smallest of the codings measured for these pages.', measured=False,
                            compared='MMR')
    if not others:
        return CodingChoice(best, tuple(measured[best]), measured,
                            f'{best}: the only coding this call can use.', compared=None)
    # Compared with the coding the engines would have taken without measuring (the most compact usable one),
    # or, when that is the one chosen, with the next smallest.
    default = max(candidates, key=_rank)
    other = default if default != best else others[0]
    return CodingChoice(best, tuple(measured[best]), measured, _shorter(best, other, measured), compared=other)


# What may be requested for a number ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Usable:
    """The codings a call to one number may request, and why the others are left out."""
    codings: frozenset
    ecm: bool                            # error correction on this call (Faxbot's side)
    receiver: frozenset | None           # what the receiving machine's DIS lists; None when not on record
    left_out: dict                       # {coding: one sentence}
    ceiling: str                         # your setting, or what engine learning chose for the number
    failed: frozenset = frozenset()      # codings left out because calls to the number failed with them

    def allowed(self, coding) -> bool:
        return coding in self.codings

    def needs_request(self, coding) -> bool:
        """Whether the engines must be told ``coding``: left alone, they would take a more compact one up to the
        ceiling that the receiving machine and error correction allow, either a usable one that measured larger or
        one that failed to this number. Equal to what they would take anyway: no request is needed."""
        return any(_rank(coding) < _rank(other) <= _rank(self.ceiling)
                   and (other in self.codings or other in self.failed) for other in CODINGS)


def receiver_codings(dis) -> frozenset | None:
    """The codings a receiving machine's DIS lists (``engine_frames.decode_dis``), or None for no DIS."""
    if not dis:
        return None
    found = {'MH'}
    if dis.get('mr'):
        found.add('MR')
    if dis.get('mmr'):
        found.add('MMR')
    if dis.get('jbig'):
        found.add('JBIG')
    return frozenset(found)


def failing(views, *, fails=None, minimum=None) -> dict:
    """{coding: failures in a row} for codings whose calls to this number failed ``fails`` times in a row after
    the machine answered, with no success with that coding since: engine learning's rule
    (``engine_learning.compression_rule``), on either engine. Needs ``minimum`` answered calls. MH is never
    left out: every call must have one coding."""
    from .. import engine_learning as learning
    fails = learning.COMPRESSION_FAILS if fails is None else fails
    minimum = learning.MIN_CALLS if minimum is None else minimum
    usable = [view for view in views if view.get('direction', 'outbound') == 'outbound' and view.get('answered')
              and view.get('compression') in CODINGS and view.get('status') in ('SUCCESS', 'FAILED')]
    if len(usable) < minimum:
        return {}
    streak, found = {}, {}
    for view in sorted(usable, key=lambda item: item['when']):
        used = view['compression']
        if view['status'] == 'SUCCESS':
            streak[used] = 0
            found.pop(used, None)
            continue
        if view.get('verdict') != 'remote_fax_failed':
            continue
        streak[used] = streak.get(used, 0) + 1
        if streak[used] >= fails and used != 'MH':
            found[used] = streak[used]
    return found


def usable_codings(*, ecm, far_ecm=None, dis=None, views=(), configured='jbig', learned=None) -> Usable:
    """What a call may request (pure): ``ecm`` this call's error correction, ``far_ecm`` whether the receiving
    machine has it (None: not known), ``dis`` its decoded DIS (None: not on record), ``views`` engine
    learning's joined calls to the number, ``configured`` your compression setting and ``learned`` the
    compression engine learning chose for the number (setting values: 'mh', 'mr', 'mmr', 'jbig')."""
    ceiling = FROM_SETTING.get(str(configured or 'jbig').lower(), 'JBIG')
    learned_coding = FROM_SETTING.get(str(learned or '').lower())
    if learned_coding and _rank(learned_coding) < _rank(ceiling):
        ceiling = learned_coding
    receiver = receiver_codings(dis)
    if far_ecm is None and dis:
        far_ecm = bool(dis.get('ecm'))
    left_out = {}
    codings = set()
    failed = failing(views)
    for coding in CODINGS:
        if coding == 'MH':
            codings.add(coding)
            continue
        if _rank(coding) > _rank(ceiling):
            left_out[coding] = f'{coding} is past the most compact coding allowed for this number ({ceiling}).'
        elif coding in NEEDS_ECM and not ecm:
            left_out[coding] = f'{coding} needs error correction, which is off for this call.'
        elif coding in NEEDS_ECM and far_ecm is False:
            left_out[coding] = f'{coding} needs error correction, which the receiving machine does not have.'
        elif receiver is not None and coding not in receiver:
            left_out[coding] = f'The receiving machine does not take {coding}.'
        elif coding in failed:
            left_out[coding] = (f'Faxes to this number with {coding} failed {failed[coding]} times in a row after '
                                'its fax machine answered.')
        else:
            codings.add(coding)
    return Usable(frozenset(codings), bool(ecm), receiver, left_out, ceiling,
                  frozenset(coding for coding in failed if coding in left_out))


def usable_for(engine, values, number, *, recipient=None, capability=None, now=None) -> Usable:
    """``usable_codings`` for a call to ``number`` from the installation's records: the call's error correction
    (``hylafax_engine.call_settings``, your settings, the number's own limits and what engine learning turned
    on), the receiving machine's newest DIS and error correction, engine learning's calls to the number and
    the compression it chose, and your compression setting. Unreadable records raise SQLAlchemy's error."""
    from .. import engine_frames, engine_learning as learning, hylafax_engine
    now = now or learning.utcnow()
    # Error correction is the same on either engine: your setting, the number's own limit, and what engine learning
    # turned on (never off).
    settings = hylafax_engine.call_settings(values, number, recipient=recipient)
    configured = getattr(values, 'sip_fax_compression', 'jbig')
    views, dis, learned = [], None, None
    if engine is not None:
        epoch = learning.current_epoch(engine, values, now=now, write=False)
        joined = learning.joined_calls(engine, number, direction='outbound',
                                       since=learning.evidence_since(epoch, now, learning.LEARN_DAYS), limit=50)
        views = learning.on_trunk(joined, values)
        for view in views:
            frame = view.get('frame') or {}
            dis = engine_frames.decode_dis(frame.get('dis')) if frame.get('dis') else None
            if dis:
                break
        # The compression engine learning chose for the number after failures (SSL Fax engine calls).
        learned, _ = learning.compression_rule(views, configured)
    far_ecm = getattr(capability, 'ecm', None)
    return usable_codings(ecm=settings.ecm, far_ecm=far_ecm, dis=dis, views=views, configured=configured,
                          learned=learned)


# Recording and reading ----------------------------------------------------------------------------------------

TABLE = 'fax_coding_choices'
_TABLES = weakref.WeakKeyDictionary()


def _table(engine):
    """The coding table, reflected once per database. Raises ``routing.database.DeliveryStoreError`` when the
    database cannot be read or has no such table (before migration 0056)."""
    found = _TABLES.get(engine)
    if found is None:
        from ..routing.database import reflect
        found = reflect(engine, (TABLE,))[TABLE]
        _TABLES[engine] = found
    return found


def record_choice(engine, *, job_id, attempt_id, number, route, choice, receiver_known, now=None):
    """Keep the coding an attempt requested, what was measured and why, once per attempt (``fax_coding_choices``,
    migration 0056); returns the row's ID. Rows are never rewritten."""
    import sqlalchemy as sa
    if not (_ID.fullmatch(str(job_id or '')) and _ID.fullmatch(str(attempt_id or ''))):
        raise ValueError('Unsupported coding record')
    if choice.coding not in CODINGS:
        raise CodingRefused('Unknown fax coding: ' + str(choice.coding) + '.')
    table = _table(engine)
    totals = {name: sum(bits) for name, bits in choice.all_measured.items() if name in CODINGS}
    row = {'id': uuid.uuid4().hex, 'job_id': job_id, 'attempt_id': attempt_id,
           'number': re.sub(r'[^0-9+]', '', str(number or ''))[:32] or None, 'route': str(route or '')[:40] or None,
           'requested': choice.coding, 'measured': 1 if choice.measured else 0,
           'compared': choice.compared if choice.compared in CODINGS else None,
           'pages': len(choice.bits_per_page), 'bits': json.dumps(totals, sort_keys=True),
           'receiver_known': 1 if receiver_known else 0, 'reason': choice.reason[:300],
           'created_at': now or datetime.utcnow()}
    with engine.begin() as connection:
        found = connection.execute(sa.select(table.c.id).where(table.c.attempt_id == attempt_id)).scalar()
        if found is not None:
            return found
        connection.execute(table.insert().values(**row))
    return row['id']


def _bits(text):
    try:
        found = json.loads(text or '{}')
    except ValueError:
        return {}
    return {name: int(bits) for name, bits in found.items() if name in CODINGS and isinstance(bits, int)} \
        if isinstance(found, dict) else {}


def negotiated(engine, attempt_id) -> str | None:
    """The coding the call of one attempt agreed with the receiving machine: from the built-in engine's frames
    (its last DCS, ``engine_frames.decode_dcs``) or the SSL Fax engine's report (``fax_engine_calls``); None
    until the call reported it."""
    import sqlalchemy as sa
    from .. import engine_frames
    from ..routing.database import reflect
    tables = reflect(engine, ('fax_call_frames', 'fax_engine_calls'))
    frames, calls = tables['fax_call_frames'], tables['fax_engine_calls']
    with engine.connect() as connection:
        frame = connection.execute(sa.select(frames.c.dcs_last, frames.c.dcs_first).where(
            frames.c.id == f'out:{attempt_id}')).first()
        reported = None
        if 'compression' in calls.c:
            reported = connection.execute(sa.select(calls.c.compression).where(
                calls.c.direction == 'outbound', calls.c.call_key == str(attempt_id))).scalar()
    if frame is not None:
        decoded = engine_frames.decode_dcs(frame[0]) or engine_frames.decode_dcs(frame[1])
        if decoded and decoded.get('compression') in CODINGS:
            return decoded['compression']
    return reported if reported in CODINGS else None


def attempt_coding(engine, attempt_id):
    """The coding record of one attempt with what the call negotiated, or None."""
    import sqlalchemy as sa
    if not _ID.fullmatch(str(attempt_id or '')):
        return None
    table = _table(engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.attempt_id == attempt_id)).mappings().first()
    if row is None:
        return None
    return {**dict(row), 'bits': _bits(row['bits']), 'negotiated': negotiated(engine, attempt_id)}


def newest_coding(engine, job_id):
    """The coding record of the fax's newest attempt that has one, with what its call negotiated, or None."""
    import sqlalchemy as sa
    if not _ID.fullmatch(str(job_id or '')):
        return None
    table = _table(engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(table.c.attempt_id).where(table.c.job_id == job_id).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).first()
    return attempt_coding(engine, row[0]) if row is not None else None


def sent_sentence(record) -> str | None:
    """The Sent detail's coding line: "Sent with MH: 20% shorter than MMR for these pages." and, when the
    receiving machine agreed to another coding, what the call used."""
    if not record:
        return None
    reason = str(record.get('reason') or '').strip()
    requested, agreed = record.get('requested'), record.get('negotiated')
    if not reason or requested not in CODINGS:
        return None
    sentence = f'Sent with {reason}'
    if agreed and agreed != requested and not (requested == 'JBIG' and not record.get('measured')):
        sentence += f' The receiving machine took {agreed}.'
    return sentence


def coding_view(record):
    """The coding record for the Sent detail and the command line: requested, negotiated, measured bits a
    coding, and one sentence; None without a record."""
    if not record:
        return None
    return {'requested': record['requested'], 'negotiated': record.get('negotiated'),
            'measured': bool(record.get('measured')), 'compared': record.get('compared'),
            'pages': record.get('pages'), 'bits': record.get('bits') or {},
            'receiver_known': bool(record.get('receiver_known')), 'sentence': sent_sentence(record)}
