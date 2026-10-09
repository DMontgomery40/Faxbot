"""Lossless encoder tuning (hylafax/patches/0003, faxd/LosslessTuning.h; pages/coding.py, pages/tuning.py).

- Faxbot's measurement equals what the patched faxd sends: the same four formula pages as
  hylafax/tests/lossless_tuning_test.c++ (which runs in the engine's build stage) give its GOLDEN bytes, read from
  that file, so the two can never drift apart. On the 20-page research corpus the two matched byte for byte on
  2026-10-08 (MR tuned and fixed, the one-dimensional row count, and the tuned JBIG setting of every page).
- The MR schedule is the minimum over every legal schedule (brute force), with faxd's tie-break.
- Who gets tuned JBIG: pages over SSL Fax (decided by faxd per page), an enrolled partner whose own Faxbot fax engine
  answers its number, or a number you turned it on for; a refused tuned page makes that number's later calls plain.
- The engine's session log lines reach Faxbot through bin/negotiation, and the job's choice reaches faxsend through
  the job's comments and bin/jobcontrol.
"""
import base64
from datetime import datetime, timedelta
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

from PIL import Image
import pytest
import sqlalchemy as sa

from api.app import schema
from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_hylafax_scripts import engine, run  # noqa: F401 - fixture
from api.tests.test_cli import trunk_cli  # noqa: F401 - fixture
from app.pages import coding, tuning

ROOT = Path(__file__).resolve().parents[2]
CXX_TEST = ROOT / 'hylafax' / 'tests' / 'lossless_tuning_test.c++'
PATCH = ROOT / 'hylafax' / 'patches' / '0003-lossless-tuning.patch'
WIDTH, ROWS = 1728, 240
NOW = datetime(2026, 10, 8, 12, 0)
NUMBER = '+15555550123'
_INVERT = bytes(255 - value for value in range(256))


# The formula pages of hylafax/tests/lossless_tuning_test.c++ --------------------------------------------------

