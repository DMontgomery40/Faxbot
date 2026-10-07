"""The run-coded layout: payload bits chosen as MH codes, so the fax line carries about the payload itself.

A dense grid of cells compresses badly under the fax codings (random cells
cost about one coded bit per pixel). This layout goes the other way: it reads
the payload bits as a walk down the MH code tree (T.4 Tables 1/T.4) and paints
the runs those codes describe. When the fax engine codes the page with MH, it
sends back almost exactly the payload bits; MR, MMR and JBIG code the same
pixels losslessly, so the receiver gets the same raster and recovers the bits
by MH-coding each line again.

Only terminating codes for runs of 1..``limit`` pixels are used while data is
written, so a line has no zero-length runs and every code is canonical. Where
the pruned code tree has a node with one allowed branch, the branch is forced
and carries no payload bit; elsewhere each branch carries one bit. Each scan
line holds::

    32-bit stream bit offset | payload bits | 16-bit CRC | one padding run

The data part ends when the line has ``reserve`` pixels or fewer left; the
CRC is then written with runs of at most ``CRC_LIMIT`` pixels, and a last run
fills the line. The CRC covers the document tag, the offset, the payload bit
count and the payload bits. Lines are independent: a dropped, repeated or
damaged line costs only its own bits, which the stream's error correction
treats as erasures.

This layout needs the exact raster: it does not survive resolution conversion
or a printed and scanned page. Use the grid layout where those can happen.
"""
from binascii import crc_hqx
from functools import lru_cache
import struct

from . import t4

CRC_LIMIT = 7
OFFSET_BITS = 32


class RunsError(ValueError):
    pass


class _Node:
    __slots__ = ('children', 'run')

    def __init__(self):
        self.children = [None, None]
        self.run = None


@lru_cache(maxsize=16)
def _tree(colour, limit):
    terminating = t4.WHITE_TERMINATING if colour == t4.WHITE else t4.BLACK_TERMINATING
    root = _Node()
    for run in range(1, limit + 1):
        node = root
        for bit in terminating[run]:
            index = int(bit)
            if node.children[index] is None:
                node.children[index] = _Node()
            node = node.children[index]
        node.run = run
    return root


@lru_cache(maxsize=16)
def _paths(colour, limit):
    """run -> (code, the payload bits its path carries)."""
    terminating = t4.WHITE_TERMINATING if colour == t4.WHITE else t4.BLACK_TERMINATING
    root = _tree(colour, limit)
    paths = {}
    for run in range(1, limit + 1):
        node, carried = root, []
        for bit in terminating[run]:
            if node.children[0] is not None and node.children[1] is not None:
                carried.append(bit)
            node = node.children[int(bit)]
        paths[run] = ''.join(carried)
    return paths


def reserve_for(limit):
    return 16 * CRC_LIMIT + limit + 1


def _crc(tag, offset, bits):
    padded = bits + '0' * (-len(bits) % 8)
    packed = int(padded, 2).to_bytes(len(padded) // 8, 'big') if padded else b''
    return crc_hqx(tag + struct.pack('>IH', offset, len(bits)) + packed, 0xFFFF)


def _walk(bits, position, colour, limit, length):
    """Read one run from ``bits`` starting at ``position``; missing bits read as 0."""
    node = _tree(colour, limit)
    while node.run is None:
        if node.children[0] is not None and node.children[1] is not None:
            bit = bits[position] if position < length else '0'
            position += 1
            node = node.children[int(bit)]
        else:
            node = node.children[0] if node.children[0] is not None else node.children[1]
    return node.run, position


def encode_line(tag, offset, source, start, width, limit):
    """Paint one line from ``source`` (a '0'/'1' string) beginning at bit ``start``.

    Returns (changing elements, payload bits consumed). Bits past the end of
    ``source`` read as zeros.
    """
    reserve = reserve_for(limit)
    header = format(offset, f'0{OFFSET_BITS}b')
    # A run of one pixel carries at most a few bits; past the end of the source, bits read as zeros.
    bits = (header + source[start:start + 8 * width]).ljust(OFFSET_BITS + 8 * width, '0')
    length = len(bits)
    runs = []
    room, colour, position = width, t4.WHITE, 0
    while room > reserve:
        run, position = _walk(bits, position, colour, limit, length)
        runs.append(run)
        room -= run
        colour ^= 1
    if position < OFFSET_BITS:
        raise RunsError('the line is too short for its offset')
    payload = bits[OFFSET_BITS:position]
    crc_bits = format(_crc(tag, offset, payload), '016b')
    crc_position = 0
    while crc_position < 16:
        run, crc_position = _walk(crc_bits, crc_position, colour, CRC_LIMIT, 16)
        runs.append(run)
        room -= run
        colour ^= 1
    if room < 1:
        raise RunsError('no room left to end the line')
    runs.append(room)  # the padding run, in whichever colour comes next
    return t4.changes_from_runs(runs), len(payload)


def decode_line(changes, width, limit, tag=None):
    """(offset, payload bits) of one line, or None when it is not a valid run-coded line.

    With ``tag`` None the CRC is not checked (the caller tries each tag).
    """
    runs = t4.runs_from_changes(changes, width)
    if len(runs) < 3 or runs[0] < 1:
        return None
    reserve = reserve_for(limit)
    carried = []
    room, colour, index = width, t4.WHITE, 0
    paths = (_paths(t4.WHITE, limit), _paths(t4.BLACK, limit))
    while room > reserve:
        if index >= len(runs):
            return None
        path = paths[colour].get(runs[index])
        if path is None:
            return None
        carried.append(path)
        room -= runs[index]
        index += 1
        colour ^= 1
    bits = ''.join(carried)
    if len(bits) < OFFSET_BITS:
        return None
    crc_paths = (_paths(t4.WHITE, CRC_LIMIT), _paths(t4.BLACK, CRC_LIMIT))
    crc_bits = []
    count = 0
    while count < 16:
        if index >= len(runs):
            return None
        path = crc_paths[colour].get(runs[index])
        if path is None:
            return None
        crc_bits.append(path)
        count += len(path)
        room -= runs[index]
        index += 1
        colour ^= 1
    if index != len(runs) - 1 or runs[index] != room or room < 1:
        return None
    crc_text = ''.join(crc_bits)
    if crc_text[16:].strip('0'):
        return None
    offset = int(bits[:OFFSET_BITS], 2)
    payload = bits[OFFSET_BITS:]
    if tag is not None and int(crc_text[:16], 2) != _crc(tag, offset, payload):
        return None
    return offset, payload, int(crc_text[:16], 2)


def check(tag, offset, payload, crc):
    return _crc(tag, offset, payload) == crc
