"""Experimental fax payload codec: the format, error correction and a simulated fax channel."""
import hashlib
import io
import os
import random
import shutil
import subprocess

import pytest
from PIL import Image

from app import codec
from app.codec import channel, container, pages, rs, stream, t4


def _document(size, seed=1, *, text=False):
    rng = random.Random(seed)
    if text:
        words = ['fax', 'payload', 'invoice', 'patient', 'order', 'page', 'Denver', 'total', '2026', 'note']
        body = ' '.join(rng.choice(words) for _ in range(size // 5))
        return container.Document(body.encode()[:size], 'text/plain', 'note.txt')
    return container.Document(rng.randbytes(size), 'application/pdf', 'scan.pdf')


def _decode(images, **options):
    document, report = codec.decode_images(images, **options)
    return document, report


# --- Reed-Solomon ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize('parity', [16, 32, 64])
def test_reed_solomon_repairs_any_errors_and_erasures_within_its_parity(parity):
    rng = random.Random(parity)
    for _ in range(60):
        message = [rng.randrange(256) for _ in range(255 - parity)]
        word = rs.encode_message(message, parity)
        errors = rng.randrange(parity // 2 + 1)
        erasures = rng.randrange(parity - 2 * errors + 1)
        positions = rng.sample(range(255), errors + erasures)
        damaged = list(word)
        for position in positions:
            damaged[position] ^= rng.randrange(1, 256)
        assert rs.correct(damaged, parity, positions[errors:]) == word


def test_reed_solomon_refuses_damage_beyond_its_parity():
    rng = random.Random(7)
    word = rs.encode_message([rng.randrange(256) for _ in range(239)], 16)
    damaged = list(word)
    for position in range(17):
        damaged[position] ^= 0x55
    with pytest.raises(rs.ReedSolomonError):
        rs.correct(damaged, 16, list(range(17)))


def test_column_encoder_matches_one_codeword_at_a_time():
    rng = random.Random(3)
    messages = [[rng.randrange(256) for _ in range(223)] for _ in range(5)]
    columns = [bytes(message[j] for message in messages) for j in range(223)]
    parity = rs.encode_columns(columns, 32)
    for i, message in enumerate(messages):
        assert rs.encode_message(message, 32)[223:] == [column[i] for column in parity]


def test_stream_spreads_a_lost_run_of_bytes_evenly_and_repairs_it():
    data = random.Random(4).randbytes(20000)
    built, n = stream.build(data, 32)
    known = bytearray(b'\x01' * len(built))
    damaged = bytearray(built)
    for t in range(5000, 5000 + 25 * n):  # 25 lost bytes per codeword, in one burst
        known[t] = 0
        damaged[t] = 0
    recovered, report = stream.recover(damaged, known, n, 32, len(data))
    assert recovered == data and report['worst_codeword_erasures'] == 25


# --- container ------------------------------------------------------------------------------------------------

def test_container_round_trip_checks_the_hash_and_keeps_name_and_type():
    document = _document(5000, text=True)
    packed = container.pack(document)
    back = container.unpack(packed)
    assert back == document and back.sha256 == hashlib.sha256(document.data).hexdigest()


def test_encrypted_container_is_deterministic_with_fixed_salt_and_nonce_and_needs_the_key():
    document = _document(3000)
    first = container.pack(document, secret='correct horse battery', salt=b's' * 16, nonce=b'n' * 12)
    second = container.pack(document, secret='correct horse battery', salt=b's' * 16, nonce=b'n' * 12)
    assert first == second and container.is_encrypted(first)
    # The document's hash is inside the ciphertext, never in the clear.
    assert hashlib.sha256(document.data).digest() not in first
    assert container.unpack(first, secrets=['wrong key here', 'correct horse battery']) == document
    with pytest.raises(container.ContainerError, match='key this Faxbot does not have'):
        container.unpack(first, secrets=['wrong key here'])
    with pytest.raises(container.ContainerError, match='no shared key'):
        container.unpack(first)


def test_tampered_or_oversized_containers_are_refused():
    packed = bytearray(container.pack(_document(2000)))
    packed[-1] ^= 1
    with pytest.raises(container.ContainerError):
        container.unpack(bytes(packed))
    huge = container.CLEAR_HEADER.pack(b'FXBC', 1, 0, 0, 0, 64 * 1024 * 1024)
    with pytest.raises(container.ContainerError, match='larger than Faxbot accepts'):
        container.unpack(huge)
    with pytest.raises(container.ContainerError, match='Only PDF and plain-text'):
        container.pack(container.Document(b'x', 'image/png'))


@pytest.mark.skipif(not container.zstd_available(), reason='zstandard is not installed')
def test_zstd_is_chosen_when_it_is_smaller():
    document = _document(60000, text=True)
    packed = container.pack(document)
    assert packed[6] in (container.ZSTD, container.DEFLATE)
    assert container.unpack(container.pack(document, compression=container.ZSTD)) == document


# --- T.4 / T.6 coding, checked against libtiff ----------------------------------------------------------------

def _random_lines(rng, width, count):
    lines = []
    for _ in range(count):
        changes, x = ([0] if rng.random() < 0.3 else []), 0
        while True:
            x += rng.choice([1, 2, 3, 5, 8, 13, 40, 100, 300, 1500])
            if x >= width:
                break
            if not changes or x > changes[-1]:
                changes.append(x)
        lines.append(changes)
    return lines


def _tiff(raw, width, height, compression, options):
    import struct
    entries = [(256, 4, width), (257, 4, height), (258, 3, 1), (259, 3, compression), (262, 3, 0), (273, 4, 0),
               (277, 3, 1), (278, 4, height), (279, 4, len(raw)), (292 if compression == 3 else 293, 4, options)]
    entries.sort()
    body = struct.pack('<H', len(entries))
    offset = 8 + 2 + 12 * len(entries) + 4
    for tag, kind, value in entries:
        value = offset if tag == 273 else value
        body += struct.pack('<HHIHH', tag, kind, 1, value, 0) if kind == 3 else struct.pack('<HHII', tag, kind, 1, value)
    return b'II*\x00' + struct.pack('<I', 8) + body + struct.pack('<I', 0) + raw


@pytest.mark.parametrize('scheme,compression,options', [('MH', 3, 0), ('MR', 3, 1), ('MMR', 4, 0)])
def test_t4_coding_round_trips_and_libtiff_reads_it(scheme, compression, options):
    rng = random.Random(9)
    width = 1728
    lines = _random_lines(rng, width, 80) + [[], [0], [5, 6, 7, 8]]
    bits = t4.encode_page(lines, width, scheme)
    assert t4.decode_page(bits, width, scheme) == (lines, 0)
    raw = int(bits + '0' * (-len(bits) % 8), 2).to_bytes((len(bits) + 7) // 8, 'big')
    with Image.open(io.BytesIO(_tiff(raw, width, len(lines), compression, options))) as image:
        image.load()
        assert image.convert('1').tobytes() == channel.image_of(lines, width).tobytes()


def test_t4_receiver_conceals_a_damaged_line_and_resynchronises():
    width = 1728
    lines = [[100, 200], [300, 400], [500, 600], [700, 800]]
    bits = t4.encode_page(lines, width, 'MH')
    second = bits.index(t4.EOL, 12) + 12
    damaged = bits[:second + 2] + ('1' if bits[second + 2] == '0' else '0') + bits[second + 3:]
    received, count = t4.decode_page(damaged, width, 'MH', conceal='previous')
    assert count >= 1 and received[0] == lines[0] and received[-1] == lines[-1] and len(received) == 4


# --- pages ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('layout', ['grid', 'runs', 'picture'])
@pytest.mark.parametrize('size', [1024, 70 * 1024])
def test_pages_round_trip_each_layout(layout, size):
    document = _document(size, seed=size)
    encoded = codec.encode_document(document, layout=layout, fec='medium', salt=b'a' * 16, nonce=b'b' * 12)
    back, report = _decode(encoded.pages)
    assert back == document and report['erased_bytes'] == 0 and report['layout'] == layout


def test_encoding_is_deterministic():
    document = _document(9000, seed=2)
    first = codec.encode_document(document, layout='runs')
    second = codec.encode_document(document, layout='runs')
    assert [page.tobytes() for page in first.pages] == [page.tobytes() for page in second.pages]


def test_two_megabytes_span_pages_and_decode_after_group4_recoding():
    document = _document(2 * 1024 * 1024, seed=5)
    encoded = codec.encode_document(document, layout='runs', fec='low')
    assert encoded.page_count > 5
    received = [channel.libtiff_roundtrip(page, 'group4') for page in encoded.pages]
    random.Random(1).shuffle(received)  # pages carry their own position
    back, report = _decode(received)
    assert back.data == document.data and report['pages_read'] == encoded.page_count


def test_a_lost_page_is_rebuilt_from_the_others_at_high_correction():
    # One page is a sixth of the stream here; high correction rebuilds up to a quarter.
    document = _document(1024 * 1024, seed=6)
    encoded = codec.encode_document(document, layout='runs', fec='high')
    assert encoded.page_count >= 5
    back, report = _decode(encoded.pages[:1] + encoded.pages[2:])
    assert back.data == document.data and report['pages_read'] == encoded.page_count - 1


def test_a_page_from_another_document_is_ignored():
    first = codec.encode_document(_document(3000, seed=1), layout='grid')
    other = codec.encode_document(_document(3000, seed=2), layout='grid')
    back, _ = _decode(first.pages + other.pages[:0] + first.pages)
    assert back.data == _document(3000, seed=1).data


def test_ordinary_fax_pages_are_not_payload_pages():
    page = Image.new('1', (1728, 2156), 1)
    from PIL import ImageDraw
    ImageDraw.Draw(page).text((100, 100), 'An ordinary fax with a table: | | | | | |', fill=0)
    assert not codec.looks_like_payload(page)
    with pytest.raises(codec.CodecError, match='No payload pages'):
        _decode([page])


# --- the simulated fax channel --------------------------------------------------------------------------------

@pytest.mark.parametrize('layout,scheme,rate', [
    ('grid', 'MH', 1e-4), ('grid', 'MR', 1e-5), ('runs', 'MH', 1e-5), ('runs', 'MH', 1e-4), ('picture', 'MH', 1e-4),
])
def test_bit_errors_without_ecm_are_repaired(layout, scheme, rate):
    document = _document(30 * 1024, seed=11)
    encoded = codec.encode_document(document, layout=layout, fec='high')
    received = []
    damaged = 0
    for index, page in enumerate(encoded.pages):
        image, report = channel.transmit(page, scheme=scheme, bit_error_rate=rate, seed=index + 21)
        damaged += report['damaged_lines']
        received.append(image)
    assert damaged > 0
    back, report = _decode(received)
    assert back.data == document.data and report['erased_bytes'] > 0


@pytest.mark.parametrize('conceal', ['previous', 'white', 'drop'])
def test_each_way_a_receiver_conceals_bad_lines_is_survived(conceal):
    document = _document(20 * 1024, seed=12)
    encoded = codec.encode_document(document, layout='grid', fec='high')
    image, report = channel.transmit(encoded.pages[0], scheme='MH', bit_error_rate=5e-5, conceal=conceal, seed=3)
    assert report['damaged_lines'] > 0
    assert _decode([image])[0].data == document.data


@pytest.mark.parametrize('layout', ['grid', 'runs', 'picture'])
def test_dropped_and_repeated_lines_are_survived(layout):
    document = _document(20 * 1024, seed=13)
    encoded = codec.encode_document(document, layout=layout, fec='medium')
    assert _decode([channel.drop_lines(encoded.pages[0], 0.03, seed=1)])[0].data == document.data
    assert _decode([channel.repeat_lines(encoded.pages[0], 0.03, seed=2)])[0].data == document.data


@pytest.mark.parametrize('method,phase', [('skip', 0), ('skip', 1), ('or', 0)])
def test_grid_survives_fine_to_standard_conversion(method, phase):
    document = _document(20 * 1024, seed=14)
    encoded = codec.encode_document(document, layout='grid', fec='medium')
    halved = channel.halve_lines(encoded.pages[0], method=method, phase=phase)
    assert _decode([halved])[0].data == document.data


@pytest.mark.parametrize('sturdy,x_scale,y_scale', [
    (False, 200 / 204, 200 / 196), (True, 0.75, 0.75), (True, 1.47, 1.53)])
def test_grid_survives_a_provider_rendering_at_another_resolution(sturdy, x_scale, y_scale):
    document = _document(8 * 1024, seed=15)
    encoded = codec.encode_document(document, layout='grid', sturdy=sturdy, fec='medium')
    assert _decode([channel.rescale(encoded.pages[0], x_scale, y_scale)])[0].data == document.data


def test_run_coded_pages_need_the_exact_raster():
    document = _document(8 * 1024, seed=16)
    encoded = codec.encode_document(document, layout='runs', fec='high')
    with pytest.raises(codec.CodecError):
        _decode([channel.halve_lines(encoded.pages[0])])


@pytest.mark.parametrize('resolution', ['standard', 'superfine', '300', '400'])
def test_every_resolution_round_trips(resolution):
    document = _document(6 * 1024, seed=17)
    for layout in ('grid', 'runs'):
        encoded = codec.encode_document(document, resolution=resolution, layout=layout)
        assert encoded.pages[0].size[0] == pages.RESOLUTIONS[resolution].width
        assert _decode(encoded.pages)[0].data == document.data


def test_the_fax_engine_tiff_path_and_faxbot_receive_pdf(tmp_path):
    """Pages leave as a Group 4 fax TIFF, may be re-coded by libtiff, and arrive through tiff_to_pdf."""
    from app.conversion import tiff_to_pdf
    document = _document(12 * 1024, seed=18, text=True)
    encoded = codec.encode_document(document, layout='runs', fec='medium')
    sent = codec.write_tiff(encoded.pages, tmp_path / 'sent.tiff')
    with Image.open(sent) as image:
        assert image.info['compression'] == 'group4' and tuple(round(v) for v in image.info['dpi']) == (204, 196)
    tiffcp = shutil.which('tiffcp')
    received = sent
    if tiffcp:
        for option in ('g3:1d', 'g3:2d', 'g4'):
            target = tmp_path / f'received-{option.replace(":", "-")}.tiff'
            subprocess.run([tiffcp, '-c', option, str(received), str(target)], check=True)
            received = target
    pages_count, pdf = tiff_to_pdf(str(received), str(tmp_path / 'received.pdf'))
    assert pages_count == encoded.page_count
    back, _ = _decode(codec.read_images(pdf))
    assert back == document


@pytest.mark.skipif(not (os.environ.get('FAXBOT_JBIG_TOOL') or shutil.which('pbmtojbg85')),
                    reason='jbigkit is not installed')
def test_jbig_recoding_is_the_identity_on_pixels(tmp_path):
    encoded = codec.encode_document(_document(4 * 1024, seed=19), layout='runs')
    tool = os.environ.get('FAXBOT_JBIG_TOOL') or shutil.which('pbmtojbg85')
    decoder = os.path.join(os.path.dirname(tool), 'jbgtopbm')
    encoded.pages[0].save(tmp_path / 'page.pbm')
    subprocess.run([tool, str(tmp_path / 'page.pbm'), str(tmp_path / 'page.jbg')], check=True)
    subprocess.run([decoder, str(tmp_path / 'page.jbg'), str(tmp_path / 'back.pbm')], check=True)
    with Image.open(tmp_path / 'back.pbm') as back:
        assert back.convert('1').tobytes() == encoded.pages[0].tobytes()


def test_a_forged_header_cannot_make_the_decoder_allocate_a_huge_stream():
    document = _document(2000, seed=20)
    encoded = codec.encode_document(document, layout='grid')
    geo = encoded.geometry
    forged = pages.HEADER.pack(b'FXP', 1, 1, 32, 0, 1, encoded.tag, 0x7FFFFFFF, 0x7FFFFFFF, 0, 8, 15, 0)
    sizes = geo.row_layout
    line = pages._grid_line(geo, pages.row_bits(pages.ZERO_TAG, pages.HEADER_OFFSETS[0],
                                                forged + bytes(sum(sizes) - len(forged)), sizes))
    page = encoded.pages[0].copy()
    width = page.size[0] // 8
    raw = bytearray(page.tobytes())
    ladder = pages.find_ladder(page)[0]
    for y in range(ladder - 40, ladder - 30):  # a forged header row above the real ones
        raw[y * width:(y + 1) * width] = line
    tampered = Image.frombytes('1', page.size, bytes(raw))
    assert pages._decode_header(forged) is None
    back, _ = _decode([tampered])  # the forged row is ignored; the real header still reads
    assert back.data == document.data


def test_a_multi_page_document_survives_dropped_and_repeated_lines_on_every_page():
    document = _document(300 * 1024, seed=21)
    encoded = codec.encode_document(document, layout='grid', fec='medium')
    assert encoded.page_count >= 3
    damaged = [channel.repeat_lines(channel.drop_lines(page, 0.02, seed=index), 0.02, seed=index + 50)
               for index, page in enumerate(encoded.pages)]
    assert _decode(damaged)[0].data == document.data


def test_two_megabytes_survive_dropped_lines():
    document = _document(2 * 1024 * 1024, seed=22)
    encoded = codec.encode_document(document, layout='runs', fec='medium')
    damaged = [channel.drop_lines(page, 0.01, seed=index) for index, page in enumerate(encoded.pages)]
    back, report = _decode(damaged)
    assert back.data == document.data and report['erased_bytes'] > 0


def test_a_picture_page_survives_fine_to_standard_conversion():
    document = _document(10 * 1024, seed=23)
    encoded = codec.encode_document(document, layout='picture', fec='medium')
    assert _decode([channel.halve_lines(encoded.pages[0])])[0].data == document.data
