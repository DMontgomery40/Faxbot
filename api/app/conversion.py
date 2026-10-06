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
        yield data[offsets[0]:offsets[0] + counts[0]], "CCITTFaxDecode", {
            "K": -1, "Columns": width, "Rows": height, "BlackIs1": b"false"}


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


def pdf_to_tiff(pdf_path: str, tiff_path: str) -> Tuple[int, str]:
    """Rasterize a validated PDF to real Group 4 fax TIFF at 204 by 196 DPI (mode FAX_IMAGE_MODE)."""
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
            os.chmod(temporary, FAX_IMAGE_MODE)
        except Exception:
            raise DocumentConversionError("PDF rasterization failed.", operational=True) from None
    return pages, tiff_path
