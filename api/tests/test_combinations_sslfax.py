"""Combinations, group 6: SSL Fax vs ordinary negotiation x receiver capability x encoded / packed / header
(brief 104).

The real outbound worker, captured transport, page preparation and engine result route run on a real installation;
only the two network edges are stand-ins: a stand-in for the engine's job port (``tests/test_hylafax_engine.py``'s
``FakeEngine``, which speaks hfaxd's protocol) and a stand-in manager connection that stores call plans, reports the
engine's fax lines and records the calls the built-in engine would place. The engine's state (set up, running,
lines registered) is the files Faxbot reads (``running``). What applied is read back from the fax's own records:
the delivery state and attempt, the engine record (``fax_engine_calls``: which engine, why, SSL Fax used or
offered), the job settings the engine was given, and the page changes.

The invariant:

- the fallback is correct: when the SSL Fax engine cannot place the call, the built-in engine places it, exactly
  once, before anything is dialed, and the engine record says why;
- there is no false success: only the engine's "done" is a delivered fax; a call that ended before any fax data
  is a plain failure, a call with confirmed pages is partly sent, and anything else waits for a person;
- the header line is present: the engine is asked to print Faxbot's header on every page (the built-in engine
  carries the same header), whatever pages go (packed, encoded, standard resolution).

Synthetic documents and numbers; nothing is dialed. SQLite and PostgreSQL.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import shutil
import threading
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from api.app import conversion, hylafax_engine, hylafax_http, hylafax_records, sip_calls
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import OutboundWorker
from api.app.pages import capability
from api.app.routing import predict
from api.app.routing.costs import RateCard, RateTerms, parse_amount
from api.app.routing.destinations import LOCAL, DestinationClass
from api.tests.combinations_support import (  # noqa: F401 - fixtures
    NOW, PEER, Case, applied, database, digests, needs_gs, record, statement_pdf)
from api.tests.test_joint_optimizer import Ami, _Frame
from api.tests.test_rules_delivery import BASE, accept, installation as delivery_installation

GROUP = '6 SSL Fax vs ordinary x capability x encoded/packed/header'
INVARIANT = 'fallback correct (one call, engine record says why); no false success; header line present'
HEADER = 'Synthetic Clinic'
CALLER = '+15555550100'
TRUNK = {**BASE, 'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_ROUTES': '', 'SIP_TRUNK_PRESET': 'telnyx',
         'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1',
         'SIP_TRUNK_CALLER_ID': CALLER, 'ASTERISK_INBOUND_SECRET': 'synthetic-inbound-secret-0123456789',
         'FAX_HEADER': HEADER, 'FAX_FRIENDLY_DOCUMENTS': 'never'}
# A synthetic per-page price for the trunk, so the layout chooser has a bill to compare (no carrier's price).
PER_PAGE = RateCard(None, 'sip', 'outbound', 'Synthetic trunk', 'USD', 0, parse_amount('0.03'), 0, 60, 0, None, NOW)


class EngineAmi(Ami):
    """The stand-in manager connection: stores and deletes call plans, reports ``ready`` registered fax lines,
    hands each call's first record to the call records (as ``sip_calls.attach`` does) and records the calls the
    built-in engine would place."""

    def __init__(self, engine, ready=2):
        super().__init__()
        self.engine, self.ready, self.plans, self.deleted = engine, ready, {}, []

    async def iax_lines_ready(self, prefix):
        return self.ready

    async def db_put(self, family, key, value):
        self.plans[key] = (family, value)

    async def db_del(self, family, key):
        self.deleted.append(key)

    def _emit(self, kind, fields):
        if kind == 'Submission':
            sip_calls.SipCallRecords(self.engine).record_submission(dict(fields))


@contextmanager
def fake_engine(values):
    """The stand-in engine job port, logged in with the installation's own engine password."""
    from api.tests.test_hylafax_engine import FakeEngine
    server = FakeEngine(hylafax_engine.engine_secrets(values)['submit_password'])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def engine_state(values, state):
    """The files Faxbot reads for the engine's state (``tests/test_hylafax_engine.running``)."""
    from api.tests.test_hylafax_engine import running
    from pathlib import Path
    root = Path(values.fax_data_dir)
    if state == 'not set up':
        return
    running(root, values, state='failed' if state == 'stopped' else 'running')
    if state == 'phone connection not restarted':
        (root / 'asterisk' / 'iax.conf.started').write_text('older lines')


