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
JBIG (T.85) through jbigkit's full encoder ``pbmtojbg`` (Debian's jbigkit-bin,
installed in the API image), the ``jbg_enc`` API HylaFAX+ 7.0.11 calls; without
the tools JBIG is not measured and is left out of the result. Strip lengths
leave out ECM framing, retransmissions and negotiation: the bits are the page
data, not the call.

Lossless tuning (``Tuning``): on the SSL Fax engine, faxd codes MR with the
fewest-bytes reset schedule and may tune JBIG (hylafax/patches/0003,
faxd/LosslessTuning.h), so MR and JBIG are measured exactly as faxd sends them:

- MR: each row's one- and two-dimensional code lengths from libtiff (the same
  T.4 codes faxd's G3Encoder writes), then the bytes faxd sends: every EOL after
  the first ends on a byte boundary, Class1Send copies the first two bytes and,
  without error correction, fills every line to the minimum scan line bytes.
  ``mr_schedule`` is the same dynamic program as faxd's, with the same
  tie-break, so the result is faxd's byte count for the page; the fixed schedule
  (a one-dimensional row every K rows) is priced the same way when tuning is off.
- JBIG: plain is options 0, MX 0; tuned is the smallest of ``JBIG_CANDIDATES``
  (ties to the lower options, then the lower MX), the same list as faxd's.

The built-in engine (spandsp) codes MR itself; its MR stays libtiff's strip.

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
- MR and MMR only when the receiving machine's own capabilities (its DIS)
  list them; with no DIS on record they may still be requested, and the
  engine falls back to what the machine has;
- JBIG only when a DIS on record lists it: the built-in engine's frames
  (``engine_frames.decode_dis``) or the SSL Fax engine's session log
  ("REMOTE format support", ``pages.capability.Capability.codings``),
  whichever call was newer. With no DIS on record JBIG is left out, so the
  time is never priced at a JBIG size the machine may not take; a request for
  MMR then leaves the engine its own negotiation, and the call may still use
  JBIG where the machine offers it;
- MMR and JBIG only with error correction on this call, and error correction
  is never turned off to make one possible;
- never a coding that failed to this number (``failing``: engine learning's
  rule, two failures in a row after the machine answered), and never past
  your compression setting or what engine learning chose for the number;
- JBIG that could not be measured (no encoder) stays the SSL Fax engine's
  request wherever it is usable: that engine sends it where the machine takes
  it, and otherwise picks between MH and MMR by its own measurement
  (faxd/FaxSend.c++). Measured on the loopback on 8 October 2026, three
  shaded pages to a JBIG machine took 58 seconds of transfer in JBIG and 108
  in MH, which measured smallest of the others. The built-in engine has no
  JBIG, so it gets the smallest measured coding (``fallback``), and the time is
  priced at that coding's measured size.

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
JBIG_ENCODER = 'pbmtojbg'
JBIG_DECODER = 'jbgtopbm'
JBIG_TIMEOUT_SECONDS = 60
# HylaFAX+ 7.0.11 encodes with jbigkit's full API (faxd/MemoryDecoder.c++: jbg_enc_init, then
# jbg_enc_options(&s, 0, options, 128, mx, 0)): one layer (D = 0, pbmtojbg -q -d 0), order 0 (-o 0), 128 lines a
# stripe (-s 128), then the options byte (-p) and MX (-m). Plain JBIG is options 0, MX 0 (no typical prediction, no
# adaptive template moves). pbmtojbg's own defaults are not T.85 (several layers, deterministic prediction). With no
# file names the tools read standard input and write standard output.
JBIG_BASE = ('-q', '-d', '0', '-o', '0', '-s', '128')
JBIG_PLAIN = (0, 0)
# The settings faxd tries for tuned JBIG (hylafax/patches/0003, faxd/LosslessTuning.h), in tie-break order: options
# 0, TPBON (8), LRLTWO (64) and both (72), each with MX 0, 8 and 32. T.85 Table 1 allows all of them at L0 128.
JBIG_CANDIDATES = tuple((options, mx) for options in (0, 8, 64, 72) for mx in (0, 8, 32))
JBIG_OPTIONS = ('-p', '0', '-m', '0', '-s', '128')  # plain JBIG's settings, as earlier releases passed them


