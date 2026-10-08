import io
import subprocess
import shutil
import tempfile
import math
import zlib
import unicodedata
import warnings
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Tuple, Optional
from PIL import Image, features  # type: ignore
from pypdf import PdfReader, apply_configuration
from pypdf.generic import ArrayObject, ContentStream, DictionaryObject, NullObject, StreamObject
import reportlab  # type: ignore
from reportlab.lib.pagesizes import letter  # type: ignore
from reportlab.pdfbase import pdfdoc, pdfmetrics  # type: ignore
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore
from reportlab.pdfgen import canvas  # type: ignore
from reportlab.pdfgen.canvas import _digester  # type: ignore
from reportlab.lib.utils import ImageReader  # type: ignore
import os


MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_PAGES = 500
MAX_RASTER_PAGE_PIXELS = 25_000_000
MAX_RASTER_TOTAL_PIXELS = 100_000_000
MAX_PDF_STREAM_BYTES = 4 * 1024 * 1024
MAX_TOTAL_PDF_STREAM_BYTES = 32 * 1024 * 1024
GHOSTSCRIPT_TIMEOUT_SECONDS = 120
SUPPORTED_TIFF_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "CMYK"})


class DocumentConversionError(Exception):
    """A sanitized document error; operational failures can be mapped to HTTP 503."""

    def __init__(self, message: str, *, operational: bool = False):
        super().__init__(message)
        self.operational = operational


def _check_file_size(path: str, *, limit: int = MAX_DOCUMENT_BYTES) -> None:
    try:
        size = Path(path).stat().st_size
    except OSError:
        raise DocumentConversionError("Document could not be read.") from None
    if size <= 0 or size > limit:
        raise DocumentConversionError("Document is empty or exceeds supported size limits.")


@contextmanager
def _atomic_output(output_path: str):
    """Only publish a complete artifact, leaving prior output intact on failure."""
    temporary = None
    try:
        destination = Path(output_path)
        fd, temporary = tempfile.mkstemp(
            prefix=".faxbot-", suffix=destination.suffix, dir=destination.parent
        )
        os.close(fd)
        yield temporary
        _check_file_size(temporary, limit=MAX_OUTPUT_BYTES)
        os.replace(temporary, output_path)
    except DocumentConversionError:
        raise
    except Exception:
        raise DocumentConversionError("Document conversion failed.", operational=True) from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


