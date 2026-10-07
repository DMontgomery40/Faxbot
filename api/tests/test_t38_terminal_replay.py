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


def lines(packets):
    return ''.join(f'{at:.6f} {seq} {ifp}\n' for seq, (at, ifp) in enumerate(packets))


def answer_sequence():
    csi, moment = hdlc(1.0, CSI, last=False)
    dis, _ = hdlc(moment, DIS)
    return lines([(0.0, NO_SIGNAL)] + csi + dis)


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
        (context / 'data' / 'sender.ifp').write_text(sender_sequence())
        (context / 'data' / 'proof.tif').write_bytes(tiff)
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


def test_a_spandsp_other_than_the_pinned_one_still_stops_the_build(replay):
    probe = ('set -e; mkdir -p /tmp/other/spandsp; sed "s/^#define SPANDSP_RELEASE_DATE .*/#define '
             'SPANDSP_RELEASE_DATE 20140101/" /usr/include/spandsp/version.h > /tmp/other/spandsp/version.h; '
             'printf "#define SPANDSP_EXPOSE_INTERNAL_STRUCTURES\\n#include <spandsp.h>\\n#include '
             '\\"faxbot_t38_gateway.h\\"\\n" > /tmp/probe.c; gcc -fsyntax-only -Wall -Werror -I /data /tmp/probe.c '
             '&& echo pinned-builds; if gcc -fsyntax-only -I /tmp/other -I /data /tmp/probe.c 2>/dev/null; then echo '
             'other-builds; fi')
    output = replay(script=probe)
    assert 'pinned-builds' in output and 'other-builds' not in output
