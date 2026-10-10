"""Error correction's frame size and error shape, and T.38 protection by packet category (brief 98 item 1,
migration 0080, Asterisk patch 0008).

Synthetic records only: the engines' reports are written as their events and logs leave them (patch 0008's FaxFrames
and FaxEngineCall fields, the SSL Fax engine's session log), then the choice for the next call is read. The frame-size
model is checked against the research's illustrative reversal (faxbot-deep-opportunities-2026-10-08 section 2: a
128,000-byte payload at 14,400 bit/s with independent bit errors of 1e-4; 64-octet frames win with a 0.25-second
turnaround and lose with one second). Nothing here places a call.
"""
import base64
import json
from pathlib import Path
import subprocess
import uuid

import pytest
import sqlalchemy as sa

from app import ami, engine_ecm, engine_frames, engine_learning, hylafax_engine
from app.schema import upgrade_schema
from api.tests.test_engine_learning import LINE, NUMBER, ago, data_folder, values  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture (SQLite, and PostgreSQL when configured)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(database):  # noqa: F811
    upgrade_schema(database)
    yield database


def ecm_text(frame=256, *, pps=4, blocks=2, frames=300, ppr=2, bad=10, runs=10, longest=1, resent=11, ctc=0,
             edges=1, edge_ms=3300, rounds=2, round_ms=6800, small=0, pages=2):
    return '.'.join(str(value) for value in (frame, pps, blocks, frames, ppr, bad, runs, longest, resent, ctc, edges,
                                             edge_ms, rounds, round_ms, small, pages))


def frames_event(key, *, direction='out', ecm=None, rx=None, protect='', endpoint='trunk-endpoint', caller='',
                 trunk=''):
    return {'Direction': direction, 'JobID': 'job' + key[:12] if direction == 'out' else '',
            'AttemptID': key if direction == 'out' else '', 'UniqueID': key if direction == 'in' else '1791391994.1',
            'Caller': caller, 'Trunk': trunk, 'Endpoint': endpoint, 'Ecm': ecm if ecm is not None else ecm_text(),
            'T38Rx': rx or '', 'T38Protect': protect}


# What the engines report -------------------------------------------------------------------------------------

def test_patch_0008s_fields_read_back_and_anything_else_is_refused():
    found = engine_ecm.parse_ecm(ecm_text(64, small=1))
    assert found['frame_octets'] == 64 and found['small_frames'] == 1 and found['bad_frames'] == 10
    assert found['edge_ms'] == 3300 and found['pages'] == 2
    assert engine_ecm.parse_ecm('0' + ecm_text()[3:])['frame_octets'] is None  # a call without error correction
    for broken in ('', '256.1', ecm_text(128), ecm_text() + '.1', ecm_text().replace('300', '-3'), 'a.b'):
        assert engine_ecm.parse_ecm(broken) is None, broken
    assert engine_ecm.parse_t38_rx('1200.14.2') == {'t38_received': 1200, 't38_recovered': 14, 't38_missing': 2}
    assert engine_ecm.parse_t38_rx('12.1') is None and engine_ecm.parse_t38_rx('x.1.2') is None
    assert engine_ecm.parse_protect('3.2.3.1.3') == '3.2.3.1.3'
    assert engine_ecm.parse_protect('5.2.3.1.3') is None and engine_ecm.parse_protect('3,2,3,1,3') is None
    assert engine_ecm.trunk_of_endpoint('trunk-endpoint') == 'sip'
    assert engine_ecm.trunk_of_endpoint('trunk-sip-leeds-endpoint') == 'sip-leeds'
    assert engine_ecm.trunk_of_endpoint('peer-' + 'a1' * 16 + '-endpoint') is None


