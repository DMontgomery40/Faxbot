"""ITU-T T.4 (MH, MR) and T.6 (MMR) bilevel fax coding, with a receiver that survives damage.

Used by the payload codec to measure what a page costs on the line, to build
the run-coded layout, and by the channel simulator, which flips bits in the
coded stream and decodes it the way fax receivers do: a damaged MH/MR line is
detected (an invalid code or the wrong number of pixels), the decoder
resynchronises at the next EOL, and the damaged line is concealed by copying
the previous line, painting it white, or dropping it. MR damage carries into
the following 2-D lines until the next 1-D line, as it does on a real
receiver. MMR (T.6) is only ever used with ECM, which retransmits damaged
frames, so its decoder stops at the first error.

Code tables: ITU-T T.4 (07/2003) Tables 1/T.4 (terminating codes), 2/T.4
(make-up codes), 3/T.4 (extended make-up codes) and 4/T.4 (2-D mode codes).
Lines are lists of changing elements: the pixel positions where the colour
differs from the pixel before (the first pixel is compared with white).
"""
import re

WHITE, BLACK = 0, 1

WHITE_TERMINATING = (
    '00110101', '000111', '0111', '1000', '1011', '1100', '1110', '1111', '10011', '10100', '00111', '01000',
    '001000', '000011', '110100', '110101', '101010', '101011', '0100111', '0001100', '0001000', '0010111',
    '0000011', '0000100', '0101000', '0101011', '0010011', '0100100', '0011000', '00000010', '00000011',
    '00011010', '00011011', '00010010', '00010011', '00010100', '00010101', '00010110', '00010111', '00101000',
    '00101001', '00101010', '00101011', '00101100', '00101101', '00000100', '00000101', '00001010', '00001011',
    '01010010', '01010011', '01010100', '01010101', '00100100', '00100101', '01011000', '01011001', '01011010',
    '01011011', '01001010', '01001011', '00110010', '00110011', '00110100')
WHITE_MAKEUP = (
    '11011', '10010', '010111', '0110111', '00110110', '00110111', '01100100', '01100101', '01101000',
    '01100111', '011001100', '011001101', '011010010', '011010011', '011010100', '011010101', '011010110',
    '011010111', '011011000', '011011001', '011011010', '011011011', '010011000', '010011001', '010011010',
    '011000', '010011011')
BLACK_TERMINATING = (
    '0000110111', '010', '11', '10', '011', '0011', '0010', '00011', '000101', '000100', '0000100', '0000101',
    '0000111', '00000100', '00000111', '000011000', '0000010111', '0000011000', '0000001000', '00001100111',
    '00001101000', '00001101100', '00000110111', '00000101000', '00000010111', '00000011000', '000011001010',
    '000011001011', '000011001100', '000011001101', '000001101000', '000001101001', '000001101010',
    '000001101011', '000011010010', '000011010011', '000011010100', '000011010101', '000011010110',
    '000011010111', '000001101100', '000001101101', '000011011010', '000011011011', '000001010100',
    '000001010101', '000001010110', '000001010111', '000001100100', '000001100101', '000001010010',
    '000001010011', '000000100100', '000000110111', '000000111000', '000000100111', '000000101000',
    '000001011000', '000001011001', '000000101011', '000000101100', '000001011010', '000001100110',
    '000001100111')
BLACK_MAKEUP = (
    '0000001111', '000011001000', '000011001001', '000001011011', '000000110011', '000000110100',
    '000000110101', '0000001101100', '0000001101101', '0000001001010', '0000001001011', '0000001001100',
    '0000001001101', '0000001110010', '0000001110011', '0000001110100', '0000001110101', '0000001110110',
    '0000001110111', '0000001010010', '0000001010011', '0000001010100', '0000001010101', '0000001011010',
    '0000001011011', '0000001100100', '0000001100101')
EXTENDED_MAKEUP = (
    '00000001000', '00000001100', '00000001101', '000000010010', '000000010011', '000000010100',
    '000000010101', '000000010110', '000000010111', '000000011100', '000000011101', '000000011110',
    '000000011111')
EOL = '000000000001'
PASS, HORIZONTAL = '0001', '001'
VERTICAL = {0: '1', 1: '011', 2: '000011', 3: '0000011', -1: '010', -2: '000010', -3: '0000010'}


def _table(terminating, makeup):
    codes = {code: run for run, code in enumerate(terminating)}
    codes.update({code: 64 * (index + 1) for index, code in enumerate(makeup)})
    codes.update({code: 1792 + 64 * index for index, code in enumerate(EXTENDED_MAKEUP)})
    return codes


DECODE = (_table(WHITE_TERMINATING, WHITE_MAKEUP), _table(BLACK_TERMINATING, BLACK_MAKEUP))
CODE_LENGTHS = tuple(sorted({len(code) for code in table}) for table in DECODE)
MODE_CODES = {PASS: 'P', HORIZONTAL: 'H', **{code: offset for offset, code in VERTICAL.items()}}
MODE_LENGTHS = sorted({len(code) for code in MODE_CODES})