@pytest.fixture(scope='module')
def documents(tmp_path_factory):
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages, as acceptance does')
    folder = tmp_path_factory.mktemp('documents')
    found = {}
    for name, document in (('two', statement_pdf(folder / 'two.pdf', pages=2, shaded=False)),
                           ('five', statement_pdf(folder / 'five.pdf', pages=5, shaded=False))):
        conversion.pdf_to_tiff(str(document.pdf), str(folder / f'{name}.tiff'))
        found[name] = SimpleNamespace(document=document, tiff=folder / f'{name}.tiff')
    return found


@pytest.fixture
def trunk(database, tmp_path, monkeypatch):  # noqa: F811 - the fixture
    return make_trunk(database, tmp_path, monkeypatch)


def make_trunk(database, tmp_path, monkeypatch, **environment):  # noqa: F811 - the fixture
    """A SIP trunk installation whose engine result route and records read its own database."""
    env = delivery_installation(database, tmp_path, {**TRUNK, **environment},
                                outbound=ProviderConfiguration('sip', traits={'requires_tiff': True}))
    env.values = env.configuration.read().active.values
    env.runtime = SimpleNamespace(frame=_Frame)
    # The engine's result route and its records read the installation's own database.
    from api.app.routing import background
    monkeypatch.setattr(background, 'installation_engine', lambda app: (database, None))
    monkeypatch.setattr(hylafax_http, '_require_engine', lambda secret: None)
    monkeypatch.setattr(hylafax_http, '_store', lambda request: env.delivery)
    return env


def set_up(env, state, *, ready=2):
    """The engine in ``state`` ('running', 'not set up', 'stopped', 'phone connection not restarted')."""
    from api.app import sip_trunk
    if state != 'not set up':
        sip_trunk.write_asterisk_configuration(env.values)
    engine_state(env.values, state)
    env.ami = EngineAmi(env.engine, ready=ready)


def fax(env, drawn):
    """Accept one fax to PEER and leave its files as acceptance does; its job and its files' hashes."""
    job = accept(env, to=PEER, pages=drawn.document.pages)
    shutil.copyfile(drawn.document.pdf, env.tmp / f'{job}.pdf')
    shutil.copyfile(drawn.tiff, env.tmp / f'{job}.tiff')
    return job, digests(env.tmp / f'{job}.pdf', env.tmp / f'{job}.tiff')


async def send(env, server, monkeypatch, *, refuse=False):
    """One worker step through the real transport, with the engine's job port at the stand-in (or refusing)."""
    defaults = {**hylafax_engine.create_job.__kwdefaults__, 'host': '127.0.0.1', 'port': server.server_address[1],
                'timeout': 5.0}
    monkeypatch.setattr(hylafax_engine.create_job, '__kwdefaults__', defaults)
    if refuse:
        server.password = 'not-the-engine-password'
    facts = predict.RouteFacts('sip', 'Synthetic trunk', DestinationClass(LOCAL, 'US', '+1', PEER),
                               RateTerms(PER_PAGE))
    with predict.facts_source(lambda route, destination, now=None: facts):
        return await OutboundWorker(env.delivery, CapturedTransport(env.delivery, env.runtime, ami=env.ami)).step()


def engine_record(env, attempt):
    return hylafax_records.records_for(env.engine).for_call('outbound', attempt)


def job_settings(server):
    """The job settings the engine was given: {name: value} from its JPARM commands."""
    found = {}
    for command in server.commands:
        if command.startswith('JPARM '):
            name, _, value = command[6:].partition(' ')
            found[name] = value
    return found