def test_each_engines_report_becomes_one_row_kept_once(db):
    key = uuid.uuid4().hex
    built_in = engine_ecm.frames_row(frames_event(key, rx='900.12.3', protect='3.2.3.1.3'))
    assert (built_in['direction'], built_in['call_key'], built_in['source'], built_in['trunk']) == (
        'outbound', key, 'builtin', 'sip')
    assert (built_in['t38_received'], built_in['t38_recovered'], built_in['t38_missing']) == (900, 12, 3)
    assert built_in['t38_protect'] == '3.2.3.1.3' and built_in['frame_octets'] == 256
    received = engine_ecm.frames_row(frames_event('1791391994.7', direction='in', caller=NUMBER, trunk='sip-leeds',
                                                  ecm=ecm_text(0), rx='500.0.0'))
    assert (received['direction'], received['number'], received['trunk'], received['frame_octets']) == (
        'inbound', NUMBER, 'sip-leeds', None)
    # Nothing to keep: no error correction and no T.38 counts.
    assert engine_ecm.frames_row(frames_event(key, ecm=ecm_text(0))) is None
    gateway = engine_ecm.gateway_row({'Direction': 'in', 'Token': '17913919947', 'Caller': NUMBER, 'Trunk': '',
                                      'T38Rx': '700.9.1', 'T38Protect': ''})
    assert (gateway['call_key'], gateway['source'], gateway['trunk'], gateway['t38_missing']) == (
        'engine.17913919947', 'gateway', 'sip', 1)
    assert engine_ecm.gateway_row({'Direction': 'out', 'Side': 'trunk', 'AttemptID': key, 'T38Rx': '1.0.0'}) is None
    sent = engine_ecm.gateway_row({'Direction': 'out', 'Side': 'engine', 'AttemptID': key, 'JobID': 'job1',
                                   'Endpoint': 'trunk-sip-leeds-endpoint', 'T38Rx': '10.0.0'})
    assert (sent['call_key'], sent['trunk'], sent['attempt_id']) == (key, 'sip-leeds', key)
    store = engine_ecm.EcmStore(db)
    assert store.record(built_in) and not store.record(built_in)  # a replayed event is kept once
    assert store.record(received) and store.record(gateway)
    rows = store.rows(direction=None)
    assert len(rows) == 3 and {row['source'] for row in rows} == {'builtin', 'gateway'}
    assert store.rows(number=NUMBER, direction='inbound')[0]['call_key'] in ('1791391994.7', 'engine.17913919947')
    with pytest.raises(engine_ecm.EcmStoreError):
        engine_ecm.EcmStore(sa.create_engine('sqlite://'))


SESSION_LOG = [
    (0, 'SESSION BEGIN 000000001 15555550199'),
    (1, 'REMOTE supports T.30 Annex A, 256-byte ECM'),
    (2, 'SEND training at v.17 14400 bit/s'), (3.5, 'TRAINING succeeded'), (3.6, 'USE 14400 bit/s'),
    (3.6, 'USE error correction mode'),
    *((4 + i * 0.5, f'SEND send frame number {i}') for i in range(10)),
    (10, 'SEND send PPS (partial page signal)'), (10.01, 'SEND send NULL (more blocks, same page)'),
    (11.2, 'SEND recv PPR (partial page request)'),
    (13.3, 'SEND send frame number 2'), (13.4, 'SEND send frame number 3'), (13.5, 'SEND send frame number 7'),
    (14, 'SEND send PPS (partial page signal)'), (14.01, 'SEND send NULL (more blocks, same page)'),
    (15, 'SEND recv MCF (message confirmation)'),
    *((17.3 + i * 0.2, f'SEND send frame number {i}') for i in range(5)),
    (20, 'SEND send PPS (partial page signal)'), (20.01, 'SEND send EOP (no more pages or documents)'),
    (21, 'SEND recv MCF (message confirmation)'), (22, 'SESSION END'),
]


def session_log(path, lines):
    text = []
    for at, message in lines:
        moment = 18 * 3600 + at
        hours, rest = divmod(moment, 3600)
        minutes, seconds = divmod(rest, 60)
        text.append(f'Oct 10 {int(hours):02d}:{int(minutes):02d}:{seconds:05.2f}: [  100]: {message}')
    path.write_text('\n'.join(text) + '\n')
    return path


def negotiation(path, shell='sh'):
    result = subprocess.run([shell, str(ROOT / 'hylafax' / 'bin' / 'negotiation'), str(path)], capture_output=True,
                            text=True, check=True)
    return json.loads(base64.b64decode(result.stdout))


