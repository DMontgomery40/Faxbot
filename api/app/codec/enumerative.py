"""Exact, bounded MH distribution matching for experimental payload pages.

Profile 1 ranks alternating positive runs of 1..63 pixels, starting white
and ending black, in lexicographic run order. Each 576-pixel chunk uses at
most 332 MH bits and carries 307 bits. A row joins whole chunks, followed
by white pixels when the fax width is not divisible by 576. Its bits are
the 32-bit stream offset, payload, and a 16-bit CRC bound to the document.

The page header selects this immutable profile; received input cannot
choose table dimensions or coding budgets. Pixels must be preserved
exactly. Reed-Solomon and the original document digest live in the existing
stream and container layers.
"""
from binascii import crc_hqx
from functools import lru_cache
import struct

from . import t4

PROFILE = 1
CHUNK_WIDTH = 576
CHUNK_BUDGET = 332
CHUNK_BITS = 307
RUN_LIMIT = 63
WIDTHS = (1728, 2592, 3456)
_COSTS = tuple(tuple(len(code) for code in codes) for codes in
               (t4.WHITE_TERMINATING, t4.BLACK_TERMINATING))


class Counts:
    """Counts and inverse ranks, bounded by the one supported wire profile."""

    def __init__(self, width, budget):
        if (type(width) is not int or type(budget) is not int
                or not 2 <= width <= CHUNK_WIDTH or not 1 <= budget <= CHUNK_BUDGET):
            raise ValueError('Unsupported enumerative table dimensions.')
        self.width, self.budget = width, budget
        table = [[[0] * (budget + 1) for _ in range(width + 1)] for _ in range(2)]
        # An empty suffix is valid only after a black run.
        table[0][0] = [1] * (budget + 1)
        for pixels in range(1, width + 1):
            for colour in (0, 1):
                row = table[colour][pixels]
                for run in range(1, min(RUN_LIMIT, pixels) + 1):
                    cost = _COSTS[colour][run]
                    previous = table[1 - colour][pixels - run]
                    for allowance in range(cost, budget + 1):
                        row[allowance] += previous[allowance - cost]
        self._table = table

    def count(self, width, budget):
        if not 0 <= width <= self.width or not 0 <= budget <= self.budget:
            raise ValueError('Unsupported enumerative chunk dimensions.')
        return self._table[0][width][budget]

    def unrank(self, value, width, budget):
        if not 0 <= value < self.count(width, budget):
            raise ValueError('Enumerative rank is outside its profile.')
        colour, runs = 0, []
        while width:
            for run in range(1, min(RUN_LIMIT, width) + 1):
                cost = _COSTS[colour][run]
                count = self._table[1 - colour][width - run][budget - cost] if budget >= cost else 0
                if value >= count:
                    value -= count
                else:
                    runs.append(run)
                    width -= run
                    budget -= cost
                    colour ^= 1
                    break
            else:
                raise ValueError('Enumerative rank has no raster.')
        return runs

    def rank(self, runs, width, budget):
        self.count(width, budget)
        colour, value = 0, 0
        for chosen in runs:
            if not 1 <= chosen <= min(RUN_LIMIT, width):
                raise ValueError('Invalid enumerative run.')
            for run in range(1, chosen):
                cost = _COSTS[colour][run]
                if cost <= budget:
                    value += self._table[1 - colour][width - run][budget - cost]
            budget -= _COSTS[colour][chosen]
            if budget < 0:
                raise ValueError('Enumerative raster exceeds its coding budget.')
            width -= chosen
            colour ^= 1
        if width or colour:
            raise ValueError('Incomplete enumerative chunk.')
        return value


@lru_cache(maxsize=1)
def _counts():
    return Counts(CHUNK_WIDTH, CHUNK_BUDGET)


def payload_bits(width, profile=PROFILE):
    if profile != PROFILE or width not in WIDTHS:
        raise ValueError('Unsupported enumerative page profile or width.')
    return width // CHUNK_WIDTH * CHUNK_BITS - 48


def _crc(tag, offset, payload):
    padded = payload + '0' * (-len(payload) % 8)
    return crc_hqx(tag + struct.pack('>IH', offset, len(payload))
                   + int(padded, 2).to_bytes(len(padded) // 8, 'big'), 0xFFFF)


def encode_line(tag, offset, source, start, width):
    room = payload_bits(width)
    payload = source[start:start + room].ljust(room, '0')
    bits = f'{offset:032b}' + payload + f'{_crc(tag, offset, payload):016b}'
    table = _counts()
    runs = []
    for start in range(0, len(bits), CHUNK_BITS):
        runs.extend(table.unrank(int(bits[start:start + CHUNK_BITS], 2), CHUNK_WIDTH, CHUNK_BUDGET))
    if width % CHUNK_WIDTH:
        runs.append(width % CHUNK_WIDTH)
    return t4.changes_from_runs(runs), room


def decode_line(changes, width, tag, *, profile=PROFILE):
    """Return (stream offset, payload bits), or None for an invalid row."""
    try:
        payload_bits(width, profile)
        if (not changes or len(changes) > width
                or any(type(x) is not int or not 0 < x < width for x in changes)
                or any(a >= b for a, b in zip(changes, changes[1:]))):
            return None
        runs = t4.runs_from_changes(changes, width)
        table = _counts()
        index, parts = 0, []
        for _ in range(width // CHUNK_WIDTH):
            first, pixels = index, 0
            while pixels < CHUNK_WIDTH and index < len(runs):
                pixels += runs[index]
                index += 1
            value = table.rank(runs[first:index], CHUNK_WIDTH, CHUNK_BUDGET)
            if value >= 1 << CHUNK_BITS:
                return None
            parts.append(f'{value:0{CHUNK_BITS}b}')
        if runs[index:] != ([width % CHUNK_WIDTH] if width % CHUNK_WIDTH else []):
            return None
        bits = ''.join(parts)
        offset, payload = int(bits[:32], 2), bits[32:-16]
        if int(bits[-16:], 2) != _crc(tag, offset, payload):
            return None
        return offset, payload
    except ValueError:
        return None
