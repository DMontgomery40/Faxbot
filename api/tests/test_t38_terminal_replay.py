"""Proof for asterisk/patches/0004: far-end frames and Internet Aware Fax on spandsp 0.0.6's T.38 terminal.

Builds asterisk/tests/t38_terminal_replay.c against Debian's spandsp (the library Faxbot's Asterisk image runs),
with the steps 0004 adds from asterisk/patches/faxbot_t38_gateway.h, the file the image build compiles:

- Far-end frames: synthetic T.38 from a far end (a DIS, or a sender's TSI, SUB and DCS) replayed into a terminal
  that sends or receives; the frames 0004 keeps must be the octets the far end sent. With FAXBOT_T38_PCAPS set to
  a folder of real captures (kept outside the repository: they hold real numbers), every capture is replayed too:
  for a send capture the far end's DIS that 0004 keeps must equal the DIS decoded from the capture's own T.38
  packets, and for a receive capture (whose far end sends no DIS) its first DCS.
- Internet Aware Fax: two terminals back to back send the same three pages paced (as Asterisk runs spandsp) and
  as two Faxbots with 0004's IAF ("peer"). IAF must finish sooner, with the same pages, pixel for pixel.

Patch 0008 (brief 98): two terminals with error correction send a dense document with 64- or 256-octet frames
(the pages must arrive pixel for pixel, and the error record count the blocks), through a bursty loss model (both
sides' records must agree on the requests to send frames again), and with T.38 protection by packet category (the
receiver drops the copies, and fewer packets stay missing over the same loss).

Runs only with FAXBOT_NATIVE_PROOF=1 (it builds a small image with Docker).
"""
import base64
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh')
IMAGE = os.environ.get('FAXBOT_PROOF_PREFIX', 'faxbot-native-proof') + '-t38-terminal:latest'

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to build and run the T.38 terminal replay.'),
]

DOCKERFILE = '''FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libspandsp-dev libtiff-dev \\
    && rm -rf /var/lib/apt/lists/*
COPY t38_terminal_replay.c faxbot_t38_gateway.h data/ /data/
RUN gcc -O1 -Wall -Werror -o /replay /data/t38_terminal_replay.c -lspandsp -ltiff -lm
'''

NO_SIGNAL, PREAMBLE, V29_TRAINING = '00', '06', '0e'
FCS_OK, SIG_END = 'c00120', 'c00110'


def reverse(octet):
    return int(f'{octet:08b}'[::-1], 2)


def wire(frame):
    """A T.30 frame as spandsp hands it over (address, control, FCF, FIF) in T.38's octet order."""
    return [f'{reverse(octet):02x}' for octet in frame]


def hdlc(start, frame, *, last=True):
    """One V.21 frame: preamble, its octets 20 ms apart, FCS good, and (when last) the signal end."""
    packets = [(start, PREAMBLE)]
    moment = start + 0.3
    for octet in wire(frame):
        packets.append((moment, 'c001800000' + octet))
        moment += 0.020
    packets.append((moment, FCS_OK))
    if last:
        packets += [(moment + 0.06, SIG_END), (moment + 0.061, NO_SIGNAL)]
    return packets, moment + 0.1


# The far end's DIS: V.17 (14,400), fine, 2-D, A4 and B4, unlimited length, ECM, T.6, subaddressing.
DIS = bytes([0xFF, 0x13, 0x80, 0x00, 0xEE, 0xF5, 0xC4, 0x80, 0x80, 0x01])
CSI = bytes([0xFF, 0x03, 0x40]) + bytes(reversed(b'+15555550199'.ljust(20)))
# A sender's TSI, SUB ("4021") and DCS (14,400, fine, 2-D, ECM).
TSI = bytes([0xFF, 0x03, 0x43]) + bytes(reversed(b'+15555550100'.ljust(20)))
SUB = bytes([0xFF, 0x03, 0xC3]) + bytes(reversed(b'4021'.ljust(20)))
DCS = bytes([0xFF, 0x13, 0x83, 0x00, 0xE2, 0x94, 0x04])


