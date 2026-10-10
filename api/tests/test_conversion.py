"""Regression checks inspect real document contents through public converters."""

import math
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


def _text_pdf(tmp_path, name, data):
    source = tmp_path / (name + ".txt")
    source.write_bytes(data)
    output = tmp_path / (name + ".pdf")
    conversion.txt_to_pdf(str(source), str(output))
    return PdfReader(output, strict=True)


def _drawn_lines(reader):
    """Each positioned text row, page by page; a blank row is drawn as ''."""
    from pypdf.generic import ContentStream
    pages = []
    for page in reader.pages:
        rows, text, positioned = [], "", False
        for operands, operator in ContentStream(page.get_contents(), reader).operations:
            if operator == b"BT":
                text, positioned = "", False
            elif operator == b"Tm":
                positioned = True
            elif operator == b"Tj":
                text += operands[0]
            elif operator == b"ET" and positioned:
                rows.append(text)
        pages.append(rows)
    return pages


def _page_streams(reader):
    return [page.get_contents().get_data() for page in reader.pages]


LINE_BREAKS = {"lf": b"\n", "crlf": b"\r\n", "cr": b"\r"}


@pytest.mark.parametrize("newline", sorted(LINE_BREAKS))
@pytest.mark.parametrize("count, pages", [(57, 1), (58, 1), (59, 2), (116, 2), (117, 3)])
def test_terminal_line_break_ends_the_last_line_without_a_blank_page(tmp_path, newline, count, pages):
    # 58 lines fill a letter page at 12-point leading inside the 54-point margins.
    lines = [f"Clinical line {index:03d}".encode() for index in range(count)]
    body = LINE_BREAKS[newline].join(lines)
    plain = _text_pdf(tmp_path, "plain", body)
    terminated = _text_pdf(tmp_path, "terminated", body + LINE_BREAKS[newline])
    assert len(plain.pages) == len(terminated.pages) == pages
    # Content pages are drawn identically; the terminator adds nothing.
    assert _page_streams(terminated) == _page_streams(plain)
    assert sum(_drawn_lines(terminated), []) == [line.decode() for line in lines]


def test_wrapped_final_line_with_terminal_line_break_fills_the_page_exactly(tmp_path):
    # 57 short lines plus one long line that wraps onto the 58th row: one page.
    long_line = "A clinical billing word " * 4
    lines = [f"Line {index:02d}" for index in range(56)] + [long_line * 2]
    plain = _text_pdf(tmp_path, "plain", "\n".join(lines).encode())
    terminated = _text_pdf(tmp_path, "terminated", ("\n".join(lines) + "\r\n").encode())
    drawn = _drawn_lines(plain)
    assert len(drawn) == 1 and len(drawn[0]) == 58  # the long line wrapped to two rows
    assert _page_streams(terminated) == _page_streams(plain)


@pytest.mark.parametrize("newline", sorted(LINE_BREAKS))
def test_intentional_blank_lines_are_kept(tmp_path, newline):
    sep = LINE_BREAKS[newline]
    reader = _text_pdf(tmp_path, "blank", b"First" + sep + sep + b"Third" + sep + sep)
    # The internal blank line and the trailing blank line are content; only the
    # last line break is a terminator.
    assert _drawn_lines(reader) == [["First", "", "Third", ""]]
    full = [f"Line {index:02d}".encode() for index in range(58)]
    spilled = _text_pdf(tmp_path, "spilled", sep.join(full) + sep + sep)
    assert len(spilled.pages) == 2 and _drawn_lines(spilled)[1] == [""]


def test_only_line_breaks_is_still_empty(tmp_path):
    for data in (b"\n", b"\r\n", b"  \n"):
        source = tmp_path / "empty.txt"
        source.write_bytes(data)
        with pytest.raises(conversion.DocumentConversionError, match="empty"):
            conversion.txt_to_pdf(str(source), str(tmp_path / "empty.pdf"))


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


