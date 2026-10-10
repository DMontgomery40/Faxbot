"""Peer fax (M1a): the fax image a call would carry, delivered directly to an enrolled Faxbot partner.

For a partner that said, in a statement it signed, that it accepts fax images
from this installation, the direct route sends the fax image instead of the
original document. The image is built from the fax's own engine image, the
TIFF Faxbot already made for a call (``<job>.tiff``), or the same conversion
(``conversion.pdf_to_tiff``) when the fax had no call image yet. Neither is
changed: the fax image is a separate, derived file.

What a call would add is added here, once, with the time fixed when the image
is made:

- the header line on every page that 47 CFR 68.318(d) requires (date and time
  sent, who sends it and the sending number), with the fields and position the
  built-in engine prints: a band of rows above each page, so nothing on the
  page is covered;
- the page width a fax call carries (T.30: 1728 dots for A4 and Letter at
  8 dots per millimetre), by trimming or padding the white margin evenly.

The planned fax facts (resolution, width, compression, page count and the
header line) travel in the signed manifest together with the image's SHA-256.
The receiving Faxbot files the image byte for byte as a received fax.

Each page is written in strips exactly as tall as the header band, so the
band is the page's first strip and the page body starts on a strip boundary.
Group 4 codes every strip on its own, so the body's strips are the same bytes
whenever the body's pixels are, whatever the header line says (``layout``).
That lets a partner that already holds the body rebuild the exact image from
Faxbot's own new header band alone (``reuse.py``). Only the band, which Faxbot
drew itself, is ever told apart from the body; nothing on the document's own
pages is recognized or rewritten.

Whether a fax goes to a partner at all is the route planner's decision;
``peer_route`` is the one function a routing rule calls to ask for "peer
first" or "never peer". Its predicted cost is zero: there is no telephone call.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import re
import struct
import tempfile

from ..conversion import MAX_DOCUMENT_PAGES, MAX_RASTER_PAGE_PIXELS, MAX_RASTER_TOTAL_PIXELS
from ..tiff_bytes import settle
from .crypto import FAX_COMPRESSIONS, FAX_IMAGE, FAX_LINES, FAX_WIDTHS


PEER_FIRST = 'peer_first'
NEVER_PEER = 'never_peer'
PREFERENCES = (PEER_FIRST, NEVER_PEER)
ORIGINAL = 'original'
NO_CALL = 'no_telephone_call'
NO_CALL_TEXT = 'No telephone call.'
# The header band: 32 rows at fine resolution (the built-in engine prints a 16-row font twice).
BAND_ROWS_FINE = 32
FONT_PIXELS = 22
# The conversion's own limits: an image the receiver could not turn into a PDF is never built or accepted.
MAX_PAGES = MAX_DOCUMENT_PAGES
MAX_TOTAL_PIXELS = MAX_RASTER_TOTAL_PIXELS
MAX_PAGE_PIXELS = MAX_RASTER_PAGE_PIXELS
MAX_PAGE_ROWS = 20000
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
# What a page body's digest covers (``layout``): the version names the strip layout and the fields below.
BODY_DIGEST_VERSION = b'faxbot-fax-body-v1'
_TIFF_GROUP4 = 4
_TAG_COMPRESSION, _TAG_STRIP_OFFSETS, _TAG_ROWS_PER_STRIP, _TAG_STRIP_BYTES = 259, 273, 278, 279


class FaxImageUnavailable(RuntimeError):
    """The fax image could not be made; nothing was sent, and the original can still go directly."""


class FaxImageInvalid(ValueError):
    """A received fax image does not match its signed facts or cannot be read; nothing was accepted."""


@dataclass(frozen=True)
class PeerRoute:
    """How a fax to an enrolled partner goes: ``fax_image`` or ``original``, always without a telephone call."""
    kind: str
    predicted_cost_micros: int
    reason: str
    sentence: str


@dataclass(frozen=True)
class FaxImage:
    data: bytes
    pages: int
    facts: dict


@dataclass(frozen=True)
class Layout:
    """Where Faxbot's header bands and the page bodies lie in a fax image this module made.

    ``body`` lists every body strip as (offset, length) in the file, page by page in reading order;
    ``body_sha256`` is the body's digest (the strips' exact bytes and each page's geometry).
    """
    body_sha256: str
    body: tuple
    pages: int
    band_rows: int


def _flag(value):
    return value is not None and int(value) == 1


def peer_route(peer, *, preference=PEER_FIRST, now=None):
    """The direct route for a fax to ``peer`` (a ``direct_peers`` row), or None when it must not go to the partner.

    ``preference`` is a routing rule's choice: ``peer_first`` (the default) or
    ``never_peer``. A partner goes first only while verified and unexpired; it
    gets the fax image only when it said, signed, that it accepts fax images
    from this installation, and the original document otherwise.
    """
    if preference not in PREFERENCES:
        raise ValueError('Unknown peer preference.')
    if preference == NEVER_PEER or not peer or peer.get('state') != 'verified':
        return None
    moment = now or datetime.utcnow()
    if peer.get('expires_at') is not None and peer['expires_at'] <= moment:
        return None
    kind = FAX_IMAGE if _flag(peer.get('partner_receives_fax_images')) else ORIGINAL
    return PeerRoute(kind, 0, NO_CALL, NO_CALL_TEXT)


def _printable(text, limit):
    text = ''.join(character for character in str(text or '') if character.isprintable())
    text = text.encode('latin-1', 'replace').decode('latin-1')
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def header_line(moment, *, header, station, page, zone_name=''):
    """The header line on one page, in the built-in engine's order: date, time, sender, sending number, page.

    `` 7-Oct-2026  14:05   Valley Hospital   +15550100001   p.1``. The time is
    the installation's local time, as on a fax the engine sends.
    """
    from .. import people_time
    aware = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    local = aware.astimezone(people_time.zone(zone_name))
    parts = [f'{local.day:2d}-{MONTHS[local.month - 1]}-{local.year}', f'{local:%H:%M}',
             _printable(header, 50), _printable(station, 21), f'p.{page}']
    return '   '.join(part for part in parts if part)


def sender_identity(values):
    """(the header's sender name, the sending number) for 68.318(d), from the installation's settings."""
    from ..routing.numbers import InvalidNumber, normalize_number
    header = (getattr(values, 'fax_header', '') or getattr(values, 'direct_organization', '') or 'Faxbot').strip()
    station = (getattr(values, 'fax_station_id', '') or '').strip()
    if not station:
        try:
            station = normalize_number(getattr(values, 'direct_fax_number', ''),
                                       country=getattr(values, 'fax_default_country', 'US'))
        except InvalidNumber:
            station = ''
    return header, station


def _fit_width(frame):
    """The page at the narrowest T.30 width that holds it, its white margin trimmed or padded evenly."""
    from PIL import Image
    width, height = frame.size
    target = next((size for size in FAX_WIDTHS if width <= size + 16), FAX_WIDTHS[-1])
    if width == target:
        return frame
    page = Image.new('1', (target, height), 1)
    if width > target:
        left = (width - target) // 2
        page.paste(frame.crop((left, 0, left + target, height)), (0, 0))
    else:
        page.paste(frame, ((target - width) // 2, 0))
    return page


def band_rows(y_dpi):
    """The header band's height in rows at a vertical resolution: 16 standard, 32 fine, 64 superfine."""
    return max(1, round(BAND_ROWS_FINE * y_dpi / 196))


def _band(width, text, y_dpi):
    from PIL import Image, ImageDraw, ImageFont
    fine = Image.new('1', (width, BAND_ROWS_FINE), 1)
    draw = ImageDraw.Draw(fine)
    draw.fontmode = '1'
    draw.text((16, 4), text, font=ImageFont.load_default(size=FONT_PIXELS), fill=0)
    rows = band_rows(y_dpi)
    return fine if rows == BAND_ROWS_FINE else fine.resize((width, rows), Image.NEAREST)


def stamp(tiff, *, header, station, moment, zone_name=''):
    """The fax image for an engine TIFF: each page with its header band and the call's width (bytes, facts)."""
    from PIL import Image
    pages, y_dpi = [], None
    with Image.open(io.BytesIO(tiff)) as image:
        if image.format != 'TIFF':
            raise FaxImageUnavailable('The fax has no usable fax image.')
        count = getattr(image, 'n_frames', 1)
        if not 0 < count <= MAX_PAGES:
            raise FaxImageUnavailable('The fax has no usable fax image.')
        for index in range(count):
            image.seek(index)
            frame = image.convert('1') if image.mode != '1' else image.copy()
            dpi = image.info.get('dpi') or (204, 196)
            lines = min(FAX_LINES, key=lambda value: abs(value - float(dpi[1])))
            if y_dpi is None:
                y_dpi = lines
            if lines != y_dpi or frame.size[1] > MAX_PAGE_ROWS:
                raise FaxImageUnavailable('The fax has no usable fax image.')
            frame = _fit_width(frame)
            text = header_line(moment, header=header, station=station, page=index + 1, zone_name=zone_name)
            band = _band(frame.size[0], text, y_dpi)
            page = Image.new('1', (frame.size[0], band.size[1] + frame.size[1]), 1)
            page.paste(band, (0, 0))
            page.paste(frame, (0, band.size[1]))
            pages.append(page)
    output = io.BytesIO()
    # Strips exactly as tall as the band: the band is each page's first strip and the body starts on a strip
    # boundary, so the body's coded bytes never depend on the header line (``layout``).
    pages[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=pages[1:],
                  dpi=(204, y_dpi), strip_size=(pages[0].size[0] + 7) // 8 * band_rows(y_dpi))
    facts = {'resolution': FAX_LINES[y_dpi], 'x_dpi': 204, 'y_dpi': y_dpi, 'width': pages[0].size[0],
             'compression': 'MMR',
             'header_line': header_line(moment, header=header, station=station, page=1, zone_name=zone_name)}
    # libtiff leaves a pad byte unwritten: settled, the same pages and time always give the same signed digest.
    return settle(output.getvalue()), facts


def build(values, job_id, *, moment=None):
    """The fax image for an accepted fax, from its engine image or the same conversion; FaxImageUnavailable if not.

    The engine image is the original document's: the pages an attempt changes (dense, lightened or the
    experimental encoded pages) are that attempt's own files (``pages/sending.py``), so a partner always gets
    the fax image of the original.
    """
    root = Path(values.fax_data_dir)
    if re.fullmatch('[a-f0-9]{32}', job_id or '') is None:
        raise FaxImageUnavailable('The fax has no usable fax image.')
    engine_image, pdf = root / (job_id + '.tiff'), root / (job_id + '.pdf')
    try:
        if engine_image.is_file() and not engine_image.is_symlink():
            tiff = engine_image.read_bytes()
        elif pdf.is_file() and not pdf.is_symlink():
            from ..conversion import DocumentConversionError, pdf_to_tiff
            with tempfile.TemporaryDirectory(prefix='.faximage-', dir=str(root)) as folder:
                made = Path(folder) / 'image.tiff'
                try:
                    pdf_to_tiff(str(pdf), str(made))
                except DocumentConversionError:
                    raise FaxImageUnavailable('The fax image could not be made.') from None
                tiff = made.read_bytes()
        else:
            raise FaxImageUnavailable('The fax has no usable fax image.')
        header, station = sender_identity(values)
        data, facts = stamp(tiff, header=header, station=station, moment=moment or datetime.utcnow(),
                            zone_name=getattr(values, 'time_zone', '') or '')
    except FaxImageUnavailable:
        raise
    except Exception:
        raise FaxImageUnavailable('The fax image could not be made.') from None
    return FaxImage(data, check(data, facts, None), facts)


def check(data, facts, pages):
    """Read a fax image against its signed facts; returns its page count or raises FaxImageInvalid.

    Every page must be one-bit, at the signed width and resolution, with the
    signed compression; ``pages`` (when given) must match the image.
    """
    from PIL import Image
    compression = {'MMR': 'group4', 'MH': 'group3', 'MR': 'group3'}.get(facts.get('compression'))
    if compression is None or facts.get('compression') not in FAX_COMPRESSIONS:
        raise FaxImageInvalid('The fax image does not match its description.')
    if not isinstance(data, (bytes, bytearray)) or data[:4] not in (b'II*\x00', b'MM\x00*'):
        raise FaxImageInvalid('This is not a fax image.')
    try:
        with Image.open(io.BytesIO(bytes(data))) as image:
            count = getattr(image, 'n_frames', 1)
            if image.format != 'TIFF' or not 0 < count <= MAX_PAGES or (pages is not None and count != pages):
                raise FaxImageInvalid('The fax image does not match its description.')
            total = 0
            for index in range(count):
                # Only each page's tags are read here; the one bounded decode is the conversion's (readable_copy).
                image.seek(index)
                x_dpi, y_dpi = (round(float(value)) for value in image.info.get('dpi', (0, 0)))
                width, height = image.size
                total += width * height
                if (image.mode != '1' or width != facts['width'] or x_dpi != facts['x_dpi']
                        or y_dpi != facts['y_dpi'] or image.info.get('compression') != compression
                        or not 0 < height <= MAX_PAGE_ROWS or width * height > MAX_PAGE_PIXELS
                        or total > MAX_TOTAL_PIXELS):
                    raise FaxImageInvalid('The fax image does not match its description.')
    except FaxImageInvalid:
        raise
    except Exception:
        raise FaxImageInvalid('The fax image cannot be read.') from None
    return count


def readable_copy(data, folder):
    """Prove a fax image converts to the PDF people read (``conversion.tiff_to_pdf``); returns its page count."""
    from ..conversion import DocumentConversionError, tiff_to_pdf
    with tempfile.TemporaryDirectory(prefix='.faximage-', dir=str(folder)) as temporary:
        tiff, pdf = Path(temporary) / 'image.tiff', Path(temporary) / 'image.pdf'
        tiff.write_bytes(bytes(data))
        try:
            pages, _ = tiff_to_pdf(str(tiff), str(pdf))
        except DocumentConversionError:
            raise FaxImageInvalid('The fax image cannot be turned into a PDF.') from None
    return pages


# The body and Faxbot's own header bands --------------------------------------------------------------------------
def layout(data):
    """Where the header bands and the page bodies lie in a fax image ``stamp`` made; None when it has no such layout.

    Only the strip tables are read; no page is decoded. Every page must be one-bit Group 4 at a T.30 width and
    resolution, in strips exactly as tall as its header band, with a body below the band. The body's digest
    covers each page's width, height and resolution and every body strip's exact bytes, so two images have the
    same body digest only when their bodies are the same bytes, which a partner can then rebuild around a new
    header band (``cut``, ``splice``). An image made before the body had its own strips (one strip for band and
    body together) has no layout; it always goes whole.
    """
    from PIL import Image
    if not isinstance(data, (bytes, bytearray)) or data[:4] not in (b'II*\x00', b'MM\x00*'):
        return None
    data = bytes(data)
    digest = hashlib.sha256(BODY_DIGEST_VERSION)
    body, spans, rows, width = [], [], None, None
    try:
        with Image.open(io.BytesIO(data)) as image:
            count = getattr(image, 'n_frames', 1)
            if image.format != 'TIFF' or not 0 < count <= MAX_PAGES:
                return None
            total = 0
            for index in range(count):
                image.seek(index)
                tags = image.tag_v2
                y_dpi = round(float((image.info.get('dpi') or (0, 0))[1]))
                page_width, height = image.size
                band = band_rows(y_dpi) if y_dpi in FAX_LINES else None
                offsets, counts = tuple(tags.get(_TAG_STRIP_OFFSETS) or ()), tuple(tags.get(_TAG_STRIP_BYTES) or ())
                total += page_width * height
                if (image.mode != '1' or tags.get(_TAG_COMPRESSION) != _TIFF_GROUP4 or band is None
                        or page_width not in FAX_WIDTHS or tags.get(_TAG_ROWS_PER_STRIP) != band
                        or not band < height <= band + MAX_PAGE_ROWS or total > MAX_TOTAL_PIXELS
                        or page_width * height > MAX_PAGE_PIXELS
                        or len(offsets) != len(counts) or len(offsets) != -(-height // band)
                        or (rows is not None and (band, page_width) != (rows, width))):
                    return None
                rows, width = band, page_width
                for offset, length in zip(offsets, counts):
                    if type(offset) is not int or type(length) is not int or offset < 8 or length < 1 \
                            or offset + length > len(data):
                        return None
                    spans.append((offset, length))
                digest.update(f'|page {index} {page_width}x{height} y{y_dpi} rows{band} strips{len(offsets)}'
                              .encode('ascii'))
                for offset, length in zip(offsets[1:], counts[1:]):
                    digest.update(length.to_bytes(4, 'big'))
                    digest.update(data[offset:offset + length])
                    body.append((offset, length))
    except (OSError, ValueError, SyntaxError, EOFError, KeyError, TypeError, IndexError, struct.error,
            Image.DecompressionBombError):
        # What Pillow raises for a damaged or foreign TIFF: such an image has no layout and goes whole.
        return None
    spans.sort()
    if any(earlier[0] + earlier[1] > later[0] for earlier, later in zip(spans, spans[1:])):
        return None  # strips that share bytes are not this module's layout
    return Layout(digest.hexdigest(), tuple(body), count, rows)


def cut(data, found):
    """The image without its body strips: (the remaining bytes, the holes as (position, length) in body order).

    What remains is Faxbot's own part of the image: the file's structure and every page's header band.
    """
    data = bytes(data)
    kept, cursor = bytearray(), 0
    for offset, length in sorted(found.body):
        kept += data[cursor:offset]
        cursor = offset + length
    kept += data[cursor:]
    return bytes(kept), list(found.body)


def body_strips(data, found):
    """The body strips' bytes, in body order."""
    return [bytes(data[offset:offset + length]) for offset, length in found.body]


def splice(remaining, holes, strips, *, size):
    """Put body strips back into the holes ``cut`` left; returns the whole image, or raises FaxImageInvalid.

    ``holes`` and ``strips`` are in body order; each strip must fill its hole exactly, and the result must be
    ``size`` bytes. Whether it is the intended image is for the caller to check against its signed digest.
    """
    if len(holes) != len(strips) or not holes:
        raise FaxImageInvalid('The fax image cannot be put together from these pages.')
    placed = sorted(zip(holes, strips), key=lambda item: item[0][0])
    out, used, position = bytearray(), 0, 0
    for (offset, length), strip in placed:
        if type(offset) is not int or type(length) is not int or length != len(strip) or offset < position:
            raise FaxImageInvalid('The fax image cannot be put together from these pages.')
        take = offset - position
        if used + take > len(remaining):
            raise FaxImageInvalid('The fax image cannot be put together from these pages.')
        out += remaining[used:used + take]
        out += strip
        used, position = used + take, offset + length
        if len(out) > size:
            raise FaxImageInvalid('The fax image cannot be put together from these pages.')
    out += remaining[used:]
    if len(out) != size:
        raise FaxImageInvalid('The fax image cannot be put together from these pages.')
    return bytes(out)
