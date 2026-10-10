"""JBIG on the SSL Fax engine (M7): measured with jbigkit as HylaFAX+ sends it, and chosen only for a receiving
machine whose capabilities on record list it.

- Measuring: ``pages.coding`` runs jbigkit's full encoder pbmtojbg (the jbg_enc API HylaFAX+ 7.0.11 calls) with
  its T.85 settings (faxd/MemoryDecoder.c++: one layer, order 0, L0 128; plain is options 0, MX 0) and no file
  names. Tuned JBIG (hylafax/patches/0003) is tested in test_encoder_tuning.py.
  A stand-in shows the exact arguments; the real tools run when installed (the API image, CI's image), and were
  run in a Debian trixie container with jbigkit-bin 2.1 on 2026-10-08 (the report has the numbers).
- Choosing: JBIG needs error correction, the SSL Fax engine, and a DIS on record that lists it, from either
  engine: the built-in engine's frames or the SSL Fax engine's "REMOTE format support" line.
"""
import base64
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

from PIL import Image
import pytest

from api.tests.test_dense_pages import ATTEMPT, JOB, PEER, SENT_LOG, _send, installation  # noqa: F401 - fixture
from api.tests.test_hylafax_scripts import engine, run  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture
from app import conversion, fax_negotiation
from app.pages import coding
from app.pages.capability import Capability

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'coding'
KEEP_SHADING = SimpleNamespace(sip_fax_fine=True, fax_friendly_documents='never')
NOW = datetime(2026, 10, 7, 9, 0, 0)
ALL = {'mr': True, 'mmr': True, 'jbig': True, 'ecm': True}


def frames(stem):
    return conversion.read_fax_frames(str(FIXTURES / f'{stem}.fine.g4.tiff'))


def stand_in(tmp_path, name, output):
    """A stand-in tool that writes its arguments to <name>.args and ``output`` to standard output."""
    script = tmp_path / name
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/{name}.args"\ncat > /dev/null\n'
                      f'printf "{output}"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


# Measuring --------------------------------------------------------------------------------------------------

def test_jbig_is_measured_with_the_engines_own_options_and_no_file_names(tmp_path, monkeypatch):
    encoder = stand_in(tmp_path, 'pbmtojbg', 'x' * 7)
    decoder = stand_in(tmp_path, 'jbgtopbm', '')
    monkeypatch.setattr(coding, 'jbig_encoder', lambda: (str(encoder), str(decoder)))
    assert coding.measure(frames('drawn_text'), codings=('JBIG',)) == {'JBIG': (8 * 7,)}
    # One layer, order 0, 128 lines a stripe, options byte 0 (no TPBON), no adaptive template moves:
    # jbg_enc_options(.., 0, 0, 128, 0, 0)
    assert (tmp_path / 'pbmtojbg.args').read_text().split() == ['-q', '-d', '0', '-o', '0', '-s', '128', '-p', '0',
                                                               '-m', '0']


