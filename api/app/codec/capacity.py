"""The capacity layout (format 2): payload bits drawn as MH runs at the capacity of the complete T.4 code.

The run-coded layout (``runs``) reads payload bits as a walk down the MH code
tree pruned to runs of 1..15 pixels; where the pruned tree has one branch, the
branch carries nothing. This layout instead turns payload bits into runs with an
arithmetic decoder whose symbol law is the maxentropic one for the complete T.4
code (terminating and make-up codes): a run of colour c and length r has
probability proportional to x**-(len_c(r) + lam * r), where len_c(r) is its MH
code length, ``lam`` the price of one pixel in coded bits and x the root that
makes the two colours' laws a capacity-achieving pair (research/faxbot-novel-
vectors-2026-10-08, section 2.1). A small ``lam`` makes long runs (fewest coded
bits per payload bit: the best line time, for routes billed by the minute); a
large one makes short runs (the most payload per page, for routes billed by the
page).

The law is frozen as integer frequencies (``capacity_tables.json``, totals of
2**20 per colour), never computed again at run time, so this decoder and the
browser decoder (tools/fax-decoder) use exactly the same numbers. A page header's
profile byte names one table (``PROFILES``); a profile is immutable once
published (``TABLES_SHA256``).

Each scan line holds::

    data part: 32-bit stream bit offset (XORed with a tag-keyed mask) | payload bits, arithmetic-coded
    16-bit CRC, written as a walk with runs of at most 7 pixels (as ``runs``)
    one padding run

The data part ends exactly when ``RESERVE`` pixels are left: each run is drawn
from the runs that still fit. The receiver re-encodes the data part's runs with
the matching arithmetic encoder; the bits that encoder has settled (every bit
on which all inputs leading to those runs agree) are the offset and the payload
bits this line carries, a number that varies from line to line. The sender
checks every line this way before it is drawn, so a line never carries a bit
the receiver would not recover. Payload bits are XORed with a keystream keyed by
the document tag and the line's offset (xorshift32), so a long run of equal bits
in the stream (zero padding) still makes ordinary runs. The CRC covers the tag,
the offset, the payload bit count and the payload bits (``runs._crc``). Lines
are independent, as in the run-coded layout: a lost or damaged line is a run of
erased stream bytes for the Reed-Solomon code.

This layout needs the exact raster, like ``runs``: ECM, T.38 and the engines'
lossless recoding keep it; resolution conversion does not.
"""
from __future__ import annotations

from bisect import bisect_right
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

from . import runs as runcode
from . import t4

PRECISION = 31
FULL = (1 << PRECISION) - 1
HALF = 1 << (PRECISION - 1)
QUARTER = 1 << (PRECISION - 2)
TOTAL_BITS = 20
OFFSET_BITS = 32
CRC_LIMIT = runcode.CRC_LIMIT
# Pixels left for the CRC walk (16 bits, runs of at most 7) and the padding run.
RESERVE = 16 * CRC_LIMIT + 1
# One extended make-up code covers runs to 2560 dots; longer runs would need two.
MAX_RUN = 2560
# A line must carry at least this many payload bits, or the page is not made (the chooser keeps another layout).
MIN_LINE_PAYLOAD = 64
TABLES_PATH = Path(__file__).with_name('capacity_tables.json')
# The frozen tables' fingerprint: a profile never changes once a page names it.
TABLES_SHA256 = 'da4933e337d1720da431c3c99bb4ae4677ce5eb433952f4fae103e0b77d9785b'
# Profile byte -> (name, pixel price). The chooser tries 'time' and 'pages' and lets the route's bill decide.
PROFILES = {1: ('time', 0.08), 2: ('balanced', 0.3), 3: ('pages', 2.0)}
PROFILE_NAMES = {name: number for number, (name, _) in PROFILES.items()}


class CapacityError(ValueError):
    pass


# --- the frozen tables -------------------------------------------------------------------------------------------

def code_length(colour, run):
    """MH code length of one run (make-up code plus terminating code), runs 1..MAX_RUN."""
    if not 1 <= run <= MAX_RUN:
        raise CapacityError('run out of range')
    return len(t4.run_code(run, colour))


def build_tables():
    """The integer frequency tables for every profile (maintenance only: the published file is frozen). Uses
    floating point, so it is run once and its output committed; nothing reads it at run time."""
    out = {'format': 1, 'total_bits': TOTAL_BITS, 'max_run': MAX_RUN, 'profiles': {}}
    lengths = {colour: [code_length(colour, run) for run in range(1, MAX_RUN + 1)] for colour in (0, 1)}
    for number, (name, lam) in PROFILES.items():
        def product(x):
            return math.prod(sum(x ** -(length + lam * run) for run, length in enumerate(lengths[colour], 1))
                             for colour in (0, 1))
        lo, hi = 1.0 + 1e-12, 4.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if product(mid) > 1:
                lo = mid
            else:
                hi = mid
        entry = {'name': name, 'pixel_price': lam, 'log2_root': round(math.log2(lo), 6)}
        for colour, key in ((0, 'white'), (1, 'black')):
            weights = [lo ** -(length + lam * run) for run, length in enumerate(lengths[colour], 1)]
            total = sum(weights)
            scale = 1 << TOTAL_BITS
            freq = [max(1 if run <= 63 else 0, round(weight / total * scale))
                    for run, weight in enumerate(weights, 1)]
            freq[freq.index(max(freq))] += scale - sum(freq)
            while freq and freq[-1] == 0:
                freq.pop()
            entry[key] = freq
        out['profiles'][str(number)] = entry
    return out


