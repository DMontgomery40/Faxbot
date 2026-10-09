"""Exact enumerative payload mapping: on-page profile, integrity and bounded decoding."""
import io
import itertools
import random

from PIL import Image
import pytest

from app import codec
from app.codec import channel, pages, t4


def _document(size=32 * 1024):
    return codec.Document(random.Random(20261009).randbytes(size), 'application/pdf', 'synthetic.pdf')


def _replace_rows(image, first, replacements):
    raw = bytearray(image.tobytes())
    stride = image.width // 8
    for index, row in enumerate(replacements):
        raw[(first + index) * stride:(first + index + 1) * stride] = row
    return Image.frombytes('1', image.size, bytes(raw))


def test_enumerative_is_self_describing_and_recovers_the_original_through_fax_tiff(tmp_path):
    document = _document()
    encoded = codec.encode_document(document, layout='enumerative')
    for compression in ('group3', 'group4'):
        target = tmp_path / f'{compression}.tiff'
        encoded.pages[0].save(target, compression=compression, dpi=(204, 196))
        recovered, report = codec.decode_images(codec.read_images(target))
        assert recovered == document
        assert report['layout'] == 'enumerative'
    header = pages.read_page(encoded.pages[0]).header
    assert header['profile'] == 1
    assert header['first_bit'] == 0
    assert header['end_bit'] == encoded.stream_bytes * 8


@pytest.mark.parametrize('resolution', ['standard', 'superfine', '300', '400'])
def test_enumerative_round_trips_every_supported_fax_resolution(resolution):
    document = _document(2048)
    encoded = codec.encode_document(document, layout='enumerative', resolution=resolution)
    assert codec.decode_images(encoded.pages)[0] == document


