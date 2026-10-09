"""A simulated fax channel for the payload codec: what can happen to a page between two fax machines.

- Lossless re-coding: the page is coded with MH, MR or MMR (``t4``) or by
  libtiff (Group 3 and Group 4, through Pillow), and decoded again. JBIG
  (T.82/T.85) is lossless too, so it is the identity on pixels; only its size
  differs, and the measurement script reports it when jbigkit is installed.
- Bit errors without ECM: bits of the coded MH or MR stream flip at random.
  The receiver (``t4.decode_page``) detects each damaged line, resynchronises
  at the next EOL and conceals the line by copying the previous one, painting
  it white or dropping it. MR damage carries into the following 2-D lines.
- Dropped and repeated scan lines, as receivers do when they skip or pad.
- Resolution conversion: halving the lines by keeping every other line or by
  merging pairs (black wins), and nearest-neighbour rescaling across and down,
  as a provider that renders a PDF at another resolution would.
"""
import io
import random

from PIL import Image

from . import t4


def lines_of(image):
    """Changing elements of every scan line of a mode "1" image."""
    image = image if image.mode == '1' else image.convert('1')
    width, height = image.size
    stride = (width + 7) // 8
    data = image.tobytes()
    return [t4.changes_from_packed(data[y * stride:(y + 1) * stride], width) for y in range(height)]


def image_of(lines, width, dpi=None):
    data = b''.join(t4.packed_from_changes(changes, width) for changes in lines)
    image = Image.frombytes('1', (width, max(1, len(lines))), data if lines else b'\xff' * ((width + 7) // 8))
    if dpi:
        image.info['dpi'] = dpi
    return image


def coded_size(image, scheme, k=4):
    """Coded bits of a page under MH, MR (``k`` lines per 1-D line) or MMR."""
    return t4.coded_bits(lines_of(image), image.size[0], scheme, k)


def flip_bits(bits, rate, rng):
    """Flip each bit with probability ``rate`` (geometric gaps, so long pages stay fast)."""
    if rate <= 0:
        return bits, 0
    data = bytearray(bits, 'ascii')
    position = -1
    flipped = 0
    while True:
        position += 1 + int(rng.expovariate(rate))
        if position >= len(data):
            break
        data[position] ^= 1  # '0' (48) <-> '1' (49)
        flipped += 1
    return data.decode('ascii'), flipped


def transmit(image, *, scheme='MH', k=4, bit_error_rate=0.0, conceal='previous', seed=0):
    """Send a page through a coded fax channel; returns (received image, report)."""
    rng = random.Random(seed)
    width = image.size[0]
    lines = lines_of(image)
    bits = t4.encode_page(lines, width, scheme, k)
    if scheme == 'MMR':
        bit_error_rate = 0.0  # T.6 is only used with ECM, which retransmits damaged frames
    damaged_bits, flipped = flip_bits(bits, bit_error_rate, rng)
    received, damaged = t4.decode_page(damaged_bits, width, scheme, conceal=conceal)
    report = {'coded_bits': len(bits), 'flipped_bits': flipped, 'damaged_lines': damaged,
              'lines_sent': len(lines), 'lines_received': len(received)}
    return image_of(received, width, image.info.get('dpi')), report


def drop_lines(image, rate, seed=0):
    rng = random.Random(seed)
    lines = [line for line in lines_of(image) if rng.random() >= rate]
    return image_of(lines, image.size[0], image.info.get('dpi'))


def repeat_lines(image, rate, seed=0):
    rng = random.Random(seed)
    out = []
    for line in lines_of(image):
        out.append(line)
        if rng.random() < rate:
            out.append(line)
    return image_of(out, image.size[0], image.info.get('dpi'))


def halve_lines(image, *, method='skip', phase=0):
    """Fine to standard (or superfine to fine): keep every other line, or merge pairs with black winning."""
    gray = image.convert('L')
    width, height = gray.size
    data = gray.tobytes()
    rows = []
    for y in range(phase, height - 1, 2):
        first = data[y * width:(y + 1) * width]
        if method == 'skip':
            rows.append(first)
        else:
            second = data[(y + 1) * width:(y + 2) * width]
            rows.append(bytes(min(a, b) for a, b in zip(first, second)))
    result = Image.frombytes('L', (width, len(rows)), b''.join(rows)).convert('1', dither=Image.Dither.NONE)
    dpi = image.info.get('dpi')
    if dpi:
        result.info['dpi'] = (dpi[0], dpi[1] / 2)
    return result


def rescale(image, x_scale, y_scale):
    """Nearest-neighbour resampling, as a renderer at another resolution would produce."""
    width, height = image.size
    size = (max(1, round(width * x_scale)), max(1, round(height * y_scale)))
    return image.convert('1').resize(size, Image.Resampling.NEAREST)


def libtiff_roundtrip(image, compression):
    """Write the page with libtiff (Pillow) as 'group3' or 'group4' and read it back."""
    buffer = io.BytesIO()
    image.convert('1').save(buffer, 'TIFF', compression=compression, dpi=image.info.get('dpi', (204, 196)))
    buffer.seek(0)
    with Image.open(buffer) as reread:
        reread.load()
        return reread.convert('1').copy()