def assert_one_call(env, server, job, attempt, originals, *, by):
    """Exactly one call for the attempt, by ``by`` ('hylafax' or 'builtin'); the engine record says which."""
    submitted = server.commands.count('JSUBM')
    if by == 'hylafax':
        assert submitted == 1 and env.ami.calls == []
        assert len(env.ami.plans) == 1 and env.ami.deleted == []  # the plan stays for Asterisk to read once
    else:
        assert submitted == 0 and len(env.ami.calls) == 1 and env.ami.calls[0]['attempt_id'] == attempt
        # A plan stored for a job the engine never took is removed: the engine can never dial it.
        assert sorted(env.ami.deleted) == sorted(env.ami.plans)
    assert env.delivery.get(job)['state'] == 'in_progress'
    found = engine_record(env, attempt)
    assert found is not None and found['engine'] == by
    assert digests(env.tmp / f'{job}.pdf', env.tmp / f'{job}.tiff') == originals
    return found


def assert_header(env, server, job, attempt, *, by, header=HEADER):
    """The header line goes with the call, on whichever engine places it: Faxbot's header text and the sending
    number (47 CFR 68.318(d) asks for the date and time, the sender and the sending number; the engines add the
    date, time and page). An empty header means no header line on either engine."""
    if by == 'hylafax':
        settings = job_settings(server)
        # The station ID the call sends is the sending number, printed in the line as %l: the job's own (a reply
        # number) or, without one, the engine's (engine.conf, written from the trunk's caller ID).
        if 'TSI' in settings:
            assert settings['TSI'].strip('"').replace(' ', '') == CALLER
        else:
            conf = hylafax_engine.engine_conf_path(env.values).read_text().splitlines()
            assert f'station_id={CALLER}' in conf
        if header:
            assert settings['USETAGLINE'] == 'YES' and header in settings['TAGLINE'] and '%%l' in settings['TAGLINE']
        else:
            assert settings['USETAGLINE'] == 'NO' and 'TAGLINE' not in settings
    else:
        from api.app.ami import originate_fields_for
        fields = originate_fields_for(env.values, job, PEER, env.ami.calls[0]['tiff'], attempt_id=attempt)
        assert 'FAXHEADER64=' + base64.b64encode(header.encode()).decode() in fields['Variable']
        assert 'FAXSTATION64=' + base64.b64encode(CALLER.encode()).decode() in fields['Variable']


# Which engine places the call: SSL Fax, or the ordinary built-in engine as the fallback ----------------------

FALLBACKS = [
    ('running', 2, False, 'hylafax', None),
    ('not set up', 2, False, 'builtin', hylafax_engine.NOT_RUNNING),
    ('stopped', 2, False, 'builtin', hylafax_engine.NOT_RUNNING),
    ('running', 0, False, 'builtin', hylafax_engine.LINES_NOT_READY),
    ('phone connection not restarted', 2, False, 'builtin', hylafax_engine.ASTERISK_NOT_CURRENT),
    # The engine is running but refuses the job (its login fails): the built-in engine, before anything is dialed.
    ('running', 2, True, 'builtin', hylafax_engine.NOT_RUNNING),
]


@needs_gs
@pytest.mark.asyncio
@pytest.mark.parametrize('state, ready, refuse, by, reason', FALLBACKS,
                         ids=['running', 'not set up', 'stopped', 'lines not ready', 'phone not restarted',
                              'job refused'])
async def test_the_engine_or_its_fallback_places_exactly_one_call_with_the_header(trunk, monkeypatch, documents,
                                                                                  state, ready, refuse, by, reason):
    set_up(trunk, state, ready=ready)
    job, originals = fax(trunk, documents['two'])
    with fake_engine(trunk.values) as server:
        assert await send(trunk, server, monkeypatch, refuse=refuse) is True
        attempt = trunk.delivery.get(job)['attempt_id']
        found = assert_one_call(trunk, server, job, attempt, originals, by=by)
        assert found['reason'] == reason
        assert_header(trunk, server, job, attempt, by=by)
    record(Case(GROUP, f'engine {state}{", job refused" if refuse else ""}{", no lines" if not ready else ""}',
                ('sslfax',), (by,), INVARIANT, 'pass' if by == 'hylafax' else 'impossible',
                alternate=None if by == 'hylafax' else f'the built-in engine placed the call: "{reason}"'))


