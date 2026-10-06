"""Proof for asterisk/patches/0002 and 0003: spandsp 0.0.6's T.38 gateway and far-end signals that end early.

0002. Live over Telnyx on 5 October 2026, T.38 sends through the SSL Fax engine failed one call in two: T.38
was agreed and the carrier's packets arrived, but the engine's modem heard an endless V.21 preamble and
never the answer. The carrier's T.38 began with two V.21 preambles closed by an HDLC signal end with no data.

0003. Live on 6 October 2026, the first fax received over T.38 failed. The carrier relayed a training check
its own modem had cut short (7 packets instead of about 51), then two V.29 trainings that ended at once with
no data. The gateway played V.29 fill to the engine for seconds and did not listen to it meanwhile, so the
engine's answer (failure to train) never reached the sender, and the engine gave up waiting for silence.

This test builds asterisk/tests/t38_gateway_replay.c against Debian's spandsp (the library Faxbot's
Asterisk image runs), replays synthetic T.38 sequences with the same structure as the captured calls,
plays the engine's own signals into the gateway, and decodes both directions: what the engine's modem
hears, and the T.38 the gateway sends to the far end. Mode 0 is spandsp as shipped, mode 1 adds 0002 and
mode 2 adds 0003 as well. The captured calls themselves gave the same results (reported with 0003).

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
COPY t38_gateway_replay.c faxbot_t38_gateway.h sequences/ /data/
RUN gcc -O1 -Wall -Werror -o /replay /data/t38_gateway_replay.c -lspandsp -ltiff -lm
'''

# IFPs (T.38 version 0): indicators no-signal, V.21 preamble and V.29 9600 bit/s training; V.21 data: one
# HDLC byte, FCS good, signal end; V.29 9600 data: non-ECM data and non-ECM signal end.
NO_SIGNAL, PREAMBLE, V29_TRAINING = '00', '06', '0e'
FCS_OK, SIG_END = 'c00120', 'c00110'
NON_ECM_END = 'c80170'
# A synthetic DIS: address, final-frame control, DIS, and four capability octets.
DIS = ['ff', 'c8', '01', '00', '46', '1f', '01']
# The DCS of the 6 October calls (9600 bit/s V.29, 2-D MR), octets as T.38 carries them.
DCS = ['ff', 'c8', 'c1', '00', '63', '1e']
# The engine's answer to a training check it refuses: FTT, as the fax machine sends it.
FTT = 'ff1344'


def byte(octet):
    return 'c001800000' + octet


def non_ecm(octets, end=False):
    """Non-ECM V.29 9600 data, ``octets`` zero bytes (the training check), optionally with the signal end."""
    return f"c801{'f0' if end else 'e0'}{octets - 1:04x}" + '00' * octets


def answer(start, data_after, frame=DIS):
    """One V.21 frame: preamble, its octets 20 ms apart starting ``data_after`` later, FCS good, end, no signal."""
    packets = [(start, PREAMBLE)]
    moment = start + data_after
    for octet in frame:
        packets.append((moment, byte(octet)))
        moment += 0.020
    packets += [(moment, FCS_OK), (moment + 0.060, SIG_END), (moment + 0.061, NO_SIGNAL)]
    return packets


def lines(packets):
    return ''.join(f'{at:.6f} {seq} {ifp}\n' for seq, (at, ifp) in enumerate(packets))


def sequence(*, empty_preambles, data_after, repeats=3, every=4.9):
    packets = [(0.0, NO_SIGNAL)]
    moment = 2.0
    for _ in range(empty_preambles):
        # A preamble the far end closed with no frame, as Telnyx relayed an answer begun before T.38.
        packets += [(moment, PREAMBLE), (moment + 0.100, SIG_END), (moment + 0.101, NO_SIGNAL)]
        moment += 0.260
    for number in range(repeats):
        packets += answer(moment + number * every, data_after)
    return lines(packets)


def receive(*, check_packets, empty_trainings):
    """A received fax's opening as Telnyx relayed it on 6 October: DCS, a training check of ``check_packets``
    packets of 36 zero bytes (about 51 make the 1.5 s a sender sends), ``empty_trainings`` V.29 trainings
    that end at once with no data, and the sender's next DCS 5 s later."""
    packets = answer(0.0, 0.800, DCS)
    moment = packets[-1][0] + 0.080
    packets.append((moment, V29_TRAINING))
    moment += 0.260
    for number in range(check_packets):
        packets.append((moment, non_ecm(36, end=number == check_packets - 1)))
        moment += 0.030
    packets.append((moment, NO_SIGNAL))
    for _ in range(empty_trainings):
        packets += [(moment + 0.120, V29_TRAINING), (moment + 0.240, NON_ECM_END), (moment + 0.240, NO_SIGNAL)]
        moment += 0.240
    packets += answer(moment + 5.0, 0.600, DCS)
    return lines(packets)