def test_enumerative_row_damage_is_an_erasure_and_larger_damage_fails_explicitly():
    document = _document()
    encoded = codec.encode_document(document, layout='enumerative')
    image = encoded.pages[0]
    white = b'\xff' * (image.width // 8)
    damaged = _replace_rows(image, encoded.geometry.data_top, [white] * 10)
    recovered, report = codec.decode_images([damaged])
    assert recovered == document
    assert report['erased_bytes'] > 0
    with pytest.raises(codec.CodecError, match='damaged'):
        codec.decode_images([_replace_rows(image, encoded.geometry.data_top, [white] * 80)])


def test_enumerative_lost_and_repeated_lines_keep_the_exact_document():
    document = _document()
    encoded = codec.encode_document(document, layout='enumerative')
    image = channel.repeat_lines(channel.drop_lines(encoded.pages[0], 0.02, seed=2), 0.02, seed=3)
    assert codec.decode_images([image])[0] == document


def test_enumerative_resolution_conversion_fails_instead_of_delivering_wrong_bytes():
    encoded = codec.encode_document(_document(), layout='enumerative')
    with pytest.raises(codec.CodecError):
        codec.decode_images([channel.halve_lines(encoded.pages[0])])


def test_enumerative_full_page_horizontal_shift_fails_instead_of_returning_wrong_bytes():
    encoded = codec.encode_document(_document(), layout='enumerative')
    image = encoded.pages[0]
    shifted = Image.new('1', image.size, 1)
    shifted.paste(image, (1, 0))
    with pytest.raises(codec.CodecError):
        codec.decode_images([shifted])


def test_enumerative_encrypted_multi_page_document_needs_the_shared_key():
    document = _document(128 * 1024)
    encoded = codec.encode_document(document, layout='enumerative', resolution='standard',
                                    secret='synthetic compatible receiver key')
    assert encoded.page_count > 1
    with pytest.raises(codec.CodecError, match='shared key'):
        codec.decode_images(encoded.pages)
    received = list(reversed(encoded.pages)) + encoded.pages[:1]
    assert codec.decode_images(received, secrets=['synthetic compatible receiver key'])[0] == document


def test_enumerative_conflicting_duplicate_page_rows_are_rejected():
    from app.codec import enumerative
    encoded = codec.encode_document(_document(2048), layout='enumerative')
    image = encoded.pages[0]
    changes, _ = enumerative.encode_line(encoded.tag, 0, '0' * 1000, 0, image.width)
    forged = _replace_rows(image, encoded.geometry.data_top,
                           [t4.packed_from_changes(changes, image.width)])
    with pytest.raises(codec.CodecError, match='Conflicting'):
        codec.decode_images([image, forged])


@pytest.mark.parametrize('field,value', [(1, 2), (3, 0), (8, 0x7fffffff), (10, 0xffffffff),
                                        (11, 15), (12, 0), (12, 255)])
def test_enumerative_header_rejects_unknown_profiles_and_forged_bounds(field, value):
    encoded = codec.encode_document(_document(512), layout='enumerative')
    values = [b'FXP', 1, 4, 32, 0, 1, encoded.tag, encoded.codewords,
              encoded.container_length, 0, encoded.stream_bytes * 8, 63, 1]
    values[field] = value
    assert pages._decode_header(pages.HEADER.pack(*values)) is None


def test_enumerative_mapping_matches_exhaustive_small_rasters():
    from app.codec import enumerative
    table = enumerative.Counts(12, 40)
    for width in range(2, 13):
        for budget in (10, 20, 40):
            expected = []
            for middle in itertools.product((0, 1), repeat=width - 2):
                runs = [len(list(group)) for _, group in itertools.groupby((0,) + middle + (1,))]
                if sum(len(t4.run_code(run, colour % 2)) for colour, run in enumerate(runs)) <= budget:
                    expected.append(runs)
            expected.sort()
            assert table.count(width, budget) == len(expected)
            for rank, runs in enumerate(expected):
                assert table.unrank(rank, width, budget) == runs
                assert table.rank(runs, width, budget) == rank


def test_enumerative_rejects_noncanonical_rows_and_wrong_document_tag():
    from app.codec import enumerative
    source = '01010111' * 150
    changes, used = enumerative.encode_line(b'test', 0, source, 0, 1728)
    assert used == 873
    assert enumerative.decode_line(changes, 1728, b'test') == (0, source[:873])
    assert enumerative.decode_line(changes, 1728, b'nope') is None
    assert enumerative.decode_line([0] + changes, 1728, b'test') is None
    assert enumerative.decode_line([change + 1 for change in changes], 1728, b'test') is None
    assert enumerative.decode_line(changes, 1_000_000, b'test') is None
    assert enumerative.decode_line(changes, 1728, b'test', profile=255) is None
    with pytest.raises(ValueError):
        enumerative.Counts(1_000_000, 1_000_000)


@pytest.mark.parametrize('offset', [1, 873 * 100])
def test_enumerative_rejects_valid_crc_offsets_outside_the_advertised_page(offset):
    from app.codec import enumerative
    encoded = codec.encode_document(_document(512), layout='enumerative')
    geo = encoded.geometry
    changes, _ = enumerative.encode_line(encoded.tag, offset, '0' * 1000, 0, geo.resolution.width)
    extra = t4.packed_from_changes(changes, geo.resolution.width)
    image = _replace_rows(encoded.pages[0], geo.data_top, [extra])
    assert all(found != offset for found, _, _ in pages.read_page(image).segments)


def _tiff_mh_bits(image):
    buffer = io.BytesIO()
    image.save(buffer, format='TIFF', compression='group3', strip_size=image.width // 8 * image.height)
    buffer.seek(0)
    with Image.open(buffer) as saved:
        return sum(saved.tag_v2[279]) * 8


def test_enumerative_uses_fewer_tiff_and_canonical_mh_bits_for_the_same_input_and_envelope():
    document = _document()
    baseline = codec.encode_document(document, layout='runs')
    candidate = codec.encode_document(document, layout='enumerative')
    assert baseline.stream_bytes == candidate.stream_bytes
    assert baseline.page_count == candidate.page_count == 1
    # Isolate the mapping: identical full raster size and identical header/footer
    # pixels, with only each encoder's data rows differing and white padding.
    geo = baseline.geometry
    stride = geo.resolution.width // 8
    footer_lines = 6 * geo.ladder_height + geo.bottom
    reference = baseline.pages[0].tobytes()
    envelope = reference[:geo.data_top * stride]
    footer = reference[-footer_lines * stride:]
    rasters = []
    for encoded in (baseline, candidate):
        body = encoded.pages[0].tobytes()[geo.data_top * stride:-footer_lines * stride]
        padding = b'\xff' * (geo.resolution.lines * stride - len(envelope) - len(body) - len(footer))
        rasters.append(Image.frombytes('1', (geo.resolution.width, geo.resolution.lines),
                                      envelope + body + padding + footer))
    assert _tiff_mh_bits(rasters[1]) < _tiff_mh_bits(rasters[0])
    # TIFF photometric conventions affect the stored strips; independently
    # check T.4's white-first wire representation of the actual page pixels.
    canonical = [t4.coded_bits(channel.lines_of(image), image.width, 'MH') for image in rasters]
    assert canonical[1] < canonical[0]
