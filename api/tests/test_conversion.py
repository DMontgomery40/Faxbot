"""Regression checks inspect real document contents through public converters."""

from pathlib import Path
import shutil
import struct
import subprocess

import pytest
from PIL import Image, TiffImagePlugin
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, NumberObject
from reportlab.pdfgen import canvas

from app import conversion


@pytest.mark.parametrize("disabled", ["true", "false"])
@pytest.mark.parametrize("folder", ["documents", "latest-test-documents"])
def test_text_contents_survive_disabled_sending_and_test_paths(
    monkeypatch, tmp_path, disabled, folder
):
    # A filename/flag-selected placeholder cannot satisfy a real PDF text read.
    monkeypatch.setenv("FAX_DISABLED", disabled)
    source_dir = tmp_path / folder
    source_dir.mkdir()
    source = source_dir / "contest.txt"
    source.write_text("First line\nFinal clinical billing marker", encoding="utf-8")
    output = source_dir / "converted.pdf"

    conversion.txt_to_pdf(str(source), str(output))

    reader = PdfReader(output, strict=True)
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "First line" in text
    assert "Final clinical billing marker" in text


def test_long_line_retains_its_final_words(tmp_path):
    # The old column-120 slice loses this literal marker.
    source = tmp_path / "long.txt"
    original = "A clinical billing word " * 30 + "FINAL-LINE-MARKER"
    source.write_text(original, encoding="utf-8")
    output = tmp_path / "long.pdf"

    conversion.txt_to_pdf(str(source), str(output))

    text = "".join(page.extract_text() for page in PdfReader(output).pages)
    assert text.replace("\n", "") == original


def test_text_paginates_without_losing_final_line(tmp_path):
    # 130 short lines need three letter pages at the documented 12-point leading.
    source = tmp_path / "pages.txt"
    lines = [f"Clinical line {index:03d}" for index in range(130)]
    source.write_text("\n".join(lines), encoding="utf-8")
    output = tmp_path / "pages.pdf"

    conversion.txt_to_pdf(str(source), str(output))

    reader = PdfReader(output, strict=True)
    assert len(reader.pages) == 3
    assert "".join(page.extract_text() for page in reader.pages).splitlines() == lines


def test_supported_accented_text_remains_extractable(tmp_path):
    source = tmp_path / "accents.txt"
    original = "Café résumé déjà vu Ångström — £25 €30"
    source.write_text(original, encoding="utf-8")
    output = tmp_path / "accents.pdf"

    conversion.txt_to_pdf(str(source), str(output))

    reader = PdfReader(output, strict=True)
    assert reader.pages[0].extract_text().strip() == original
    fonts = reader.pages[0]["/Resources"]["/Font"].get_object().values()
    assert any(
        "/FontFile2" in font.get_object()["/FontDescriptor"].get_object()
        for font in fonts
        if "/FontDescriptor" in font.get_object()
    )


@pytest.mark.parametrize("data", [
    b"bad\xff UTF-8", "Unsupported 😀".encode(), b"hidden\x00text", b"",
    "soft\u00adhyphen".encode(), "e\u0301".encode(), "Arabic العربية".encode(),
])
def test_invalid_text_fails_without_replacing_existing_document(tmp_path, data):
    # Silent decoding/glyph replacement would drop source content without a failure.
    source = tmp_path / "invalid.txt"
    source.write_bytes(data)
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"existing accepted document")

    with pytest.raises(conversion.DocumentConversionError):
        conversion.txt_to_pdf(str(source), str(output))

    assert output.read_bytes() == b"existing accepted document"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.pdf", "invalid.txt"]


def test_text_expands_tabs_and_accepts_standard_line_breaks(tmp_path):
    source = tmp_path / "tabs.txt"
    source.write_bytes(b"Name\tValue\r\nSecond line\rFinal line")
    output = tmp_path / "tabs.pdf"

    conversion.txt_to_pdf(str(source), str(output))

    assert PdfReader(output).pages[0].extract_text().splitlines() == [
        "Name    Value", "Second line", "Final line"
    ]


def make_pdf(path, *, pages=2):
    """Independent 100-point square pages alternate black-box positions."""
    document = canvas.Canvas(str(path), pagesize=(100, 100))
    for index in range(pages):
        document.setFillColorRGB(0, 0, 0)
        document.rect(10 if index == 0 else 60, 10, 30, 30, fill=1, stroke=0)
        document.showPage()
    document.save()


def test_pdf_page_count_uses_real_structure_without_ghostscript(monkeypatch, tmp_path):
    source = tmp_path / "two-pages.pdf"
    make_pdf(source)
    monkeypatch.setattr(conversion.shutil, "which", lambda command: None)

    assert conversion.count_pdf_pages(str(source)) == 2
    assert conversion.validate_pdf(str(source)) == 2