def _pixel(page, x, y, noise):
    if page == 0:
        return (96 <= x < 1632 and 4 <= y % 24 < 16 and ((x // 6) * 7 + (y // 24) * 3) % 5 != 0
                and (x // 6) % 9 != 8), noise
    if page == 1:
        return ((64 <= x < 1664 and 20 <= y < 220 and (x * 3 + y * 5) % 16 == 0)
                or (y in (20, 120, 219) and 64 <= x < 1664) or x in (64, 863)), noise
    if page == 2:
        if y < 40 or y >= 200 or x < 200 or x >= 1500:
            return False, noise
        noise = (noise * 1103515245 + 12345) & 0x7fffffff
        return ((noise >> 16) % 100) < 30, noise
    return y in (10, 229) or x in (40, 1687) or (y % 40 == 20 and 300 < x < 1400), noise


def formula_page(page):
    """The page as Faxbot holds one: Pillow mode "1" (a set bit is paper), fine resolution."""
    raster = bytearray(WIDTH // 8 * ROWS)
    noise = 4242
    for y in range(ROWS):
        for x in range(WIDTH):
            black, noise = _pixel(page, x, y, noise)
            if black:
                raster[y * (WIDTH // 8) + x // 8] |= 0x80 >> (x % 8)
    image = Image.frombytes('1', (WIDTH, ROWS), bytes(raster).translate(_INVERT))
    image.info['dpi'] = (204, 196)
    return image


def golden():
    """GOLDEN from the build-stage test: name -> (mr fixed, mr tuned, fixed and tuned without ECM, JBIG plain,
    JBIG tuned, options, MX)."""
    text = CXX_TEST.read_text()
    found = re.findall(r'\{ "(\w+)",\s*([0-9]+),\s*([0-9]+),\s*([0-9]+),\s*([0-9]+),\s*([0-9]+),\s*([0-9]+),'
                       r'\s*([0-9]+),\s*([0-9]+) \}', text)
    assert len(found) == 4 and int(re.search(r'PADDED = ([0-9]+);', text).group(1)) == 18
    return {name: tuple(int(value) for value in values) for name, *values in found}


@pytest.fixture(scope='module')
def pages():
    return [formula_page(page) for page in range(4)]


def test_mr_as_faxd_sends_it_equals_the_build_stage_golden_bytes(pages):
    gold = golden()
    for page, name in zip(pages, ('text', 'tint', 'noise', 'sparse')):
        fixed, tuned, fixed_padded, tuned_padded = gold[name][:4]
        on = coding.measure([page], codings=('MR',), tuning=coding.Tuning())['MR'][0]
        off = coding.measure([page], codings=('MR',), tuning=coding.Tuning(mr=False))['MR'][0]
        padded = coding.measure([page], codings=('MR',), tuning=coding.Tuning(min_line_bytes=18))['MR'][0]
        plain_padded = coding.measure([page], codings=('MR',),
                                      tuning=coding.Tuning(mr=False, min_line_bytes=18))['MR'][0]
        assert (off, on, plain_padded, padded) == (8 * fixed, 8 * tuned, 8 * fixed_padded, 8 * tuned_padded), name
        assert on <= off and padded <= plain_padded


REAL = coding.jbig_encoder()


@pytest.mark.skipif(REAL is None, reason="jbigkit's pbmtojbg and jbgtopbm are not installed here (the API image has "
                                          "them; run with them on PATH to check)")
def test_jbig_as_faxd_sends_it_equals_the_build_stage_golden_bytes(pages):
    gold = golden()
    for page, name in zip(pages, ('text', 'tint', 'noise', 'sparse')):
        plain, tuned, options, mx = gold[name][4:]
        assert coding.jbig_setting(page, REAL, check=True) == (plain, 0, 0), name
        assert coding.jbig_setting(page, REAL, tuned=True, check=True) == (tuned, options, mx), name
        assert coding.measure([page], codings=('JBIG',), tuning=coding.Tuning(jbig=True))['JBIG'] == (8 * tuned,)


def test_faxd_and_faxbot_try_the_same_jbig_settings_in_the_same_order():
    patch = PATCH.read_text()
    assert 'JBIG_OPTIONS[4] = { 0x00, 0x08, 0x40, 0x48 }' in patch and 'JBIG_MX[3] = { 0, 8, 32 }' in patch
    assert coding.JBIG_CANDIDATES == tuple((options, mx) for options in (0, 8, 64, 72) for mx in (0, 8, 32))
    assert coding.jbig_arguments(72, 8) == ('-q', '-d', '0', '-o', '0', '-s', '128', '-p', '72', '-m', '8')
    assert coding.JBIG_ENCODER == 'pbmtojbg'  # the full jbg_enc API HylaFAX calls, not the T.85 streaming one


def _brute(one, two, k, minimum):
    rows, best = len(one), None
    for choice in itertools.product((True, False), repeat=rows - 1):
        schedule = (True,) + choice
        run_length, legal = 0, True
        for flag in schedule:
            run_length = 0 if flag else run_length + 1
            legal = legal and run_length < k
        if legal:
            total = 2 + sum(coding.mr_row_bytes(one[y] if schedule[y] else two[y], y, rows, minimum)
                            for y in range(rows))
            best = total if best is None else min(best, total)
    return best


def test_the_mr_schedule_is_the_cheapest_legal_one():
    import random
    chance = random.Random(8)
    for _ in range(400):
        rows, k, minimum = chance.randint(1, 9), chance.randint(1, 5), chance.choice((0, 2, 4))
        one = [chance.randint(3, 60) for _ in range(rows)]
        two = [0] + [chance.randint(1, 60) for _ in range(rows - 1)]
        total, chosen = coding.mr_schedule(one, two, k, minimum)
        assert total == _brute(one, two, k, minimum)
        assert chosen[0] and all(not all(not flag for flag in chosen[i:i + k]) for i in range(rows - k + 1))


def test_the_built_in_engine_keeps_libtiffs_mr_and_the_cache_keeps_tunings_apart(pages, tmp_path):
    page = pages[1]
    builtin = coding.measure([page], codings=('MR',), tuning=coding.Tuning(engine='builtin'))
    assert builtin['MR'] == (coding._tiff_bits(page, 'MR'),)
    cache = tmp_path / 'packed.coding.json'
    on = coding.measure_cached([page], cache, codings=('MR',), tuning=coding.Tuning())
    off = coding.measure_cached([page], cache, codings=('MR',), tuning=coding.Tuning(mr=False))
    assert on['MR'][0] < off['MR'][0] and len(json.loads(cache.read_text())) == 2


# Who gets what ------------------------------------------------------------------------------------------------

def test_tuning_follows_your_setting_your_choice_what_was_learned_and_who_answers():
    """Why tuned JBIG is gated: a HylaFAX+ receiver stores a JBIG page's BIE in its TIFF undecoded
    (faxd/CopyQuality.c++ ~350-410, COMPRESSION_JBIG; decoding happens later in libtiff), so it answers MCF
    whatever is inside. A receiver that cannot decode a tuned page would confirm it all the same, and the sender
    would never know. So tuned JBIG goes only over SSL Fax (HylaFAX+ at the other end, decoding with jbigkit's
    full decoder), to a partner whose own Faxbot answers, or where you turned it on for the number."""
    assert tuning.decide() == tuning.CallTuning(mr=True, jbig='sslfax')
    assert tuning.decide().comment() == 'faxbot-tuning mr=on jbig=sslfax'
    assert tuning.decide(setting=False) == tuning.CallTuning(False, 'never', (tuning.SETTING_OFF,))
    assert tuning.decide(choice={'tune': False, 'tune_jbig': True}).jbig == 'never'
    assert tuning.decide(choice={'tune': None, 'tune_jbig': True}).jbig == 'always'
    assert tuning.decide(partner=True).jbig == 'always'
    # A refusal wins over a partner and over the warning switch until you save your choice again.
    refused = tuning.decide(choice={'tune': None, 'tune_jbig': True}, partner=True,
                            learned={'JBIG': tuning.REFUSED['JBIG']})
    assert refused.jbig == 'never' and refused.mr and tuning.REFUSED['JBIG'] in refused.reasons
    assert not tuning.decide(learned={'MR': tuning.REFUSED['MR']}).mr
    assert tuning.CallTuning(jbig='sslfax').priced_jbig(sslfax_expected=True)
    assert not tuning.CallTuning(jbig='sslfax').priced_jbig() and tuning.CallTuning(jbig='always').priced_jbig()


def _report(**changes):
    found = {'tuning_jbig_pages': 3, 'tuning_jbig_tuned': 2, 'tuning_jbig_bytes': 6000, 'tuning_jbig_plain': 30000,
             'tuning_jbig_settings': '8/32,72/8', 'tuning_mr_pages': 0, 'tuning_mr_tuned': 0, 'tuning_mr_bytes': 0,
             'tuning_mr_plain': 0, 'tuning_digests': 'a' * 64, 'tuning_refused': 0, 'tuning_unconfirmed': 0}
    return {**found, **changes}


def test_a_refused_tuned_page_makes_later_calls_plain_until_you_save_the_number_again(database):  # noqa: F811
    schema.upgrade_schema(database)
    values = SimpleNamespace(sip_fax_tune_coding=True)
    assert tuning.for_call(values, database, NUMBER) == tuning.CallTuning()
    assert tuning.record_call(database, call_key='c1', job_id='j1', number=NUMBER, negotiation=_report(),
                              success=True, sslfax=True, now=NOW) == 1
    assert tuning.for_call(values, database, NUMBER).jbig == 'sslfax'  # delivered: nothing learned
    # The receiving machine answered a tuned page with RTN: the next call is plain for JBIG only.
    tuning.record_call(database, call_key='c2', job_id='j2', number=NUMBER, negotiation=_report(tuning_refused=1),
                       success=True, now=NOW + timedelta(minutes=1))
    later = tuning.for_call(values, database, NUMBER)
    assert later.jbig == 'never' and later.mr and later.reasons == (tuning.REFUSED['JBIG'],)
    assert later.comment() == 'faxbot-tuning mr=on jbig=never'
    # Recorded once per call and coding; a failed call whose last tuned page was never confirmed counts too.
    assert tuning.record_call(database, call_key='c2', job_id='j2', number=NUMBER,
                              negotiation=_report(tuning_refused=1), success=True) == 0
    tuning.record_call(database, call_key='c3', job_id='j3', number=NUMBER,
                       negotiation=_report(tuning_jbig_pages=0, tuning_mr_pages=2, tuning_mr_tuned=2,
                                           tuning_mr_bytes=900, tuning_mr_plain=1200, tuning_unconfirmed=1),
                       success=False, now=NOW + timedelta(minutes=2))
    assert not tuning.for_call(values, database, NUMBER).mr
    # Saving your choice again clears what was learned; the warning switch then tunes JBIG for this number.
    tuning.save_choice(database, NUMBER, tune=None, tune_jbig=True, actor='admin', now=NOW + timedelta(minutes=3))
    assert tuning.for_call(values, database, NUMBER) == tuning.CallTuning(True, 'always', (tuning.JBIG_ON,))
    tuning.save_choice(database, NUMBER, tune=False, now=NOW + timedelta(minutes=4))
    assert tuning.for_call(values, database, NUMBER).comment() == 'faxbot-tuning mr=off jbig=never'
    assert tuning.for_call(SimpleNamespace(sip_fax_tune_coding=False), database, '+15555550999').reasons == (
        tuning.SETTING_OFF,)
    rows = tuning.call_tuning(database, 'c1')
    assert rows['JBIG']['settings'] == '8/32,72/8' and rows['JBIG']['sslfax'] == 1 and rows['JBIG']['refused'] == 0
    with pytest.raises(ValueError):
        tuning.save_choice(database, NUMBER, tune=True)


def test_a_partner_qualifies_only_when_its_signed_statement_says_its_own_faxbot_answers(database):  # noqa: F811
    from app.direct.crypto import capabilities, parse_capabilities, parse_own_engine
    schema.upgrade_schema(database)
    said = capabilities(fax_images=True, peer_calls=False, own_engine=True, said_at='2026-10-08T12:00:00Z')
    assert parse_capabilities(said)[:2] == (True, False) and parse_own_engine(said) is True
    older = capabilities(fax_images=True, peer_calls=False, said_at='2026-10-08T12:00:00Z')
    assert parse_capabilities(older) is not None and parse_own_engine(older) is None
    assert parse_capabilities({**older, 'own_engine': 'yes'}) is None
    peers = sa.Table('direct_peers', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(peers.insert().values(
            id='p' * 32, organization='Synthetic Clinic', phone_number=NUMBER, endpoint_url='https://peer.invalid',
            signing_key='a' * 64, exchange_key='b' * 64, state='verified', challenge_failures=0,
            verified_at=NOW, version=1, created_at=NOW, updated_at=NOW))
    assert not tuning.partner_own_engine(database, NUMBER)  # enrolled, but it never said so
    assert not tuning.note_partner_engine(database, 'p' * 32, None, NOW)
    tuning.note_partner_engine(database, 'p' * 32, True, NOW)
    assert tuning.partner_own_engine(database, NUMBER)
    assert tuning.for_call(SimpleNamespace(sip_fax_tune_coding=True), database, NUMBER).jbig == 'always'
    tuning.note_partner_engine(database, 'p' * 32, False, NOW + timedelta(minutes=1))  # now on a cloud service
    assert not tuning.partner_own_engine(database, NUMBER)
    own = SimpleNamespace(effective_inbound='sip', direct_fax_number='(555) 555-0123', fax_default_country='US',
                          sip_trunk_did_list=('+1 555 555 0123',))
    assert tuning.own_engine_answers(own)
    assert not tuning.own_engine_answers(SimpleNamespace(**{**vars(own), 'effective_inbound': 'humblefax'}))


# The engine's side --------------------------------------------------------------------------------------------

LOG = [
    'LOSSLESS TUNING JBIG tuned: options 8, MX 32, 5629 bytes (plain 29003 bytes), 76 ms, raster 1728x2156 sha256 '
    + 'a' * 64,
    'SEND recv MCF (message confirmation)',
    'LOSSLESS TUNING JBIG tuned: options 0, MX 0, 1227 bytes (plain 1227 bytes), 55 ms, raster 1728x2156 sha256 '
    + 'b' * 64,
    'SEND recv MCF (message confirmation)',
    'LOSSLESS TUNING MR schedule: 839 of 2156 rows one-dimensional, 57974 bytes (fixed schedule 88526 bytes), '
    'minimum 0 bytes a line, 5 ms, raster 1728x2156 sha256 ' + 'c' * 64,
    'SEND recv RTN (retrain negative)',
]


def test_the_session_logs_tuning_lines_reach_faxbot(engine):  # noqa: F811
    from api.tests.test_fax_negotiation import log, parsed
    spool, _, _, environment = engine
    found = parsed(spool, environment, log('USE 14400 bit/s', 'USE 2-D MR', 'SEND training at v.17 14400 bit/s',
                                           'TRAINING succeeded', *LOG))
    assert {name: value for name, value in found.items() if name.startswith('tuning_')} == {
        'tuning_jbig_pages': 2, 'tuning_jbig_tuned': 1, 'tuning_jbig_bytes': 6856, 'tuning_jbig_plain': 30230,
        'tuning_jbig_settings': '8/32', 'tuning_mr_pages': 1, 'tuning_mr_tuned': 1, 'tuning_mr_bytes': 57974,
        'tuning_mr_plain': 88526, 'tuning_digests': ','.join(c * 64 for c in 'abc'), 'tuning_refused': 1,
        'tuning_unconfirmed': 0}
    assert found['compression'] == 'MR' and found['rate_first'] == 14400
    assert tuning.refusal_reason(tuning.report(found), success=True)
    # A log without tuning lines reports exactly what it did before.
    plain = parsed(spool, environment, log('USE 14400 bit/s', 'SEND training at v.17 14400 bit/s',
                                           'TRAINING succeeded'))
    assert not any(name.startswith('tuning_') for name in plain) and tuning.report(plain) is None


def test_the_jobs_choice_reaches_faxsend_through_its_comments_and_job_controls(tmp_path):
    script = ROOT / 'hylafax' / 'bin' / 'jobcontrol'
    tools, spool = tmp_path / 'tools', tmp_path / 'spool'
    (spool / 'sendq').mkdir(parents=True)
    tools.mkdir()
    for tool in ('sh', 'sed', 'tail'):
        (tools / tool).symlink_to(shutil.which(tool))

    def controls(job, comments=None):
        lines = f'jobid:{job}\ndesireddf:1\n' + (f'comments:{comments}\n' if comments is not None else '')
        (spool / 'sendq' / f'q{job}').write_text(lines)
        found = subprocess.run([str(tools / 'sh'), str(script), str(job)], cwd=spool, env={'PATH': str(tools)},
                               capture_output=True, text=True, timeout=30)
        assert found.returncode == 0 and found.stderr == '', found
        return found.stdout
    assert controls(1, tuning.CallTuning().comment()) == 'DesiredDF: 1\n'  # the engine's defaults
    assert controls(2, 'faxbot-tuning mr=off jbig=never') == 'DesiredDF: 1\nTuneMR: no\nTuneJBIG: never\n'
    assert controls(3, 'faxbot-tuning mr=on jbig=always') == 'DesiredDF: 1\nTuneJBIG: always\n'
    assert controls(4, 'a cover page note mr=off jbig=always') == 'DesiredDF: 1\n'  # not Faxbot's
    assert controls(5) == 'DesiredDF: 1\n'


def test_the_engine_job_carries_only_a_well_formed_tuning_comment():
    from app import hylafax_engine
    source = (ROOT / 'api' / 'app' / 'hylafax_engine.py').read_text()
    assert "commands.append(f'JPARM COMMENTS {_quote(settings.tuning)}')" in source
    assert hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=True, fine=True, compression='jbig').tuning is None


# The patch, as the image build applies it ----------------------------------------------------------------------

def test_the_patch_tunes_mr_everywhere_and_jbig_over_ssl_fax_by_default_and_is_tested_in_the_build():
    patch = PATCH.read_text()
    assert '+{ "tunemr",			&ModemConfig::tuneMR,			true },' in patch
    assert '+{ "tunejbig",			&ModemConfig::tuneJBIG,		"sslfax" },' in patch
    # The page goes over SSL Fax: faxd decides per page, so a call that falls back sends its later pages plain.
    assert 'convertPhaseCData(dp, totdata, fillorder, params, newparams, rowsperstrip, isSSLFax);' in patch
    # An MR file is coded again when the session is MR too, so the schedule is never skipped.
    assert '(params.df != newparams.df || (newparams.df == DF_2DMR && conf.tuneMR))' in patch
    # faxsend keeps the data format the job asked for (Faxbot measured it) instead of its own MH-versus-MMR guess.
    assert '+\t\tif (!useDF && (params.df == DF_2DMMR || params.df == DF_2DMR) && formatSize[0] < formatSize[1])' in patch
    # Plain JBIG keeps HylaFAX's own settings.
    assert 'encodeJBIG(rasterdst, width, rows, 0, 0, best);' in patch
    assert 'LOSSLESS TUNING JBIG tuned: options %d, MX %d' in patch and 'LOSSLESS TUNING MR schedule:' in patch
    dockerfile = (ROOT / 'hylafax' / 'Dockerfile').read_text()
    assert 'tests/lossless_tuning_test.c++' in dockerfile
    built, tested = dockerfile.index('    && make \\'), dockerfile.index('/usr/src/lossless-tuning-test \\')
    assert built < tested < dockerfile.index('make install INSTALLROOT=/opt/install')
    # Patches apply in name order with --fuzz=0; this one comes after the SSL Fax address policy.
    assert sorted(path.name for path in PATCH.parent.glob('*.patch'))[0] == '0001-sslfax-address-policy.patch'


# The Sent detail ----------------------------------------------------------------------------------------------

def test_the_sent_line_says_when_tuning_ran():
    record = {'requested': 'JBIG', 'negotiated': 'JBIG', 'measured': True, 'compared': 'MMR',
              'reason': 'JBIG: 81% shorter than MMR for these pages.', 'attempt_phase': 'success',
              'tuning': {'JBIG': {'tuned_pages': 2, 'tuned_bytes': 5629, 'plain_bytes': 29003, 'refused': 0}}}
    assert coding.sent_sentence(record) == 'Sent with JBIG, tuned: 81% shorter than MMR for these pages.'
    view = coding.coding_view({**record, 'bits': {}, 'pages': 1, 'receiver_known': 1})
    assert view['tuned'] == ['JBIG'] and view['tuning_sentence'] == (
        'Tuned JBIG sent 5,629 bytes where plain JBIG would have sent 29,003, the same pixels either way.')
    mr = {**record, 'requested': 'MMR', 'negotiated': 'MR', 'reason': 'MMR: 10% shorter than MR for these pages.',
          'tuning': {'MR': {'tuned_pages': 1, 'tuned_bytes': 900, 'plain_bytes': 1200, 'refused': 0}}}
    assert coding.sent_sentence(mr).endswith('The call used MR, tuned schedule.')
    refused = {**record, 'tuning': {'JBIG': {'tuned_pages': 1, 'tuned_bytes': 10, 'plain_bytes': 20, 'refused': 1}}}
    assert coding.coding_view({**refused, 'bits': {}, 'pages': 1})['tuning_sentence'] == tuning.REFUSED['JBIG']
    assert coding.sent_sentence({**record, 'tuning': {}}) == 'Sent with JBIG: 81% shorter than MMR for these pages.'


# The command line and the HTTP API ----------------------------------------------------------------------------

def test_the_command_line_shows_and_sets_smaller_pages_for_one_number(trunk_cli):  # noqa: F811
    shown = trunk_cli('recipients', 'tuning', NUMBER)
    assert shown.exit_code == 0, shown.stdout
    assert 'Smaller pages' in shown.stdout and tuning.SSLFAX in shown.stdout
    turned = trunk_cli('recipients', 'tuning', NUMBER, '--smallest-format', 'on')
    assert turned.exit_code == 0 and tuning.JBIG_WARNING in turned.stdout and tuning.JBIG_ON in turned.stdout
    assert trunk_cli.json('recipients', 'tuning', NUMBER)['jbig'] == 'always'
    off = trunk_cli('recipients', 'tuning', NUMBER, '--smaller', 'off')
    assert off.exit_code == 0 and 'off for this number' in off.stdout and tuning.NUMBER_OFF in off.stdout
    assert trunk_cli('recipients', 'tuning', NUMBER, '--smaller', 'maybe').exit_code != 0
    from api.tests.test_cli import BOOTSTRAP
    refused = trunk_cli.client.put('/routing/destinations/%2B15555550123/coding-tuning',
                                   json={'tune': True, 'tune_jbig': False}, headers={'X-API-Key': BOOTSTRAP})
    assert refused.status_code == 400 and refused.json()['detail'] == 'Choose off, or leave it as set for all faxes.'
