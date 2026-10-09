"""Experimental fax payload codec: a whole document carried on a few dense fax pages.

The sender packs the original document (``container``), protects it with
Reed-Solomon error correction spread across every row (``stream``) and paints
it onto bilevel pages sized to the receiver's resolution (``pages``):

- ``grid``: visible cells, readable after resolution conversion or rescaling;
- ``runs``: payload bits chosen as MH codes, so the fax line carries about the
  payload itself; it needs the exact raster (``runs``);
- ``picture``: a halftone of a picture whose dot clusters lean left or right
  to carry the bits (``pages``; StegaTone-style cluster shifting).
- ``enumerative``: exact ranked MH run sequences (profile 1), explicitly
  selected for compatible Faxbot decoders and an unchanged raster.

A receiving Faxbot finds the pattern, decodes, checks the SHA-256 and delivers
the original; the received fax image is kept unchanged as evidence. Everything
here is experimental and used only for recipients who agreed to it.
"""
import io
from pathlib import Path

from .container import ContainerError, Document, key_fingerprint, pack, unpack, zstd_available  # noqa: F401
from .pages import PageError, RESOLUTIONS, find_ladder  # noqa: F401
from . import pages as _pages

LAYOUTS = tuple(_pages.LAYOUTS)


class CodecError(ValueError):
    """Why a payload could not be made or read, in one sentence a person can act on."""


def encode_document(document, *, resolution='fine', layout='grid', fec='medium', secret=None, picture=None,
                    sturdy=False, salt=None, nonce=None, max_pages=200, run_limit=_pages.DEFAULT_RUN_LIMIT):
    """Payload pages (``pages.EncodedPages``) carrying ``document``."""
    try:
        packed = pack(document, secret=secret, salt=salt, nonce=nonce)
        return _pages.encode(packed, resolution=resolution, layout=layout, fec=fec, sturdy=sturdy,
                             picture=picture, max_pages=max_pages, run_limit=run_limit)
    except (ContainerError, PageError) as error:
        raise CodecError(str(error)) from None


def decode_images(images, *, secrets=()):
    """(the original Document, the stream report) from page images; raises CodecError."""
    try:
        decoded = _pages.decode(images)
        document = unpack(decoded.container, secrets=secrets)
    except (ContainerError, PageError) as error:
        raise CodecError(str(error)) from None
    report = dict(decoded.report, pages_read=decoded.pages_read, pages_expected=decoded.pages_expected,
                  layout=decoded.header['layout'])
    return document, report


def looks_like_payload(image, *, lines=None):
    """True when a page shows the payload pattern; cheap enough to run on every received fax."""
    return find_ladder(image, limit=lines) is not None


def write_tiff(images, path):
    """A multi-page Group 4 fax TIFF, as the fax engines send (each page keeps its resolution)."""
    first, rest = images[0], list(images[1:])
    dpi = first.info.get('dpi', (204, 196))
    first.convert('1').save(path, 'TIFF', compression='group4', dpi=dpi, save_all=True,
                            append_images=[image.convert('1') for image in rest])
    return path


MAX_INPUT_PAGES = 200


def read_images(path_or_bytes):
    """Page images from a received fax file: TIFF (any fax coding), PDF, PNG, JPEG, GIF or BMP."""
    from PIL import Image
    data = Path(path_or_bytes).read_bytes() if isinstance(path_or_bytes, (str, Path)) else bytes(path_or_bytes)
    if data[:5] == b'%PDF-':
        return _pdf_images(data)
    images = []
    try:
        with Image.open(io.BytesIO(data)) as source:
            for index in range(MAX_INPUT_PAGES):
                try:
                    source.seek(index)
                except EOFError:
                    break
                frame = source.copy()
                frame.info['dpi'] = source.info.get('dpi', (204, 196))
                images.append(frame)
    except (OSError, ValueError):
        raise CodecError('This file is not an image Faxbot can read.') from None
    return images


def first_page(data):
    """Page one of a received fax file only, or None: the cheap probe before reading every page."""
    from PIL import Image
    data = bytes(data)
    try:
        if data[:5] == b'%PDF-':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            images = sorted((item.image for item in reader.pages[0].images),
                            key=lambda image: image.size[0] * image.size[1])
            if images:
                return images[-1]
            rendered = _rendered(data, last_page=1)
            return rendered[0] if rendered else None
        with Image.open(io.BytesIO(data)) as source:
            return source.copy()
    except Exception:
        try:
            rendered = _rendered(data, last_page=1) if data[:5] == b'%PDF-' else []
        except CodecError:
            return None
        return rendered[0] if rendered else None


def _pdf_images(data):
    """The largest image on each PDF page; Ghostscript renders pages whose images pypdf cannot decode."""
    from pypdf import PdfReader
    images = []
    try:
        reader = PdfReader(io.BytesIO(data))
        for page in list(reader.pages)[:MAX_INPUT_PAGES]:
            best = None
            for item in page.images:
                image = item.image
                if best is None or image.size[0] * image.size[1] > best.size[0] * best.size[1]:
                    best = image
            if best is None:
                return _rendered(data)
            width_points = float(page.mediabox.width)
            if width_points > 0:
                dpi_x = round(best.size[0] * 72 / width_points)
                dpi_y = round(best.size[1] * 72 / float(page.mediabox.height))
                best.info['dpi'] = (dpi_x, dpi_y)
            images.append(best)
    except Exception:
        return _rendered(data)
    return images


def _rendered(data, last_page=MAX_INPUT_PAGES):
    import shutil
    import subprocess
    import tempfile
    from PIL import Image
    executable = shutil.which('gs')
    if executable is None:
        raise CodecError('This PDF needs Ghostscript to read, and it is not installed.')
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / 'in.pdf'
        source.write_bytes(data)
        target = Path(folder) / 'page-%03d.png'
        try:
            subprocess.run([executable, '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-sDEVICE=pngmono', '-r204x196',
                            f'-dLastPage={last_page}', f'-sOutputFile={target}', str(source)],
                           check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            raise CodecError('This PDF could not be read.') from None
        images = []
        for path in sorted(Path(folder).glob('page-*.png')):
            with Image.open(path) as image:
                images.append(image.copy())
        return images