@pytest.mark.parametrize("kind", ["header", "garbage", "zero-page", "encrypted", "bad-contents", "bad-resources", "unterminated-content"])
def test_invalid_pdf_does_not_invent_a_page_count(tmp_path, kind):
    source = tmp_path / "invalid.pdf"
    if kind in {"header", "garbage"}:
        source.write_bytes(b"%PDF-1.4\ntest\n%%EOF" if kind == "header" else b"not a PDF")
    else:
        writer = PdfWriter()
        if kind != "zero-page":
            page = writer.add_blank_page(width=100, height=100)
            if kind == "bad-contents":
                page[NameObject("/Contents")] = NumberObject(17)
            elif kind == "bad-resources":
                page[NameObject("/Resources")] = NumberObject(17)
            elif kind == "unterminated-content":
                content = DecodedStreamObject()
                content.set_data(b"BT (unclosed clinical text")
                page[NameObject("/Contents")] = writer._add_object(content)
            elif kind == "encrypted":
                writer.encrypt("private-password")
        writer.write(source)

    with pytest.raises(conversion.DocumentConversionError) as error:
        conversion.validate_pdf(str(source))

    assert str(tmp_path) not in str(error.value)
    assert conversion.count_pdf_pages(str(source)) is None


def test_multipage_tiff_preserves_image_pixels_and_order(tmp_path):
    source = tmp_path / "contest.tiff"
    first = Image.new("RGB", (20, 10), (200, 40, 20))
    second = Image.new("RGB", (20, 10), (20, 80, 180))
    first.save(source, save_all=True, append_images=[second], compression="tiff_deflate")
    output = tmp_path / "converted.pdf"

    assert conversion.tiff_to_pdf(str(source), str(output)) == (2, str(output))

    reader = PdfReader(output, strict=True)
    assert len(reader.pages) == 2
    assert [list(page.images)[0].image.getpixel((10, 5)) for page in reader.pages] == [
        (200, 40, 20), (20, 80, 180)
    ]
    assert [list(page.images)[0].image.size for page in reader.pages] == [(20, 10), (20, 10)]


@pytest.mark.parametrize("data", [b"TIFF_PLACEHOLDER", b"II*\x00truncated", b""])
def test_corrupt_tiff_fails_without_output(tmp_path, data):
    source = tmp_path / "corrupt.tiff"
    source.write_bytes(data)
    output = tmp_path / "converted.pdf"

    with pytest.raises(conversion.DocumentConversionError):
        conversion.tiff_to_pdf(str(source), str(output))

    assert not output.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_pillow_decompression_bomb_protection_remains_active(monkeypatch, tmp_path):
    source = tmp_path / "bomb.tiff"
    Image.new("RGB", (20, 10), "white").save(source)
    output = tmp_path / "converted.pdf"
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(conversion.DocumentConversionError):
        conversion.tiff_to_pdf(str(source), str(output))

    assert not output.exists()
    assert Image.MAX_IMAGE_PIXELS == 100


@pytest.mark.skipif(shutil.which("gs") is None, reason="real Ghostscript not installed")
@pytest.mark.parametrize("disabled", ["true", "false"])
def test_real_ghostscript_preserves_two_pdf_pages(monkeypatch, tmp_path, disabled):
    monkeypatch.setenv("FAX_DISABLED", disabled)
    source = tmp_path / "contest.pdf"
    make_pdf(source)
    output = tmp_path / "converted.tiff"

    assert conversion.pdf_to_tiff(str(source), str(output)) == (2, str(output))

    with Image.open(output) as image:
        assert image.format == "TIFF"
        assert image.n_frames == 2
        image.seek(0)
        assert image.getpixel((57, 218)) == 0  # first page's lower-left box
        assert image.getpixel((198, 218)) != 0
        image.seek(1)
        assert image.getpixel((57, 218)) != 0
        assert image.getpixel((198, 218)) == 0  # second page's lower-right box


@pytest.mark.parametrize("failure", ["missing", "exit", "timeout", "partial", "invalid-output"])
def test_rasterization_failure_is_sanitized_and_atomic(monkeypatch, tmp_path, failure):
    source = tmp_path / "private-input.pdf"
    make_pdf(source)
    output = tmp_path / "existing.tiff"
    output.write_bytes(b"existing accepted document")
    monkeypatch.setattr(conversion.shutil, "which", lambda command: None if failure == "missing" else "/private/gs")

    def run_process(arguments, **kwargs):
        temporary = next(item.split("=", 1)[1] for item in arguments if item.startswith("-sOutputFile="))
        assert "-dSAFER" in arguments
        assert kwargs["timeout"] > 0
        assert kwargs.get("shell", False) is False
        if failure == "partial":
            Image.new("1", (20, 10), 1).save(temporary, format="TIFF")
            return subprocess.CompletedProcess(arguments, 0)
        Path(temporary).write_bytes(b"broken partial output")
        if failure == "exit":
            raise subprocess.CalledProcessError(1, arguments, stderr=b"/private/secret-path")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(conversion.subprocess, "run", run_process)
    with pytest.raises(conversion.DocumentConversionError) as error:
        conversion.pdf_to_tiff(str(source), str(output))

    assert error.value.operational is True
    assert "/private" not in str(error.value)
    assert str(tmp_path) not in str(error.value)
    assert output.read_bytes() == b"existing accepted document"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.tiff", "private-input.pdf"]