def jbig_arguments(options, mx):
    """pbmtojbg's arguments for one T.85 setting as faxd encodes it."""
    return (*JBIG_BASE, '-p', str(int(options)), '-m', str(int(mx)))
FINE_DPI = (204.0, 196.0)
_INVERT = bytes(255 - value for value in range(256))
_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')


class CodingRefused(ValueError):
    """A documented refusal: no pages, a page that is not one-bit (mode "1"), or an unknown coding name."""


@dataclass(frozen=True)
class Tuning:
    """How the engine that is expected to place the call codes MR and JBIG (``pages.tuning.for_call``).

    ``engine``: 'hylafax' (the SSL Fax engine; faxd converts MR and JBIG pages, hylafax/patches/0003) or 'builtin'
    (spandsp codes MR itself). ``mr``: faxd's fewest-bytes MR schedule (else its fixed one). ``jbig``: tuned JBIG
    is expected (else plain). ``min_line_bytes``: without error correction, the bytes each coded line is filled to
    (the receiving machine's minimum scan line time at the call's speed); 0 with error correction.
    """
    engine: str = 'hylafax'
    mr: bool = True
    jbig: bool = False
    min_line_bytes: int = 0

    def key(self) -> str:
        return f'{self.engine}:mr={int(self.mr)}:jbig={int(self.jbig)}:min={int(self.min_line_bytes)}'


PLAIN = Tuning(mr=False, jbig=False)


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


def jbig_setting(page, tools, *, tuned=False, check=False):
    """(bytes, options, MX) of one page in JBIG (T.85) as faxd sends it: plain (options 0, MX 0), or the smallest of
    ``JBIG_CANDIDATES`` with faxd's tie-break. The page goes as a PBM (1 is black, as PBM has it)."""
    from PIL import Image
    encoder, decoder = tools
    pbm = io.BytesIO()
    page.save(pbm, 'PPM')
    best = None
    for options, mx in (JBIG_CANDIDATES if tuned else (JBIG_PLAIN,)):
        encoded = subprocess.run([encoder, *jbig_arguments(options, mx)], input=pbm.getvalue(), capture_output=True,
                                 check=True, timeout=JBIG_TIMEOUT_SECONDS).stdout
        if check:
            decoded = subprocess.run([decoder], input=encoded, capture_output=True, check=True,
                                     timeout=JBIG_TIMEOUT_SECONDS).stdout
            with Image.open(io.BytesIO(decoded)) as back:
                if back.convert('1').tobytes() != page.tobytes():
                    raise CodingRefused('JBIG did not decode to the same page.')
        if best is None or len(encoded) < best[0]:
            best = (len(encoded), options, mx)
    return best


def _jbig_bits(page, tools, *, check=False, tuned=False):
    """Bits of one page in JBIG (T.85), plain or tuned (``jbig_setting``)."""
    return 8 * jbig_setting(page, tools, tuned=tuned, check=check)[0]


# MR as faxd sends it (hylafax/patches/0003, faxd/LosslessTuning.h) --------------------------------------------

_EOL = '000000000001'


def _row_codes(image, two_dimensional):
    """libtiff's T.4 code of each row of a one-bit image (white runs coded as zeros), EOL removed, tag bit kept
    for MR. One strip, so every row starts at an EOL."""
    stream = io.BytesIO()
    options = {'tiffinfo': {292: 1}} if two_dimensional else {}
    image.save(stream, 'TIFF', compression='group3', dpi=FINE_DPI,
               strip_size=math.ceil(image.width / 8) * image.height, **options)
    value = stream.getvalue()
    stream.seek(0)
    from PIL import Image
    with Image.open(stream) as written:
        offset, length = written.tag_v2[273][0], written.tag_v2[279][0]
    bits = ''.join(format(byte, '08b') for byte in value[offset:offset + length])
    rows = bits.split(_EOL)
    if rows[0] or len(rows) < image.height + 1:
        raise CodingRefused('MR could not be measured on this page.')
    return rows[1:image.height + 1]


