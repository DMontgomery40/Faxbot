"""The page images a received fax file holds, exactly as they arrived (brief 85 M3).

Encoded pages are read from whatever a receiver keeps: Faxbot's own fax image, a fax service's PDF or the pictures
a public test line publishes. Run-coded pages need every pixel as it arrived, so nothing here resamples a page.

- TIFF, PNG, JPEG, GIF and BMP: each frame as stored, with its resolution (204 x 196 when the file has none).
- PDF: each page's images as the page draws them. One image is the page. Several images of the same width drawn
  one under the other (a page stored in strips) are joined top to bottom. Otherwise the largest image is the page.
  Each image's resolution is its pixels over the size the page draws it at.
- CCITT images (Group 3 one- and two-dimensional, Group 4) are decoded here rather than by pypdf, which leaves a
  two-dimensional Group 3 image to a one-dimensional decoder. The codes say which runs are white, so the paper
  comes out white whatever ``/BlackIs1`` or ``/Decode`` say; a page that still comes out inverted (a picture whose
  polarity a writer got wrong) is turned the right way round by the page reader (``pages.read_page``).
- A page with no image, or one no reader here can decode, is drawn by Ghostscript at 204 x 196.
"""
from __future__ import annotations

import io
import logging
import struct
from pathlib import Path

log = logging.getLogger(__name__)
MAX_INPUT_PAGES = 200
FINE = (204, 196)


class ReadingError(ValueError):
    """The file cannot be read as fax pages (one sentence a person can act on)."""


def _pdf_errors():
    """What pypdf and Pillow raise for a PDF or image they cannot read; anything else is a bug and propagates."""
    from pypdf.errors import PyPdfError
    return (PyPdfError, OSError, ValueError, KeyError, IndexError, NotImplementedError, TypeError, struct.error,
            AttributeError)


def read_images(path_or_bytes, *, last_page=MAX_INPUT_PAGES):
    """Page images from a received fax file: TIFF (any fax coding), PDF, PNG, JPEG, GIF or BMP."""
    data = Path(path_or_bytes).read_bytes() if isinstance(path_or_bytes, (str, Path)) else bytes(path_or_bytes)
    if data[:5] == b'%PDF-':
        return pdf_images(data, last_page=last_page)
    from PIL import Image
    images = []
    try:
        with Image.open(io.BytesIO(data)) as source:
            for index in range(min(MAX_INPUT_PAGES, last_page)):
                try:
                    source.seek(index)
                except EOFError:
                    break
                frame = source.copy()
                frame.info['dpi'] = _dpi(source.info.get('dpi'))
                images.append(frame)
    except (OSError, ValueError):
        raise ReadingError('This file is not an image Faxbot can read.') from None
    return images


def first_page(data):
    """Page one of a received fax file only, or None: the cheap probe before reading every page."""
    try:
        images = read_images(data, last_page=1)
    except ReadingError:
        return None
    return images[0] if images else None


def _dpi(value):
    try:
        x, y = (float(part) for part in value)
    except (TypeError, ValueError):
        return FINE
    if not (x > 1 and y > 1):
        return FINE
    return (round(x), round(y))


# --- PDF --------------------------------------------------------------------------------------------------------

def pdf_images(data, *, last_page=MAX_INPUT_PAGES):
    """One image per PDF page (see the module text); Ghostscript draws the pages no image reader here can."""
    from pypdf import PdfReader
    errors = _pdf_errors()
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = list(reader.pages)[:min(MAX_INPUT_PAGES, last_page)]
    except errors as error:
        log.info('The PDF could not be opened (%s); Ghostscript draws its pages.', type(error).__name__)
        return rendered(data, last_page=last_page)
    images = []
    for page in pages:
        try:
            image = page_image(page)
        except errors as error:
            log.info('A PDF page image could not be decoded (%s); Ghostscript draws the pages.', type(error).__name__)
            image = None
        if image is None:
            return rendered(data, last_page=last_page)
        images.append(image)
    return images


def _placements(page):
    """[(name, (a, b, c, d, e, f))]: each image XObject the page's content draws, with the matrix it is drawn with."""
    from pypdf.generic import ContentStream
    resources = page.get('/Resources')
    resources = resources.get_object() if resources is not None else {}
    xobjects = resources.get('/XObject')
    xobjects = xobjects.get_object() if xobjects is not None else {}
    contents = page.get_contents()
    if contents is None or not xobjects:
        return []
    found = []
    matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack = []
    for operands, operator in ContentStream(contents, page.pdf).operations:
        if operator == b'q':
            stack.append(matrix)
        elif operator == b'Q':
            matrix = stack.pop() if stack else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        elif operator == b'cm' and len(operands) == 6:
            a, b, c, d, e, f = (float(value) for value in operands)
            m = matrix
            matrix = (a * m[0] + b * m[2], a * m[1] + b * m[3], c * m[0] + d * m[2], c * m[1] + d * m[3],
                      e * m[0] + f * m[2] + m[4], e * m[1] + f * m[3] + m[5])
        elif operator == b'Do' and operands:
            name = str(operands[0])
            target = xobjects.get(name)
            if target is not None and target.get_object().get('/Subtype') == '/Image':
                found.append((name, matrix))
    return found


