"""The capacity layout (format 2, brief 85 M2): payload bits drawn as runs at the capacity of the complete T.4 code.

Checks the frozen tables, every line's exact read-back (the sender's own tripwire), the table arithmetic against
libtiff's MH coder at the three fax widths (research/faxbot-novel-vectors-2026-10-08/mh_capacity.py's check),
round trips at every profile, the simulated channel's damage on this layout and the run-coded one, and that a page
of a format or layout this release does not know is refused in one sentence.
"""
import hashlib
import random

import pytest

from app import codec
from app.codec import capacity, channel, container, pages, t4
from app.pages import coding


def _document(size, seed=1):
    return container.Document(random.Random(seed).randbytes(size), 'application/pdf', 'scan.pdf')


def test_the_tables_are_the_published_ones_and_well_formed():
    data = capacity.TABLES_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == capacity.TABLES_SHA256
    assert set(capacity.PROFILES) == {1, 2, 3}
    for profile in capacity.PROFILES:
        for cum in capacity.tables(profile):
            assert cum[0] == 0 and cum[-1] == 1 << capacity.TOTAL_BITS
            assert all(cum[run] > cum[run - 1] for run in range(1, 64))  # every terminating run can be drawn
            assert len(cum) - 1 <= capacity.MAX_RUN
            assert cum[-1] * (capacity.FULL + 1) < 2 ** 53  # the browser decoder's integers stay exact
    with pytest.raises(capacity.CapacityError):
        capacity.tables(9)


@pytest.mark.parametrize('profile', sorted(capacity.PROFILES))
def test_every_line_reads_back_exactly_even_from_runs_of_equal_bits(profile):
    tag = b'\x01\x02\x03\x04'
    for source in ('0' * 50_000, '1' * 50_000, ''.join(random.Random(profile).choice('01') for _ in range(50_000))):
        position = 0
        for _ in range(12):
            changes, carried = capacity.encode_line(tag, position, source, position, 1728, profile)
            assert carried >= capacity.MIN_LINE_PAYLOAD
            offset, payload, _ = capacity.decode_line(changes, 1728, tag, profile)
            assert offset == position and payload == source[position:position + carried].ljust(carried, '0')
            position += carried
    # Another document's tag or another profile never reads the line as its own.
    changes, _ = capacity.encode_line(tag, 0, '01' * 5000, 0, 1728, profile)
    assert capacity.decode_line(changes, 1728, b'\x09\x09\x09\x09', profile) is None


def _table_bits(page):
    """MH bits of a page by the T.4 tables: each row's run codes plus one EOL (T.4 4.1.2)."""
    total = 0
    for changes in channel.lines_of(page):
        colour = t4.WHITE
        for run in t4.runs_from_changes(changes, page.size[0]):
            total += len(t4.run_code(run, colour))
            colour ^= 1
        total += len(t4.EOL)
    return total


@pytest.mark.parametrize('resolution', ['fine', '300', '400'])
def test_the_table_arithmetic_agrees_with_libtiffs_mh_coder(resolution):
    page = codec.encode_document(_document(40_000, seed=7), layout='capacity', resolution=resolution,
                                 profile=1).pages[0]
    libtiff = coding.measure([page], codings=('MH',))['MH'][0]
    table = _table_bits(page)
    assert 0 <= libtiff - table < 8, (libtiff, table)  # libtiff pads its strip to a whole byte


@pytest.mark.parametrize('profile', sorted(capacity.PROFILES))
@pytest.mark.parametrize('size', [1024, 70 * 1024])
def test_pages_round_trip_at_every_profile(profile, size):
    document = _document(size, seed=size + profile)
    encoded = codec.encode_document(document, layout='capacity', profile=profile, salt=b'a' * 16, nonce=b'b' * 12)
    back, report = codec.decode_images(encoded.pages)
    assert back == document and report['layout'] == 'capacity' and report['erased_bytes'] == 0
    assert encoded.pages[0].info['dpi'] == (204, 196)


def test_a_larger_profile_carries_more_per_page_and_the_time_profile_fewer_bits_per_byte():
    document = _document(500_000, seed=3)
    found = {}
    for name in ('time', 'pages'):
        encoded = codec.encode_document(document, layout='capacity', profile=capacity.profile_for(name), fec='low')
        page = encoded.pages[0]
        found[name] = (encoded.page_bits[0], coding.measure([page], codings=('MH',))['MH'][0])
    shipped = codec.encode_document(document, layout='runs', fec='low')
    runs = (shipped.page_bits[0], coding.measure([shipped.pages[0]], codings=('MH',))['MH'][0])
    assert found['pages'][0] > 1.4 * runs[0]  # payload per page
    assert found['time'][1] / found['time'][0] < 0.95 * runs[1] / runs[0]  # MH bits per payload bit