def mr_rows(page):
    """(one, two): each row's one- and two-dimensional code length in bits (EOL and tag excluded), the
    two-dimensional one against the row above (``two[0]`` is 0: the first row is always one-dimensional)."""
    from PIL import Image
    width, height = page.size
    stride = math.ceil(width / 8)
    raw = page.tobytes().translate(_INVERT)  # 1 is black: libtiff then codes 0 bits (paper) as white runs
    white = bytes(stride)
    # One white row after the page gives its last row a following EOL to split on.
    extended = Image.frombytes('1', (width, height + 1), raw + white)
    one = [len(code) for code in _row_codes(extended, False)[:height]]
    mixed = _row_codes(extended, True)
    # Rows libtiff coded one-dimensionally get their two-dimensional code from the page moved down one row.
    shifted = _row_codes(Image.frombytes('1', (width, height + 2), white + raw + white), True)[1:]
    two = [0] * height
    for y in range(1, height):
        code = mixed[y] if mixed[y][:1] == '0' else shifted[y]
        if code[:1] != '0':
            raise CodingRefused('MR could not be measured on this page.')
        two[y] = len(code) - 1
    return one, two


def mr_row_bytes(bits, row, rows, min_line):
    """The bytes row ``row`` adds to an MR page faxd sends (faxd/LosslessTuning.h ``mrRowBytes``)."""
    if row == 0:
        found = ((13 if rows == 1 else 25) + bits + 7) // 8 - 2
    elif row + 1 == rows:
        found = (1 + bits + 7) // 8
    else:
        found = (13 + bits + 7) // 8
    return max(found, min_line)


def mr_schedule(one, two, k, min_line=0):
    """(bytes, one_dimensional): faxd's fewest-bytes MR schedule for these row lengths (faxd/LosslessTuning.h
    ``scheduleMR``: the same recurrence and tie-break), and the page's bytes with its two leading bytes."""
    rows = len(one)
    if not rows or k < 1:
        return 0, []
    none = float('inf')
    cost = [mr_row_bytes(one[0], 0, rows, min_line)] + [none] * (k - 1)
    came = [[0] * k]
    for y in range(1, rows):
        best = min(range(k), key=lambda j: (cost[j], j))
        nxt = [cost[best] + mr_row_bytes(one[y], y, rows, min_line)]
        two_bytes = mr_row_bytes(two[y], y, rows, min_line)
        nxt += [cost[j - 1] + two_bytes for j in range(1, k)]
        came.append([best] + list(range(k - 1)))
        cost = nxt
    state = min(range(k), key=lambda j: (cost[j], j))
    total = 2 + cost[state]
    chosen = [False] * rows
    for y in range(rows - 1, -1, -1):
        chosen[y] = state == 0
        state = came[y][state]
    return int(total), chosen


def mr_fixed(one, two, k, min_line=0):
    """The bytes of faxd's fixed MR schedule (a one-dimensional row every K rows), priced the same way."""
    rows = len(one)
    return 2 + sum(mr_row_bytes(one[y] if y % k == 0 else two[y], y, rows, min_line) for y in range(rows)) \
        if rows else 0


def mr_k(page):
    """K for the page's vertical resolution, as faxd uses it: 2 at standard, 4 otherwise (T.4 4.2.1.3.4)."""
    return 2 if _dpi(page)[1] < 150 else 4


def _faxd_mr_bits(page, tuning):
    one, two = mr_rows(page)
    k = mr_k(page)
    found = (mr_schedule(one, two, k, tuning.min_line_bytes)[0] if tuning.mr
             else mr_fixed(one, two, k, tuning.min_line_bytes))
    return 8 * found


def measure(raster_pages, *, codings=None, check=False, tuning=None) -> dict:
    """{coding: bits of each page} for MH, MR and MMR, and JBIG when its encoder is installed.

    ``codings`` limits what is measured (JBIG is still left out without an
    encoder). ``check`` decodes every page again and refuses one that comes
    back different (the tests do; the engines encode the call themselves).
    ``tuning`` (``Tuning``) says how the engine expected to place the call
    codes MR and JBIG; None measures libtiff's MR and plain JBIG, an estimate
    for when the call is not known yet. Raises ``CodingRefused`` for no pages,
    a page that is not one-bit, or an unknown coding name.
    """
    pages = _pages(raster_pages)
    wanted = _known(CODINGS if codings is None else codings)
    tuning = Tuning(engine='builtin', mr=False, jbig=False) if tuning is None else tuning
    result = {}
    for coding in CODINGS:
        if coding not in wanted:
            continue
        if coding == 'JBIG':
            tools = jbig_encoder()
            if tools is None:
                continue
            try:
                result[coding] = tuple(_jbig_bits(page, tools, check=check, tuned=tuning.jbig) for page in pages)
            except (OSError, subprocess.SubprocessError):
                logging.getLogger(__name__).warning('JBIG could not be measured on these pages.', exc_info=True)
            continue
        if coding == 'MR' and tuning.engine == 'hylafax':
            if check:
                for page in pages:
                    _tiff_bits(page, coding, check=True)
            result[coding] = tuple(_faxd_mr_bits(page, tuning) for page in pages)
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