@lru_cache(maxsize=1)
def _loaded():
    data = TABLES_PATH.read_bytes()
    if TABLES_SHA256 != 'pending' and hashlib.sha256(data).hexdigest() != TABLES_SHA256:
        raise CapacityError('The capacity tables are not the published ones.')
    return json.loads(data)


@lru_cache(maxsize=8)
def tables(profile):
    """(white cumulative, black cumulative): cum[r] is the frequency of runs 1..r; cum[0] is 0."""
    found = _loaded()['profiles'].get(str(profile))
    if found is None:
        raise CapacityError('Unknown capacity profile.')
    out = []
    for key in ('white', 'black'):
        cum = [0]
        for frequency in found[key]:
            cum.append(cum[-1] + frequency)
        if cum[-1] != 1 << TOTAL_BITS or any(cum[run] == cum[run - 1] for run in range(1, 64)):
            raise CapacityError('The capacity tables are damaged.')
        out.append(tuple(cum))
    return tuple(out)


# --- the keystream -----------------------------------------------------------------------------------------------

def offset_mask(tag):
    """32 bits keyed by the document tag alone, XORed with every line's offset: small offsets start with many zero
    bits, which would otherwise draw a line's first runs as single dots that a cropped left edge loses."""
    state = (int.from_bytes(tag, 'big') ^ 0xA5A5A5A5) or 0x6D2B79F5
    state ^= (state << 13) & 0xFFFFFFFF
    state ^= state >> 17
    state ^= (state << 5) & 0xFFFFFFFF
    return state