# What the call negotiated, as the engine reports it: SSL Fax, or ordinary pages on the same call -----------------

ENGINE_ID = '0123456789abcdef'


def b64(text):
    return base64.b64encode(text.encode()).decode()


def epoch(moment):
    return str(int((moment - datetime(1970, 1, 1)).total_seconds()))


def call_events(env, job, attempt, *, session='1', state='ENABLED', rtp_rx=''):
    """The engine channel's and the trunk channel's own hang-up events, as Asterisk reports them before the
    engine's result arrives (tests/test_hylafax_results.py)."""
    calls = sip_calls.SipCallRecords(env.engine)
    started = datetime.utcnow() - timedelta(seconds=60)
    calls.record_engine_call({'Direction': 'out', 'Side': 'engine', 'JobID': job, 'AttemptID': attempt,
                              'T38Session': session, 'Cause': '16', 'Started': epoch(started),
                              'Answered': epoch(started), 'Ended': epoch(started + timedelta(seconds=40)),
                              'CallID64': b64('synthetic-call')})
    calls.record_engine_call({'Direction': 'out', 'Side': 'trunk', 'JobID': job, 'AttemptID': attempt, 'T38': state,
                              'Cause': '16', 'RtpRx': rtp_rx, 'CallID64': b64('synthetic-call')})


async def report(env, job, attempt, why, **fields):
    """The engine's notify script posts its result for the job (``/_internal/hylafax/result``)."""
    payload = {'tag': f'{job}.{attempt}', 'why': why, 'engine_id': ENGINE_ID, 'commid': '000000001', **fields}
    return await hylafax_http.engine_result(SimpleNamespace(app=None), payload, x_internal_secret='synthetic')


def attempt_row(env, attempt):
    with env.engine.connect() as connection:
        attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=connection)
        return dict(connection.execute(sa.select(attempts).where(attempts.c.id == attempt)).mappings().one())


STATION = '+1 555 555 0199'
RESULTS = [
    # (case, why, fields, expected state, expected category, the engine record's SSL Fax use)
    ('ssl fax used', 'done', {'dials': 1, 'pages': 2, 'sslfax': True, 'sslfax_offered': True,
                              'remote_station_b64': STATION}, 'success', None, True),
    # The receiving machine has no SSL Fax (it did not say it takes it when it answered): an ordinary fax.
    ('ordinary, receiver without ssl fax', 'done', {'dials': 1, 'pages': 2, 'sslfax': False,
                                                   'sslfax_offered': False, 'remote_station_b64': STATION},
     'success', None, False),
    # The receiving machine takes SSL Fax but the connection to it was refused (a private address): the pages went
    # on the same call as an ordinary fax, and the number is still known to take SSL Fax.
    ('ordinary, ssl fax connection refused', 'done', {'dials': 1, 'pages': 2, 'sslfax': False,
                                                      'sslfax_offered': True, 'sslfax_refused': True,
                                                      'remote_station_b64': STATION}, 'success', None, False),
    ('busy before any fax data', 'failed', {'dials': 1, 'pages': 0, 'status_code': 'E001',
                                            'status_b64': 'Busy signal detected {E001}'}, 'failed', None, False),
    ('never dialed', 'failed', {'dials': 0, 'pages': 0, 'status_b64': 'Job aborted by request'},
     'failed', None, False),
    ('one page confirmed, then the call failed', 'failed', {
        'dials': 1, 'pages': 1, 'sslfax': True, 'remote_station_b64': STATION,
        'status_b64': 'No response to MPS repeated 3 tries {E150}'}, 'failed', 'partly_sent', True),
    ('answered, no page confirmed', 'failed', {
        'dials': 1, 'pages': 0, 'remote_station_b64': STATION,
        'status_b64': 'No response to EOP repeated 3 tries {E151}'},
     'reconciliation_required', 'pages_unconfirmed', False),
    ('removed after it dialed', 'killed', {'dials': 1, 'pages': 0}, 'reconciliation_required', 'pages_unconfirmed',
     False),
]