# The far end's internet address (CSA) as an SSL Fax engine sends it, laid out as T.30 5.3.6.2.12 says: sequence 0,
# type 2 (URL), the length, the address. 45 octets: 0004 keeps CSA and TSA whole, up to 83 (every other frame 32).
SSL_ADDRESS = b'ssl://Synthetic1Pass@198.51.100.7:10443'
CSA = bytes([0xFF, 0x03, 0x24, 0x00, 0x02, len(SSL_ADDRESS)]) + SSL_ADDRESS


def lines(packets):
    return ''.join(f'{at:.6f} {seq} {ifp}\n' for seq, (at, ifp) in enumerate(packets))


def answer_sequence():
    csi, moment = hdlc(1.0, CSI, last=False)
    dis, _ = hdlc(moment, DIS)
    return lines([(0.0, NO_SIGNAL)] + csi + dis)


def answer_with_address_sequence():
    csi, moment = hdlc(1.0, CSI, last=False)
    csa, moment = hdlc(moment, CSA, last=False)
    dis, _ = hdlc(moment, DIS)
    return lines([(0.0, NO_SIGNAL)] + csi + csa + dis)


def sender_sequence():
    tsi, moment = hdlc(0.5, TSI, last=False)
    sub, moment = hdlc(moment, SUB, last=False)
    dcs, moment = hdlc(moment, DCS)
    training = [(moment + 0.1, V29_TRAINING)]
    return lines([(0.0, NO_SIGNAL)] + tsi + sub + dcs + training)