def _seed(tag, offset):
    value = (int.from_bytes(tag, 'big') ^ ((offset * 0x9E3779B1) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return value or 0x6D2B79F5


class _Keystream:
    """xorshift32 bits, most significant first, keyed by the document tag and the line's offset."""

    def __init__(self, tag, offset):
        self.state = _seed(tag, offset)
        self.buffer = ''

    def take(self, count):
        while len(self.buffer) < count:
            x = self.state
            x ^= (x << 13) & 0xFFFFFFFF
            x ^= x >> 17
            x ^= (x << 5) & 0xFFFFFFFF
            self.state = x
            self.buffer += format(x, '032b')
        part, self.buffer = self.buffer[:count], self.buffer[count:]
        return part


def _xor(bits, key):
    if not bits:
        return ''
    return format(int(bits, 2) ^ int(key, 2), f'0{len(bits)}b')


class _Input:
    """The line's input bits: the offset as it is, then the scrambled stream (bits past its end read as zeros)."""

    def __init__(self, tag, offset, source, start):
        self.head = format(offset ^ offset_mask(tag), f'0{OFFSET_BITS}b')
        self.source, self.start = source, start
        self.key = _Keystream(tag, offset)
        self.read = 0  # bits read so far, offset included
        self.text = ''

    def take(self, count):
        if count <= 0:
            return 0
        part = ''
        while count:
            if self.read < OFFSET_BITS:
                piece = self.head[self.read:self.read + count]
            else:
                at = self.start + self.read - OFFSET_BITS
                plain = self.source[at:at + count].ljust(count, '0')
                piece = _xor(plain, self.key.take(len(plain)))
            part += piece
            self.read += len(piece)
            count -= len(piece)
        self.text += part
        return int(part, 2)


# --- one line ----------------------------------------------------------------------------------------------------

def _data_runs(bits, width, cum):
    """The sender's arithmetic decoder: runs from ``bits`` (an ``_Input``) until RESERVE pixels are left."""
    low, high, code = 0, FULL, bits.take(PRECISION)
    runs = []
    room, colour = width, t4.WHITE
    while room > RESERVE:
        table = cum[colour]
        limit = min(len(table) - 1, room - RESERVE)
        total = table[limit]
        span = high - low + 1
        target = ((code - low + 1) * total - 1) // span
        run = bisect_right(table, target, 1, limit + 1)
        high = low + span * table[run] // total - 1
        low = low + span * table[run - 1] // total
        while True:
            # The bits low and high now agree on leave together.
            same = PRECISION - (low ^ high).bit_length()
            if same:
                low = (low << same) & FULL
                high = ((high << same) & FULL) | ((1 << same) - 1)
                code = ((code << same) & FULL) | bits.take(same)
                continue
            if low >= QUARTER and high < HALF + QUARTER:
                low = (low - QUARTER) << 1
                high = ((high - QUARTER) << 1) | 1
                code = ((code - QUARTER) << 1) | bits.take(1)
                continue
            break
        runs.append(run)
        room -= run
        colour ^= 1
    return runs


def _settled(data_runs, width, cum):
    """The receiver's arithmetic encoder: the bits the data part's runs settle, or None for an invalid data part."""
    low, high, pending = 0, FULL, 0
    out = []
    room, colour = width, t4.WHITE
    for run in data_runs:
        table = cum[colour]
        limit = min(len(table) - 1, room - RESERVE)
        if not 1 <= run <= limit or table[run] == table[run - 1]:
            return None
        total = table[limit]
        span = high - low + 1
        high = low + span * table[run] // total - 1
        low = low + span * table[run - 1] // total
        while True:
            if high < HALF:
                out.append('0' + '1' * pending)
                pending = 0
            elif low >= HALF:
                out.append('1' + '0' * pending)
                pending = 0
                low -= HALF
                high -= HALF
            elif low >= QUARTER and high < HALF + QUARTER:
                pending += 1
                low -= QUARTER
                high -= QUARTER
            else:
                break
            low <<= 1
            high = (high << 1) | 1
        room -= run
        colour ^= 1
    if room != RESERVE:
        return None
    return ''.join(out)


def encode_line(tag, offset, source, start, width, profile):
    """Paint one line from ``source`` (a '0'/'1' string) beginning at bit ``start``; returns (changing elements,
    payload bits carried). Raises CapacityError when the line would carry too little (the chooser then keeps
    another layout) or would not read back exactly (a defect: never drawn)."""
    cum = tables(profile)
    if width - RESERVE < 64:
        raise CapacityError('The line is too narrow for the capacity layout.')
    reader = _Input(tag, offset, source, start)
    data_runs = _data_runs(reader, width, cum)
    settled = _settled(data_runs, width, cum)
    if settled is None or not reader.text.startswith(settled) or len(settled) < OFFSET_BITS + MIN_LINE_PAYLOAD:
        raise CapacityError('A capacity line did not read back exactly.')
    carried = len(settled) - OFFSET_BITS
    payload = source[start:start + carried].ljust(carried, '0')
    crc_bits = format(runcode._crc(tag, offset, payload), '016b')
    line = list(data_runs)
    room = width - sum(line)
    colour = len(line) % 2
    position = 0
    while position < 16:
        run, position = runcode._walk(crc_bits, position, colour, CRC_LIMIT, 16)
        line.append(run)
        room -= run
        colour ^= 1
    if room < 1:
        raise CapacityError('no room left to end the line')
    line.append(room)
    return t4.changes_from_runs(line), carried


def decode_line(changes, width, tag, profile):
    """(offset, payload bits, crc) of one capacity line, or None when it is not a valid line. Raises CapacityError
    for an unknown profile."""
    cum = tables(profile)
    line = t4.runs_from_changes(changes, width)
    if len(line) < 3 or line[0] < 1:
        return None
    room, index = width, 0
    while room > RESERVE:
        if index >= len(line):
            return None
        room -= line[index]
        index += 1
    if room != RESERVE:
        return None
    settled = _settled(line[:index], width, cum)
    if settled is None or len(settled) < OFFSET_BITS:
        return None
    colour = index % 2
    crc_paths = (runcode._paths(t4.WHITE, CRC_LIMIT), runcode._paths(t4.BLACK, CRC_LIMIT))
    crc, count = [], 0
    while count < 16:
        if index >= len(line):
            return None
        path = crc_paths[colour].get(line[index])
        if path is None:
            return None
        crc.append(path)
        count += len(path)
        room -= line[index]
        index += 1
        colour ^= 1
    if index != len(line) - 1 or line[index] != room or room < 1:
        return None
    crc_text = ''.join(crc)
    if crc_text[16:].strip('0'):
        return None
    offset = int(settled[:OFFSET_BITS], 2) ^ offset_mask(tag)
    payload = _xor(settled[OFFSET_BITS:], _Keystream(tag, offset).take(len(settled) - OFFSET_BITS))
    value = int(crc_text[:16], 2)
    if value != runcode._crc(tag, offset, payload):
        return None
    return offset, payload, value


def profile_for(name):
    """The profile byte for 'time', 'balanced' or 'pages'."""
    if name not in PROFILE_NAMES:
        raise CapacityError('Choose the time, balanced or pages capacity profile.')
    return PROFILE_NAMES[name]


def main():  # pragma: no cover - maintenance: python -m app.codec.capacity > capacity_tables.json
    print(json.dumps(build_tables(), separators=(',', ':')))


if __name__ == '__main__':  # pragma: no cover
    main()