def test_the_ssl_fax_engines_session_log_gives_the_same_counts(tmp_path, db):
    """hylafax/bin/negotiation reads a sent call's partial pages from its session log: a 10-frame block whose first
    request named frames 2, 3 and 7 (two runs, the longest two frames), resent 3.3 s after the PPS; the block edge
    (PPS-NULL answered MCF) 3.3 s before the next block's first frame; one page."""
    found = negotiation(session_log(tmp_path / 'c1', SESSION_LOG))
    assert {key: found[key] for key in found if key.startswith('ecm_')} == {
        'ecm_frame': 256, 'ecm_pages': 1, 'ecm_pps': 3, 'ecm_blocks': 2, 'ecm_frames': 15, 'ecm_ppr': 1,
        'ecm_bad': 3, 'ecm_runs': 2, 'ecm_longest': 2, 'ecm_resent': 3, 'ecm_ctc': 0, 'ecm_edges': 1,
        'ecm_edge_ms': 3300, 'ecm_rounds': 1, 'ecm_round_ms': 3300}
    # The other machine prefers 64-byte frames; a call without partial pages reports none of these.
    sixty_four = [(at, text.replace('256-byte', '64-byte')) for at, text in SESSION_LOG]
    assert negotiation(session_log(tmp_path / 'c2', sixty_four))['ecm_frame'] == 64
    plain = [(at, text) for at, text in SESSION_LOG if 'frame number' not in text and 'PPS' not in text]
    assert not any(key.startswith('ecm_') for key in negotiation(session_log(tmp_path / 'c3', plain)))
    # Kept as a hylafax row; the job's own request for 64-byte frames (bin/notify's ecm_small) says what was sent.
    encoded = base64.b64encode(json.dumps(found).encode()).decode()
    key = uuid.uuid4().hex
    assert engine_ecm.record_engine_report(db, direction='outbound', call_key=key, negotiation_b64=encoded,
                                           small=True, number=NUMBER, trunk='sip')
    (row,) = engine_ecm.EcmStore(db).rows(number=NUMBER)
    assert (row['source'], row['frame_octets'], row['small_frames'], row['bad_frames'], row['bad_runs']) == (
        'hylafax', 64, 1, 3, 2)
    assert not engine_ecm.record_engine_report(db, direction='outbound', call_key=key, negotiation_b64=encoded)


@pytest.mark.skipif(not __import__('shutil').which('gawk'), reason='The engine image runs gawk')
def test_the_session_log_reads_the_same_with_gnu_awk(tmp_path):
    """The engine image is Debian (gawk); this Mac's awk read it above. Both must agree."""
    path = session_log(tmp_path / 'c1', SESSION_LOG)
    script = (ROOT / 'hylafax' / 'bin' / 'negotiation').read_text().replace("awk '", "gawk '", 1)
    gnu = tmp_path / 'negotiation-gawk'
    gnu.write_text(script)
    assert negotiation(path, shell='sh') == json.loads(base64.b64decode(subprocess.run(
        ['sh', str(gnu), str(path)], capture_output=True, text=True, check=True).stdout))


# The frame size ----------------------------------------------------------------------------------------------

def shape(bursts_per_octet, reach_frames=0.0, frame=256, page_octets=None):
    exposure = 10_000_000
    runs = round(bursts_per_octet * exposure)
    return engine_ecm.ErrorShape(5, exposure, runs, round(runs * (1 + reach_frames)), frame, page_octets)


def test_the_model_reproduces_the_researchs_reversal():
    """Independent bit errors of 1e-4 spoil an octet with probability about 8e-4: 64-octet frames win with a
    0.25-second turnaround and lose with one second (research: 87.4 against 91.8 s, then 102.8 against 98.1 s)."""
    errors = shape(8e-4)
    frame, small, usual = engine_ecm.choose_frame(errors, page_octets=128_000, rate=14_400, turnaround=0.25)
    assert frame == 64 and small == pytest.approx(87.4, abs=2.5) and usual == pytest.approx(91.8, abs=2.5)
    frame, small, usual = engine_ecm.choose_frame(errors, page_octets=128_000, rate=14_400, turnaround=1.0)
    assert frame == 256 and small == pytest.approx(102.8, abs=2.5) and usual == pytest.approx(98.1, abs=2.5)
    # With the measured 3.3-second turnaround a page past 16 KB pays three more block edges at 64 octets.
    assert engine_ecm.choose_frame(errors, page_octets=128_000, rate=14_400, turnaround=3.3)[0] == 256


