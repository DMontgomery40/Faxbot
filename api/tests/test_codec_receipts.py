"""Encoded pages decode from what real receivers publish (brief 85 M3).

Each layout's pages go through every transformation in ``codec_receipts`` (the containers and page changes real
receivers and fax services apply) and decode to the original byte for byte, or are refused in one sentence that
says what to do. The Faxbeep receipt container is reproduced exactly; when the saved research receipts are present
locally, their structure is compared with the fixture's.
"""
from pathlib import Path
import random
import re

import pytest
from PIL import Image

from api.tests import codec_receipts as receipts
from app import codec
from app.codec import container, pages as codec_pages

LAYOUTS = ('grid', 'runs', 'picture', 'enumerative')
EXACT_LAYOUTS = ('runs', 'enumerative')
RECEIPTS = (Path(__file__).resolve().parents[2] / 'research' / 'faxbot-nondirect-encyclopedia-2026-10-09' / 'live')


def _document(size=5000, seed=3):
    return container.Document(random.Random(seed).randbytes(size), 'application/pdf', 'synthetic.pdf')


@pytest.fixture(scope='module')
def encoded():
    """One small document encoded in every layout, made once for the module."""
    document = _document()
    return document, {layout: codec.encode_document(document, layout=layout, fec='medium', salt=b's' * 16,
                                                    nonce=b'n' * 12).pages for layout in LAYOUTS}


def _one_sentence(text):
    return text.endswith('.') and len(re.findall(r'[.!?](?:\s|$)', text)) == 1


def _decodes(document, images):
    found, report = codec.decode_images(images)
    assert found == document
    return report


@pytest.mark.parametrize('layout', LAYOUTS)
@pytest.mark.parametrize('change', sorted(receipts.PAGE_TRANSFORMS))
def test_every_layout_survives_what_receivers_do_to_a_page(encoded, layout, change):
    document, made = encoded
    pages = [receipts.PAGE_TRANSFORMS[change](page) for page in made[layout]]
    if layout == 'enumerative' and change in ('moved_right', 'moved_left'):
        # Enumerative rows fill the whole width: a page moved sideways at the same width loses data at one edge.
        with pytest.raises(codec.CodecError) as refused:
            codec.decode_images(pages)
        assert _one_sentence(str(refused.value)), refused.value
        return
    _decodes(document, pages)


CONTAINERS = {
    'faxbeep_pdf': lambda pages: codec.read_images(receipts.faxbeep_pdf(pages)),
    'faxbeep_pngs': lambda pages: [image for data in receipts.faxbeep_pngs(pages) for image in codec.read_images(data)],
    'pdf_g3_one_dimensional': lambda pages: codec.read_images(receipts.pdf_bytes(pages, k=0)),
    'pdf_g3_two_dimensional': lambda pages: codec.read_images(receipts.pdf_bytes(pages, k=4)),
    'pdf_g4_black_is_1': lambda pages: codec.read_images(receipts.pdf_bytes(pages, black_is_1=True)),
    'pdf_g4_decode_inverted': lambda pages: codec.read_images(receipts.pdf_bytes(pages, decode_inverted=True)),
    'pdf_flate_one_bit': lambda pages: codec.read_images(receipts.pdf_bytes(pages, kind='flate1')),
    'pdf_flate_one_bit_inverted': lambda pages: codec.read_images(
        receipts.pdf_bytes(pages, kind='flate1', decode_inverted=True)),
    'pdf_flate_gray': lambda pages: codec.read_images(receipts.pdf_bytes(pages, kind='flate8')),
    'pdf_image_mask': lambda pages: codec.read_images(receipts.pdf_bytes(pages, kind='mask')),
    'pdf_in_strips': lambda pages: codec.read_images(receipts.pdf_bytes(pages, strip_rows=128)),
    'pdf_rotated_page': lambda pages: codec.read_images(receipts.pdf_bytes([receipts.rotated(p) for p in pages])),
    'tiff_g3': lambda pages: codec.read_images(receipts.tiff_bytes(pages, 'group3')),
    'tiff_g4': lambda pages: codec.read_images(receipts.tiff_bytes(pages, 'group4')),
    'png_with_resolution': lambda pages: [codec.read_images(receipts.png_bytes(p, dpi=True))[0] for p in pages],
}