SEQUENCES = {
    'empty': sequence(empty_preambles=2, data_after=0.300),  # the failed live sends, 5 October
    'clean': sequence(empty_preambles=0, data_after=0.580),  # the live send that worked
    'quick': sequence(empty_preambles=0, data_after=0.040),  # a far end that sends data right away
    'short': receive(check_packets=7, empty_trainings=2),     # the failed live receive, 6 October
    'full': receive(check_packets=51, empty_trainings=0),     # the live receive that worked
    'quiet': lines([(0.0, NO_SIGNAL)]),                       # the far end listens: the engine sends
}
# The training check ends at the far end 0.08 + 0.26 + 7 x 0.03 s after the DCS (2.21 s into 'short').
SHORT_CHECK_END = 0.0 + 0.800 + 6 * 0.020 + 0.061 + 0.080 + 0.260 + 6 * 0.030
MODEMS = {
    'ftt': f'{SHORT_CHECK_END + 0.360:.3f} v21 {FTT}\n',  # the engine refuses the training check
    'send': '1.000 v21 ff138300c678\n2.300 tcf 1.5\n',   # the engine sends a DCS and its training check
}


@pytest.fixture(scope='module')
def replay():
    with tempfile.TemporaryDirectory() as folder:
        context = Path(folder)
        (context / 'sequences').mkdir()
        for name, text in SEQUENCES.items():
            (context / 'sequences' / f'{name}.ifp').write_text(text)
        for name, text in MODEMS.items():
            (context / 'sequences' / f'{name}.modem').write_text(text)
        (context / 't38_gateway_replay.c').write_text(
            (ROOT / 'asterisk' / 'tests' / 't38_gateway_replay.c').read_text())
        # The gateway steps and the spandsp guard, from the file the image build compiles into Asterisk.
        (context / 'faxbot_t38_gateway.h').write_text(
            (ROOT / 'asterisk' / 'patches' / 'faxbot_t38_gateway.h').read_text())
        (context / 'Dockerfile').write_text(DOCKERFILE)
        subprocess.run(['docker', '--context', CONTEXT, 'build', '-q', '-t', IMAGE, str(context)], check=True,
                       capture_output=True, timeout=900)

    def run(name, mode, modem=None):
        command = ['docker', '--context', CONTEXT, 'run', '--rm', IMAGE, '/replay', f'/data/{name}.ifp', str(mode)]
        if modem:
            command.append(f'/data/{modem}.modem')
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)
        # What each side heard; when 0002 or 0003 acted differs by mode by definition.
        return [line for line in result.stdout.splitlines() if not line.startswith(('cut ', 'ended '))]
    yield run
    subprocess.run(['docker', '--context', CONTEXT, 'rmi', IMAGE], capture_output=True, timeout=120)


def kind(lines, prefix):
    return [line.split() for line in lines if line.startswith(prefix)]


# 0002: an empty V.21 preamble ------------------------------------------------------------------------------

def test_spandsp_as_shipped_loses_every_answer_after_an_empty_v21_preamble(replay):
    assert replay('empty', 0)[-1] == 'frames 0'


@pytest.mark.parametrize('mode', [1, 2])
def test_faxbots_fix_delivers_every_answer_after_an_empty_v21_preamble(replay, mode):
    lines = replay('empty', mode)
    assert lines[-1] == 'frames 3'
    # Whole DIS frames: 7 octets as sent (the V.21 receiver reports each octet in transmission bit order).
    assert [frame[2:6] for frame in kind(lines, 'frame ')] == [['7', 'ff', '13', '80']] * 3, lines