class CodingError(ValueError):
    """The coded line is damaged."""


def run_code(run, colour):
    """The MH code for one run of ``colour`` (make-up codes first, then the terminating code)."""
    terminating = WHITE_TERMINATING if colour == WHITE else BLACK_TERMINATING
    makeup = WHITE_MAKEUP if colour == WHITE else BLACK_MAKEUP
    parts = []
    while run >= 2624:
        parts.append(EXTENDED_MAKEUP[-1])
        run -= 2560
    if run >= 1792:
        parts.append(EXTENDED_MAKEUP[(run - 1792) // 64])
        run %= 64
    elif run >= 64:
        parts.append(makeup[run // 64 - 1])
        run %= 64
    parts.append(terminating[run])
    return ''.join(parts)


# --- lines as changing elements --------------------------------------------------------------------------------

_RUNS = re.compile(r'0+|1+')
_BLACK_BITS = bytes.maketrans(bytes(range(256)), b'1' * 128 + b'0' * 128)


def changes_from_pixels(pixels):
    """Changing elements of one line given as bytes, one per pixel, 0 black and 255 white."""
    text = pixels.translate(_BLACK_BITS).decode('ascii')
    changes = []
    position = 0
    for match in _RUNS.finditer(text):
        if match.start() == 0 and text[0] == '0':
            position = match.end()
            continue
        changes.append(match.start())
        position = match.end()
    return changes


def changes_from_packed(row, width):
    """Changing elements of one packed line (PIL mode "1" raw: bit 1 is white)."""
    text = format(int.from_bytes(row, 'big'), f'0{len(row) * 8}b')[:width]
    changes = []
    for match in _RUNS.finditer(text):
        if match.start() == 0 and text[0] == '1':
            continue
        changes.append(match.start())
    return changes


def runs_from_changes(changes, width):
    """Alternating run lengths, starting with white (the first may be 0)."""
    runs = []
    previous = 0
    for change in changes:
        runs.append(change - previous)
        previous = change
    runs.append(width - previous)
    return runs


def changes_from_runs(runs):
    changes = []
    position = 0
    for run in runs[:-1]:
        position += run
        changes.append(position)
    return changes


def packed_from_changes(changes, width):
    """PIL mode "1" raw bytes for one line (bit 1 is white)."""
    parts = []
    previous = 0
    colour = '1'
    for change in changes:
        parts.append(colour * (change - previous))
        previous = change
        colour = '0' if colour == '1' else '1'
    parts.append(colour * (width - previous))
    text = ''.join(parts)
    padded = text + '1' * (-len(text) % 8)
    return int(padded, 2).to_bytes(len(padded) // 8, 'big')


# --- encoding --------------------------------------------------------------------------------------------------

def encode_1d(changes, width):
    parts = []
    colour = WHITE
    for run in runs_from_changes(changes, width):
        parts.append(run_code(run, colour))
        colour ^= 1
    return ''.join(parts)


def _next_change(changes, after, start=0):
    for index in range(start, len(changes)):
        if changes[index] > after:
            return index
    return len(changes)


def encode_2d(changes, reference, width):
    parts = []
    a0, colour = -1, WHITE
    ref = list(reference) + [width, width]
    cur = list(changes) + [width, width]
    while True:
        i = _next_change(cur, a0)
        a1 = cur[i]
        a2 = cur[i + 1] if i + 1 < len(cur) else width
        # b1: first reference change right of a0 whose new colour is opposite to a0's colour.
        j = _next_change(ref, a0)
        while j < len(reference) and (j % 2 == 0) != (colour == WHITE):
            j += 1
        b1 = ref[j] if j < len(ref) else width
        b2 = ref[j + 1] if j + 1 < len(ref) else width
        if b2 < a1:
            parts.append(PASS)
            a0 = b2
        elif abs(a1 - b1) <= 3:
            parts.append(VERTICAL[a1 - b1])
            a0 = a1
            colour ^= 1
        else:
            start = 0 if a0 < 0 else a0
            parts.append(HORIZONTAL + run_code(a1 - start, colour) + run_code(a2 - a1, colour ^ 1))
            a0 = a2
        if a0 >= width:
            return ''.join(parts)


def encode_page(lines, width, scheme, k=4):
    """The coded bits of a page, as a '0'/'1' string.

    ``lines`` are changing-element lists. ``scheme`` is 'MH' (T.4 1-D, an EOL
    before every line, RTC after), 'MR' (T.4 2-D, EOL plus a tag bit, a 1-D
    line every ``k`` lines) or 'MMR' (T.6, no EOLs, EOFB after).
    """
    parts = []
    white = []
    reference = white
    for index, changes in enumerate(lines):
        if scheme == 'MH':
            parts.append(EOL + encode_1d(changes, width))
        elif scheme == 'MR':
            if index % k == 0:
                parts.append(EOL + '1' + encode_1d(changes, width))
            else:
                parts.append(EOL + '0' + encode_2d(changes, reference, width))
        elif scheme == 'MMR':
            parts.append(encode_2d(changes, reference, width))
        else:
            raise ValueError('unknown coding scheme')
        reference = changes
    if scheme == 'MH':
        parts.append(EOL * 6)
    elif scheme == 'MR':
        parts.append((EOL + '1') * 6)
    else:
        parts.append(EOL * 2)
    return ''.join(parts)


def coded_bits(lines, width, scheme, k=4):
    return len(encode_page(lines, width, scheme, k))


# --- decoding --------------------------------------------------------------------------------------------------

def _read_code(bits, position, table, lengths):
    for length in lengths:
        value = table.get(bits[position:position + length])
        if value is not None:
            return value, position + length
    raise CodingError('invalid code')


def _read_run(bits, position, colour):
    total = 0
    while True:
        value, position = _read_code(bits, position, DECODE[colour], CODE_LENGTHS[colour])
        total += value
        if value < 64:
            return total, position


def decode_1d(bits, position, width):
    changes = []
    x, colour = 0, WHITE
    while x < width:
        run, position = _read_run(bits, position, colour)
        x += run
        if x > width:
            raise CodingError('line too long')
        if x < width:
            changes.append(x)
        colour ^= 1
    # A run that ends exactly at the width still toggles; drop a trailing change at the width.
    return changes, position


def decode_2d(bits, position, reference, width):
    changes = []
    a0, colour = -1, WHITE
    ref = list(reference) + [width, width]
    while a0 < width:
        mode = None
        for length in MODE_LENGTHS:
            mode = MODE_CODES.get(bits[position:position + length])
            if mode is not None:
                position += length
                break
        if mode is None:
            raise CodingError('invalid mode code')
        j = _next_change(ref, a0)
        while j < len(reference) and (j % 2 == 0) != (colour == WHITE):
            j += 1
        b1 = ref[j] if j < len(ref) else width
        b2 = ref[j + 1] if j + 1 < len(ref) else width
        if mode == 'P':
            if b2 > width:
                raise CodingError('pass beyond the line')
            a0 = b2
        elif mode == 'H':
            first, position = _read_run(bits, position, colour)
            second, position = _read_run(bits, position, colour ^ 1)
            start = 0 if a0 < 0 else a0
            a1, a2 = start + first, start + first + second
            if a2 > width or (changes and a1 < changes[-1]):
                raise CodingError('horizontal run beyond the line')
            changes.extend((a1, a2))
            a0 = a2
        else:
            a1 = b1 + mode
            if a1 < 0 or a1 > width or (changes and a1 < changes[-1]) or a1 <= a0 and a0 >= 0:
                raise CodingError('vertical position outside the line')
            changes.append(a1)
            a0 = a1
            colour ^= 1
    if a0 > width:
        raise CodingError('line too long')
    # Changes at the width are not pixels; remove them (and a zero-length pair).
    cleaned = []
    for change in changes:
        if change >= width:
            break
        if cleaned and cleaned[-1] == change:
            cleaned.pop()
        else:
            cleaned.append(change)
    return cleaned, position


def _next_eol(bits, position):
    found = bits.find('00000000000' + '1', position)
    return None if found < 0 else found + 12


def decode_page(bits, width, scheme, *, conceal='previous', max_lines=20000):
    """Decode a coded page the way a fax receiver does, returning (lines, damaged line count).

    A damaged MH or MR line is concealed: 'previous' copies the last line,
    'white' paints it white and 'drop' leaves it out. MMR stops at damage.
    """
    lines = []
    damaged = 0
    white = []
    if scheme == 'MMR':
        position = 0
        reference = white
        while len(lines) < max_lines and position < len(bits):
            if bits.startswith(EOL + EOL, position):
                break
            try:
                changes, position = decode_2d(bits, position, reference, width)
            except CodingError:
                damaged += 1
                break
            lines.append(changes)
            reference = changes
        return lines, damaged
    position = _next_eol(bits, 0)
    reference = white
    while position is not None and position < len(bits) and len(lines) < max_lines:
        if bits.startswith(EOL, position) or (scheme == 'MR' and bits.startswith(EOL, position + 1)):
            break  # RTC: a second EOL straight after an EOL (after its tag bit in MR)
        try:
            if scheme == 'MH':
                changes, end = decode_1d(bits, position, width)
            else:
                tag = bits[position:position + 1]
                if tag == '1':
                    changes, end = decode_1d(bits, position + 1, width)
                elif tag == '0':
                    changes, end = decode_2d(bits, position + 1, reference, width)
                else:
                    raise CodingError('missing tag bit')
            # Fill bits are zeros; the next thing must be an EOL (eleven or more zeros, then a one).
            following = bits.find('1', end)
            if following < 0 or following - end < 11:
                raise CodingError('line does not end at an EOL')
            lines.append(changes)
            reference = changes
            position = following + 1
        except CodingError:
            damaged += 1
            if conceal == 'previous':
                concealed = lines[-1] if lines else []
                lines.append(concealed)
                reference = concealed
            elif conceal == 'white':
                lines.append([])
                reference = []
            position = _next_eol(bits, position)
    return lines, damaged