def encoded(fields):
    return {name: b64(value) if name.endswith('_b64') else value for name, value in fields.items()}


@needs_gs
@pytest.mark.asyncio
@pytest.mark.parametrize('case, why, fields, state, category, sslfax', RESULTS, ids=[row[0] for row in RESULTS])
async def test_only_the_engines_done_is_a_delivered_fax_and_ssl_fax_use_is_recorded_as_reported(
        trunk, monkeypatch, documents, case, why, fields, state, category, sslfax):
    set_up(trunk, 'running')
    job, originals = fax(trunk, documents['two'])
    with fake_engine(trunk.values) as server:
        assert await send(trunk, server, monkeypatch) is True
        attempt = trunk.delivery.get(job)['attempt_id']
        assert_one_call(trunk, server, job, attempt, originals, by='hylafax')
        assert job_settings(server)['USESSLFAX'] == 'YES'
    call_events(trunk, job, attempt)
    answer = await report(trunk, job, attempt, why, **encoded(fields))
    found = trunk.delivery.get(job)
    # No false success: the fax is delivered only when the engine said done.
    assert (found['state'] == 'success') == (why == 'done')
    assert found['state'] == state, (answer, found)
    assert attempt_row(trunk, attempt)['error_category'] == category
    if state == 'reconciliation_required':
        assert answer == {'status': 'uncertain'}
    engine = engine_record(trunk, attempt)
    assert engine['engine'] == 'hylafax' and bool(engine['sslfax']) == sslfax
    if why == 'done':
        # What the receiving machine showed about SSL Fax is kept for its number (Recipients, Details).
        accepts = hylafax_records.records_for(trunk.engine).accepts_sslfax(PEER)
        assert accepts is not None and accepts['accepts'] is fields['sslfax_offered']
    # An uncertain, partly sent or failed fax is never sent again by itself.
    if state != 'success':
        with fake_engine(trunk.values) as again:
            await send(trunk, again, monkeypatch)
            assert again.commands.count('JSUBM') == 0 and trunk.ami.calls == []
        assert trunk.delivery.get(job)['attempt_id'] == attempt
    record(Case(GROUP, f'engine result: {case}', ('sslfax',), (f'state:{found["state"]}', f'sslfax:{sslfax}'),
                INVARIANT))


@needs_gs
@pytest.mark.asyncio
async def test_with_ssl_fax_turned_off_the_engine_sends_ordinary_pages_and_records_no_ssl_fax(
        database, tmp_path, monkeypatch, documents):  # noqa: F811 - the fixture
    env = make_trunk(database, tmp_path, monkeypatch, SIP_SSLFAX_ENABLED='false')
    set_up(env, 'running')
    job, originals = fax(env, documents['two'])
    with fake_engine(env.values) as server:
        assert await send(env, server, monkeypatch) is True
        attempt = env.delivery.get(job)['attempt_id']
        assert_one_call(env, server, job, attempt, originals, by='hylafax')
        assert job_settings(server)['USESSLFAX'] == 'NO'
        assert_header(env, server, job, attempt, by='hylafax')
    call_events(env, job, attempt)
    await report(env, job, attempt, 'done', dials=1, pages=2, sslfax=False, sslfax_offered=False)
    assert env.delivery.get(job)['state'] == 'success'
    assert engine_record(env, attempt)['sslfax'] is False
    record(Case(GROUP, 'SSL Fax turned off', ('sslfax',), ('hylafax', 'sslfax:False'), INVARIANT, 'impossible',
                alternate='the engine sends ordinary pages (USESSLFAX NO); the header line still goes'))