@pytest.mark.parametrize("mode", ["I;16", "I;16L", "I;16B", "I", "F"])
def test_high_depth_tiff_is_rejected_without_replacing_output(tmp_path, mode):
    # ImageReader clips integers above 255 and quantizes float samples to RGB.
    source = tmp_path / "high-depth.tiff"
    image = Image.new(mode, (4, 1))
    image.putdata([0.0, 0.25, 0.5, 1.0] if mode == "F" else [0, 1000, 32000, 65535])
    image.save(source, format="TIFF")
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"existing accepted document")

    with pytest.raises(conversion.DocumentConversionError) as error:
        conversion.tiff_to_pdf(str(source), str(output))

    assert error.value.operational is False
    assert str(tmp_path) not in str(error.value)
    assert output.read_bytes() == b"existing accepted document"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.pdf", "high-depth.tiff"]


def test_high_depth_rgb_tiff_is_rejected_before_decoding(monkeypatch, tmp_path):
    # A real 16-bit RGB TIFF appears as mode RGB before Pillow reduces its samples.
    source = tmp_path / "rgb16.tiff"
    tags = [
        (256, 4, 1, 4), (257, 4, 1, 1), (258, 3, 3, 134),
        (259, 3, 1, 1), (262, 3, 1, 2), (273, 4, 1, 140),
        (277, 3, 1, 3), (278, 4, 1, 1), (279, 4, 1, 24), (284, 3, 1, 1),
    ]
    data = b"II" + struct.pack("<HIH", 42, 8, len(tags))
    data += b"".join(struct.pack("<HHII", *tag) for tag in tags)
    data += struct.pack("<I3H12H", 0, 16, 16, 16, 0, 1000, 32000, 65535, 32000, 1000, 1, 2, 3, 4, 5, 6)
    source.write_bytes(data)
    with Image.open(source) as image:
        assert image.mode == "RGB"
        assert image.tag_v2[258] == (16, 16, 16)
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"existing accepted document")

    def unexpected_decode(*args, **kwargs):
        pytest.fail("Unsupported sample depth must fail before pixel decoding")

    monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "load", unexpected_decode)
    with pytest.raises(conversion.DocumentConversionError) as error:
        conversion.tiff_to_pdf(str(source), str(output))

    assert error.value.operational is False
    assert str(tmp_path) not in str(error.value)
    assert output.read_bytes() == b"existing accepted document"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.pdf", "rgb16.tiff"]


@pytest.mark.parametrize("mode, samples, expected_mode, expected", [
    ("1", [0, 255, 0, 255], "1", [0, 255, 0, 255]),
    ("L", [0, 32, 128, 255], "L", [0, 32, 128, 255]),
    ("RGB", [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)], "RGB", [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]),
    ("CMYK", [(16, 32, 64, 128)] * 4, "CMYK", [(16, 32, 64, 128)] * 4),
    ("RGBA", [(10, 20, 30, 64)] * 4, "RGBA", [(10, 20, 30, 64)] * 4),
    ("LA", [(128, 64)] * 4, "LA", [(128, 64)] * 4),
    ("P", [0, 1, 2, 3], "RGB", [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]),
])
def test_supported_tiff_modes_preserve_literal_pixel_samples(
    tmp_path, mode, samples, expected_mode, expected
):
    source = tmp_path / "supported.tiff"
    image = Image.new(mode, (4, 1))
    image.putdata(samples)
    if mode == "P":
        image.putpalette([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120] + [0] * 756)
    image.save(source, format="TIFF")
    output = tmp_path / "converted.pdf"

    assert conversion.tiff_to_pdf(str(source), str(output)) == (1, str(output))

    converted = list(PdfReader(output).pages[0].images)[0].image
    assert converted.mode == expected_mode
    assert [converted.getpixel((index, 0)) for index in range(4)] == expected