def test_a_jbig_tool_that_fails_leaves_jbig_unmeasured_and_logs_why(tmp_path, monkeypatch, caplog):
    failing = tmp_path / 'pbmtojbg'
    failing.write_text('#!/bin/sh\necho "usage" >&2\nexit 1\n')
    failing.chmod(failing.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(coding, 'jbig_encoder', lambda: (str(failing), str(failing)))
    found = coding.measure(frames('drawn_text'))
    assert 'JBIG' not in found and set(found) == {'MH', 'MR', 'MMR'}
    assert 'JBIG could not be measured on these pages.' in caplog.text


REAL = coding.jbig_encoder()


@pytest.mark.skipif(REAL is None, reason="jbigkit's pbmtojbg and jbgtopbm are not installed here (the API "
                                          "image and the integrator's CI image have them)")
def test_the_real_jbigkit_tools_measure_every_fixture_page_losslessly_as_hylafax_sends_it():
    for stem in ('drawn_text', 'photo', 'shaded_0', 'scan_8'):
        found = coding.measure(frames(stem), check=True)
        # JBIG is the smallest of the four on each of these pages (2026-10-08, jbigkit-bin 2.1-6.1+b2).
        assert sum(found['JBIG']) < min(sum(found[name]) for name in ('MH', 'MR', 'MMR')), stem
    page = frames('photo')[0]
    pbm = io.BytesIO()
    page.save(pbm, 'PPM')
    encoded = subprocess.run([REAL[0], *coding.jbig_arguments(0, 0)], input=pbm.getvalue(), capture_output=True,
                             check=True).stdout
    # The BIH (T.85 / T.82 6.2): width 1728, L0 128, MX 0, options 0, as HylaFAX+ writes it.
    assert int.from_bytes(encoded[4:8], 'big') == 1728 and int.from_bytes(encoded[12:16], 'big') == 128
    assert encoded[16] == 0 and encoded[19] == 0
    assert coding.measure([page], codings=('JBIG',))['JBIG'] == (8 * len(encoded),)
    with Image.open(io.BytesIO(subprocess.run([REAL[1]], input=encoded, capture_output=True,
                                              check=True).stdout)) as back:
        assert back.convert('1').tobytes() == page.tobytes()


# Choosing ---------------------------------------------------------------------------------------------------

def test_jbig_is_usable_only_when_a_dis_on_record_lists_it_with_error_correction():
    unknown = coding.usable_codings(ecm=True, configured='jbig')
    assert 'JBIG' not in unknown.codings and unknown.left_out['JBIG'] == coding.JBIG_NOT_ON_RECORD
    assert {'MH', 'MR', 'MMR'} <= unknown.codings  # MR and MMR still go to the engine, which falls back by itself
    known = coding.usable_codings(ecm=True, dis=ALL, configured='jbig')
    assert 'JBIG' in known.codings and 'JBIG' not in known.left_out
    without = coding.usable_codings(ecm=True, dis={**ALL, 'jbig': False}, configured='jbig')
    assert without.left_out['JBIG'] == 'The receiving machine does not take JBIG.'
    no_ecm = coding.usable_codings(ecm=False, dis=ALL, configured='jbig')
    assert no_ecm.left_out['JBIG'] == 'JBIG needs error correction, which is off for this call.'
    far = coding.usable_codings(ecm=True, dis={**ALL, 'ecm': False}, configured='jbig')
    assert far.left_out['JBIG'] == 'JBIG needs error correction, which the receiving machine does not have.'


def test_the_newest_dis_from_either_engine_decides():
    seen = datetime(2026, 10, 8, 9, 0)
    engine_said = Capability('unlimited', seen, ecm=True, codings=frozenset({'MH', 'MR', 'MMR', 'JBIG'}))
    assert coding.receiver_dis((), engine_said) == ALL
    assert coding.receiver_dis((), Capability('a4', seen)) is None
    # A built-in engine call after the SSL Fax engine's call: its frames win (here, a machine without JBIG).
    dis_without_jbig = 'ff1380' + '00' * 8
    newer = [{'when': seen + timedelta(hours=1), 'frame': {'dis': dis_without_jbig}}]
    older = [{'when': seen - timedelta(hours=1), 'frame': {'dis': dis_without_jbig}}]
    assert not coding.receiver_dis(newer, engine_said)['jbig']
    assert coding.receiver_dis(older, engine_said)['jbig']


def test_the_built_in_engine_is_never_asked_for_jbig_even_without_a_fallback():
    """A JBIG choice always carries the smallest other measured coding; one built without it (only JBIG measured)
    still asks the built-in engine for MH, never JBIG, which that engine would turn into MMR."""
    pages = frames('shaded_0')
    measured = coding.measure(pages, codings=('MH', 'MR', 'MMR'))
    measured['JBIG'] = (1,)  # JBIG smallest by far
    choice = coding.best_coding(pages, {'MH', 'MR', 'MMR', 'JBIG'}, ecm=True, measured=measured)
    assert choice.coding == 'JBIG' and choice.fallback in ('MH', 'MR', 'MMR')
    alone = coding.CodingChoice('JBIG', (1,), {'JBIG': (1,)}, 'JBIG: the only coding measured.')
    assert alone.fallback is None and alone.request('builtin') == 'MH' and alone.request('hylafax') == 'JBIG'
    from app import hylafax_engine
    call = hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=True, fine=True, compression='jbig')
    assert hylafax_engine.with_coding(call, alone.request('builtin')).compression == 'mh'