# The receiving machine's capability x packed / encoded pages x the header line, on the SSL Fax engine ---------

def learn(env, *, max_length='unlimited', ecm=1, codings=None):
    observed = {'max_length': max_length, 'ecm': ecm, 'fine': 1}
    if codings:
        observed['codings'] = ','.join(codings)
    capability.records_for(env.engine).record_observation(PEER, source='d' * 32, engine='hylafax', values=observed,
                                                          now=NOW)


def uploaded(server):
    [image] = server.uploads
    return image


def attempt_file(env, job, attempt):
    return env.tmp / f'packed-{job}-{attempt}.tiff'


@needs_gs
@pytest.mark.asyncio
async def test_packed_pages_go_to_the_engine_exactly_as_prepared_with_the_header_line(trunk, monkeypatch, documents):
    """A receiving machine that takes pages of any length: five pages go as two long pages. The engine gets exactly
    the attempt's packed image, at fine resolution, with Faxbot's header line on each page."""
    set_up(trunk, 'running')
    learn(trunk)
    job, originals = fax(trunk, documents['five'])
    with fake_engine(trunk.values) as server:
        assert await send(trunk, server, monkeypatch) is True
        attempt = trunk.delivery.get(job)['attempt_id']
        assert_one_call(trunk, server, job, attempt, originals, by='hylafax')
        assert_header(trunk, server, job, attempt, by='hylafax')
        settings = job_settings(server)
        image = uploaded(server)
    found = applied(trunk.engine, job, attempt)
    assert found.mechanisms == {'dense'} and found.change['sent_pages'] == 2
    assert hashlib.sha256(image).hexdigest() == hashlib.sha256(attempt_file(trunk, job, attempt).read_bytes()
                                                                ).hexdigest()
    assert len(conversion.read_fax_frames(str(attempt_file(trunk, job, attempt)))) == 2
    assert settings['VRES'] == '196'
    record(Case(GROUP, 'packed pages over the SSL Fax engine', ('sslfax', 'dense', 'header'),
                tuple(sorted(found.mechanisms | {'hylafax', 'header'})), INVARIANT))


@needs_gs
@pytest.mark.asyncio
async def test_encoded_pages_go_to_the_engine_in_the_coding_measured_for_them_with_the_header_line(
        trunk, monkeypatch, documents):
    from api.app import codec
    from api.app.codec.store import CodecSettings
    set_up(trunk, 'running')
    learn(trunk, max_length='a4')
    CodecSettings(trunk.engine).save(PEER, enabled=True, recipient_agreed=True, actor='principal:synthetic', now=NOW)
    job, originals = fax(trunk, documents['five'])
    with fake_engine(trunk.values) as server:
        assert await send(trunk, server, monkeypatch) is True
        attempt = trunk.delivery.get(job)['attempt_id']
        assert_one_call(trunk, server, job, attempt, originals, by='hylafax')
        assert_header(trunk, server, job, attempt, by='hylafax')
        settings = job_settings(server)
        image = uploaded(server)
    found = applied(trunk.engine, job, attempt)
    assert found.mechanisms == {'codec'}
    sent = attempt_file(trunk, job, attempt)
    assert image == sent.read_bytes()
    document, _ = codec.decode_images(conversion.read_fax_frames(str(sent)))
    assert document.data == (trunk.tmp / f'{job}.pdf').read_bytes()
    # The engine is asked for the coding measured smallest for the encoded pages (or its own best, when that is
    # what it would use anyway).
    requested = found.coding['requested']
    assert settings['DATAFORMAT'].strip('"') in (hylafax_engine._DATA_FORMATS[requested.lower()], 'G4', 'JBIG')
    record(Case(GROUP, 'encoded pages over the SSL Fax engine', ('sslfax', 'codec', 'header'),
                tuple(sorted(found.mechanisms | {'hylafax', 'header', f'coding:{requested}'})), INVARIANT))