@pytest.mark.parametrize('layout', LAYOUTS)
@pytest.mark.parametrize('kind', sorted(CONTAINERS))
def test_every_layout_decodes_from_every_container(encoded, layout, kind):
    document, made = encoded
    images = CONTAINERS[kind](made[layout])
    assert len(images) == len(made[layout])
    _decodes(document, images)


def test_the_faxbeep_pdf_keeps_every_pixel_but_the_trailing_row_and_says_fine_resolution(encoded):
    _, made = encoded
    page = made['runs'][0]
    image = codec.read_images(receipts.faxbeep_pdf([page]))[0]
    assert image.size == (page.width, page.height - 1) and image.info['dpi'] == (204, 196)
    assert image.convert('1').tobytes() == page.crop((0, 0, page.width, page.height - 1)).tobytes()


@pytest.mark.parametrize('layout', LAYOUTS)
@pytest.mark.parametrize('dpi', [(200, 200), (300, 300)])
def test_a_page_drawn_again_at_another_resolution_decodes_or_is_refused_in_one_sentence(encoded, layout, dpi):
    document, made = encoded
    images = [receipts.resampled(page, *dpi) for page in made[layout]]
    # The picture layout's one-dot clusters survive being drawn larger, not smaller.
    if layout in EXACT_LAYOUTS or (layout == 'picture' and dpi[0] < 204):
        with pytest.raises(codec.CodecError) as refused:
            codec.decode_images(images)
        assert str(refused.value) == codec_pages.RESIZED, refused.value
        return
    _decodes(document, images)


def test_a_small_preview_is_refused_with_what_to_decode_instead(encoded):
    _, made = encoded
    preview = codec.read_images(receipts.png_bytes(receipts.thumbnail(made['runs'][0])))
    with pytest.raises(codec.CodecError) as refused:
        codec.decode_images(preview)
    assert str(refused.value) == codec_pages.PREVIEW
    assert _one_sentence(codec_pages.PREVIEW) and _one_sentence(codec_pages.RESIZED)


@pytest.mark.parametrize('change', ['exact', 'rotated_180', 'inverted', 'rotated_inverted', 'header_added'])
def test_the_received_fax_probe_finds_encoded_pages_however_they_arrived(encoded, change):
    _, made = encoded
    for layout in LAYOUTS:
        page = receipts.PAGE_TRANSFORMS[change](made[layout][0])
        assert codec.looks_like_payload(page), layout
        assert codec.looks_like_payload(codec.first_page(receipts.faxbeep_pdf([page]))), layout


def test_ordinary_pages_are_not_payload_pages_either_way_up_or_inverted():
    from PIL import ImageDraw
    page = Image.new('1', (1728, 2156), 1)
    ImageDraw.Draw(page).text((100, 100), 'An ordinary fax with a table: | | | | | |', fill=0)
    for image in (page, receipts.rotated(page), receipts.inverted(page)):
        assert not codec.looks_like_payload(image)
    with pytest.raises(codec.CodecError, match='No payload pages'):
        codec.decode_images([receipts.inverted(page)])


def test_a_mirrored_page_reads_back_only_through_its_checked_rows(encoded):
    """Mirrored, the page is read turned round, which leaves it upside down: its rows are independent and each
    is checked, and the document's fingerprint is checked, so it decodes exactly or not at all."""
    document, made = encoded
    for layout in ('runs', 'grid'):
        _decodes(document, [receipts.mirror(page) for page in made[layout]])