def test_jbig_measured_smallest_for_a_known_jbig_machine_is_chosen_and_priced_as_jbig(monkeypatch):
    pages = frames('shaded_0')
    measured = coding.measure(pages, codings=('MH', 'MR', 'MMR'))
    # As jbigkit measured this page with the engine's options (2026-10-08): 321,664 bits.
    measured['JBIG'] = (321_664,)
    usable = coding.usable_codings(ecm=True, dis=ALL, configured='jbig')
    choice = coding.best_coding(pages, usable.codings, ecm=True, measured=measured)
    assert (choice.coding, choice.measured, choice.priced) == ('JBIG', True, 'JBIG')
    assert choice.reason == 'JBIG: 54% shorter than MH for these pages.'
    # The built-in engine has no JBIG: it is asked for the smallest other measured coding, never left to take MMR
    # unasked (loopback case u, 10 October 2026).
    assert (choice.request('hylafax'), choice.request('builtin'), choice.fallback) == ('JBIG', 'MH', 'MH')
    assert usable.needs_request('MH', 'builtin')
    unknown = coding.usable_codings(ecm=True, configured='jbig')
    first = coding.best_coding(pages, unknown.codings, ecm=True, measured=measured, negotiate=True)
    assert (first.coding, first.priced) == ('MH', 'MH') and unknown.needs_request('MH', 'builtin')
    # Not on record: the SSL Fax engine is asked for nothing; its job keeps your setting (JBIG), so the job
    # controls hand faxsend no DesiredDF and it negotiates the most compact coding itself.
    assert first.request('hylafax') is None and first.request('builtin') == 'MH'
    assert not unknown.needs_request(None)
    from app import hylafax_engine
    settings = hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=True, fine=True, compression='jbig')
    assert hylafax_engine.with_coding(settings, first.request('hylafax')) is settings


# The SSL Fax engine reports what the machine takes -------------------------------------------------------------

def test_the_engines_session_log_names_the_codings_the_receiving_machine_takes(engine):  # noqa: F811
    spool, _, _, environment = engine
    path = spool / 'log' / 'c000000021'
    path.write_text(SENT_LOG.replace('REMOTE best rate 14400 bit/s\n',
                                     'REMOTE best rate 14400 bit/s\n'
                                     'Oct 07 10:00:01.00: [  200]: REMOTE format support: MH, MR, MMR, JBIG\n'))
    result = run('negotiation', environment, str(path))
    assert result.returncode == 0, result.stderr
    assert json.loads(base64.b64decode(result.stdout))['remote_codings'] == 'MH,MR,MMR,JBIG'
    assert fax_negotiation.page_capability(result.stdout)['codings'] == 'MH,MR,MMR,JBIG'
    path.write_text(SENT_LOG.replace('REMOTE best rate 14400 bit/s\n',
                                     'REMOTE best rate 14400 bit/s\n'
                                     'Oct 07 10:00:01.00: [  200]: REMOTE format support: MH, MR\n'))
    assert fax_negotiation.page_capability(run('negotiation', environment, str(path)).stdout)['codings'] == 'MH,MR'
    path.write_text(SENT_LOG)  # no format line: nothing said about codings
    assert 'codings' not in fax_negotiation.page_capability(run('negotiation', environment, str(path)).stdout)
    assert fax_negotiation.codings_text('MH,JPEG') is None and fax_negotiation.codings_text('MR,MMR') is None


@pytest.fixture
def ssl_fax_engine_places_calls(monkeypatch):
    """The SSL Fax engine is set up and running, so it places the call (``coding.measuring_tuning``)."""
    from dataclasses import replace
    real = coding.measuring_tuning
    monkeypatch.setattr(coding, 'measuring_tuning', lambda *args, **kwargs: replace(real(*args, **kwargs),
                                                                                    engine='hylafax'))