@lru_cache(maxsize=1)
def _text_font():
    # ReportLab distributes this font; no host font discovery is necessary.
    try:
        font = TTFont("FaxbotVera", str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"))
        pdfmetrics.registerFont(font)
        return font
    except Exception:
        raise DocumentConversionError("Text rendering is unavailable.", operational=True) from None


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def txt_to_pdf(txt_path: str, pdf_path: str) -> None:
    """Render strict UTF-8 using embedded Vera glyphs, wrapping without data loss.

    CRLF/CR/LF are line breaks; one final line break ends the last line rather
    than starting an empty one, so it never adds a page. Tabs expand to
    eight-column stops. Other control characters and characters missing from
    Vera's cmap are rejected. Text is rendered left to right without
    complex-script shaping. Limits apply to source bytes, output bytes and pages
    independently of transmission settings.
    """
    _check_file_size(txt_path)
    try:
        with open(txt_path, "rb") as source:
            data = source.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise DocumentConversionError("Document exceeds supported size limits.")
        text = data.decode("utf-8", errors="strict")
    except (OSError, UnicodeError):
        raise DocumentConversionError("Text document must contain valid UTF-8.") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.endswith("\n"):
        # A terminated last line is ordinary text-file form, not an extra line.
        text = text[:-1]
    font = _text_font()
    if not text.strip():
        raise DocumentConversionError("Text document is empty.")
    for character in text:
        if character in "\n\t":
            continue
        if (
            unicodedata.category(character).startswith("C")
            or unicodedata.combining(character)
            or unicodedata.bidirectional(character) in {"R", "AL", "AN"}
            or ord(character) not in font.face.charWidths
        ):
            raise DocumentConversionError("Text document contains unsupported characters.")

    width, height = letter
    margin = 54
    with _atomic_output(pdf_path) as temporary:
        c = canvas.Canvas(temporary, pagesize=letter)
        c.setFont(font.fontName, 10)
        y = height - margin
        pages = 1
        for raw_line in text.split("\n"):
            line = []
            line_width = 0.0
            wrapped = []
            for character in raw_line.expandtabs(8):
                character_width = font.face.charWidths[ord(character)] / 100
                if line and line_width + character_width > width - 2 * margin:
                    wrapped.append("".join(line))
                    line, line_width = [], 0.0
                line.append(character)
                line_width += character_width
            wrapped.append("".join(line))
            for line in wrapped:
                if y < margin:
                    pages += 1
                    if pages > MAX_DOCUMENT_PAGES:
                        raise DocumentConversionError("Document exceeds supported page limits.")
                    c.showPage()
                    c.setFont(font.fontName, 10)
                    y = height - margin
                c.drawString(margin, y, line)
                y -= 12
        c.save()


def _pdf_dimensions(page) -> tuple[float, float]:
    if page.get("/Type") != "/Page":
        raise ValueError("Invalid page object")
    box = page.mediabox
    coordinates = [float(value) for value in box]
    unit = float(page.get("/UserUnit", 1))
    if len(coordinates) != 4 or not all(math.isfinite(value) for value in coordinates + [unit]):
        raise ValueError("Invalid page geometry")
    width = (coordinates[2] - coordinates[0]) * unit
    height = (coordinates[3] - coordinates[1]) * unit
    if unit <= 0 or not (0 < width <= 14400 and 0 < height <= 14400):
        raise ValueError("Unsupported page geometry")
    rotation = float(page.get("/Rotate", 0))
    if not math.isfinite(rotation) or rotation % 90:
        raise ValueError("Invalid page rotation")
    return (height, width) if rotation % 180 else (width, height)


def _inspect_pdf(pdf_path: str, *, raster: bool = False) -> int:
    _check_file_size(pdf_path, limit=MAX_OUTPUT_BYTES)
    try:
        with apply_configuration(
            maximum_declared_stream_length=MAX_OUTPUT_BYTES,
            array_based_stream_maximum_output_length=MAX_PDF_STREAM_BYTES,
            zlib_maximum_output_length=MAX_PDF_STREAM_BYTES,
            lzw_maximum_output_length=MAX_PDF_STREAM_BYTES,
            run_length_maximum_output_length=MAX_PDF_STREAM_BYTES,
            page_tree_maximum_entries=MAX_DOCUMENT_PAGES * 10,
            jbig2dec_binary=None,
        ), open(pdf_path, "rb") as source:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise DocumentConversionError("Encrypted PDF documents are not supported.")
            pages = len(reader.pages)
            if not 0 < pages <= MAX_DOCUMENT_PAGES:
                raise DocumentConversionError("PDF has no pages or exceeds supported page limits.")
            total_pixels = total_content_bytes = 0
            for page in reader.pages:
                width, height = _pdf_dimensions(page)
                if raster:
                    pixels = math.ceil(width * 204 / 72) * math.ceil(height * 196 / 72)
                    total_pixels += pixels
                    if pixels > MAX_RASTER_PAGE_PIXELS or total_pixels > MAX_RASTER_TOTAL_PIXELS:
                        raise DocumentConversionError("PDF exceeds supported raster limits.")
                resources = page.get("/Resources")
                if resources is not None and not isinstance(resources.get_object(), DictionaryObject):
                    raise ValueError("Invalid page resources")
                content = page.get("/Contents")
                if content is None or isinstance(content.get_object(), NullObject):
                    continue  # A structurally valid blank page is allowed.
                resolved = content.get_object()
                streams = resolved if isinstance(resolved, ArrayObject) else [resolved]
                page_content_bytes = 0
                for stream in streams:
                    stream = stream.get_object()
                    if not isinstance(stream, StreamObject):
                        raise ValueError("Invalid page contents")
                    page_content_bytes += len(stream.get_data())
                    if page_content_bytes > MAX_PDF_STREAM_BYTES:
                        raise DocumentConversionError("PDF exceeds supported content limits.")
                total_content_bytes += page_content_bytes
                if total_content_bytes > MAX_TOTAL_PDF_STREAM_BYTES:
                    raise DocumentConversionError("PDF exceeds supported content limits.")
                # Force bounded content parsing; page-tree length alone is insufficient.
                if len(ContentStream(content, reader).operations) > 200_000:
                    raise DocumentConversionError("PDF exceeds supported content limits.")
            return pages
    except DocumentConversionError:
        raise
    except Exception:
        raise DocumentConversionError("PDF document is invalid or unsupported.") from None


def validate_pdf(pdf_path: str) -> int:
    """Return a real positive page count after bounded structural/content validation."""
    return _inspect_pdf(pdf_path)


def count_pdf_pages(pdf_path: str) -> Optional[int]:
    """Return the real validated page count, or None if counting is impossible."""
    try:
        return validate_pdf(pdf_path)
    except DocumentConversionError:
        return None


def _tiff_frames(image):
    """Yield decoded frames one at a time with per-frame/document limits."""
    if image.format != "TIFF":
        raise DocumentConversionError("Document must be a valid TIFF.")
    total_pixels = 0
    for index in range(MAX_DOCUMENT_PAGES + 1):
        try:
            image.seek(index)
        except EOFError:
            return
        if index >= MAX_DOCUMENT_PAGES:
            raise DocumentConversionError("TIFF exceeds supported page limits.")
        if image.mode not in SUPPORTED_TIFF_MODES:
            raise DocumentConversionError("TIFF pixel mode is unsupported.")
        # Pillow can expose 16-bit RGB samples as mode RGB and discard their
        # low bits while decoding. Check the original tag before loading pixels.
        expected_depth = 1 if image.mode == "1" else 8
        if any(depth != expected_depth for depth in image.tag_v2.get(258, (1,))):
            raise DocumentConversionError("TIFF sample depth is unsupported.")
        width, height = image.size
        pixels = width * height
        total_pixels += pixels
        if not pixels or pixels > MAX_RASTER_PAGE_PIXELS or total_pixels > MAX_RASTER_TOTAL_PIXELS:
            raise DocumentConversionError("TIFF exceeds supported raster limits.")
        image.load()
        yield image


_INVERT_BITS = bytes(255 - value for value in range(256))


def _bilevel_candidates(frame):
    """Yield ``(data, filter, parameters)`` lossless one-bit encodings of a frame.

    Flate over the packed rows is always available; CCITT Group 4 is added when
    Pillow has libtiff. Every encoding decodes to exactly the pixels read.
    """
    # Pillow packs mode "1" rows MSB first with 1 for white, which is exactly
    # one-bit DeviceGray.
    yield zlib.compress(frame.tobytes(), 9), "FlateDecode", None
    if not features.check("libtiff"):
        return
    data = _g4_data(frame)
    if data is not None:
        yield data, "CCITTFaxDecode", {"K": -1, "Columns": frame.width, "Rows": frame.height, "BlackIs1": b"false"}


def _g4_data(frame):
    """One mode "1" frame as a complete CCITT Group 4 image whose white runs are the white paper, or None."""
    width, height = frame.size
    # libtiff codes stored 0 bits as white runs. Pillow stores mode "1" with
    # 1 for white, so encode the inverted picture: the paper is then coded as
    # CCITT white runs, which decode to white with BlackIs1 false.
    inverted = Image.frombytes("1", frame.size, frame.tobytes().translate(_INVERT_BITS))
    encoded = io.BytesIO()
    # One strip holds the whole page, so the strip is one complete G4 image.
    inverted.save(encoded, "TIFF", compression="group4", strip_size=math.ceil(width / 8) * height)
    encoded.seek(0)
    with Image.open(encoded) as written:
        tags = written.tag_v2
        offsets, counts = tags.get(273), tags.get(279)
        photometric, compression = tags.get(262), tags.get(259)
    data = encoded.getvalue()
    if (compression == 4 and photometric == 1 and offsets is not None and counts is not None
            and len(offsets) == len(counts) == 1 and offsets[0] + counts[0] <= len(data)):
        return data[offsets[0]:offsets[0] + counts[0]]
    return None


def _bilevel_stream(frame):
    """The smaller lossless encoding of this page (G4 suits line art, Flate dense scans)."""
    return min(_bilevel_candidates(frame), key=lambda candidate: len(candidate[0]))


class _BilevelImage(pdfdoc.PDFImageXObject):
    """A one-bit DeviceGray image XObject; reportlab would expand it to RGB."""

    def __init__(self, name, frame):
        super().__init__(name)
        self.width, self.height = frame.size
        self.bitsPerComponent = 1
        self.colorSpace = "DeviceGray"
        self.streamContent, self._filter, self._parameters = _bilevel_stream(frame)

    def format(self, document):
        stream = pdfdoc.PDFStream(content=self.streamContent)
        entries = stream.dictionary
        entries["Type"] = pdfdoc.PDFName("XObject")
        entries["Subtype"] = pdfdoc.PDFName("Image")
        entries["Width"] = self.width
        entries["Height"] = self.height
        entries["BitsPerComponent"] = 1
        entries["ColorSpace"] = pdfdoc.PDFName("DeviceGray")
        entries["Filter"] = pdfdoc.PDFArray([pdfdoc.PDFName(self._filter)])
        if self._parameters is not None:
            entries["DecodeParms"] = pdfdoc.PDFArray([pdfdoc.PDFDictionary(dict(self._parameters))])
        return stream.format(document)


class _RegisteredImage:
    """A drawImage source whose XObject is already registered under its name."""

    def __init__(self, key):
        self.key = key

    def __str__(self):
        return self.key


def _draw_bilevel(document, frame, page_number, width, height):
    """Place a mode "1" frame on the page as a one-bit image.

    drawImage names a non-ImageReader source by digesting ``str(source)`` and
    the mask, then reuses an XObject already registered under that name; the
    image is registered first so drawImage only positions it.
    """
    source = _RegisteredImage(f"faxbot-bilevel-page-{page_number}")
    name = _digester(f"{source}{None}".encode("utf-8"))
    image = _BilevelImage(name, frame)
    registered = document._doc.getXObjectName(name)
    document._doc.Reference(image, registered)
    document._doc.addForm(name, image)
    document.drawImage(source, 0, 0, width, height)


def tiff_to_pdf(tiff_path: str, pdf_path: str) -> Tuple[int, str]:
    """Preserve supported one/eight-bit TIFF frames in lossless PDF streams.

    Accept 1, L, LA, RGB, RGBA, CMYK and eight-bit indexed P. One-bit frames
    stay one-bit, in the smaller of CCITT Group 4 and Flate. Palettes are
    explicitly expanded to RGB/RGBA; high-depth and other modes are rejected.
    """
    _check_file_size(tiff_path)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            warnings.filterwarnings("error", category=UserWarning, module=r"PIL\.TiffImagePlugin")
            with Image.open(tiff_path) as image, _atomic_output(pdf_path) as temporary:
                document = canvas.Canvas(temporary)
                pages = 0
                for frame in _tiff_frames(image):
                    dpi = frame.info.get("dpi", (72, 72))
                    x_dpi, y_dpi = (float(value) for value in dpi)
                    if not all(math.isfinite(value) and value > 0 for value in (x_dpi, y_dpi)):
                        raise ValueError("Invalid image resolution")
                    width, height = frame.width * 72 / x_dpi, frame.height * 72 / y_dpi
                    if not (0 < width <= 14400 and 0 < height <= 14400):
                        raise ValueError("Unsupported page geometry")
                    if frame.mode == "P":
                        has_alpha = "transparency" in frame.info or (
                            frame.palette is not None and frame.palette.mode == "RGBA"
                        )
                        frame = frame.convert("RGBA" if has_alpha else "RGB")
                    document.setPageSize((width, height))
                    if frame.mode == "1":
                        _draw_bilevel(document, frame, pages, width, height)
                    else:
                        document.drawImage(ImageReader(frame), 0, 0, width, height, mask="auto")
                    document.showPage()
                    pages += 1
                if pages == 0:
                    raise DocumentConversionError("TIFF document has no pages.")
                document.save()
                if validate_pdf(temporary) != pages:
                    raise DocumentConversionError("TIFF conversion failed.", operational=True)
        return pages, pdf_path
    except DocumentConversionError:
        raise
    except Exception:
        raise DocumentConversionError("TIFF document is invalid or unsupported.") from None


# A fax image Asterisk sends: readable by its group as well (the data folder gives new files Asterisk's
# group; asterisk/start.sh), because Asterisk runs as its own user. Nothing else Faxbot writes there is.
FAX_IMAGE_MODE = 0o640


def pdf_to_tiff(pdf_path: str, tiff_path: str, *, match_resolution: bool = False,
                friendly=None) -> Tuple[int, str]:
    """Rasterize a validated PDF to real Group 4 fax TIFF at 204 by 196 DPI (mode FAX_IMAGE_MODE).

    ``match_resolution``: a document that is really standard resolution comes out at 204 by 98
    (pages/resolution.py). Off by default: an accepted fax's own image stays fine, because faxes sent
    together share one call and the built-in engine is not reliable with mixed resolutions in a call;
    each single send matches its resolution at send time (pages/sending.py).

    ``friendly``: a ``pages.friendly.Request`` for fax-friendly pages (light shading left out, specks
    removed; ``fax_friendly_pages``); its ``result`` says what changed. Off (None) by default."""
    pages = _inspect_pdf(pdf_path, raster=True)
    executable = shutil.which("gs")
    if executable is None:
        raise DocumentConversionError("PDF rasterization is unavailable.", operational=True)
    with _atomic_output(tiff_path) as temporary:
        arguments = [
            executable, "-q", "-dSAFER", "-dNOPAUSE", "-dBATCH", "-dPDFSTOPONERROR",
            "-sDEVICE=tiffg4", "-r204x196", f"-sOutputFile={temporary}",
            "-f", str(Path(pdf_path).resolve()),
        ]
        try:
            subprocess.run(
                arguments, check=True, timeout=GHOSTSCRIPT_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _check_file_size(temporary, limit=MAX_OUTPUT_BYTES)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                warnings.filterwarnings("error", category=UserWarning, module=r"PIL\.TiffImagePlugin")
                with Image.open(temporary) as image:
                    actual_pages = sum(1 for _ in _tiff_frames(image))
            if actual_pages != pages:
                raise ValueError("Incomplete raster output")
            if friendly is not None:
                fax_friendly_pages(pdf_path, temporary, friendly, executable)
            if match_resolution:
                from .pages.resolution import standard_frames
                standard = standard_frames(read_fax_frames(temporary) or [])
                if standard is not None:
                    _write_frames(standard, temporary)
            os.chmod(temporary, FAX_IMAGE_MODE)
        except Exception:
            raise DocumentConversionError("PDF rasterization failed.", operational=True) from None
    return pages, tiff_path


def fax_friendly_pages(pdf_path: str, tiff_path: str, request, executable: str) -> None:
    """The fax-friendly hook (pages/friendly.py): Ghostscript draws ``pdf_path`` again in gray, and the fax image
    it just wrote at ``tiff_path`` loses its light shading and specks, in place. It never fails the conversion:
    when anything goes wrong the image stays as Ghostscript drew it and ``request.result`` stays None."""
    from .pages import friendly
    friendly.apply(pdf_path, tiff_path, request, gs=executable)


def fax_image_resolution(tiff_path: str) -> Optional[str]:
    """'standard' or 'fine' for a fax image Faxbot wrote, from its first page; None when unreadable."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(tiff_path) as image:
                y_dpi = float((image.info.get("dpi") or (0, 0))[1])
    except Exception:
        return None
    if y_dpi <= 0:
        return None
    return "standard" if y_dpi < 150 else "fine"


def fax_page_bits(tiff_path: str) -> Optional[Tuple[int, ...]]:
    """Compressed bits of each page of a Group 4 fax TIFF (its strip sizes), or None when unreadable."""
    try:
        with Image.open(tiff_path) as image:
            sizes = []
            for index in range(MAX_DOCUMENT_PAGES + 1):
                try:
                    image.seek(index)
                except EOFError:
                    break
                counts = image.tag_v2.get(279)
                if not counts:
                    return None
                sizes.append(8 * sum(int(count) for count in counts))
        return tuple(sizes) or None
    except Exception:
        return None


# Dense pages (pages/): several original pages on one long fax page, and back ---------------------------------

def read_fax_frames(tiff_path: str):
    """Every frame of a fax TIFF as a separate mode "1" image with its resolution, or None when a frame is
    not one-bit (then it is not a fax image Faxbot can pack or split)."""
    _check_file_size(tiff_path, limit=MAX_OUTPUT_BYTES)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            warnings.filterwarnings("error", category=UserWarning, module=r"PIL\.TiffImagePlugin")
            with Image.open(tiff_path) as image:
                frames = []
                for frame in _tiff_frames(image):
                    if frame.mode != "1":
                        return None
                    copy = frame.copy()
                    copy.info["dpi"] = tuple(float(value) for value in frame.info.get("dpi", (0, 0)))
                    frames.append(copy)
        return frames or None
    except DocumentConversionError:
        raise
    except Exception:
        raise DocumentConversionError("TIFF document is invalid or unsupported.") from None


def _rational(value: float) -> Tuple[int, int]:
    return int(round(value * 100)), 100


def _fax_tiff_bytes(frames) -> bytes:
    """Mode "1" frames as the TIFF fax engines expect: one Group 4 strip a page, white is zero (as
    Ghostscript's tiffg4 writes), each page's resolution, page numbers."""
    import struct
    if not frames or len(frames) > MAX_DOCUMENT_PAGES:
        raise DocumentConversionError("Fax image has no pages or too many pages.")
    if not features.check("libtiff"):
        raise DocumentConversionError("Fax image writing is unavailable.", operational=True)
    out = bytearray(b"II*\x00\x00\x00\x00\x00")
    previous_link = 4
    for number, frame in enumerate(frames):
        if frame.mode != "1" or frame.width * frame.height > MAX_RASTER_PAGE_PIXELS:
            raise DocumentConversionError("Fax image page is unsupported.")
        strip = _g4_data(frame)
        if strip is None:
            raise DocumentConversionError("Fax image could not be encoded.", operational=True)
        x_dpi, y_dpi = (float(value) for value in frame.info.get("dpi", (204, 196)))
        strip_at = len(out)
        out += strip
        if len(out) % 2:
            out += b"\x00"
        rationals_at = len(out)
        for value in (x_dpi, y_dpi):
            out += struct.pack("<II", *_rational(value))
        ifd_at = len(out)
        entries = [
            (254, 4, 1, 2), (256, 4, 1, frame.width), (257, 4, 1, frame.height), (258, 3, 1, 1),
            (259, 3, 1, 4), (262, 3, 1, 0), (266, 3, 1, 1), (273, 4, 1, strip_at), (277, 3, 1, 1),
            (278, 4, 1, frame.height), (279, 4, 1, len(strip)), (282, 5, 1, rationals_at),
            (283, 5, 1, rationals_at + 8), (293, 4, 1, 0), (296, 3, 1, 2),
        ]
        out += struct.pack("<H", len(entries) + 1)
        for tag, kind, count, value in entries:
            packed = struct.pack("<HI", value, 0)[:4] if kind == 3 else struct.pack("<I", value)
            out += struct.pack("<HHI", tag, kind, count) + packed
        out += struct.pack("<HHIHH", 297, 3, 2, number, len(frames))
        struct.pack_into("<I", out, previous_link, ifd_at)
        previous_link = len(out)
        out += b"\x00\x00\x00\x00"
    return bytes(out)


def _write_frames(frames, path: str) -> None:
    """Write ``frames`` to ``path`` and check that they read back pixel for pixel (mode FAX_IMAGE_MODE)."""
    with open(path, "wb") as handle:
        handle.write(_fax_tiff_bytes(frames))
    written = read_fax_frames(path)
    if written is None or len(written) != len(frames) or any(
            a.size != b.size or a.tobytes() != b.tobytes() for a, b in zip(written, frames)):
        raise DocumentConversionError("Fax image did not read back unchanged.", operational=True)
    os.chmod(path, FAX_IMAGE_MODE)


def write_fax_tiff(frames, tiff_path: str) -> int:
    """Publish mode "1" frames as a fax TIFF once they read back unchanged; returns the page count."""
    with _atomic_output(tiff_path) as temporary:
        _write_frames(frames, temporary)
    return len(frames)


def pack_fax_image(tiff_path: str, output_path: str, limit: str, *, worth=None):
    """Stack the fax image's pages onto long pages for a receiver whose longest page is ``limit``
    ('a4', 'b4' or 'unlimited'): returns (original pages, packed pages), or None when it would not send
    fewer pages or ``worth(original pages, packed pages)`` says no. The source image is not changed.
    Raises pages.packing.NotPackable when these pages cannot be packed."""
    from .pages import packing
    frames = read_fax_frames(tiff_path)
    if frames is None:
        raise packing.NotPackable("The fax image is not one-bit")
    layout = packing.layout_for(frames, limit)
    if layout.pages >= len(frames) or (worth is not None and not worth(len(frames), layout.pages)):
        return None
    pages = packing.render(frames, layout)
    write_fax_tiff(pages, output_path)
    return len(frames), len(pages)


def split_received_image(tiff_path: str, output_path: str) -> Optional[int]:
    """When a received fax image carries Faxbot's page bands, write its original pages to ``output_path``
    and return how many; None when it does not (deliver it as received). The received image is unchanged."""
    from .pages import unpack
    frames = read_fax_frames(tiff_path)
    if frames is None:
        return None
    originals = unpack.split_frames(frames)
    if originals is None:
        return None
    write_fax_tiff(originals, output_path)
    return len(originals)


# The layout chooser: exactly one way to send a fax's pages ------------------------------------------------------

LAYOUTS = ("normal", "dense", "codec")


def frame_bits(frames) -> Tuple[int, ...]:
    """Compressed bits of each mode "1" page as the engines send it (Group 4, Faxbot's own fax image format)."""
    return tuple(8 * len(_g4_data(frame) or b"") for frame in frames)


def frames_resolution(frames) -> str:
    """'standard' when every page is at standard resolution (under 150 lines per inch), else 'fine'."""
    return "standard" if frames and all(
        0 < float((frame.info.get("dpi") or (0, 0))[1]) < 150 for frame in frames) else "fine"


def codec_pages(frames, *, engine, number, route, capability=None, pdf_path, seal=None, recipient=None,
                exact_raster=False, resolution=None, tools=None):
    """The experimental codec's encoded pages for this attempt, as (pages, sentence[, details]), or None.

    ``frames`` are the pages this attempt would otherwise send (the same pages ``choose_layout`` prices as
    normal); the codec encodes the fax's original document at ``pdf_path`` instead. None, quickly, unless the
    number's recipient agreed to encoded pages (``codec/send.py``); None when the codec's own check says they
    would not save on ``route`` or the document cannot be encoded. Writes nothing. A programming error is
    not hidden here: it reaches the attempt hook, which logs it and sends the pages as they are.
    """
    from .codec import CodecError
    from .codec import send as codec_send
    setting = codec_send.setting_for(engine, number, recipient)
    if setting is None or not frames:
        return None
    if resolution is None:
        resolution = "standard" if capability is not None and capability.fine is False else "fine"
    try:
        return codec_send.attempt_pages(engine, setting, frames=frames, page_bits=frame_bits(frames), number=number,
                                        route=route, pdf_path=pdf_path, seal=seal, exact_raster=exact_raster,
                                        resolution=resolution, tools=tools)
    except (CodecError, DocumentConversionError, OSError):
        import logging
        logging.getLogger(__name__).warning('Encoded pages could not be made for this attempt; it sends other pages.')
        return None


def choose_layout(frames, *, route, destination, limit, dense_allowed, codec=None, card=None,
                  boundary_seconds=None, predict=None, describe_dense=None, usable=None, measure_cache=None,
                  renderings=None, faster=None):
    """Price every way these pages may go and keep exactly one (``pages.decision.choose``).

    Candidates: ``normal`` (the pages as they are); ``dense`` (packed onto long pages, when
    ``dense_allowed`` and the receiver's ``limit`` puts fewer pages on the call); ``codec`` (``codec(frames)``'s
    encoded pages, when it returns any). Each candidate is made from the same pages, so dense pages and the codec
    never stack. ``renderings`` (pages/friendly.py) are other renderings of the same pages, {'screened' or
    'whitened': (pages, fidelity.Fidelity)}: each goes as it is or dense too, never encoded. Every candidate is
    priced with the shared predictor through ``pages.decision`` for ``route`` and ``destination``, at its own
    resolution. The cheapest expected bill wins; among candidates with the same expected bill the most faithful
    does (the pages as they are first), then fewer pages billed, less time, the simpler layout. ``faster``: a
    named reason ('administrator', 'deadline', 'capacity') that puts less time before fidelity at the same bill.

    ``usable`` (``pages.coding.Usable``, Faxbot's own engines only): each candidate's codings are measured on its
    own pages (kept in ``measure_cache`` with the attempt's files) and it is priced with the smallest coding the
    call may use, so the layout, the rendering and the coding are chosen together.

    Returns a dict: layout, pages, reason (one sentence, None for normal), seconds_saved, predictions
    {layout: Prediction} of the pages as they are, ``codec``: what ``codec()`` returned when the codec was kept,
    else None, ``coding``: the chosen candidate's ``pages.coding.CodingChoice`` (None without ``usable``),
    ``rendering``: None or the rendering kept, ``rendering_bits``: (bits of the pages as they are, bits of the kept
    rendering), both as the call codes them, and ``faster``: the named reason when it decided. ``seconds_saved`` is
    the layout's own saving, against the same rendering's normal pages.
    """
    from .pages import coding as codings
    from .pages import decision, fidelity, packing
    pieces = {("as_is", "normal"): (list(frames), None, None, fidelity.UNCHANGED.rank)}
    sources = [("as_is", list(frames), fidelity.UNCHANGED.rank)]
    for name, (pages, found) in (renderings or {}).items():
        if name not in ("screened", "whitened") or not pages or len(pages) != len(frames):
            raise ValueError("Unknown rendering")
        sources.append((name, list(pages), found.rank))
        pieces[(name, "normal")] = (list(pages), None, None, found.rank)
    if dense_allowed:
        for name, pages, faithful in sources:
            try:
                layout = packing.layout_for(pages, limit)
                if layout.pages < len(pages):
                    packed = packing.render(pages, layout)
                    reason = describe_dense(len(pages), len(packed)) if describe_dense else None
                    pieces[(name, "dense")] = (packed, reason, None, faithful)
            except packing.NotPackable:
                pass
    if codec is not None:
        encoded = codec(frames)
        if encoded and encoded[0]:
            pieces[("as_is", "codec")] = (list(encoded[0]), encoded[1], encoded, fidelity.UNCHANGED.rank)
    keys = list(pieces)
    choices, shapes = {}, {}
    for key in keys:
        pages = pieces[key][0]
        if usable is None:
            shapes[key] = decision.Shape(len(pages), frame_bits(pages), frames_resolution(pages), key[1],
                                         boundary_seconds)
            continue
        measured = (codings.measure_cached(pages, measure_cache) if measure_cache is not None
                    else codings.measure(pages))
        choice = codings.best_coding(pages, usable.codings, ecm=usable.ecm, measured=measured)
        choices[key] = choice
        # A JBIG request that could not be measured is priced at its fallback's measured size (codings.best_coding):
        # ``priced`` names the coding whose measured bits (``bits_per_page``) the predictor reads.
        shapes[key] = decision.Shape(len(pages), tuple(measured["MMR"]), frames_resolution(pages), key[1],
                                     boundary_seconds, measured=measured, coding=choice.priced)
    priced = dict(zip(keys, decision.price_all(route, destination, [shapes[key] for key in keys], card=card,
                                               predict=predict)))
    candidates = [decision.Candidate(key[0], key[1], priced[key], shapes[key], pieces[key][3],
                                     decision.bill(priced[key], card=card, route=route)) for key in keys]
    chosen, quicker = decision.choose(candidates, faster=faster)
    key = (chosen.rendering, chosen.layout)
    # The layout's saving is against the same rendering's normal pages, and the rendering's against the pages as they
    # are: each change is said once, in the coding the call is priced with.
    normal, picked = priced[(chosen.rendering, "normal")], priced[key]
    seconds = (math.floor(normal.seconds - picked.seconds)
               if normal.seconds is not None and picked.seconds is not None else None)
    rendered = None
    if chosen.rendering != "as_is":
        before = decision._bits(shapes[("as_is", "normal")])
        after = decision._bits(shapes[(chosen.rendering, "normal")])
        if before is not None and after is not None:
            rendered = (sum(before), sum(after))
    pages, reason, details, _ = pieces[key]
    return {"layout": chosen.layout, "pages": pages, "reason": reason,
            "seconds_saved": max(0, seconds) if seconds is not None else None,
            "predictions": {layout: prediction for (name, layout), prediction in priced.items() if name == "as_is"},
            "codec": details, "coding": choices.get(key),
            "rendering": None if chosen.rendering == "as_is" else chosen.rendering, "rendering_bits": rendered,
            "faster": faster if quicker else None}