def test_oversized_pdf_page_is_rejected_before_rasterization(monkeypatch, tmp_path):
    source = tmp_path / "enormous.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=10000, height=10000)
    writer.write(source)
    output = tmp_path / "converted.tiff"

    def unexpected_process(*args, **kwargs):
        pytest.fail("Oversized geometry must fail before starting Ghostscript")

    monkeypatch.setattr(conversion.subprocess, "run", unexpected_process)
    with pytest.raises(conversion.DocumentConversionError):
        conversion.pdf_to_tiff(str(source), str(output))
    assert not output.exists()


def test_missing_bundled_font_fails_without_exposing_host_details(monkeypatch, tmp_path):
    source = tmp_path / "text.txt"
    source.write_text("Clinical text", encoding="utf-8")
    output = tmp_path / "converted.pdf"

    def missing_font(*args, **kwargs):
        raise OSError("/private/font/package/path")

    conversion._text_font.cache_clear()
    monkeypatch.setattr(conversion, "TTFont", missing_font)
    with pytest.raises(conversion.DocumentConversionError) as error:
        conversion.txt_to_pdf(str(source), str(output))

    assert error.value.operational is True
    assert "/private" not in str(error.value)
    assert not output.exists()


def test_oversized_input_is_rejected_without_loading_text(tmp_path):
    source = tmp_path / "large.txt"
    with source.open("wb") as stream:
        stream.seek(32 * 1024 * 1024)
        stream.write(b"A")
    output = tmp_path / "converted.pdf"

    with pytest.raises(conversion.DocumentConversionError):
        conversion.txt_to_pdf(str(source), str(output))

    assert not output.exists()


def test_tiff_pixel_limit_is_checked_before_decoding(monkeypatch, tmp_path):
    source = tmp_path / "large-header.tiff"
    Image.new("RGB", (20, 10), "white").save(source)
    data = bytearray(source.read_bytes())
    # The bounded fixture claims 36M pixels without allocating those pixels.
    endian = "<" if data[:2] == b"II" else ">"
    offset = struct.unpack_from(endian + "I", data, 4)[0]
    entries = struct.unpack_from(endian + "H", data, offset)[0]
    for index in range(entries):
        entry = offset + 2 + index * 12
        tag = struct.unpack_from(endian + "H", data, entry)[0]
        if tag in {256, 257}:
            struct.pack_into(endian + "I", data, entry + 8, 6000)
    source.write_bytes(data)
    output = tmp_path / "converted.pdf"

    def unexpected_decode(*args, **kwargs):
        pytest.fail("Oversized TIFF must fail before decoding its pixels")

    monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "load", unexpected_decode)
    with pytest.raises(conversion.DocumentConversionError):
        conversion.tiff_to_pdf(str(source), str(output))
    assert not output.exists()


def test_total_raster_limit_is_checked_before_starting_ghostscript(monkeypatch, tmp_path):
    source = tmp_path / "many-pages.pdf"
    writer = PdfWriter()
    for _ in range(30):
        writer.add_blank_page(width=612, height=792)
    writer.write(source)
    output = tmp_path / "converted.tiff"

    def unexpected_process(*args, **kwargs):
        pytest.fail("Excessive total raster pixels must fail before starting Ghostscript")

    monkeypatch.setattr(conversion.subprocess, "run", unexpected_process)
    with pytest.raises(conversion.DocumentConversionError):
        conversion.pdf_to_tiff(str(source), str(output))
    assert not output.exists()


def test_pdf_user_unit_is_included_in_raster_limit(monkeypatch, tmp_path):
    source = tmp_path / "scaled-page.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    page[NameObject("/UserUnit")] = NumberObject(100)
    writer.write(source)
    output = tmp_path / "converted.tiff"

    def unexpected_process(*args, **kwargs):
        pytest.fail("Physical PDF size must be checked before starting Ghostscript")

    monkeypatch.setattr(conversion.subprocess, "run", unexpected_process)
    with pytest.raises(conversion.DocumentConversionError):
        conversion.pdf_to_tiff(str(source), str(output))
    assert not output.exists()


def test_generated_output_limit_does_not_publish_oversized_pdf(monkeypatch, tmp_path):
    source = tmp_path / "text.txt"
    source.write_text("Clinical text", encoding="utf-8")
    output = tmp_path / "converted.pdf"
    monkeypatch.setattr(conversion, "MAX_OUTPUT_BYTES", 100)

    with pytest.raises(conversion.DocumentConversionError):
        conversion.txt_to_pdf(str(source), str(output))

    assert not output.exists()
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("pages, operational", [(26, True), (27, False)])
def test_letter_fax_raster_budget_supports_26_pages(monkeypatch, tmp_path, pages, operational):
    source = tmp_path / "letter-pages.pdf"
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    writer.write(source)
    output = tmp_path / "converted.tiff"
    monkeypatch.setattr(conversion.shutil, "which", lambda command: None)

    with pytest.raises(conversion.DocumentConversionError) as error:
        conversion.pdf_to_tiff(str(source), str(output))

    # 26 pages reach the unavailable tool; page 27 exceeds the input raster budget.
    assert error.value.operational is operational
    assert not output.exists()
