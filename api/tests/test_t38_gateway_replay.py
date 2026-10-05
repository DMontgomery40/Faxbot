"""Proof for asterisk/patches/0002: spandsp 0.0.6's T.38 gateway and a V.21 preamble that carries no frame.

Live over Telnyx on 5 October 2026, T.38 sends through the SSL Fax engine failed one call in two: T.38 was
agreed and the carrier's packets arrived, but the engine's modem heard an endless V.21 preamble and never
the answer. The carrier's T.38 began with two V.21 preambles closed by an HDLC signal end with no data.
This test builds asterisk/tests/t38_gateway_replay.c against Debian's spandsp (the library Faxbot's
Asterisk image runs), replays synthetic T.38 sequences with the same structure, and decodes the audio the
gateway makes for the fax machine: spandsp as shipped loses every answer after an empty preamble, Faxbot's
fix delivers every one, and sequences without an empty preamble come out the same either way.

Runs only with FAXBOT_NATIVE_PROOF=1 (it builds a small image with Docker).
"""
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh')
IMAGE = os.environ.get('FAXBOT_PROOF_PREFIX', 'faxbot-native-proof') + '-t38-replay:latest'

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to build and run the T.38 gateway replay.'),
]

DOCKERFILE = '''FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libspandsp-dev libtiff-dev \\
    && rm -rf /var/lib/apt/lists/*
COPY t38_gateway_replay.c sequences/ /data/
RUN gcc -O1 -Wall -o /replay /data/t38_gateway_replay.c -lspandsp -ltiff -lm
'''

# IFPs (T.38 version 0): indicators no-signal and V.21 preamble; V.21 data: one HDLC byte, FCS good, signal end.
NO_SIGNAL, PREAMBLE, FCS_OK, SIG_END = '00', '06', 'c00120', 'c00110'
# A synthetic DIS: address, final-frame control, DIS, and four capability octets.
DIS = ['ff', 'c8', '01', '00', '46', '1f', '01']


def byte(octet):
    return 'c001800000' + octet


def answer(start, data_after):
    """One DIS: preamble, its octets 20 ms apart starting ``data_after`` later, FCS good, end, no signal."""
    packets = [(start, PREAMBLE)]
    moment = start + data_after
    for octet in DIS:
        packets.append((moment, byte(octet)))
        moment += 0.020
    packets += [(moment, FCS_OK), (moment + 0.060, SIG_END), (moment + 0.061, NO_SIGNAL)]
    return packets


def sequence(*, empty_preambles, data_after, repeats=3, every=4.9):
    packets = [(0.0, NO_SIGNAL)]
    moment = 2.0
    for _ in range(empty_preambles):
        # A preamble the far end closed with no frame, as Telnyx relayed an answer begun before T.38.
        packets += [(moment, PREAMBLE), (moment + 0.100, SIG_END), (moment + 0.101, NO_SIGNAL)]
        moment += 0.260
    for number in range(repeats):
        packets += answer(moment + number * every, data_after)
    return ''.join(f'{at:.6f} {seq} {ifp}\n' for seq, (at, ifp) in enumerate(packets))


SEQUENCES = {
    'empty': sequence(empty_preambles=2, data_after=0.300),  # the failed live calls
    'clean': sequence(empty_preambles=0, data_after=0.580),  # the live call that worked
    'quick': sequence(empty_preambles=0, data_after=0.040),  # a far end that sends data right away
}


@pytest.fixture(scope='module')
def replay():
    with tempfile.TemporaryDirectory() as folder:
        context = Path(folder)
        (context / 'sequences').mkdir()
        for name, text in SEQUENCES.items():
            (context / 'sequences' / f'{name}.ifp').write_text(text)
        (context / 't38_gateway_replay.c').write_text(
            (ROOT / 'asterisk' / 'tests' / 't38_gateway_replay.c').read_text())
        (context / 'Dockerfile').write_text(DOCKERFILE)
        subprocess.run(['docker', '--context', CONTEXT, 'build', '-q', '-t', IMAGE, str(context)], check=True,
                       capture_output=True, timeout=900)

    def run(name, mode):
        result = subprocess.run(['docker', '--context', CONTEXT, 'run', '--rm', IMAGE, '/replay',
                                 f'/data/{name}.ifp', str(mode)], capture_output=True, text=True, timeout=120,
                                check=True)
        return result.stdout.splitlines()
    yield run
    subprocess.run(['docker', '--context', CONTEXT, 'rmi', IMAGE], capture_output=True, timeout=120)


def test_spandsp_as_shipped_loses_every_answer_after_an_empty_v21_preamble(replay):
    assert replay('empty', 0)[-1] == 'frames 0'


def test_faxbots_fix_delivers_every_answer_after_an_empty_v21_preamble(replay):
    lines = replay('empty', 1)
    assert lines[-1] == 'frames 3'
    # Whole DIS frames: 7 octets as sent (the V.21 receiver reports each octet in transmission bit order).
    assert all(line.split()[2:6] == ['7', 'ff', '13', '80'] for line in lines[:-1]), lines


@pytest.mark.parametrize('name', ['clean', 'quick'])
def test_faxbots_fix_changes_nothing_without_an_empty_preamble(replay, name):
    shipped, fixed = replay(name, 0), replay(name, 1)
    assert shipped[-1] == 'frames 3' and fixed == shipped