def measure_cached(raster_pages, cache, *, codings=None, tuning=None):
    """``measure``, kept in ``cache`` (a JSON file) by the pages' fingerprint and the tuning; a later call on the
    same pages reads it back. An unreadable or foreign cache is measured again; a cache that cannot be written is
    only logged (the measurement itself is returned either way)."""
    key = digest(raster_pages) + ';' + (tuning.key() if tuning is not None else 'estimate')
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
    measured = measure(raster_pages, codings=wanted, tuning=tuning)
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
    bits_per_page: tuple                 # its measured bits per page (the fallback's for a JBIG not measured)
    all_measured: dict                   # {coding: bits per page} for every coding measured
    reason: str                          # one sentence: "MH: 20% shorter than MMR for these pages."
    measured: bool = True                # False only for a JBIG request that could not be measured
    compared: str | None = None          # the coding the sentence compares with, when there is one
    fallback: str | None = None          # for a JBIG not measured: the smallest measured coding, otherwise sent
    fallback_reason: str | None = None   # that coding's own sentence
    # True when JBIG was left out only because the receiving machine's capabilities are not on record: the SSL Fax
    # engine is then asked for nothing and negotiates the most compact coding itself (JBIG where the machine offers
    # it), and Faxbot learns the machine's codings from that call; the time is priced at the measured coding, an
    # estimate (the lead's rule, 2026-10-08).
    negotiate: bool = False

    @property
    def bits(self) -> int:
        return sum(self.bits_per_page)

    @property
    def priced(self) -> str:
        """The coding the time is priced with: the request, or the fallback for a JBIG not measured."""
        return self.coding if self.measured else self.fallback

    def request(self, engine) -> str | None:
        """The coding to ask ``engine`` ('hylafax' or 'builtin') for: the built-in engine has no JBIG. None for the
        SSL Fax engine and a machine not on record: nothing is asked, so no job control narrows the engine."""
        if engine == 'hylafax' and self.negotiate:
            return None
        return self.coding if self.measured or engine == 'hylafax' else self.fallback


def _shorter(chosen, other, measured):
    mine, theirs = sum(measured[chosen]), sum(measured[other])
    if theirs <= 0 or mine >= theirs:
        return f'{chosen}: the same size as {other} for these pages.'
    percent = round((theirs - mine) * 100 / theirs)
    if percent < 1:
        return f'{chosen}: about the same size as {other} for these pages.'
    return f'{chosen}: {percent}% shorter than {other} for these pages.'


def best_coding(frames, allowed, *, ecm, measured=None, negotiate=False) -> CodingChoice:
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
    if not others:
        other, reason = None, f'{best}: the only coding this call can use.'
    else:
        # Compared with the coding the engines would have taken without measuring (the most compact usable one),
        # or, when that is the one chosen, with the next smallest.
        default = max(candidates, key=_rank)
        other = default if default != best else others[0]
        reason = _shorter(best, other, measured)
    if 'JBIG' in usable and 'JBIG' not in measured:
        # JBIG could not be measured: the SSL Fax engine keeps it where the machine takes it; otherwise, and on the
        # built-in engine, the smallest measured coding goes.
        return CodingChoice('JBIG', tuple(measured[best]), measured,
                            f'JBIG where the receiving machine takes it (not measured here), otherwise {reason}',
                            measured=False, compared=other, fallback=best, fallback_reason=reason,
                            negotiate=negotiate)
    return CodingChoice(best, tuple(measured[best]), measured, reason, compared=other, negotiate=negotiate)


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
    # How the engine expected to place the call codes MR and JBIG (``Tuning``, from ``usable_for``); measuring
    # prices exactly that. None: not known (libtiff's MR and plain JBIG, an estimate).
    tuning: Tuning | None = None

    def allowed(self, coding) -> bool:
        return coding in self.codings

    def needs_request(self, coding, engine='hylafax') -> bool:
        """Whether ``engine`` ('hylafax' or 'builtin', which has no JBIG) must be told ``coding``: left alone, it
        would take a more compact one up to the ceiling that the receiving machine and error correction allow,
        either a usable one that measured larger or one that failed to this number. Equal to what it would take
        anyway: no request is needed. None (nothing asked of the SSL Fax engine) needs none."""
        if coding is None:
            return False
        return any(_rank(coding) < _rank(other) <= _rank(self.ceiling)
                   and (other in self.codings or other in self.failed)
                   and not (engine == 'builtin' and other == 'JBIG') for other in CODINGS)


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