@needs_gs
@pytest.mark.asyncio
async def test_a_receiver_that_declared_only_mh_gets_mh_from_the_engine(trunk, monkeypatch, documents):
    set_up(trunk, 'running')
    learn(trunk, max_length='a4', codings=('MH',))
    job, originals = fax(trunk, documents['two'])
    with fake_engine(trunk.values) as server:
        assert await send(trunk, server, monkeypatch) is True
        attempt = trunk.delivery.get(job)['attempt_id']
        assert_one_call(trunk, server, job, attempt, originals, by='hylafax')
        assert_header(trunk, server, job, attempt, by='hylafax')
        settings = job_settings(server)
    found = applied(trunk.engine, job, attempt)
    # The pages were measured and priced in MH, the only coding the receiving machine declared. The job's data
    # format is the engine's ceiling (its usual setting, since the engine negotiates MH from that machine's DIS
    # anyway): T.30 negotiation itself is HylaFAX's and needs a real call (combinations-for-live.md).
    assert found.coding is not None and found.coding['requested'] == 'MH'
    assert settings['DATAFORMAT'].strip('"') in hylafax_engine._DATA_FORMATS.values()
    record(Case(GROUP, 'receiver declared MH only', ('sslfax', 'coding'), ('hylafax', 'coding:MH', 'header'),
                INVARIANT))


@needs_gs
@pytest.mark.asyncio
async def test_a_trunk_at_standard_resolution_sends_the_pages_unpacked_at_standard_with_the_header_line(
        database, tmp_path, monkeypatch, documents):  # noqa: F811 - the fixture
    """The engine would draw fine pages again at standard resolution, so nothing is packed for it; the job asks for
    standard resolution and still carries the header line."""
    env = make_trunk(database, tmp_path, monkeypatch, SIP_FAX_FINE='false')
    set_up(env, 'running')
    learn(env)
    job, originals = fax(env, documents['five'])
    with fake_engine(env.values) as server:
        assert await send(env, server, monkeypatch) is True
        attempt = env.delivery.get(job)['attempt_id']
        assert_one_call(env, server, job, attempt, originals, by='hylafax')
        assert_header(env, server, job, attempt, by='hylafax')
        settings = job_settings(server)
        image = uploaded(server)
    found = applied(env.engine, job, attempt)
    assert found.mechanisms == frozenset() and found.change is None
    assert settings['VRES'] == '98'
    # The fax's own image goes, one page per sheet; the engine draws it at standard resolution.
    assert image == (env.tmp / f'{job}.tiff').read_bytes() and not attempt_file(env, job, attempt).exists()
    record(Case(GROUP, 'packing on a standard-resolution trunk (SSL Fax engine)', ('sslfax', 'dense', 'header'),
                tuple(sorted(found.mechanisms | {'hylafax', 'header', 'standard'})), INVARIANT, 'impossible',
                alternate='one page per sheet; the engine sends at standard resolution'))


@needs_gs
@pytest.mark.asyncio
@pytest.mark.parametrize('state, by', [('running', 'hylafax'), ('stopped', 'builtin')], ids=['ssl fax', 'fallback'])
async def test_an_empty_header_is_the_same_on_both_engines_and_the_sending_number_still_goes(
        database, tmp_path, monkeypatch, documents, state, by):  # noqa: F811 - the fixture
    """Faxbot's header text emptied by the administrator: neither engine prints a header line (the built-in engine's
    dialplan skips an empty one), and both still send the sending number as the station ID."""
    env = make_trunk(database, tmp_path, monkeypatch, FAX_HEADER='')
    set_up(env, state)
    job, originals = fax(env, documents['two'])
    with fake_engine(env.values) as server:
        assert await send(env, server, monkeypatch) is True
        attempt = env.delivery.get(job)['attempt_id']
        assert_one_call(env, server, job, attempt, originals, by=by)
        assert_header(env, server, job, attempt, by=by, header='')
    record(Case(GROUP, f'empty header text, {by}', ('header',), (by, 'no header line', 'station ID sent'),
                INVARIANT, 'impossible', alternate='no header line on either engine; the station ID still goes'))