def proof_tiff():
    """Three 1728-pixel-wide bilevel pages at fine resolution, each different, as T.6 (G4) TIFF."""
    from PIL import Image, ImageDraw
    pages = []
    for number in range(3):
        page = Image.new('1', (1728, 2200), 1)
        draw = ImageDraw.Draw(page)
        for row in range(120, 2100, 40 + number * 7):
            draw.rectangle([100 + number * 30, row, 1600 - number * 50, row + 9], fill=0)
        draw.text((200, 60), f'Faxbot 0004 proof page {number + 1}', fill=0)
        pages.append(page)
    output = io.BytesIO()
    pages[0].save(output, format='TIFF', save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
    return output.getvalue(), pages


# -- captures (outside the repository) -------------------------------------------------------------

def udptl_from_far_end(pcap):
    """[(seconds, sequence, IFP hex)] of every T.38 packet sent to Faxbot's UDPTL ports (4000-4499) in a capture
    (pcap, Linux cooked v2 or Ethernet), the primary IFP of each UDPTL packet, first copy only."""
    data = Path(pcap).read_bytes()
    magic = data[:4]
    endian = '<' if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1') else '>'
    nanos = magic in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d')
    link = struct.unpack(endian + 'I', data[20:24])[0]
    offset, start, seen, found = 24, None, set(), []
    while offset + 16 <= len(data):
        seconds, fraction, caplen, _ = struct.unpack(endian + 'IIII', data[offset:offset + 16])
        frame = data[offset + 16:offset + 16 + caplen]
        offset += 16 + caplen
        moment = seconds + fraction / (1e9 if nanos else 1e6)
        ip = frame[20:] if link == 276 else frame[16:] if link == 113 else frame[14:] if link == 1 else b''
        if len(ip) < 28 or ip[0] >> 4 != 4 or ip[9] != 17:
            continue
        header = (ip[0] & 0x0F) * 4
        source_port, destination_port = struct.unpack('>HH', ip[header:header + 4])
        if not 4000 <= destination_port <= 4499 or 4000 <= source_port <= 4499:
            continue
        payload = ip[header + 8:]
        if len(payload) < 4:
            continue
        sequence = struct.unpack('>H', payload[:2])[0]
        if payload[2] < 0x80:
            length, at = payload[2], 3
        else:
            length, at = ((payload[2] & 0x3F) << 8) | payload[3], 4
        ifp = payload[at:at + length]
        if sequence in seen or len(ifp) != length:
            continue
        seen.add(sequence)
        start = moment if start is None else start
        found.append((moment - start, sequence, ifp.hex()))
    return found


def far_end_frames(packets):
    """The T.30 frames in the far end's V.21 HDLC, decoded straight from the IFPs (T.38 version 0), in spandsp's
    octet order: independent of spandsp, to check what 0004 keeps."""
    frames, current = [], bytearray()
    for _, _, text in packets:
        ifp = bytes.fromhex(text)
        if len(ifp) < 2 or not ifp[0] & 0x80 or not ifp[0] & 0x40 or (ifp[0] >> 1) & 0x0F != 0:
            continue  # not V.21 data
        count, at = ifp[1], 2
        for _ in range(count):
            if at >= len(ifp):
                break
            present, kind = ifp[at] >> 7, (ifp[at] >> 4) & 0x07
            at += 1
            if present:
                size = ((ifp[at] << 8) | ifp[at + 1]) + 1
                if kind == 0:
                    current += bytes(reverse(octet) for octet in ifp[at + 2:at + 2 + size])
                at += 2 + size
            if kind in (2, 4):  # FCS good
                frames.append(bytes(current))
                current = bytearray()
            elif kind in (1, 3, 5):
                current = bytearray()
    return frames


CAPTURES = sorted(Path(os.environ['FAXBOT_T38_PCAPS']).glob('t38-*.pcap')) if os.environ.get('FAXBOT_T38_PCAPS') else []


@pytest.fixture(scope='module')
def replay():
    tiff, _ = proof_tiff()
    with tempfile.TemporaryDirectory() as folder:
        context = Path(folder)
        (context / 'data').mkdir()
        (context / 'data' / 'answer.ifp').write_text(answer_sequence())
        (context / 'data' / 'answer-address.ifp').write_text(answer_with_address_sequence())
        (context / 'data' / 'sender.ifp').write_text(sender_sequence())
        (context / 'data' / 'proof.tif').write_bytes(tiff)
        (context / 'data' / 'dense.tif').write_bytes(dense_tiff()[0])
        for capture in CAPTURES:
            (context / 'data' / (capture.stem + '.ifp')).write_text(lines_of(udptl_from_far_end(capture)))
        (context / 't38_terminal_replay.c').write_text((ROOT / 'asterisk' / 'tests' / 't38_terminal_replay.c').read_text())
        (context / 'faxbot_t38_gateway.h').write_text((ROOT / 'asterisk' / 'patches' / 'faxbot_t38_gateway.h').read_text())
        (context / 'Dockerfile').write_text(DOCKERFILE)
        subprocess.run(['docker', '--context', CONTEXT, 'build', '-q', '-t', IMAGE, str(context)], check=True,
                       capture_output=True, timeout=900)

    def run(*args, script=None):
        command = ['docker', '--context', CONTEXT, 'run', '--rm', IMAGE]
        command += ['sh', '-c', script] if script else ['/replay', *args]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=True)
        return result.stdout
    yield run
    subprocess.run(['docker', '--context', CONTEXT, 'rmi', IMAGE], capture_output=True, timeout=120)


def lines_of(packets):
    return ''.join(f'{at:.6f} {sequence} {ifp}\n' for at, sequence, ifp in packets)


def kept(output):
    return dict((line.split(' ', 1) + [''])[:2] for line in output.splitlines())


def test_the_far_ends_dis_is_kept_exactly_as_it_sent_it(replay):
    found = kept(replay('replay', '/data/answer.ifp', 'send'))
    assert found['dis'] == DIS.hex()
    # A sending terminal answers with its own DCS: kept as the session's DCS, sent by this side.
    assert found['dcs_first'].startswith('ff1383') and found['trainings'] == '1'