@pytest.mark.parametrize('page, rate_per_octet, reach, expected', [
    (16_000, 0.0, 0.0, 256),     # no errors on record: 256 (less framing)
    (16_000, 1e-4, 0.0, 256),    # rare errors: not worth four times the frames
    (16_000, 5e-4, 0.0, 64),     # frequent scattered errors on a page inside one 64-octet block
    (16_000, 5e-4, 3.0, 64),     # bursts reaching three 256-octet frames: smaller frames still resend less
    (30_000, 5e-4, 0.0, 256),    # a larger page: the extra block edge costs more than the resends save
])
def test_the_frame_size_follows_the_error_shape_and_the_page(page, rate_per_octet, reach, expected):
    found = shape(rate_per_octet, reach) if rate_per_octet else None
    frame, small, usual = engine_ecm.choose_frame(found, page_octets=page, rate=14_400, turnaround=3.3)
    assert frame == expected, (small, usual)
    assert (usual - small >= engine_ecm.FRAME_MARGIN_SECONDS) == (expected == 64)


def keep(db, *, number=NUMBER, frame=256, bad=40, runs=40, frames=60, pages=1, rx=None, direction='out',
         trunk='sip', when=None, edges=0, edge_ms=0):
    """One call's error-correction record, as the built-in engine reports it."""
    key = uuid.uuid4().hex
    event = frames_event(key, direction=direction, caller=number if direction == 'in' else '',
                         endpoint='trunk-endpoint' if trunk == 'sip' else f'trunk-{trunk}-endpoint', trunk=trunk,
                         ecm=ecm_text(frame, frames=frames, bad=bad, runs=runs, pages=pages, edges=edges,
                                      edge_ms=edge_ms), rx=rx)
    row = engine_ecm.frames_row(event)
    row['number'] = number
    engine_ecm.EcmStore(db).record(row, now=when or ago(hours=1))
    return key


def test_a_number_whose_line_spoils_short_stretches_gets_64_octet_frames_on_both_engines(db, monkeypatch):
    """Three calls to the number, each with a 15 KB page and 40 scattered damaged frames in one block: engine
    learning asks for 64-octet frames, with a sentence; the built-in engine's call carries FAXBOT_ECM_FRAME=64, the SSL
    Fax engine's job ECMTYPE 64BIT after USEECM YES, and the predictor prices the call at 64 octets. Two calls are not
    enough, and a number whose own calls show no errors keeps 256."""
    settings = values()
    for _ in range(2):
        keep(db)
    assert engine_learning.decide(settings, NUMBER, db=db).ecm_frame is None  # two calls: not enough
    keep(db)
    decision = engine_learning.decide(settings, NUMBER, db=db)
    assert decision.ecm_frame == 64 and decision.changed()
    assert any(text.startswith('Faxbot sends this number error-correction frames of 64 bytes, not 256: 3 calls to '
                               'this number with error correction had 120 damaged stretches')
               for text in decision.reasons), decision.reasons
    assert ami.learned_options(decision)['ecm_frame'] == 64
    fields = ami.prepare_originate_fields('job1', NUMBER, '/faxdata/x.tiff', caller_id=LINE, ecm_frame=64,
                                          t38_protect='3.2.3.1.3')
    assert 'FAXBOT_ECM_FRAME=64' in fields['Variable'] and 'FAXBOT_T38_PROTECT=3.2.3.1.3' in fields['Variable']
    with pytest.raises(ValueError):
        ami.prepare_originate_fields('job1', NUMBER, '/x.tiff', caller_id=LINE, ecm_frame=128)
    with pytest.raises(ValueError):
        ami.prepare_originate_fields('job1', NUMBER, '/x.tiff', caller_id=LINE, t38_protect='3,2,3,1,3')
    # The SSL Fax engine: ECMTYPE goes after USEECM YES (alone it would turn error correction on).
    call = hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=True, fine=True, compression='mmr',
                                       learned=decision)
    commands = job_commands(monkeypatch, settings, call)
    assert commands.index('JPARM ECMTYPE 64BIT') > commands.index('JPARM USEECM YES')
    off = hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=False, fine=True, compression='mh',
                                      learned=decision)
    assert 'JPARM ECMTYPE 64BIT' not in job_commands(monkeypatch, settings, off)
    # The predictor's fact is the call's own frame size (its settings, as call_settings gives them), with the same
    # sentence.
    from app.routing.error_correction import fact_for
    fact = fact_for(db, settings, NUMBER, settings=call)
    assert fact.frame_octets == 64 and fact.frame_why in decision.reasons
    assert fact_for(db, settings, NUMBER, settings=off).frame_octets == 256
    # Another number with clean calls of its own keeps 256, whatever the trunk shows.
    other = '+13035550199'
    for _ in range(3):
        keep(db, number=other, bad=0, runs=0)
    assert engine_learning.decide(settings, other, db=db).ecm_frame is None