def test_the_decode_command_reads_a_faxbeep_receipt(encoded, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from app.cli.main import app
    document, made = encoded
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FAXBOT_CLI_CONFIG', str(tmp_path / 'absent-config.toml'))
    receipt = tmp_path / 'fax_00000000.pdf'
    receipt.write_bytes(receipts.faxbeep_pdf([receipts.rotated(page) for page in made['runs']]))
    result = CliRunner().invoke(app, ['system', 'codec', 'decode', str(receipt)])
    assert result.exit_code == 0, result.output
    saved = tmp_path / 'fax_00000000-decoded.pdf'
    assert saved.read_bytes() == document.data
    assert 'fingerprint checked' in ' '.join(result.output.split())


@pytest.mark.skipif(not (RECEIPTS / 'au-combined-received.pdf').is_file(),
                    reason='The saved Faxbeep receipts are local research files, not in the repository.')
def test_the_fixture_container_matches_a_real_faxbeep_receipt(encoded):
    """Local only: the real receipt (an ordinary fax, not encoded pages) has the fixture's structure."""
    from pypdf import PdfReader
    _, made = encoded
    real = PdfReader(str(RECEIPTS / 'au-combined-received.pdf')).pages[0]
    ours = PdfReader(__import__('io').BytesIO(receipts.faxbeep_pdf(made['grid'][:1]))).pages[0]

    def shape(page):
        image = page['/Resources']['/XObject']['/Im0'].get_object()
        parms = image['/DecodeParms'][0]
        width, height = image['/Width'], image['/Height']
        box = [float(value) for value in page.mediabox]
        return {'filter': list(image['/Filter']), 'K': parms['/K'], 'BlackIs1': bool(parms['/BlackIs1']),
                'columns_match': parms['/Columns'] == width, 'rows_match': parms['/Rows'] == height,
                'dpi': (round(width * 72 / box[2]), round(height * 72 / box[3])),
                'content': page.get_contents().get_data().split()}
    real_shape, our_shape = shape(real), shape(ours)
    assert {k: v for k, v in real_shape.items() if k != 'content'} == {
        k: v for k, v in our_shape.items() if k != 'content'}
    assert [part for part in real_shape['content'] if not re.match(rb'^[0-9.]+$', part)] == [
        part for part in our_shape['content'] if not re.match(rb'^[0-9.]+$', part)]
    # And the real receipt reads through the same path, exactly as its saved page picture.
    images = codec.read_images(RECEIPTS / 'au-combined-received.pdf')
    with Image.open(RECEIPTS / 'au-combined-received-page-1-1.png') as picture:
        assert images[0].convert('1').tobytes() == picture.convert('1').tobytes()
    assert images[0].info['dpi'] == (204, 196)


# --- bounded input: received files come from anyone, and the receive path reads every one ----------------------

def _never_decoded(monkeypatch):
    from PIL import TiffImagePlugin
    monkeypatch.setattr(TiffImagePlugin.TiffImageFile, 'load',
                        lambda *args, **kwargs: pytest.fail('pixels decoded before the size check'))


def test_a_small_file_claiming_a_huge_page_is_refused_before_any_page_is_decoded(monkeypatch):
    from app.codec import reading
    huge = receipts.tiff_bytes([Image.new('1', (6000, 6000), 1)])  # 36 million blank pixels, a few kilobytes
    assert len(huge) < 100_000
    _never_decoded(monkeypatch)
    with pytest.raises(codec.CodecError) as refused:
        codec.read_images(huge)
    assert str(refused.value) == reading.PAGES_TOO_LARGE
    assert codec.first_page(huge) is None  # the automatic receive probe: not checked, delivered as received
    pdf = receipts.pdf_bytes([Image.new('1', (6000, 6000), 1)])
    monkeypatch.setattr(reading, 'ccitt_image', lambda *a, **k: pytest.fail('decoded before the size check'))
    with pytest.raises(codec.CodecError, match='too large to be fax pages'):
        codec.read_images(pdf)


def test_many_pages_over_the_total_and_a_file_over_the_byte_limit_are_refused(monkeypatch):
    from app.codec import reading
    page = Image.new('1', (4800, 4800), 1)  # 23 million pixels, under the per-page limit
    many = receipts.tiff_bytes([page] * 44)  # over a billion pixels in all
    _never_decoded(monkeypatch)
    with pytest.raises(codec.CodecError, match='too large to be fax pages'):
        codec.read_images(many)
    monkeypatch.setattr(reading, 'MAX_INPUT_BYTES', 1000)
    with pytest.raises(codec.CodecError) as refused:
        codec.read_images(b'II*\x00' + bytes(2000))
    assert str(refused.value) == reading.TOO_LARGE == 'This file is too large to be a received fax.'


def test_received_pages_are_decoded_one_at_a_time_when_used(encoded, monkeypatch):
    from app.codec import reading
    document, made = encoded
    pages = codec.read_images(receipts.tiff_bytes(made['runs'] * 3))
    assert isinstance(pages, reading.ReceivedPages) and len(pages) == 3 * len(made['runs'])
    decoded = []

    def counting(index, load):
        def made_once():
            decoded.append(index)
            return load()
        return made_once
    pages._loaders = [(size, counting(index, load)) for index, (size, load) in enumerate(pages._loaders)]
    assert pages.sizes() == [page.size for page in made['runs'] * 3] and not decoded
    assert codec.decode_images(pages)[0] == document
    assert sorted(decoded) == list(range(len(pages)))  # each page made once, as it was read
