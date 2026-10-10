"""Received-fax fixtures for the payload codec: what real receivers do to an encoded page before anyone decodes it.

Brief 85 M3. A public test receiver (Faxbeep, UK and Australia) publishes each received fax as a PDF that
ImageMagick wrote: one CCITT Group 4 image per page (``/K -1 /BlackIs1 false``), a page box of the image's
pixels at 204 x 196 dpi, the content stream ``q W 0 0 H 0 0 cm /Im0 Do Q``, and one completely white trailing row
fewer than was sent (research/faxbot-nondirect-encyclopedia-2026-10-09/live: every received raster equals the
sent one with its last, white row removed). ``faxbeep_pdf`` writes exactly that container. The other
transformations are the ones other receivers and services apply: page PNGs without a resolution, an extra white
row, square pixels, 180-degree rotation, a header line overlaid on the top band or added above the page, a page
moved sideways or padded to letter width, inverted polarity, CCITT G3 (1-D and 2-D), Flate (one-bit, eight-bit,
image mask) and a page split into strips.

Every function takes and returns PIL images or file bytes; nothing touches the network or a real receiver.
"""
from __future__ import annotations

import io
import math
import zlib

from PIL import Image, ImageDraw, ImageOps

_INVERT = bytes(255 - value for value in range(256))


# --- page transformations ---------------------------------------------------------------------------------------

def _keep_dpi(source, image):
    image.info['dpi'] = source.info.get('dpi', (204, 196))
    return image


def drop_trailing_row(page):
    """The page without its last row (Faxbeep's receipts; the row is white on every Faxbot page)."""
    return _keep_dpi(page, page.crop((0, 0, page.width, page.height - 1)))


def add_trailing_row(page):
    """One white row more at the bottom."""
    out = Image.new('1', (page.width, page.height + 1), 1)
    out.paste(page, (0, 0))
    return _keep_dpi(page, out)


def square_pixels(page, *, resample=Image.Resampling.NEAREST, dpi=204):
    """A fine page stored at ``dpi`` x ``dpi``: only the rows are resampled (196 lines per inch become ``dpi``)."""
    xdpi, ydpi = page.info.get('dpi', (204, 196))
    height = round(page.height * dpi / float(ydpi))
    if resample == Image.Resampling.NEAREST:
        out = page.resize((page.width, height), resample)
    else:
        out = page.convert('L').resize((page.width, height), resample).point(lambda v: 255 if v >= 128 else 0)
        out = out.convert('1')
    out.info['dpi'] = (xdpi, dpi)
    return out


def resampled(page, xdpi, ydpi, *, resample=Image.Resampling.NEAREST):
    """The page drawn again at ``xdpi`` x ``ydpi`` (a service that stores every fax at one resolution)."""
    sx, sy = page.info.get('dpi', (204, 196))
    size = (round(page.width * xdpi / float(sx)), round(page.height * ydpi / float(sy)))
    out = page.convert('L').resize(size, resample).point(lambda v: 255 if v >= 128 else 0).convert('1')
    out.info['dpi'] = (xdpi, ydpi)
    return out


def rotated(page):
    """Upside down (a receiver that stores the page as it came out of a scanner the other way round)."""
    return _keep_dpi(page, page.rotate(180))


def header_overlaid(page, text='OCT-10-2026 09:41  FROM +1 555 555 0100  TO FAXBEEP  P.1/1'):
    """A header line drawn over the top band of the page, as HylaFAX's tag line and many receivers do."""
    out = page.copy()
    ImageDraw.Draw(out).text((40, 12), text, fill=0)
    return _keep_dpi(page, out)


def header_added(page, rows=40, text='Received 10/10/2026 09:41 from +1 555 555 0100 page 1'):
    """A header line added above the page: ``rows`` new rows, the page itself unchanged below them."""
    out = Image.new('1', (page.width, page.height + rows), 1)
    ImageDraw.Draw(out).text((40, 10), text, fill=0)
    out.paste(page, (0, rows))
    return _keep_dpi(page, out)


def moved(page, dx):
    """The page moved ``dx`` dots to the right (left when negative), the same width, white filled in."""
    out = Image.new('1', page.size, 1)
    out.paste(page, (dx, 0))
    return _keep_dpi(page, out)


def padded(page, width=1734):
    """Padded with white on the right to ``width`` dots (8.5 inches at 204 dpi), the page itself unmoved."""
    out = Image.new('1', (width, page.height), 1)
    out.paste(page, (0, 0))
    return _keep_dpi(page, out)