def job_commands(monkeypatch, settings, call):
    """The hfaxd commands create_job sends for one job, from a stand-in engine (nothing is dialed)."""
    import threading
    from api.tests.test_hylafax_engine import ATTEMPT, JOB, FakeEngine
    password = hylafax_engine.engine_secrets(settings)['submit_password']
    server = FakeEngine(password)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        image = Path(settings.fax_data_dir) / 'fax.tiff'
        image.write_bytes(b'II*\x00synthetic')
        hylafax_engine.create_job(settings, tag=hylafax_engine.new_tag(), job_id=JOB, attempt_id=ATTEMPT,
                                  tiff_path=str(image), settings=call, host='127.0.0.1',
                                  port=server.server_address[1]).discard()
    finally:
        server.shutdown()
        server.server_close()
    return [command for command in server.commands if command.startswith('JPARM')]


def test_the_measured_turnaround_replaces_the_loopbacks_once_three_calls_timed_one(db):
    settings = values()
    for milliseconds in (4100, 4300, 3900):
        keep(db, bad=0, runs=0, edges=1, edge_ms=milliseconds)
    from app.routing.error_correction import Fact, fact_for
    call = hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=True, fine=True, compression='mmr')
    fact = fact_for(db, settings, NUMBER, settings=call)
    assert (fact.block_seconds, fact.block_calls, fact.turnaround) == (4.1, 3, 4.1)
    shown = Fact(True, True, True, fact.frame_octets, fact.block_seconds, fact.block_calls)
    assert shown.sentences()[1] == ('Each 64 KB of a page past the first adds about 4.1 seconds on the line (measured '
                                    'on 3 of your calls).')


# T.38 protection ---------------------------------------------------------------------------------------------