def test_a_long_internet_address_from_the_far_end_is_kept_whole(replay):
    """An SSL Fax CSA is often longer than 32 octets; 0004 keeps CSA and TSA up to T.30's longest (83)."""
    found = kept(replay('replay', '/data/answer-address.ifp', 'send'))
    assert len(CSA) > 32 and found['csa'] == CSA.hex() and found['dis'] == DIS.hex()
    from app import engine_frames
    assert engine_frames.far_address({'csa_full': found['csa']})['address'] == SSL_ADDRESS.decode()


def test_a_senders_subaddress_and_dcs_are_kept_when_receiving(replay):
    found = kept(replay('replay', '/data/sender.ifp', 'receive'))
    assert found['sub'] == SUB.hex() and found['dcs_first'] == DCS.hex() and found['rates'] == '20'
    from app import engine_frames
    assert engine_frames.decode_sub(found['sub']) == '4021'
    assert engine_frames.dcs_rate(found['dcs_first']) == 14400


@pytest.mark.skipif(not CAPTURES, reason='Set FAXBOT_T38_PCAPS to a folder of real captures to replay them.')
@pytest.mark.parametrize('capture', CAPTURES, ids=[capture.stem for capture in CAPTURES])
def test_replaying_a_real_capture_keeps_the_dis_bytes_the_far_end_sent(replay, capture):
    packets = udptl_from_far_end(capture)
    frames = far_end_frames(packets)
    sent_dis = [frame for frame in frames if len(frame) > 3 and frame[2] in (0x80, 0x81)]
    mode = 'send' if 'engine' in capture.stem else 'receive'
    found = kept(replay('replay', f'/data/{capture.stem}.ifp', mode))
    print(capture.stem, mode, 'far-end frames', len(frames), 'DIS', sent_dis[-1].hex() if sent_dis else None, found)
    if sent_dis:
        assert found['dis'] == sent_dis[-1][:32].hex()
    else:
        sent_dcs = [frame for frame in frames if len(frame) > 3 and frame[2] & 0xFE == 0x82]
        assert sent_dcs and found['dcs_first'] == sent_dcs[0][:32].hex()


def test_internet_aware_fax_between_two_faxbots_is_faster_with_the_same_pages(replay):
    from PIL import Image
    script = ('/replay pair /data/proof.tif /tmp/{mode}.tif {mode} && echo image '
              '&& base64 -w0 /tmp/{mode}.tif && echo')
    results = {}
    for mode in ('paced', 'peer'):
        output = replay(script=script.format(mode=mode))
        summary, image = output.split('image\n', 1)
        found = kept(summary)
        results[mode] = found
        assert found['status'] == '0 0', (mode, found)  # T30_ERR_OK on both sides
        assert found['pages'] == '3', (mode, found)
        received = Image.open(io.BytesIO(base64.b64decode(image.strip())))
        _, pages = proof_tiff()
        for number, page in enumerate(pages):
            received.seek(number)
            assert received.convert('1').tobytes() == page.tobytes(), (mode, number)
    paced, peer = float(results['paced']['seconds']), float(results['peer']['seconds'])
    print({'paced_seconds': paced, 'iaf_seconds': peer, 'paced': results['paced'], 'iaf': results['peer']})
    assert peer < paced / 2, results


def _pair_with_subaddress(replay, mode, *extra):
    from PIL import Image
    script = (f'/replay pair /data/proof.tif /tmp/{mode}.tif {mode} {" ".join(extra)} && echo image '
              f'&& base64 -w0 /tmp/{mode}.tif && echo')
    summary, image = replay(script=script).split('image\n', 1)
    found = kept(summary)
    received = Image.open(io.BytesIO(base64.b64decode(image.strip())))
    _, pages = proof_tiff()
    for number, page in enumerate(pages):
        received.seek(number)
        assert received.convert('1').tobytes() == page.tobytes(), (mode, extra, number)
    return found