# Why JBIG is left out for a receiving machine Faxbot has not seen yet (lead's decision, 2026-10-08).
JBIG_NOT_ON_RECORD = 'JBIG is used only after an earlier call shows that the receiving machine takes it.'


def receiver_dis(views, capability=None):
    """The receiving machine's newest capabilities as ``decode_dis`` gives them: from the built-in engine's frames
    (``views``, newest first) or the SSL Fax engine's reported codings (``capability.codings`` with its error
    correction), whichever was seen later; None when neither is on record."""
    from .. import engine_frames
    found, when = None, None
    for view in views:
        frame = view.get('frame') or {}
        decoded = engine_frames.decode_dis(frame.get('dis')) if frame.get('dis') else None
        if decoded:
            found, when = decoded, view.get('when')
            break
    codings = getattr(capability, 'codings', None)
    seen = getattr(capability, 'learned_at', None)
    if codings and (found is None or (seen is not None and when is not None and seen > when)):
        found = {'mr': 'MR' in codings, 'mmr': 'MMR' in codings, 'jbig': 'JBIG' in codings,
                 'ecm': bool(getattr(capability, 'ecm', False))}
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
        elif coding == 'JBIG' and receiver is None:
            left_out[coding] = JBIG_NOT_ON_RECORD
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
    from .. import engine_learning as learning, hylafax_engine
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
        # The newest DIS from either engine: built-in frames or the SSL Fax engine's logged capabilities.
        dis = receiver_dis(views, capability)
        # The compression engine learning chose for the number after failures (SSL Fax engine calls).
        learned, _ = learning.compression_rule(views, configured)
    elif getattr(capability, 'codings', None):
        dis = receiver_dis((), capability)
    far_ecm = getattr(capability, 'ecm', None)
    found = usable_codings(ecm=settings.ecm, far_ecm=far_ecm, dis=dis, views=views, configured=configured,
                           learned=learned)
    from dataclasses import replace
    return replace(found, tuning=measuring_tuning(values, engine, number, ecm=settings.ecm, far_ecm=far_ecm,
                                                  max_rate=settings.max_rate, capability=capability))