def test_the_trunks_measured_loss_sets_t38_protection_and_reaches_every_call(db, monkeypatch):
    """Packets the far ends sent on a trunk's calls: under 0.5% lost, every packet goes once; from 0.5% the
    handshake's packets go up to three times; from 3%, four. Page data always goes once. Each trunk on its own: a
    lossy second trunk changes nothing on the first."""
    settings = values()
    assert engine_ecm.protection(None, 0) is None
    assert engine_ecm.protection(0.004, 5) is None and engine_ecm.protection(0.01, 2) is None
    assert engine_ecm.protection(0.01, 3) == '3.2.3.1.3' and engine_ecm.protection(0.05, 3) == '4.3.4.1.4'
    for counts in ('3.2.3.1.3', '4.3.4.1.4'):
        assert counts.split('.')[3] == '1'  # image data once
    for _ in range(3):
        keep(db, rx='1000.10.2', bad=0, runs=0)               # 1.2% on the first trunk
        keep(db, rx='1000.0.0', bad=0, runs=0, trunk='leeds')  # nothing lost on the second
    found = engine_learning.t38_protection(db, settings, 'sip')
    assert found.counts == '3.2.3.1.3' and found.calls == 3 and found.loss == pytest.approx(12 / 1002)
    assert found.sentence == ('Fax-over-IP calls on this trunk lost 1.2% of their packets (3 calls recently), so '
                              'Faxbot sends the short packets that carry the fax handshake up to three times; page '
                              'data still goes once, as error correction repairs it.')
    assert engine_learning.t38_protection(db, settings, 'leeds').counts is None
    monkeypatch.setattr(ami, '_database', lambda: db)
    assert ami.t38_protect_for(settings) == '3.2.3.1.3' and ami.t38_protect_for(settings, 'leeds') is None
    # Received calls on either engine read it from Asterisk's database, kept per trunk with the other learned keys.
    store = engine_frames.FrameStore(db)
    families = engine_frames.desired_families(store, db, settings)
    assert families[engine_frames.FAMILY_T38PROTECT] == {'sip': '3.2.3.1.3'}
    # The trunk page (and faxbot delivery providers trunk negotiation) says the same, per trunk.
    view = engine_ecm.trunk_view(db, settings)
    assert view['sentences'] == ['On 6 sent calls with error correction in the last 30 days, the receiving machines '
                                 'asked for 0 damaged frames again, in 0 stretches.']
    (first,) = view['trunks']
    assert (first['trunk'], first['protect'], first['loss_percent']) == ('sip', '3.2.3.1.3', 1.2)
    assert first['sentence'] == 'Telnyx: ' + found.sentence
    # The SSL Fax engine's call plan carries it in a ninth field, for the gateway on the engine's channel.
    fields = {'Channel': f'PJSIP/{NUMBER}@trunk-endpoint', 'CallerID': LINE, 'Variable': ''}
    job, attempt = 'a' * 32, 'b' * 32
    plan = hylafax_engine.call_plan(fields, job, attempt, protect='3.2.3.1.3')
    assert plan.split('/')[6:] == ['', '', '3.2.3.1.3']
    assert hylafax_engine.call_plan(fields, job, attempt).count('/') == 5
    with pytest.raises(ValueError):
        hylafax_engine.call_plan(fields, job, attempt, t38=False, protect='3.2.3.1.3')


def test_the_dialplan_reads_and_reports_patch_0008s_variables():
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    receive = dialplan.split('[faxbot-inbound-receive]', 1)[1].split('\n\n', 1)[0]
    assert ' same => n,Gosub(faxbot-t38-protect,s,1)' in receive
    assert receive.index('faxbot-t38-protect') < receive.index('Gosub(faxbot-engine-in,s,1)')
    lookup = dialplan.split('[faxbot-t38-protect]', 1)[1].split('\n\n', 1)[0]
    assert 'DB_EXISTS(faxbot-t38protect/${FAXBOT_PROTECT_KEY})' in lookup
    assert '${IF($["${FAXBOT_TRUNK}" = ""]?sip:${FAXBOT_TRUNK})}' in lookup
    engine_out = dialplan.split('[faxbot-engine-out]', 1)[1].split('\n\n', 1)[0]
    assert 'Set(FAXBOT_T38_PROTECT=${FILTER(0123456789.,${CUT(FAXBOT_PLAN,/,9)})})' in engine_out
    assert engine_out.index('FAXBOT_T38_PROTECT') < engine_out.index('Set(FAXOPT(gateway)=yes)')
    frames = dialplan.split('[faxbot-frames]', 1)[1].split('\n\n', 1)[0]
    for field in ('Ecm:${FILTER(0123456789.,${FAXBOT_ECM})}', 'T38Rx:${FILTER(0123456789.,${FAXBOT_T38_RX})}',
                  'T38Protect:${FILTER(0123456789.,${FAXBOT_T38_PROTECT_USED})}'):
        assert field in frames
    assert dialplan.count('T38Rx:${FILTER(0123456789.,${FAXBOT_T38_RX})}') == 3  # frames, engine out, engine in
    patch = (ROOT / 'asterisk' / 'patches' / '0008-faxbot-ecm-frames-t38-protection.patch').read_text()
    for name in ('FAXBOT_ECM_FRAME', 'FAXBOT_T38_PROTECT', 'FAXBOT_ECM', 'FAXBOT_T38_RX', 'FAXBOT_T38_PROTECT_USED',
                 'AST_UDPTL_FRFLAG_REPEAT', 'AST_UDPTL_FRFLAG_REPAIRED'):
        assert name in patch, name