def test_the_built_in_engine_is_never_priced_or_asked_for_jbig(installation, database, tmp_path):  # noqa: F811
    """With the SSL Fax engine not running, the built-in engine places the call and has no JBIG: a recorded JBIG
    machine still gets the smallest coding the built-in engine sends (loopback case u, 10 October 2026: a payload
    page measured smallest in JBIG went as MMR, the engine's own choice, instead of MR)."""
    installation.record_observation(PEER, source='d' * 32, engine='hylafax', now=NOW,
                                    values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1,
                                            'codings': 'MH,MR,MMR,JBIG'})
    changed = _send(database, tmp_path, pages=frames('shaded_0'), values=KEEP_SHADING)
    assert changed.coding.coding != 'JBIG' and changed.coding.measured
    assert changed.coding.request('builtin') == changed.coding.coding
    usable = coding.usable_for(database, KEEP_SHADING, PEER, capability=installation.capability(PEER))
    assert 'JBIG' not in usable.codings and usable.left_out['JBIG'] == coding.BUILTIN_NO_JBIG


def test_a_recorded_jbig_machine_gets_jbig_on_the_next_fax(installation, database, tmp_path,  # noqa: F811
                                                         ssl_fax_engine_places_calls):
    installation.record_observation(PEER, source='d' * 32, engine='hylafax', now=NOW,
                                    values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1,
                                            'codings': 'MH,MR,MMR,JBIG'})
    assert installation.capability(PEER).codings == frozenset({'MH', 'MR', 'MMR', 'JBIG'})
    changed = _send(database, tmp_path, pages=frames('shaded_0'), values=KEEP_SHADING)
    assert changed.coding.coding == 'JBIG'
    if REAL is None:  # no tools here: JBIG goes unmeasured on the SSL Fax engine, the smallest measured elsewhere
        smallest = min(('MH', 'MR', 'MMR'), key=lambda name: sum(changed.coding.all_measured[name]))
        assert (changed.coding.priced, changed.coding.request('builtin')) == (smallest, smallest)
    else:
        assert changed.coding.measured and changed.coding.priced == 'JBIG'
    assert changed.coding.request('hylafax') == 'JBIG'
    record = coding.newest_coding(database, JOB)
    assert record['requested'] == 'JBIG' and record['receiver_known'] == 1
    # The engine reported a machine without JBIG later: the next fax leaves JBIG out again.
    installation.record_observation(PEER, source='e' * 32, engine='hylafax', now=NOW + timedelta(hours=1),
                                    values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1, 'codings': 'MH,MR,MMR'})
    later = _send(database, tmp_path, pages=frames('shaded_0'), values=KEEP_SHADING, attempt='c' * 32)
    # A known machine without JBIG: the smallest measured coding, asked of both engines.
    smallest = min(('MH', 'MR', 'MMR'), key=lambda name: sum(later.coding.all_measured[name]))
    assert later.coding.coding == smallest == later.coding.request('hylafax') == later.coding.request('builtin')


def test_an_unknown_number_on_the_ssl_fax_engine_is_not_narrowed(installation, database, tmp_path):  # noqa: F811
    changed = _send(database, tmp_path, pages=frames('shaded_0'), values=KEEP_SHADING)
    assert changed.coding.negotiate and changed.coding.request('hylafax') is None
    assert coding.newest_coding(database, JOB)['receiver_known'] == 0


def test_jbig_tools_are_found_on_the_path(tmp_path, monkeypatch):
    for name in ('pbmtojbg', 'jbgtopbm'):
        stand_in(tmp_path, name, '')
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ.get("PATH", "")}')
    assert coding.jbig_encoder() == (str(tmp_path / 'pbmtojbg'), str(tmp_path / 'jbgtopbm'))


def test_the_engine_is_built_with_jbig_and_the_api_image_can_measure_it():
    root = Path(__file__).resolve().parents[2]
    engine_image = (root / 'hylafax' / 'Dockerfile').read_text()
    # The build stops unless HylaFAX+ found jbigkit (configure: jbg_enc_init) and links it.
    assert 'libjbig-dev' in engine_image and "grep -q '^#define HAVE_JBIG 1' config.h" in engine_image
    assert "ldd /usr/lib/libfaxserver.so.7.0.11 | grep -q 'libjbig.so'" in engine_image
    api_image = (root / 'api' / 'Dockerfile').read_text()
    assert 'jbigkit-bin' in api_image and 'command -v pbmtojbg && command -v jbgtopbm' in api_image
    # The GPL source offer is written down beside each install.
    assert 'apt-get source jbigkit' in api_image and 'apt-get source jbigkit' in engine_image
