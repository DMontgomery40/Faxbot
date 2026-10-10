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

from .container import ContainerError, Document, key_fingerprint, pack, unpack, zstd_available  # noqa: F401
from .pages import PageError, RESOLUTIONS, find_ladder  # noqa: F401
from . import pages as _pages
from . import reading as _reading

LAYOUTS = tuple(_pages.LAYOUTS)


class CodecError(ValueError):
    """Why a payload could not be made or read, in one sentence a person can act on."""


def encode_document(document, *, resolution='fine', layout='grid', fec='medium', secret=None, picture=None,
                    sturdy=False, salt=None, nonce=None, max_pages=200, run_limit=_pages.DEFAULT_RUN_LIMIT,
                    profile=None):
    """Payload pages (``pages.EncodedPages``) carrying ``document``. ``profile``: the capacity layout's profile
    (``capacity.PROFILES``)."""
    try:
        packed = pack(document, secret=secret, salt=salt, nonce=nonce)
        return _pages.encode(packed, resolution=resolution, layout=layout, fec=fec, sturdy=sturdy,
                             picture=picture, max_pages=max_pages, run_limit=run_limit, profile=profile)
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
                  layout=decoded.header['layout'], corrections=decoded.corrections)
    return document, report


def looks_like_payload(image, *, lines=None):
    """True when a page shows the payload pattern, either way up and either way round in black and white; cheap
    enough to run on every received fax."""
    return find_ladder(_pages.paper_white(image)[0], limit=lines) is not None


def write_tiff(images, path):
    """A multi-page Group 4 fax TIFF, as the fax engines send (each page keeps its resolution)."""
    first, rest = images[0], list(images[1:])
    dpi = first.info.get('dpi', (204, 196))
    first.convert('1').save(path, 'TIFF', compression='group4', dpi=dpi, save_all=True,
                            append_images=[image.convert('1') for image in rest])
    return path


MAX_INPUT_PAGES = _reading.MAX_INPUT_PAGES


def read_images(path_or_bytes):
    """Page images from a received fax file, exactly as received: TIFF (any fax coding), PDF (CCITT, Flate or other
    images, one per page or in strips), PNG, JPEG, GIF or BMP (``reading``)."""
    try:
        return _reading.read_images(path_or_bytes)
    except _reading.ReadingError as error:
        raise CodecError(str(error)) from None


def first_page(data):
    """Page one of a received fax file only, or None: the cheap probe before reading every page."""
    return _reading.first_page(data)