@pytest.mark.parametrize('mode', ['paced', 'peer'])
def test_a_subaddress_the_built_in_engine_asks_for_reaches_a_receiver_that_takes_one(replay, mode):
    """0005: the sender asks for subaddress 4021; the receiver's DIS sets bit 49, so spandsp sends SUB and the
    receiving engine reads it (the frame 0004 keeps and spandsp's own reading), with the same three pages."""
    from app import engine_frames
    found = _pair_with_subaddress(replay, mode, '4021')
    assert found['status'] == '0 0' and found['pages'] == '3', found
    assert found['sub_asked'] == '4021'
    assert engine_frames.decode_dis(found['far_dis'])['subaddress'] is True, found
    assert engine_frames.decode_sub(found['sub']) == '4021' and found['rx_sub'] == '4021', found
    # The longest a subaddress may be: a 20-digit notice ID (direct/notice.py).
    notice = '73019265018273640192'
    found = _pair_with_subaddress(replay, mode, notice)
    assert found['status'] == '0 0' and found['rx_sub'] == notice
    assert engine_frames.decode_sub(found['sub']) == notice


def test_a_receiver_that_takes_no_subaddress_gets_none_and_the_fax_still_goes(replay):
    """Requested is not carried: without DIS bit 49 spandsp 0.0.6 withholds SUB (what Faxbot records as asked but
    not carried), and the pages still arrive."""
    from app import engine_frames
    found = _pair_with_subaddress(replay, 'paced', '4021', 'nosub')
    assert found['status'] == '0 0' and found['pages'] == '3', found
    assert found['sub_asked'] == '4021'
    assert engine_frames.decode_dis(found['far_dis'])['subaddress'] is False
    assert found['sub'] == '' and found['rx_sub'] == '', found


def test_a_fax_without_a_subaddress_sends_none(replay):
    found = _pair_with_subaddress(replay, 'paced')
    assert found['status'] == '0 0' and found['sub_asked'] == '' and found['sub'] == '' and found['rx_sub'] == ''


@pytest.mark.parametrize('value, expected', [
    ('4021', '4021'), (' 40 21 ', '4021'), ('+#*1', '+#*1'), ('12345678901234567890', '12345678901234567890'),
    ('123456789012345678901', ''), ('40a1', ''), ('40;21', ''), ('', ''), ('$(id)', ''),
])
def test_the_subaddress_check_keeps_only_what_a_subaddress_may_hold(replay, value, expected):
    """0005's faxbot_sub_clean, as compiled into Asterisk: anything else sends nothing rather than part of it."""
    from app.access.receiving_rules import normalize_subaddress
    assert replay('clean', value).strip() == f'[{expected}]'
    # The same characters a receiving rule accepts (at most 20), so what Faxbot sends a rule can match.
    assert (normalize_subaddress(value) or '') == expected


@pytest.mark.parametrize('coding, ecm, expected', [
    ('-', 'ecm', 'MMR'),      # unset: Asterisk's own set, and spandsp takes the best the receiver has
    ('mh', 'ecm', 'MH'),      # 0006: the measured ceiling, even with error correction
    ('mr', 'ecm', 'MR'),
    ('mmr', 'ecm', 'MMR'),    # MMR when the receiver has error correction
    ('mmr', 'noecm', 'MR'),   # ...and never without it: the ceiling never forces a coding
    ('-', 'noecm', 'MR'),
])
def test_the_measured_coding_is_the_one_the_call_uses(replay, coding, ecm, expected):
    """Patch 0006 (builder CA): FAXBOT_COMPRESSION is a ceiling on what a sent fax offers; the sender's DCS shows the
    coding the pages went with, and the receiver gets the same three pages, pixel for pixel."""
    from PIL import Image
    from app import engine_frames
    script = (f'/replay coded /data/proof.tif /tmp/coded.tif {coding} {ecm} && echo image '
              f'&& base64 -w0 /tmp/coded.tif && echo')
    summary, image = replay(script=script).split('image\n', 1)
    found = kept(summary)
    assert found['status'] == '0 0' and found['pages'] == '3', found
    dcs = engine_frames.decode_dcs(found['dcs'])
    assert dcs['compression'] == expected and dcs['ecm'] is (ecm == 'ecm'), (coding, ecm, dcs)
    received = Image.open(io.BytesIO(base64.b64decode(image.strip())))
    _, pages = proof_tiff()
    for number, page in enumerate(pages):
        received.seek(number)
        assert received.convert('1').tobytes() == page.tobytes(), (coding, ecm, number)