def bilevel_pattern(width=96, height=40, seed=7):
    """An asymmetric one-bit page: a mirrored, flipped or inverted copy differs."""
    import random
    image = Image.new("1", (width, height), 1)
    for x in range(width // 3):
        image.putpixel((x, 0), 0)  # top edge from the left
    for y in range(height // 2):
        image.putpixel((0, y), 0)  # left edge from the top
    for x in range(width - 12, width - 4):
        for y in range(height - 9, height - 3):
            image.putpixel((x, y), 0)  # block near the lower right
    noise = random.Random(seed)
    for _ in range(width * height // 20):
        image.putpixel((noise.randrange(8, width - 16), noise.randrange(4, height - 12)), 0)
    return image


def _g4_strip(image):
    """libtiff's Group 4 strip for an image stored BlackIsZero (Pillow's mode 1 form)."""
    from io import BytesIO
    encoded = BytesIO()
    image.save(encoded, "TIFF", compression="group4",
               strip_size=((image.width + 7) // 8) * image.height)
    encoded.seek(0)
    with Image.open(encoded) as written:
        assert written.tag_v2[262] == 1 and len(written.tag_v2[273]) == 1
        offset, count = written.tag_v2[273][0], written.tag_v2[279][0]
    return encoded.getvalue()[offset:offset + count]


def _inverted(image):
    return Image.frombytes("1", image.size, bytes(255 - value for value in image.tobytes()))


def write_bilevel_tiff(path, pages, *, photometric, compression, dpi=(204, 98)):
    """Write one-bit pages with explicit photometric and compression tags.

    Fax TIFFs are usually WhiteIsZero; Pillow can only write BlackIsZero, so
    WhiteIsZero pages store the inverted bits (or the G4 code of the inverted
    image) under photometric 0, which decodes back to the same picture.
    """
    strips = []
    for page in pages:
        stored = _inverted(page) if photometric == 0 else page
        strips.append(_g4_strip(stored) if compression == 4 else stored.tobytes())
    data = bytearray(b"II*\x00\x00\x00\x00\x00")
    previous_link = 4
    for page, strip in zip(pages, strips):
        strip_offset = len(data)
        data += strip
        if len(data) % 2:
            data += b"\x00"
        rational_offset = len(data)
        data += struct.pack("<IIII", int(dpi[0]), 1, int(dpi[1]), 1)
        tags = [(256, 4, 1, page.width), (257, 4, 1, page.height), (258, 3, 1, 1),
                (259, 3, 1, compression), (262, 3, 1, photometric), (273, 4, 1, strip_offset),
                (277, 3, 1, 1), (278, 4, 1, page.height), (279, 4, 1, len(strip)),
                (282, 5, 1, rational_offset), (283, 5, 1, rational_offset + 8), (296, 3, 1, 2)]
        ifd = len(data)
        struct.pack_into("<I", data, previous_link, ifd)
        data += struct.pack("<H", len(tags))
        data += b"".join(struct.pack("<HHII", *tag) for tag in tags)
        previous_link = len(data)
        data += b"\x00\x00\x00\x00"
    path.write_bytes(bytes(data))
    with Image.open(path) as written:  # the fixture itself decodes to the intended pages
        for index, page in enumerate(pages):
            written.seek(index)
            assert written.mode == "1" and written.tobytes() == page.tobytes()


def _page_image(reader, page):
    xobjects = page["/Resources"]["/XObject"].get_object()
    assert len(xobjects) == 1
    return next(iter(xobjects.values())).get_object()


def force_bilevel_encoder(monkeypatch, name):
    monkeypatch.setattr(conversion, "_bilevel_stream", lambda frame: next(
        candidate for candidate in conversion._bilevel_candidates(frame) if candidate[1] == name))


def _ghostscript_render(pdf, out, dpi):
    subprocess.run(["gs", "-q", "-dSAFER", "-dNOPAUSE", "-dBATCH", "-dNOINTERPOLATE",
                    "-sDEVICE=pnggray", f"-r{dpi[0]}x{dpi[1]}", f"-sOutputFile={out}", str(pdf)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.parametrize("encoder", ["g4", "flate"])
@pytest.mark.parametrize("compression", [1, 4])
@pytest.mark.parametrize("photometric", [0, 1])
def test_one_bit_tiff_stays_one_bit_with_exact_pixels_and_geometry(
    monkeypatch, tmp_path, encoder, compression, photometric
):
    force_bilevel_encoder(monkeypatch, {"g4": "CCITTFaxDecode", "flate": "FlateDecode"}[encoder])
    pattern = bilevel_pattern()
    source = tmp_path / "fax.tiff"
    write_bilevel_tiff(source, [pattern], photometric=photometric, compression=compression)
    output = tmp_path / "fax.pdf"

    assert conversion.tiff_to_pdf(str(source), str(output)) == (1, str(output))

    reader = PdfReader(output, strict=True)
    page = reader.pages[0]
    image = _page_image(reader, page)
    assert image["/BitsPerComponent"] == 1 and image["/ColorSpace"] == "/DeviceGray"
    assert (image["/Width"], image["/Height"]) == pattern.size
    if encoder == "flate":
        assert image["/Filter"] == ["/FlateDecode"]
        assert image.get_data() == pattern.tobytes()  # packed rows, 1 = white
    else:
        assert image["/Filter"] == ["/CCITTFaxDecode"]
        parameters = image["/DecodeParms"][0]
        assert (parameters["/K"], parameters["/Columns"], parameters["/Rows"]) == (-1, *pattern.size)
        assert list(page.images)[0].image.tobytes() == pattern.tobytes()
    # Physical size follows the TIFF resolution: 96 x 40 pixels at 204 x 98 DPI.
    width, height = 96 * 72 / 204, 40 * 72 / 98
    assert [float(value) for value in page.mediabox] == pytest.approx([0, 0, width, height], abs=1e-4)
    from pypdf.generic import ContentStream
    operations = ContentStream(page.get_contents(), reader).operations
    drawn = [index for index, (_, operator) in enumerate(operations) if operator == b"Do"]
    assert len(drawn) == 1
    matrix = [float(value) for value in operations[drawn[0] - 1][0]]
    assert operations[drawn[0] - 1][1] == b"cm"
    assert matrix == pytest.approx([width, 0, 0, height, 0, 0], abs=1e-4)  # upright, unmirrored, full page


@pytest.mark.skipif(shutil.which("gs") is None, reason="real Ghostscript not installed")
@pytest.mark.parametrize("encoder", ["g4", "flate"])
@pytest.mark.parametrize("photometric", [0, 1])
def test_ghostscript_renders_one_bit_pages_pixel_for_pixel(monkeypatch, tmp_path, encoder, photometric):
    # An independent renderer confirms black stays black and nothing is flipped.
    force_bilevel_encoder(monkeypatch, {"g4": "CCITTFaxDecode", "flate": "FlateDecode"}[encoder])
    pages = [bilevel_pattern(seed=1), _inverted(bilevel_pattern(seed=2)).transpose(Image.Transpose.ROTATE_180)]
    source = tmp_path / "fax.tiff"
    write_bilevel_tiff(source, pages, photometric=photometric, compression=4)
    output = tmp_path / "fax.pdf"
    assert conversion.tiff_to_pdf(str(source), str(output)) == (2, str(output))
    for index, expected in enumerate(pages):
        single = tmp_path / f"page-{index}.pdf"
        writer = PdfWriter()
        writer.add_page(PdfReader(output).pages[index])
        writer.write(single)
        rendered_path = tmp_path / f"page-{index}.png"
        _ghostscript_render(single, rendered_path, (204, 98))
        with Image.open(rendered_path) as rendered:
            assert rendered.size == expected.size
            assert rendered.convert("1", dither=Image.Dither.NONE).tobytes() == expected.tobytes()
    # The second page is mostly black: polarity is carried per pixel, not assumed.
    black = [page.histogram()[0] / (page.width * page.height) for page in pages]
    assert black[0] < 0.5 < black[1]


def test_multipage_mixed_tiff_keeps_page_order_modes_and_sizes(tmp_path):
    first, third = bilevel_pattern(seed=3), bilevel_pattern(width=64, height=50, seed=4)
    gray = Image.new("L", (30, 20), 0)
    gray.putdata([value % 256 for value in range(600)])
    pieces = tmp_path / "pieces"
    pieces.mkdir()
    write_bilevel_tiff(pieces / "a.tiff", [first], photometric=0, compression=4)
    write_bilevel_tiff(pieces / "c.tiff", [third], photometric=1, compression=4, dpi=(204, 196))
    with Image.open(pieces / "a.tiff") as a, Image.open(pieces / "c.tiff") as c:
        a.load()
        c.load()
        source = tmp_path / "mixed.tiff"
        a.save(source, save_all=True, append_images=[gray, c], compression="tiff_deflate",
               dpi=(204, 98))
    output = tmp_path / "mixed.pdf"

    assert conversion.tiff_to_pdf(str(source), str(output)) == (3, str(output))

    reader = PdfReader(output, strict=True)
    images = [_page_image(reader, page) for page in reader.pages]
    assert [image["/BitsPerComponent"] for image in images] == [1, 8, 1]
    assert [image["/ColorSpace"] for image in images] == ["/DeviceGray"] * 3
    decoded = [list(page.images)[0].image for page in reader.pages]
    assert decoded[0].tobytes() == first.tobytes() and decoded[2].tobytes() == third.tobytes()
    assert decoded[1].mode == "L" and decoded[1].tobytes() == gray.tobytes()


def test_one_bit_pages_use_the_smaller_lossless_encoding(monkeypatch, tmp_path):
    import random
    from PIL import ImageDraw
    sparse = Image.new("1", (400, 300), 1)
    draw = ImageDraw.Draw(sparse)  # curved line art: Group 4 codes slowly moving edges compactly
    draw.ellipse((20, 20, 380, 280), outline=0, width=3)
    draw.ellipse((100, 75, 200, 150), fill=0)
    draw.polygon([(10, 290), (200, 30), (390, 260)], outline=0, width=2)
    dense = Image.new("1", (400, 300), 1)
    noise = random.Random(9)
    for _ in range(12000):
        dense.putpixel((noise.randrange(400), noise.randrange(300)), 0)
    source = tmp_path / "pages.tiff"
    write_bilevel_tiff(source, [sparse, dense], photometric=0, compression=4)
    output = tmp_path / "pages.pdf"
    assert conversion.tiff_to_pdf(str(source), str(output)) == (2, str(output))
    reader = PdfReader(output, strict=True)
    chosen = []
    for page, original in zip(reader.pages, (sparse, dense)):
        image = _page_image(reader, page)
        sizes = {name: len(data) for data, name, _ in conversion._bilevel_candidates(original)}
        assert image["/Filter"] == ["/" + min(sizes, key=sizes.get)]
        chosen.append(image["/Filter"][0])
        assert list(page.images)[0].image.tobytes() == original.tobytes()
    assert chosen == ["/CCITTFaxDecode", "/FlateDecode"]  # line art, then dense noise
    monkeypatch.setattr(conversion.features, "check", lambda feature: False)  # no libtiff: Flate only
    assert [name for _, name, _ in conversion._bilevel_candidates(sparse)] == ["FlateDecode"]


def test_identical_one_bit_pages_are_each_drawn(tmp_path):
    page = bilevel_pattern(seed=5)
    source = tmp_path / "repeat.tiff"
    write_bilevel_tiff(source, [page, page, page], photometric=0, compression=4)
    output = tmp_path / "repeat.pdf"
    assert conversion.tiff_to_pdf(str(source), str(output)) == (3, str(output))
    reader = PdfReader(output, strict=True)
    assert all(list(p.images)[0].image.tobytes() == page.tobytes() for p in reader.pages)


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
    # A fax image Asterisk sends: readable by the data folder's group (Asterisk's own user), no one else.
    assert oct(output.stat().st_mode & 0o777) == "0o640"

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
    # 120 pages, each just under the per-page limit (5,001 x 4,805 pixels at fine resolution), are far more than
    # the page limit's worth of fax pages.
    source = tmp_path / "many-pages.pdf"
    writer = PdfWriter()
    for _ in range(120):
        writer.add_blank_page(width=1765, height=1765)
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


@pytest.mark.parametrize("pages, operational", [(26, True), (27, True), (100, True), (500, True), (501, False)])
def test_letter_fax_raster_budget_supports_the_page_limit(monkeypatch, tmp_path, pages, operational):
    source = tmp_path / "letter-pages.pdf"
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    writer.write(source)
    output = tmp_path / "converted.tiff"
    monkeypatch.setattr(conversion.shutil, "which", lambda command: None)

    with pytest.raises(conversion.DocumentConversionError) as error:
        conversion.pdf_to_tiff(str(source), str(output))

    # Up to the page limit, Letter pages reach the (here unavailable) tool; page 501 exceeds the page limit. Until
    # 10 October 2026 the raster total refused page 27.
    assert error.value.operational is operational
    assert not output.exists()


def _letter_pdf(path, pages):
    """A synthetic Letter document of ``pages`` pages, each with its own text, as a long medical record would be."""
    from reportlab.lib.pagesizes import letter
    document = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    for number in range(pages):
        document.setFont("Helvetica-Bold", 16)
        document.drawString(72, 740, f"SYNTHETIC RECORD PAGE {number + 1} OF {pages}")
        document.setFont("Helvetica", 10)
        for line in range(40):
            document.drawString(72, 710 - line * 14, f"Line {line + 1} of page {number + 1}: synthetic text only.")
        document.showPage()
    document.save()


def test_the_raster_total_admits_the_page_limits_worth_of_fax_pages():
    letter_fine = math.ceil(612 * 204 / 72) * math.ceil(792 * 196 / 72)
    legal_fine = math.ceil(612 * 204 / 72) * math.ceil(1008 * 196 / 72)
    assert conversion.MAX_DOCUMENT_PAGES * legal_fine <= conversion.MAX_RASTER_TOTAL_PIXELS
    assert letter_fine < legal_fine < conversion.FAX_PAGE_PIXELS < conversion.MAX_RASTER_PAGE_PIXELS
    assert conversion.ghostscript_timeout(0) == conversion.GHOSTSCRIPT_TIMEOUT_SECONDS
    assert conversion.ghostscript_timeout(500) > conversion.ghostscript_timeout(100) > 120


@pytest.mark.skipif(shutil.which("gs") is None, reason="real Ghostscript not installed")
def test_a_100_page_letter_pdf_is_accepted_and_converted(tmp_path):
    """Medical records of 30 to 100+ pages are common; 32 Letter pages were refused before (2026-10-10)."""
    source = tmp_path / "record.pdf"
    _letter_pdf(source, 100)
    assert conversion.validate_pdf(str(source)) == 100
    output = tmp_path / "record.tiff"
    assert conversion.pdf_to_tiff(str(source), str(output)) == (100, str(output))
    frames = conversion.read_fax_frames(str(output))
    assert len(frames) == 100 and all(frame.size == (1728, 2156) or frame.size[1] == 2156 for frame in frames)
    assert frames[0].tobytes() != frames[99].tobytes()  # every page drawn, not one page repeated
    # And back to a PDF through the TIFF path, which checks the same totals.
    back = tmp_path / "record-back.pdf"
    assert conversion.tiff_to_pdf(str(output), str(back)) == (100, str(back))


def test_the_ghostscript_timeout_grows_with_the_page_count(monkeypatch, tmp_path):
    source = tmp_path / "record.pdf"
    writer = PdfWriter()
    for _ in range(60):
        writer.add_blank_page(width=612, height=792)
    writer.write(source)
    seen = []

    def run_process(arguments, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

    monkeypatch.setattr(conversion.shutil, "which", lambda command: "/synthetic/gs")
    monkeypatch.setattr(conversion.subprocess, "run", run_process)
    with pytest.raises(conversion.DocumentConversionError):
        conversion.pdf_to_tiff(str(source), str(tmp_path / "record.tiff"))
    assert seen == [conversion.ghostscript_timeout(60)]


def test_a_single_oversized_page_is_still_refused_on_both_paths(monkeypatch, tmp_path):
    """One page over the per-page limit is refused before any drawing."""
    source = tmp_path / "one-huge-page.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=2600, height=2600)  # 7,367 x 7,078 pixels at fine resolution
    writer.write(source)

    def unexpected_process(*args, **kwargs):
        pytest.fail("An oversized page must fail before starting Ghostscript")

    monkeypatch.setattr(conversion.subprocess, "run", unexpected_process)
    with pytest.raises(conversion.DocumentConversionError, match="raster limits"):
        conversion.pdf_to_tiff(str(source), str(tmp_path / "out.tiff"))


def test_a_page_over_the_per_page_limit_is_refused_and_a_legal_scan_at_400_dpi_is_not(monkeypatch, tmp_path):
    """The per-page limit is about four times a Legal page at fine resolution (20 million pixels)."""
    assert conversion.MAX_RASTER_PAGE_PIXELS == 20_000_000
    legal_fine = math.ceil(612 * 204 / 72) * math.ceil(1008 * 196 / 72)
    assert 4 * legal_fine <= conversion.MAX_RASTER_PAGE_PIXELS < 5 * legal_fine
    source = tmp_path / "oversized.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=1800, height=1800)  # 5,100 x 4,900 = 25 million pixels at fine resolution
    writer.write(source)
    monkeypatch.setattr(conversion.subprocess, "run", lambda *a, **k: pytest.fail("refused before drawing"))
    with pytest.raises(conversion.DocumentConversionError, match="raster limits"):
        conversion.pdf_to_tiff(str(source), str(tmp_path / "out.tiff"))
    scan = tmp_path / "legal-400.tiff"
    Image.new("1", (3400, 5600), 1).save(scan, "TIFF", compression="group4", dpi=(400, 400))
    assert conversion.tiff_to_pdf(str(scan), str(tmp_path / "scan.pdf"))[0] == 1


def test_no_more_than_two_ghostscript_drawings_run_at_once(monkeypatch, tmp_path):
    import threading
    import time
    source = tmp_path / "one.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(source)
    lock, release = threading.Lock(), threading.Event()
    state = {"active": 0, "most": 0}

    def drawing(arguments, **kwargs):
        with lock:
            state["active"] += 1
            state["most"] = max(state["most"], state["active"])
        release.wait(timeout=30)
        with lock:
            state["active"] -= 1
        raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

    monkeypatch.setattr(conversion.shutil, "which", lambda command: "/synthetic/gs")
    monkeypatch.setattr(conversion.subprocess, "run", drawing)
    refused = []

    def convert(index):
        try:
            conversion.pdf_to_tiff(str(source), str(tmp_path / f"out-{index}.tiff"))
        except conversion.DocumentConversionError:
            refused.append(index)
    threads = [threading.Thread(target=convert, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:  # waits on the condition, not a fixed time
        with lock:
            if state["active"] == conversion.GHOSTSCRIPT_SLOTS:
                break
    with lock:
        assert state["active"] == conversion.GHOSTSCRIPT_SLOTS
    release.set()
    for thread in threads:
        thread.join(timeout=30)
    assert state["most"] == conversion.GHOSTSCRIPT_SLOTS == 2 and sorted(refused) == [0, 1, 2, 3]


def test_a_fax_image_over_its_size_limit_is_refused_as_too_large_to_fax(monkeypatch, tmp_path):
    assert conversion.fax_image_limit(10) == conversion.MAX_OUTPUT_BYTES
    assert conversion.fax_image_limit(200) == 200 * 1024 * 1024
    assert conversion.fax_image_limit(500) == 500 * 1024 * 1024
    assert conversion.fax_image_limit(10_000) == conversion.MAX_FAX_IMAGE_BYTES == 512 * 1024 * 1024
    source = tmp_path / "two.pdf"
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=612, height=792)
    writer.write(source)
    monkeypatch.setattr(conversion, "MAX_OUTPUT_BYTES", 2 * 1024 * 1024)

    def drawing(arguments, **kwargs):
        temporary = next(item.split("=", 1)[1] for item in arguments if item.startswith("-sOutputFile="))
        Path(temporary).write_bytes(b"\0" * (3 * 1024 * 1024))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(conversion.shutil, "which", lambda command: "/synthetic/gs")
    monkeypatch.setattr(conversion.subprocess, "run", drawing)
    output = tmp_path / "out.tiff"
    with pytest.raises(conversion.DocumentConversionError) as refused:
        conversion.pdf_to_tiff(str(source), str(output))
    assert str(refused.value) == "This document is too large to fax: its fax pages would take more than 2 MB."
    assert refused.value.operational is False and not output.exists()