def page_image(page):
    """The page's image as it was received, with its resolution; None when the page draws no image."""
    from PIL import Image
    placed = _placements(page)
    xobjects = page['/Resources'].get_object()['/XObject'].get_object() if placed else {}
    pictures = []
    for name, matrix in placed:
        stream = xobjects[name].get_object()
        image = xobject_image(stream, page, name)
        if image is None:
            return None
        pictures.append((image, matrix))
    if not pictures:
        return None
    if len(pictures) > 1 and _stacked(pictures):
        # Strips of one page: same width and horizontal placement, drawn one under the other.
        ordered = sorted(pictures, key=lambda item: -(item[1][5] + item[1][3]))
        width = ordered[0][0].width
        height = sum(image.height for image, _ in ordered)
        joined = Image.new(ordered[0][0].mode if all(i.mode == ordered[0][0].mode for i, _ in ordered) else 'L',
                           (width, height), 255 if ordered[0][0].mode != '1' else 1)
        top = 0
        for image, _ in ordered:
            joined.paste(image if image.mode == joined.mode else image.convert(joined.mode), (0, top))
            top += image.height
        drawn_height = sum(abs(matrix[3]) for _, matrix in ordered)
        joined.info['dpi'] = _dpi((width * 72 / abs(ordered[0][1][0]), height * 72 / drawn_height))
        return joined
    image, matrix = max(pictures, key=lambda item: item[0].width * item[0].height)
    width_points, height_points = abs(matrix[0]) or abs(matrix[1]), abs(matrix[3]) or abs(matrix[2])
    if width_points > 0 and height_points > 0:
        image.info['dpi'] = _dpi((image.width * 72 / width_points, image.height * 72 / height_points))
    else:
        image.info['dpi'] = FINE
    return image


def _stacked(pictures):
    first, matrix = pictures[0]
    for image, other in pictures[1:]:
        if (image.width != first.width or abs(other[0] - matrix[0]) > 0.5 or abs(other[4] - matrix[4]) > 0.5
                or other[1] or other[2] or matrix[1] or matrix[2]):
            return False
    return True


def _filters(stream):
    found = stream.get('/Filter')
    if found is None:
        return []
    found = found.get_object()
    return [str(item) for item in found] if isinstance(found, list) else [str(found)]


def _parameters(stream):
    found = stream.get('/DecodeParms')
    if found is None:
        return {}
    found = found.get_object()
    if isinstance(found, list):
        found = found[0].get_object() if found else {}
    return {str(key): value.get_object() if hasattr(value, 'get_object') else value for key, value in found.items()}


def xobject_image(stream, page, name):
    """One image XObject as a PIL image: CCITT here (``ccitt_image``), everything else through pypdf."""
    if _filters(stream) == ['/CCITTFaxDecode']:
        parameters = _parameters(stream)
        width = int(parameters.get('/Columns', stream.get('/Width', 1728)))
        rows = int(parameters.get('/Rows', 0) or stream.get('/Height', 0))
        # The stream's own bytes: pypdf's get_data() would run its CCITT filter first.
        raw = stream._data
        return ccitt_image(raw, width=width, rows=rows, k=int(parameters.get('/K', 0)),
                           byte_aligned=bool(parameters.get('/EncodedByteAlign', False)))
    for item in page.images:
        if item.name.lstrip('/').split('.')[0] == name.lstrip('/'):
            return item.image
    return None


def ccitt_image(data, *, width, rows, k=-1, byte_aligned=False):
    """CCITT data decoded by libtiff, the paper white: Group 4 (``k`` < 0), Group 3 1-D (0) or 2-D (> 0)."""
    from PIL import Image
    if not (0 < width <= 20000 and 0 < rows <= 100000):
        raise ValueError('CCITT image size out of range')
    compression = 4 if k < 0 else 3
    t4_options = (1 if k > 0 else 0) | (4 if byte_aligned else 0)
    tags = [(256, 4, width), (257, 4, rows), (258, 3, 1), (259, 3, compression),
            (262, 3, 0),  # white is zero: CCITT white runs decode to white paper
            (273, 4, 0), (277, 3, 1), (278, 4, rows), (279, 4, len(data))]
    if compression == 3:
        tags.append((292, 4, t4_options))
    tags.sort()
    header_size = 8 + 2 + 12 * len(tags) + 4
    entries = b''.join(struct.pack('<HHLL', tag, kind, 1, header_size if tag == 273 else value)
                       if kind == 4 else struct.pack('<HHLHH', tag, kind, 1, value, 0)
                       for tag, kind, value in tags)
    tiff = b'II*\x00' + struct.pack('<L', 8) + struct.pack('<H', len(tags)) + entries + b'\x00' * 4 + data
    with Image.open(io.BytesIO(tiff)) as image:
        image.load()
        return image.convert('1').copy()


def rendered(data, last_page=MAX_INPUT_PAGES):
    """The PDF's pages drawn by Ghostscript at 204 x 196 dots per inch."""
    import shutil
    import subprocess
    import tempfile
    from PIL import Image
    executable = shutil.which('gs')
    if executable is None:
        raise ReadingError('This PDF needs Ghostscript to read, and it is not installed.')
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / 'in.pdf'
        source.write_bytes(data)
        target = Path(folder) / 'page-%03d.png'
        try:
            subprocess.run([executable, '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-sDEVICE=pngmono', '-r204x196',
                            f'-dLastPage={last_page}', f'-sOutputFile={target}', str(source)],
                           check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            raise ReadingError('This PDF could not be read.') from None
        images = []
        for path in sorted(Path(folder).glob('page-*.png')):
            with Image.open(path) as image:
                copy = image.copy()
                copy.info['dpi'] = FINE
                images.append(copy)
        return images