@pytest.mark.parametrize('layout,options', [('runs', {}), ('capacity', {'profile': 1}), ('capacity', {'profile': 3})])
@pytest.mark.parametrize('rate', [1e-5, 1e-4])
def test_bit_errors_without_ecm_are_repaired_on_both_exact_layouts(layout, options, rate):
    document = _document(30 * 1024, seed=11)
    encoded = codec.encode_document(document, layout=layout, fec='high', **options)
    received, damaged = [], 0
    for index, page in enumerate(encoded.pages):
        image, report = channel.transmit(page, scheme='MH', bit_error_rate=rate, seed=index + 31)
        damaged += report['damaged_lines']
        received.append(image)
    assert damaged > 0
    back, report = codec.decode_images(received)
    assert back.data == document.data and report['erased_bytes'] > 0


@pytest.mark.parametrize('layout,options', [('runs', {}), ('capacity', {'profile': 1}), ('capacity', {'profile': 3})])
def test_dropped_repeated_and_recoded_lines_are_survived_on_both_exact_layouts(layout, options):
    document = _document(20 * 1024, seed=13)
    page = codec.encode_document(document, layout=layout, fec='medium', **options).pages[0]
    assert codec.decode_images([channel.drop_lines(page, 0.03, seed=1)])[0].data == document.data
    assert codec.decode_images([channel.repeat_lines(page, 0.03, seed=2)])[0].data == document.data
    for compression in ('group3', 'group4'):
        assert codec.decode_images([channel.libtiff_roundtrip(page, compression)])[0].data == document.data
    with pytest.raises(codec.CodecError):
        codec.decode_images([channel.halve_lines(page)])


def _header_page(version, layout_byte, profile=0):
    """A grid page whose every header copy (above and below the data) claims ``version`` and ``layout_byte``,
    each with a valid CRC, as a newer Faxbot would write it."""
    from PIL import Image
    encoded = pages.encode(b'synthetic container bytes' * 4, layout='grid')
    page, geo = encoded.pages[0], encoded.geometry
    fields = (encoded.parity, 0, 1, encoded.tag, encoded.codewords, encoded.container_length, 0,
              encoded.stream_bytes * 8)

    def lines(header):
        payload = header + bytes(sum(geo.row_layout) - len(header))
        return [pages._grid_line(geo, pages.row_bits(pages.ZERO_TAG, offset, payload, geo.row_layout))
                for offset in pages.HEADER_OFFSETS]
    real = lines(pages.HEADER.pack(b'FXP', 1, pages.LAYOUTS['grid'], *fields, geo.run_limit, 0))
    forged = lines(pages.HEADER.pack(b'FXP', version, layout_byte, *fields, 7, profile))
    stride = geo.resolution.width // 8
    data = bytearray(page.tobytes())
    replaced = 0
    for row in range(page.size[1]):
        line = bytes(data[row * stride:(row + 1) * stride])
        if line in real:
            data[row * stride:(row + 1) * stride] = forged[real.index(line)]
            replaced += 1
    assert replaced == 6 * geo.ladder_height  # three copies above the data and three below
    image = Image.frombytes('1', page.size, bytes(data))
    image.info['dpi'] = page.info['dpi']
    return image


@pytest.mark.parametrize('version,layout_byte,profile', [(2, 1, 0), (1, 6, 0), (1, 5, 9)])
def test_a_page_of_a_newer_format_layout_or_profile_is_refused_in_one_sentence(version, layout_byte, profile):
    with pytest.raises(codec.CodecError) as refused:
        codec.decode_images([_header_page(version, layout_byte, profile)])
    assert str(refused.value) == pages.NEWER
    assert str(refused.value) == 'These encoded pages were made by a newer version of Faxbot; update Faxbot to decode them.'


def test_the_capacity_page_says_format_2_and_older_layouts_still_say_format_1():
    assert pages.LAYOUTS['capacity'] == 5 and 'capacity' in pages.EXACT_LAYOUTS and 'capacity' in codec.LAYOUTS
    capacity_page = codec.encode_document(_document(2000), layout='capacity').pages[0]
    runs_page = codec.encode_document(_document(2000), layout='runs').pages[0]
    caption = pages.geometry('fine', 'capacity')
    assert caption.columns == pages.geometry('fine', 'runs').columns  # the same ladder: moved rows realign
    assert capacity_page.tobytes() != runs_page.tobytes()