# Migration 0080 ----------------------------------------------------------------------------------------------

def test_0080_is_the_head_keeps_one_row_per_call_and_source_and_its_downgrade_keeps_records(db):
    from api.app import schema, schema_engine_package
    from api.tests.test_digital_schema import _downgrade, _table, _tables
    assert schema_engine_package.REVISION == '0080_engine_package' == schema.HEAD
    assert schema.POTS_QUOTE == '0079_pots_quote'
    assert schema_engine_package.TABLES <= schema.STRICT_TABLES and schema_engine_package.TABLES <= _tables(db)
    with db.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    table = _table(db, 'fax_call_ecm')
    base = {'direction': 'outbound', 'call_key': 'k1', 'source': 'builtin', 'created_at': ago(hours=1)}
    with db.begin() as connection:
        connection.execute(table.insert().values(id='r1', **base))
    for bad in ({**base, 'call_key': 'k2', 'source': 'other'}, {**base, 'call_key': 'k3', 'frame_octets': 128},
                {**base, 'call_key': 'k4', 'direction': 'sideways'}, base):
        with pytest.raises(sa.exc.IntegrityError):
            with db.begin() as connection:
                connection.execute(table.insert().values(id=uuid.uuid4().hex, **bad))
    with pytest.raises(schema.SchemaUpgradeError, match='Error-correction records are kept'):
        _downgrade(db, '0079_pots_quote')
    with db.begin() as connection:
        connection.execute(table.delete())
    _downgrade(db, '0079_pots_quote')
    assert 'fax_call_ecm' not in _tables(db)
    schema.upgrade_schema(db)
    assert 'fax_call_ecm' in _tables(db)


# The background work keeps both engines' reports ---------------------------------------------------------------

def test_the_far_end_frames_work_keeps_each_engines_report_once(monkeypatch, tmp_path):
    """FramesWork (the background step that hears Asterisk's events) keeps the built-in engine's FaxFrames report
    and the gateway's FaxEngineCall report as error-correction records, each once; a report that carries neither
    keeps nothing."""
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from api.tests.test_access_management_http import ORIGIN, _environment
    from api.tests.test_screening import FakeAsterisk
    from app import engine_frames_http
    from app.main import app

    class Asterisk(FakeAsterisk):
        _connected = SimpleNamespace(is_set=lambda: True)
        connected_at = 1.0

        def __getattr__(self, name):
            if name.startswith('on_'):
                return lambda callback: None
            raise AttributeError(name)
    _environment(monkeypatch, tmp_path)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}):
        engine = app.state.configuration_runtime.manager.store.engine
        work = engine_frames_http.FramesWork(app, Asterisk({}))
        key = uuid.uuid4().hex
        event = {**frames_event(key, rx='800.4.1'), 'UserEvent': 'FaxFrames', 'Mode': 'T38', 'Status': 'SUCCESS',
                 'Answered': '1791391994', 'Dis': '', 'DcsFirst': '', 'DcsLast': '', 'DcsSent': '1', 'Rates': '20',
                 'Trainings': '1', 'Ftt': '0'}
        for _ in range(2):
            work.heard(event)
        work.gateway({'Direction': 'in', 'Token': '17913919947', 'Caller': NUMBER, 'Trunk': '', 'T38Rx': '600.2.0'})
        work.gateway({'Direction': 'out', 'Side': 'trunk', 'AttemptID': key, 'T38Rx': '600.2.0'})
        rows = engine_ecm.EcmStore(engine).rows(direction=None)
    assert sorted((row['source'], row['direction']) for row in rows) == [('builtin', 'outbound'),
                                                                         ('gateway', 'inbound')]
    built_in = next(row for row in rows if row['source'] == 'builtin')
    assert (built_in['call_key'], built_in['t38_received'], built_in['bad_frames']) == (key, 800, 10)