def centred(page, width=1734):
    """Padded with white on both sides to ``width`` dots: the page moves right by half the padding."""
    out = Image.new('1', (width, page.height), 1)
    out.paste(page, ((width - page.width) // 2, 0))
    return _keep_dpi(page, out)


def inverted(page):
    """Black and white swapped (a file whose polarity flag a writer or reader got wrong)."""
    return _keep_dpi(page, Image.frombytes('1', page.size, page.tobytes().translate(_INVERT)))


def thumbnail(page, width=725):
    """A small colour preview of the page, like the picture a receiver's web page shows."""
    height = round(page.height * width / page.width)
    return page.convert('RGB').resize((width, height), Image.Resampling.LANCZOS)


# --- containers -------------------------------------------------------------------------------------------------

def png_bytes(page, *, dpi=False):
    """A one-bit PNG of the page; Faxbeep's page PNGs carry no resolution."""
    buffer = io.BytesIO()
    options = {'dpi': page.info.get('dpi', (204, 196))} if dpi else {}
    page.save(buffer, 'PNG', **options)
    return buffer.getvalue()


def jpeg_bytes(page, quality=90):
    buffer = io.BytesIO()
    page.convert('L').save(buffer, 'JPEG', quality=quality)
    return buffer.getvalue()


def tiff_bytes(pages, compression='group4'):
    buffer = io.BytesIO()
    first = pages[0]
    first.save(buffer, 'TIFF', compression=compression, dpi=first.info.get('dpi', (204, 196)), save_all=True,
               append_images=list(pages[1:]))
    return buffer.getvalue()


def _ccitt(page, k=-1):
    """CCITT data whose white runs are the paper: G4 (``k`` -1), G3 1-D (0) or G3 2-D (> 0), through libtiff."""
    flipped = Image.frombytes('1', page.size, page.tobytes().translate(_INVERT))
    buffer = io.BytesIO()
    options = {'compression': 'group4'} if k < 0 else {'compression': 'group3'}
    if k > 0:
        options['tiffinfo'] = {292: 1}
    flipped.save(buffer, 'TIFF', strip_size=math.ceil(page.width / 8) * page.height, **options)
    buffer.seek(0)
    with Image.open(buffer) as written:
        offset, count = written.tag_v2[273][0], written.tag_v2[279][0]
    return buffer.getvalue()[offset:offset + count]


def _image_object(page, kind, *, k=-1, black_is_1=False, decode_inverted=False):
    """(dictionary text, stream bytes) of one image XObject for ``page``."""
    width, height = page.size
    decode = ' /Decode [1 0]' if decode_inverted else ''
    if kind == 'ccitt':
        data = _ccitt(page, k)
        if black_is_1:
            # The same codes; the writer says 1 is black, so a correct reader inverts unless /Decode flips back.
            pass
        parms = f'<< /K {k} /BlackIs1 {"true" if black_is_1 else "false"} /Columns {width} /Rows {height} >>'
        head = (f'/Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray '
                f'/BitsPerComponent 1 /Filter [ /CCITTFaxDecode ] /DecodeParms [ {parms} ]{decode}')
        return head, data
    if kind == 'flate1':
        return (f'/Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray '
                f'/BitsPerComponent 1 /Filter /FlateDecode{decode}', zlib.compress(page.tobytes(), 9))
    if kind == 'flate8':
        return (f'/Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray '
                f'/BitsPerComponent 8 /Filter /FlateDecode', zlib.compress(page.convert('L').tobytes(), 9))
    if kind == 'mask':
        # An image mask paints its 0 samples (Decode [0 1]) in the fill colour, black by default.
        return (f'/Type /XObject /Subtype /Image /Width {width} /Height {height} /ImageMask true '
                f'/BitsPerComponent 1 /Filter /FlateDecode', zlib.compress(page.tobytes(), 9))
    raise ValueError(kind)


def pdf_bytes(pages, *, kind='ccitt', k=-1, black_is_1=False, decode_inverted=False, strip_rows=None,
              producer='https://imagemagick.org', thumb=True):
    """A PDF with one image per page (or one image per ``strip_rows`` rows, stacked top to bottom), each page box
    the image's pixels at its resolution, as ImageMagick writes a fax. ``kind``: 'ccitt' (``k`` -1 G4, 0 G3 1-D,
    > 0 G3 2-D), 'flate1', 'flate8' or 'mask'."""
    objects = []  # (number, dictionary text, stream bytes or None)

    def add(text, stream=None):
        objects.append([len(objects) + 1, text, stream])
        return len(objects)

    catalog = add('')
    tree = add('')
    kids = []
    for page in pages:
        xdpi, ydpi = (float(value) for value in page.info.get('dpi', (204, 196)))
        width_points = round(page.width * 72 / xdpi, 3)
        height_points = round(page.height * 72 / ydpi, 3)
        strips = [(0, page.height)] if not strip_rows else [
            (top, min(page.height, top + strip_rows)) for top in range(0, page.height, strip_rows)]
        names, drawing = [], []
        for index, (top, bottom) in enumerate(strips):
            part = page.crop((0, top, page.width, bottom))
            head, data = _image_object(part, kind, k=k, black_is_1=black_is_1, decode_inverted=decode_inverted)
            number = add(f'<< {head} /Length {len(data)} >>', data)
            name = f'Im{index}'
            names.append(f'/{name} {number} 0 R')
            # PDF's y axis points up: the first strip is drawn at the top.
            y = round((page.height - bottom) * 72 / ydpi, 3)
            h = round((bottom - top) * 72 / ydpi, 3)
            drawing.append(f'q\n{width_points} 0 0 {h} 0 {y} cm\n/{name} Do\nQ\n')
        content = ''.join(drawing).encode('latin-1')
        contents = add(f'<< /Length {len(content)} >>', content)
        page_number = add('')
        thumb_text = ''
        if thumb:
            thumb_data = _ccitt(Image.new('1', (1, 1), 1))
            thumb_object = add(f'<< /Filter [ /CCITTFaxDecode ] /DecodeParms [ << /K -1 /BlackIs1 false /Columns 1 '
                               f'/Rows 1 >> ] /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 1 '
                               f'/Length {len(thumb_data)} >>', thumb_data)
            thumb_text = f'\n/Thumb {thumb_object} 0 R'
        objects[page_number - 1][1] = (
            f'<<\n/Type /Page\n/Parent {tree} 0 R\n/Resources << /XObject << {" ".join(names)} >> '
            f'/ProcSet [ /PDF /Text /ImageC ] >>\n/MediaBox [0 0 {width_points} {height_points}]\n'
            f'/CropBox [0 0 {width_points} {height_points}]\n/Contents {contents} 0 R{thumb_text}\n>>')
        kids.append(page_number)
    objects[catalog - 1][1] = f'<<\n/Pages {tree} 0 R\n/Type /Catalog\n>>'
    listed = ' '.join(f'{kid} 0 R' for kid in kids)
    objects[tree - 1][1] = f'<<\n/Type /Pages\n/Kids [ {listed} ]\n/Count {len(kids)}\n>>'
    info = add(f'<<\n/Author ({producer})\n/Producer ({producer})\n>>')
    out = io.BytesIO()
    out.write(b'%PDF-1.3 \n')
    offsets = []
    for number, text, stream in objects:
        offsets.append(out.tell())
        out.write(f'{number} 0 obj\n{text}\n'.encode('latin-1'))
        if stream is not None:
            out.write(b'stream\n' + stream + b'\nendstream\n')
        out.write(b'endobj\n')
    start = out.tell()
    out.write(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode('latin-1'))
    for offset in offsets:
        out.write(f'{offset:010d} 00000 n \n'.encode('latin-1'))
    out.write(f'trailer\n<<\n/Size {len(objects) + 1}\n/Info {info} 0 R\n/Root {catalog} 0 R\n>>\n'
              f'startxref\n{start}\n%%EOF\n'.encode('latin-1'))
    return out.getvalue()


def faxbeep_pdf(pages):
    """Faxbeep's published receipt: each page one trailing white row shorter, in ImageMagick's G4 PDF."""
    return pdf_bytes([drop_trailing_row(page) for page in pages])


def faxbeep_pngs(pages):
    """Faxbeep's page pictures (``*-received-page-N-1.png``): one-bit PNGs, no resolution, one row shorter."""
    return [png_bytes(drop_trailing_row(page)) for page in pages]


# Page transformations a receiver applies, by name: each a function of one page.
PAGE_TRANSFORMS = {
    'exact': lambda page: page,
    'trailing_row_removed': drop_trailing_row,
    'trailing_row_added': add_trailing_row,
    'square_pixels_nearest': square_pixels,
    'square_pixels_bilinear': lambda page: square_pixels(page, resample=Image.Resampling.BILINEAR),
    'rotated_180': rotated,
    'header_overlaid': header_overlaid,
    'header_added': header_added,
    'moved_right': lambda page: moved(page, 3),
    'moved_left': lambda page: moved(page, -2),
    'padded_to_letter': padded,
    'centred_on_letter': centred,
    'inverted': inverted,
    'rotated_inverted': lambda page: inverted(rotated(page)),
}


def mirror(page):
    """Unused by receivers; kept to prove a mirrored page is refused rather than misread."""
    return _keep_dpi(page, ImageOps.mirror(page))