def measuring_tuning(values, engine, number, *, ecm, far_ecm, max_rate, capability=None) -> Tuning:
    """The ``Tuning`` this call's pages are measured with: the SSL Fax engine when it is set up and running (it then
    places the call, ``hylafax_engine.choose``), else the built-in engine; the MR schedule and tuned JBIG as
    ``pages.tuning.for_call`` asks for them (tuned JBIG over SSL Fax only for a machine that took SSL Fax before);
    and, without error correction on both sides, the receiving machine's minimum scan line time at this call's
    speed, when on record."""
    from .. import hylafax_engine, hylafax_records
    from . import tuning as rules
    running = (bool(getattr(values, 'fax_data_dir', None)) and hylafax_engine.engine_conf_path(values).is_file()
               and hylafax_engine.read_status(values).state == 'running')
    chosen = rules.for_call(values, engine, number)
    sslfax = False
    if engine is not None and chosen.jbig == 'sslfax':
        import sqlalchemy as sa
        try:
            sslfax = bool(hylafax_records.records_for(engine).recipient_detail(number).get('accepts_sslfax'))
        except (sa.exc.SQLAlchemyError, hylafax_records.EngineRecordError):
            logging.getLogger(__name__).warning('Whether this number takes SSL Fax could not be read; its JBIG is '
                                                'priced plain.', exc_info=True)
    scan_ms = getattr(capability, 'scan_ms', None)
    minimum = 0
    if not (ecm and far_ecm is not False) and isinstance(scan_ms, int) and scan_ms > 0:
        minimum = (int(max_rate) // 8 * scan_ms) // 1000
    return Tuning(engine='hylafax' if running else 'builtin', mr=chosen.mr, jbig=chosen.priced_jbig(sslfax),
                  min_line_bytes=minimum)


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
    # A JBIG request that could not be measured keeps its fallback (the coding the built-in engine, and the SSL Fax
    # engine for a machine without JBIG, sends) in ``compared`` and that coding's sentence in ``reason``.
    compared = choice.compared if choice.measured else choice.fallback
    reason = choice.reason if choice.measured else choice.fallback_reason
    row = {'id': uuid.uuid4().hex, 'job_id': job_id, 'attempt_id': attempt_id,
           'number': re.sub(r'[^0-9+]', '', str(number or ''))[:32] or None, 'route': str(route or '')[:40] or None,
           'requested': choice.coding, 'measured': 1 if choice.measured else 0,
           'compared': compared if compared in CODINGS else None,
           'pages': len(choice.bits_per_page), 'bits': json.dumps(totals, sort_keys=True),
           'receiver_known': 1 if receiver_known else 0, 'reason': str(reason or '')[:300],
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


def call_of(engine, attempt_id):
    """(coding the call agreed, engine that placed it) for one attempt: the coding from the built-in engine's
    frames (its last DCS, ``engine_frames.decode_dcs``) or the SSL Fax engine's report (``fax_engine_calls``),
    None until the call reported it; the engine 'hylafax' or 'builtin' from the engine record (frames alone mean
    the built-in engine), None before the call."""
    import sqlalchemy as sa
    from .. import engine_frames
    from ..routing.database import reflect
    tables = reflect(engine, ('fax_call_frames', 'fax_engine_calls'))
    frames, calls = tables['fax_call_frames'], tables['fax_engine_calls']
    columns = [calls.c.engine] + ([calls.c.compression] if 'compression' in calls.c else [])
    with engine.connect() as connection:
        frame = connection.execute(sa.select(frames.c.dcs_last, frames.c.dcs_first).where(
            frames.c.id == f'out:{attempt_id}')).first()
        row = connection.execute(sa.select(*columns).where(
            calls.c.direction == 'outbound', calls.c.call_key == str(attempt_id))).first()
    placed = row[0] if row is not None and row[0] in ('hylafax', 'builtin') else ('builtin' if frame else None)
    if frame is not None:
        decoded = engine_frames.decode_dcs(frame[0]) or engine_frames.decode_dcs(frame[1])
        if decoded and decoded.get('compression') in CODINGS:
            return decoded['compression'], placed
    reported = row[1] if row is not None and len(row) > 1 else None
    return (reported if reported in CODINGS else None), placed


def negotiated(engine, attempt_id) -> str | None:
    """The coding the call of one attempt agreed with the receiving machine (``call_of``); None until reported."""
    return call_of(engine, attempt_id)[0]


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
    agreed, placed = call_of(engine, attempt_id)
    from ..routing.database import DeliveryStoreError
    from . import tuning
    try:
        tuned = tuning.call_tuning(engine, attempt_id)
    except DeliveryStoreError:
        tuned = {}  # before migration 0063: no tuning records yet
    return {**dict(row), 'bits': _bits(row['bits']), 'negotiated': agreed, 'engine': placed, 'tuning': tuned}


def newest_coding(engine, job_id):
    """The coding record of the fax's newest attempt, with what its call negotiated and the attempt's state
    (``attempt_phase``); None when that attempt has none (it went by a fax service, or before measuring)."""
    import sqlalchemy as sa
    if not _ID.fullmatch(str(job_id or '')):
        return None
    table = _table(engine)
    with engine.connect() as connection:
        try:
            attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=connection)
        except sa.exc.NoSuchTableError:
            attempts = None
        newest = None
        if attempts is not None:
            newest = connection.execute(sa.select(attempts.c.id, attempts.c.phase).where(
                attempts.c.job_id == job_id).order_by(attempts.c.sequence.desc()).limit(1)).first()
        query = sa.select(table.c.attempt_id).where(table.c.job_id == job_id)
        if newest is not None:
            query = query.where(table.c.attempt_id == newest[0])
        row = connection.execute(query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).first()
    if row is None:
        return None
    record = attempt_coding(engine, row[0])
    return {**record, 'attempt_phase': newest[1] if newest is not None else None} if record else None


def sent_sentence(record, phase=None) -> str | None:
    """The Sent detail's coding line for the attempt's state: "Sent with MH: 20% shorter than MMR for these
    pages." (delivered, or not known), "Going with ..." (on its way), "Tried with ..." (the call failed),
    "Prepared with ..." (cancelled); and, when the call used another coding, which one ("The call used MMR."),
    as a fact without a cause: the machine, or an engine that does not yet take the request, may be why."""
    if not record:
        return None
    reason = str(record.get('reason') or '').strip()
    requested, agreed = record.get('requested'), record.get('negotiated')
    if not reason or requested not in CODINGS:
        return None
    phase = phase if phase is not None else record.get('attempt_phase')
    verb = {None: 'Sent with', 'success': 'Sent with', 'failed': 'Tried with',
            'cancelled': 'Prepared with'}.get(phase, 'Going with')
    expected = {requested}
    if requested == 'JBIG' and not record.get('measured'):
        # JBIG not measured: the record keeps the fallback (``compared``) and its sentence (``reason``). The built-in
        # engine has no JBIG and sent the fallback; the SSL Fax engine sent JBIG where the machine took it.
        fallback = record.get('compared')
        expected = {'JBIG', fallback}
        if record.get('engine') == 'builtin':
            expected = {fallback}
        else:
            reason = f'JBIG where the receiving machine takes it (not measured here), otherwise {reason}'
    # Lossless tuning, when the engine reported it for the coding the call used: "JBIG, tuned", "MR, tuned schedule".
    from .tuning import sent_suffix
    tuned = record.get('tuning') or {}
    if agreed and agreed not in expected:
        return f'{verb} {reason} The call used {agreed}{sent_suffix(tuned, agreed)}.'
    used = agreed or requested
    suffix = sent_suffix(tuned, used)
    if suffix and reason.startswith(f'{used}:'):
        reason = f'{used}{suffix}:' + reason[len(used) + 1:]
    elif suffix:
        reason += f' The call used {used}{suffix}.'
    return f'{verb} {reason}'


def seconds_text(bits, rate=14400):
    """'about 49 seconds' of page data at ``rate`` bit/s."""
    from ..routing.predict import duration_text
    return duration_text(bits / rate)


def measured_sentence(bits) -> str | None:
    """"Measured on these pages at 14,400 bit/s: MH about 49 seconds, MR about 59 seconds, MMR about 1 minute
    1 second." from the measured bits of each coding; None without measurements."""
    parts = [f'{name} {seconds_text(bits[name])}' for name in CODINGS if name in (bits or {})]
    return ('Measured on these pages at 14,400 bit/s: ' + ', '.join(parts) + '.') if parts else None


def coding_view(record):
    """The coding record for the Sent detail and the command line: requested, negotiated, measured bits a
    coding, and one sentence each; None without a record."""
    if not record:
        return None
    bits = record.get('bits') or {}
    return {'requested': record['requested'], 'negotiated': record.get('negotiated'), 'engine': record.get('engine'),
            'measured': bool(record.get('measured')), 'compared': record.get('compared'),
            'pages': record.get('pages'), 'bits': bits,
            'receiver_known': bool(record.get('receiver_known')), 'sentence': sent_sentence(record),
            'measured_sentence': measured_sentence(bits), **_tuning_view(record.get('tuning'))}


def _tuning_view(rows):
    """What lossless tuning did on the call (``pages.tuning``): which codings it tuned, and one sentence."""
    from .tuning import sent_sentence as tuning_sentence
    rows = rows or {}
    return {'tuned': sorted(coding for coding, row in rows.items() if row.get('tuned_pages')),
            'tuning_refused': sorted(coding for coding, row in rows.items() if row.get('refused')),
            'tuning_sentence': tuning_sentence(rows)}


def sent_view(engine, job_id):
    """The Sent detail's coding block for the fax's newest attempt (``coding_view``), or None. Unreadable records
    (before migration 0056) read as none."""
    from ..routing.database import DeliveryStoreError
    import sqlalchemy as sa
    try:
        return coding_view(newest_coding(engine, job_id))
    except (DeliveryStoreError, sa.exc.SQLAlchemyError):
        logging.getLogger(__name__).warning('The fax coding of this fax could not be read.')
        return None