def test_a_spandsp_other_than_the_pinned_one_still_stops_the_build(replay):
    probe = ('set -e; mkdir -p /tmp/other/spandsp; sed "s/^#define SPANDSP_RELEASE_DATE .*/#define '
             'SPANDSP_RELEASE_DATE 20140101/" /usr/include/spandsp/version.h > /tmp/other/spandsp/version.h; '
             'printf "#define SPANDSP_EXPOSE_INTERNAL_STRUCTURES\\n#include <spandsp.h>\\n#include '
             '\\"faxbot_t38_gateway.h\\"\\n" > /tmp/probe.c; gcc -fsyntax-only -Wall -Werror -I /data /tmp/probe.c '
             '&& echo pinned-builds; if gcc -fsyntax-only -I /tmp/other -I /data /tmp/probe.c 2>/dev/null; then echo '
             'other-builds; fi')
    output = replay(script=probe)
    assert 'pinned-builds' in output and 'other-builds' not in output


# -- patch 0008: error-correction frame size, error record and T.38 protection ----------------------------------

def dense_tiff(seed=5):
    """Two pages of scattered dots in a band, about 24 KB each in MH: past one 64-octet-frame block (16 KB), inside
    one 256-octet-frame block (64 KB)."""
    import random
    from PIL import Image
    rng = random.Random(seed)
    pages = []
    for _ in range(2):
        page = Image.new('1', (1728, 1100), 1)
        pixels = page.load()
        for y in range(100, 170):
            for x in range(0, 1728, 2):
                if rng.random() < 0.35:
                    pixels[x, y] = 0
        pages.append(page)
    output = io.BytesIO()
    pages[0].save(output, format='TIFF', save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
    return output.getvalue(), pages


ECM_NAMES = ('frame', 'pps', 'blocks', 'frames', 'ppr', 'bad', 'runs', 'longest', 'resent', 'ctc', 'edges',
             'edge_ms', 'rounds', 'round_ms', 'small', 'pages')


def ecm_pair(replay, frame, start, end, seed, protect='-'):
    from PIL import Image
    # A call the loss broke leaves no image: the summary still comes back.
    script = (f'/replay ecm /data/dense.tif /tmp/ecm.tif {frame} {start} {end} {seed} {protect}; echo image; '
              f'base64 -w0 /tmp/ecm.tif 2>/dev/null; echo')
    summary, image = replay(script=script).split('image\n', 1)
    found = kept(summary)
    for side in ('sender_ecm', 'receiver_ecm'):
        found[side] = dict(zip(ECM_NAMES, (int(value) for value in found[side].split('.'))))
    found['received'] = Image.open(io.BytesIO(base64.b64decode(image.strip()))) if image.strip() else None
    return found


def same_pages(received, pages):
    for number, page in enumerate(pages):
        received.seek(number)
        if received.convert('1').tobytes() != page.tobytes():
            return False
    return True


def test_64_octet_error_correction_frames_carry_the_same_pages_in_more_blocks(replay):
    """Patch 0008: FAXBOT_ECM_FRAME=64 makes the sender's DCS ask for 64-octet frames (bit 28) and spandsp send
    them; spandsp's receiver takes them, and the pages arrive pixel for pixel. A page of about 25 KB is one block at
    256 octets and two at 64 (a partial page holds 256 frames); the error record counts the blocks each side saw."""
    from app import engine_frames
    _, pages = dense_tiff()
    found = {frame: ecm_pair(replay, frame, 0, 1, 1) for frame in (256, 64)}
    for frame, run in found.items():
        assert run['status'] == '0 0' and run['pages'] == '2', run
        assert same_pages(run['received'], pages), frame
        dcs = bytes.fromhex(run['dcs'])
        assert bool(dcs[6] & 0x08) is (frame == 64) and bool(dcs[6] & 0x04), run['dcs']  # bit 28, and ECM (27)
        assert engine_frames.decode_dcs(run['dcs'])['ecm'] is True
        sender, receiver = run['sender_ecm'], run['receiver_ecm']
        assert sender['frame'] == receiver['frame'] == frame and sender['small'] == (frame == 64)
        assert sender['pages'] == receiver['pages'] == 2 and sender['blocks'] == receiver['blocks']
        assert sender['ppr'] == receiver['ppr'] == 0 and sender['bad'] == 0
    usual, small = found[256]['sender_ecm'], found[64]['sender_ecm']
    assert usual['blocks'] == 2 and small['blocks'] == 4, (usual, small)
    assert small['frames'] >= 4 * usual['frames'] - 8  # the same octets in four times the frames
    print({frame: {'seconds': run['seconds'], 'sender_ecm': run['sender_ecm']} for frame, run in found.items()})


def test_the_error_record_counts_the_requests_a_bursty_loss_causes_on_both_sides(replay):
    """Bursts of lost packets towards the receiver (two-state model: 2% start a burst, half end each packet) spoil
    frames; the receiver asks for them again (PPR) and the sender resends them. Both sides' records agree on the
    requests and the frames they named, the runs are counted, and the pages still arrive whole."""
    found = ecm_pair(replay, 256, 0.02, 0.5, 7)
    _, pages = dense_tiff()
    sender, receiver = found['sender_ecm'], found['receiver_ecm']
    assert found['status'] == '0 0' and same_pages(found['received'], pages), found
    assert sender['ppr'] > 0 and sender['bad'] > 0 and 0 < sender['runs'] <= sender['bad'], sender
    assert (sender['ppr'], sender['bad'], sender['runs'], sender['resent']) == (
        receiver['ppr'], receiver['bad'], receiver['runs'], receiver['resent']), found
    assert sender['rounds'] >= 1 and sender['round_ms'] > 0
    print({'lost': found['lost'], 'seconds': found['seconds'], 'sender_ecm': sender})


def test_protection_by_category_sends_copies_the_receiver_drops_and_loses_fewer_packets(replay):
    """Patch 0008's protection: each packet goes as many times as its category asks, with one sequence number. Over
    the same loss model and seeds (no UDPTL redundancy in this harness: a lost packet is lost), every protected fax
    arrives whole and sooner, where unprotected ones fail or wait out T.30 timeouts; a delivered fax matches pixel
    for pixel, so no copy was taken twice."""
    _, pages = dense_tiff()
    results = {'plain': [], 'protected': []}
    for seed in (3, 5, 9, 13):
        unprotected = ecm_pair(replay, 256, 0.02, 0.5, seed)
        both = ecm_pair(replay, 256, 0.02, 0.5, seed, '4.3.4.1.4')
        assert int(both['packets']) > int(unprotected['packets'])  # the copies went
        for name, found in (('plain', unprotected), ('protected', both)):
            delivered = found['status'] == '0 0'
            # A fax that arrives arrives whole: no copy was taken twice.
            assert not delivered or same_pages(found['received'], pages), (name, seed)
            results[name].append({'seed': seed, 'delivered': delivered, 'status': found['status'],
                                  'missing': int(found['missing']), 'seconds': float(found['seconds']),
                                  'ppr': found['sender_ecm']['ppr'], 'resent': found['sender_ecm']['resent']})
    print({'protection_replay': results})
    delivered = {name: sum(row['delivered'] for row in rows) for name, rows in results.items()}
    assert delivered['protected'] == 4 and delivered['protected'] >= delivered['plain'], delivered
    # Where both delivered, protection made the call shorter: a lost control packet costs a T.30 timeout.
    both = [(plain, protected) for plain, protected in zip(results['plain'], results['protected'])
            if plain['delivered'] and protected['delivered']]
    assert both and sum(row[1]['seconds'] for row in both) < sum(row[0]['seconds'] for row in both), both