@pytest.mark.parametrize('name', ['clean', 'quick'])
def test_faxbots_fix_changes_nothing_without_an_empty_preamble(replay, name):
    shipped = replay(name, 0)
    assert shipped[-1] == 'frames 3' and replay(name, 1) == shipped and replay(name, 2) == shipped


# 0003: a fast modem signal the far end left open -------------------------------------------------------------

@pytest.mark.parametrize('mode', [0, 1])
def test_spandsp_plays_fill_and_never_relays_the_engines_answer_after_an_empty_training(replay, mode):
    lines = replay('short', mode, 'ftt')
    first, *rest = kind(lines, 'data ')
    # The training check the far end cut short, as the engine heard it live (refused: too short).
    assert int(first[2]) < 300
    # Then V.29 fill (all ones) for as long as the far end sends nothing new; the engine is not heard.
    assert rest and int(rest[0][2]) > 3000 and int(rest[0][3]) > 0.95 * int(rest[0][2]), lines
    assert kind(lines, 't38 frame') == []


def test_faxbots_fix_relays_the_engines_answer_within_the_senders_wait(replay):
    lines = replay('short', 2, 'ftt')
    assert kind(lines, 'data ')[0] == kind(replay('short', 0, 'ftt'), 'data ')[0]
    # The two empty trainings end after their training: no fill to speak of.
    assert all(int(data[2]) < 100 for data in kind(lines, 'data ')[1:]), lines
    # The engine's FTT reaches the far end within T.30's 3 s response time after the training check.
    (answer,) = kind(lines, 't38 frame')
    assert answer[3:] == ['3', 'ff', '13', '44'] and float(answer[2]) < SHORT_CHECK_END + 3.0, lines
    # And the engine hears the sender's next DCS.
    assert [frame[2:6] for frame in kind(lines, 'frame ')] == [['6', 'ff', '13', '83']] * 2, lines


def test_a_full_training_check_either_way_is_unchanged(replay):
    received = replay('full', 0)
    (check,) = kind(received, 'data ')
    assert int(check[2]) >= 1800 and int(check[4]) >= 1500, received
    assert replay('full', 1) == received and replay('full', 2) == received
    sent = replay('quiet', 0, 'send')
    (dcs,) = kind(sent, 't38 frame')
    (data,) = kind(sent, 't38 data')
    assert dcs[3:6] == ['6', 'ff', '13'] and int(data[3]) >= 1700 and int(data[5]) >= 1700, sent
    assert replay('quiet', 1, 'send') == sent and replay('quiet', 2, 'send') == sent


# The spandsp version guard ------------------------------------------------------------------------------------

GUARD_PROBE = r'''
set -e
mkdir -p /tmp/other/spandsp
sed 's/^#define SPANDSP_RELEASE_DATE .*/#define SPANDSP_RELEASE_DATE 20140101/' /usr/include/spandsp/version.h \
  > /tmp/other/spandsp/version.h
printf '#define SPANDSP_EXPOSE_INTERNAL_STRUCTURES\n#include <spandsp.h>\n#include "faxbot_t38_gateway.h"\n' > /tmp/probe.c
gcc -fsyntax-only -Wall -Werror -I /data /tmp/probe.c && echo pinned-builds
if gcc -fsyntax-only -I /tmp/other -I /data /tmp/probe.c 2> /tmp/other.err; then echo other-builds; fi
cat /tmp/other.err
'''


def test_a_spandsp_other_than_the_pinned_one_stops_the_build(replay):
    """0002 and 0003 reach into spandsp 0.0.6's internal gateway state: the shared header refuses any other
    spandsp release, so a package update fails the image build instead of shipping unchecked gateway code."""
    result = subprocess.run(['docker', '--context', CONTEXT, 'run', '--rm', IMAGE, 'sh', '-c', GUARD_PROBE],
                            capture_output=True, text=True, timeout=120, check=True)
    assert 'pinned-builds' in result.stdout and 'other-builds' not in result.stdout, result.stdout
    assert 'checked against spandsp 0.0.6 (20110122) only' in result.stdout, result.stdout
