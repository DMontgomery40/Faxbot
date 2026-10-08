"""The built-in engine asks for a subaddress (M17, patch 0005): the Originate field, where it comes from, and the
record of it as requested and, only when the far end takes one, carried.

Synthetic numbers and frames only. The DIS frames are the receiving engine's as the replay proof
(test_t38_terminal_replay.py) gets them from spandsp 0.0.6: with patch 0005 (bit 49 set) and without it.
"""
import logging
from datetime import datetime

import pytest
import sqlalchemy as sa

from app import ami, engine_frames, sip_calls
from app.config_values import ConfigurationValues
from app.schema import create_database_engine, upgrade_schema

NUMBER = '+13035550150'
NOTICE_ID = '73019265018273640192'
# The receiving engine's DIS: with 0005 it says it takes a subaddress (bit 49); spandsp's own default does not.
DIS_TAKES_SUB = 'ff138004eef8c4809181808018'
DIS_NO_SUB = 'ff138004eef8c4809180808018'


def values(**extra):
    return ConfigurationValues.from_environment({'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                                                 'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1',
                                                 'SIP_TRUNK_CALLER_ID': '+13035550100', **extra})


@pytest.fixture
def engine(tmp_path):
    database = create_database_engine('sqlite:///' + str(tmp_path / 'subaddress.db'))
    upgrade_schema(database)
    yield database
    database.dispose()


# -- the Originate field ------------------------------------------------------------------------

def test_the_subaddress_goes_on_the_call_as_the_engine_sends_it():
    fields = ami.prepare_originate_fields('job1', NUMBER, '/faxdata/a.tiff', caller_id='+13035550100',
                                          subaddress=' 40 21 ')
    assert 'FAXBOT_TX_SUB=4021' in fields['Variable'].split(',')
    assert ami.requested_subaddress(fields) == '4021'
    plain = ami.prepare_originate_fields('job1', NUMBER, '/faxdata/a.tiff', caller_id='+13035550100')
    assert 'FAXBOT_TX_SUB' not in plain['Variable'] and ami.requested_subaddress(plain) is None


@pytest.mark.parametrize('value', ['40a1', '4021,FAXBOT_IAF=peer', '123456789012345678901', '', '${EVIL}', 4021])
def test_a_subaddress_a_fax_cannot_carry_is_refused_before_anything_is_sent(value):
    with pytest.raises(ValueError, match='subaddress'):
        ami.prepare_originate_fields('job1', NUMBER, '/faxdata/a.tiff', caller_id='+13035550100', subaddress=value)


def test_the_longest_subaddress_with_every_option_still_fits_one_manager_line():
    fields = ami.prepare_originate_fields(
        'j' * 128, NUMBER, '/faxdata/' + 'a' * 100 + '.tiff', caller_id='+13035550100', header='H' * 120,
        attempt_id='a' * 128, station_id='+1 303 555 0100 xxxxxx', dial='12345678*+13035550150',
        fax_preference=True, max_rate=9600, ecm=True, t38_now=True, iaf='peer', audio=True,
        endpoint='trunk-' + 'k' * 32 + '-endpoint', subaddress=NOTICE_ID)
    assert len(f"Variable: {fields['Variable']}\r\n".encode()) <= ami.AMI_MAX_LINE_BYTES
    assert ami.requested_subaddress(fields) == NOTICE_ID


# -- where it comes from: a notice fax's notice ID first, then the sending rules --------------------------------

def _notice(database, job_id, notice_id=NOTICE_ID):
    """A notice fax as direct/notice.py records it for the sender: its notice ID and the notice fax's own job."""
    now = datetime(2026, 10, 8, 12, 0)
    metadata = sa.MetaData()
    peers = sa.Table('direct_peers', metadata, autoload_with=database)
    notices = sa.Table('direct_notices', metadata, autoload_with=database)
    with database.begin() as connection:
        connection.execute(peers.insert().values(
            id='peer-1', organization='County Clinic', phone_number='+15550100002',
            endpoint_url='https://clinic.example', signing_key='s' * 43, exchange_key='e' * 43, state='verified',
            challenge_failures=0, version=1, created_at=now, updated_at=now))
        connection.execute(notices.insert().values(
            id='n' * 32, role='sender', notice_id=notice_id, message_id='m' * 32, peer_id='peer-1',
            document_sha256='d' * 64, state='queued', link_statement='{}', link_signature='', job_id='original1',
            notice_job_id=job_id, created_at=now, updated_at=now))


def test_a_notice_fax_asks_for_its_notice_id_on_the_built_in_engine(monkeypatch, engine):
    """Read through the real direct/notice.py: the notice fax asks for its notice ID, any other fax for none."""
    _notice(engine, 'notice1')
    monkeypatch.setattr(ami, '_database', lambda: engine)
    trunk = values()
    fields = ami.originate_fields_for(trunk, 'notice1', NUMBER, '/faxdata/notice1.tiff',
                                      choice=ami.reply_choice(trunk))
    assert f'FAXBOT_TX_SUB={NOTICE_ID}' in fields['Variable'].split(',')
    other = ami.originate_fields_for(trunk, 'job1', NUMBER, '/faxdata/job1.tiff', choice=ami.reply_choice(trunk))
    assert 'FAXBOT_TX_SUB' not in other['Variable']


def test_unreadable_notice_records_ask_for_no_notice_id_and_a_bug_raises(monkeypatch, engine, caplog):
    from app.direct import notice
    from app.routing.database import DeliveryStoreError
    _notice(engine, 'notice1')

    def unavailable(self, job_id):
        raise DeliveryStoreError('Delivery storage is unavailable.')
    monkeypatch.setattr(notice.NoticeStore, 'for_job', unavailable)
    with caplog.at_level(logging.WARNING, logger=notice.__name__):
        assert notice.subaddress_for(engine, 'notice1') is None
    assert 'notice records could not be read' in caplog.text

    def broken(self, job_id):
        raise TypeError('a bug in reading notices')
    monkeypatch.setattr(notice.NoticeStore, 'for_job', broken)
    with pytest.raises(TypeError, match='a bug in reading notices'):
        notice.subaddress_for(engine, 'notice1')


def test_the_ssl_fax_engine_asks_for_the_same_subaddress_as_the_built_in_engine(monkeypatch, engine):
    """The SSL Fax engine's job takes ami.fax_subaddress: a notice fax's notice ID (real direct/notice.py), else the
    one the fax's sending rules chose."""
    import asyncio
    from types import SimpleNamespace
    from app import hylafax_engine, outbound_transport
    _notice(engine, 'notice1')
    monkeypatch.setattr(ami, '_database', lambda: engine)
    monkeypatch.setattr(ami, 'rule_subaddress', lambda job_id: '2001' if job_id == 'ruled1' else None)

    async def engine_chosen(values, **kwargs):
        return hylafax_engine.EngineChoice('hylafax')
    asked = {}

    async def prepare_job(values, ami_client, *, job_id, subaddress=None, **kwargs):
        asked[job_id] = subaddress
        return SimpleNamespace(job_id=job_id)
    monkeypatch.setattr(hylafax_engine, 'choose', engine_chosen)
    monkeypatch.setattr(hylafax_engine, 'prepare_job', prepare_job)
    transport = SimpleNamespace(store=SimpleNamespace(configuration=SimpleNamespace(engine=engine)), ami=None)
    for job_id in ('notice1', 'ruled1', 'job1'):
        claim = SimpleNamespace(job_id=job_id, attempt_id='a' * 32, members=())
        asyncio.run(outbound_transport.CapturedTransport._prepare_engine(
            transport, values(), claim, {'to_number': NUMBER}, '/faxdata/a.tiff'))
    assert asked == {'notice1': NOTICE_ID, 'ruled1': '2001', 'job1': None}


# -- requested, and carried only when the far end takes one ------------------------------------------------

def test_the_call_record_keeps_the_subaddress_as_requested(engine):
    records = sip_calls.SipCallRecords(engine)
    records.record_submission({'JobID': 'job1', 'AttemptID': 'attempt1', 'Called': NUMBER,
                               'CallerID': '+13035550100', 'Preset': 'telnyx', 'FaxPreference': 'no',
                               'Subaddress': '4021'})
    records.record_submission({'JobID': 'job2', 'AttemptID': 'attempt2', 'Called': NUMBER,
                               'CallerID': '+13035550100', 'Subaddress': '40;21'})
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        found = dict(connection.execute(sa.select(table.c.attempt_id, table.c.subaddress)).all())
    assert found == {'attempt1': '4021', 'attempt2': None}


@pytest.mark.parametrize('dis, carried, words', [
    (DIS_TAKES_SUB, True, 'takes subaddresses, so it was sent'),
    (DIS_NO_SUB, False, 'does not take subaddresses, so it was not sent'),
    (None, None, 'did not show whether'),
])
def test_carried_only_when_the_far_ends_machine_takes_a_subaddress(dis, carried, words):
    assert engine_frames.decode_dis(DIS_TAKES_SUB)['subaddress'] and not engine_frames.decode_dis(DIS_NO_SUB)['subaddress']
    frame = {'dis': dis} if dis else {}
    assert engine_frames.subaddress_carried('4021', frame) is carried
    assert words in engine_frames.subaddress_sentence('4021', frame)
    assert engine_frames.subaddress_carried(None, frame) is None and engine_frames.subaddress_sentence(None, frame) is None


def test_their_fax_machine_view_says_whether_the_subaddress_was_carried(engine, monkeypatch):
    from app import engine_frames_http
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=engine)
    now = datetime.utcnow()
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job1', to_number=NUMBER, file_name='a.pdf', tiff_path='a.tiff',
                                                status='SUCCESS', backend='sip', created_at=now, updated_at=now))
    sip_calls.SipCallRecords(engine).record_submission({'JobID': 'job1', 'AttemptID': 'attempt1', 'Called': NUMBER,
                                                        'CallerID': '+13035550100', 'Subaddress': '4021'})
    row = engine_frames.parse_event({'UserEvent': 'FaxFrames', 'Direction': 'out', 'JobID': 'job1',
                                     'AttemptID': 'attempt1', 'Mode': 'T38', 'Status': 'SUCCESS',
                                     'Dis': DIS_NO_SUB, 'DcsSent': '1', 'Trainings': '1', 'Ftt': '0'})
    engine_frames.FrameStore(engine).record(row)
    view = engine_frames_http.fax_machine_view(engine, values(), NUMBER)
    call = view['calls'][0]
    assert call['subaddress_requested'] == '4021' and call['subaddress_carried'] is False
    assert any('does not take subaddresses' in sentence for sentence in call['sentences'])


# -- a subaddress the sending rules chose -------------------------------------------------------------------

def _decide(then, to='+13035550150'):
    from app.rules import model
    from app.rules.compile import compile_document
    from app.rules.evaluate import decide
    document = {'format': 1, 'limits': [], 'routes': [{'id': 'r-dept', 'name': 'Cardiology', 'on': True,
                                                         'when': {'destination': {'numbers': [to]}}, 'then': then}]}
    compiled = compile_document(model.RevisionRef('organization', '', 'org-1', 1), document)
    accounts = (model.Account('sip', 'sip', 'Telnyx trunk', default=True, automatic=True),)
    return decide({'organization': compiled}, model.Facts(to, '2026-10-08T15:00:00', country='US'), accounts)


def test_a_routing_rule_can_ask_for_a_subaddress_and_a_bad_one_is_refused():
    from app.rules import model
    from app.rules.compile import DocumentError, compile_document
    decision = _decide({'automatic': True, 'subaddress': '2001'})
    assert decision.envelope.subaddress == '2001' and decision.route.rule_id == 'r-dept'
    # A stored decision from before the setting existed reads back with none, and a new one round-trips.
    stored = model.Decision.from_json(decision.to_json())
    assert stored.envelope.subaddress == '2001'
    old = decision.to_json().replace('"subaddress":"2001",', '')
    assert model.Decision.from_json(old).envelope.subaddress is None
    assert _decide({'automatic': True}).envelope.subaddress is None
    for bad in ('20a1', '123456789012345678901', 2001):
        with pytest.raises(DocumentError, match='a subaddress is up to 20 digits'):
            compile_document(model.RevisionRef('organization', '', 'org-2', 2), {
                'format': 1, 'limits': [], 'routes': [{'id': 'r-bad', 'name': 'Bad', 'on': True, 'when': {},
                                                       'then': {'automatic': True, 'subaddress': bad}}]})
    with pytest.raises(ValueError):
        model.Envelope(mode='automatic', subaddress='20;01')


def test_the_rule_subaddress_reaches_the_call_and_a_notice_id_comes_first(monkeypatch, engine):
    from app.routing import envelope as envelopes
    from types import SimpleNamespace
    pinned = SimpleNamespace(envelope=_decide({'automatic': True, 'subaddress': '2001'}).envelope)
    monkeypatch.setattr(envelopes, 'load', lambda database, job_id: pinned if job_id != 'old' else None)
    monkeypatch.setattr(ami, '_database', lambda: engine)
    trunk = values()
    fields = ami.originate_fields_for(trunk, 'job1', NUMBER, '/faxdata/job1.tiff', choice=ami.reply_choice(trunk))
    assert ami.requested_subaddress(fields) == '2001'
    assert ami.rule_subaddress('old') is None
    # A notice fax's notice ID (the real direct/notice.py) comes first.
    _notice(engine, 'job1')
    assert ami.fax_subaddress('job1') == NOTICE_ID

    def unreadable(database, job_id):
        raise envelopes.UnreadableDecision('The stored routing decision for this fax cannot be read.')
    monkeypatch.setattr(envelopes, 'load', unreadable)
    assert ami.rule_subaddress('job1') is None


def test_a_routing_decision_table_the_database_lacks_is_never_taken_for_no_subaddress(monkeypatch, engine):
    """Only a decision the rules cannot read means "no subaddress". A database error, such as a migration that did
    not run, raises: dropping the subaddress quietly would send a department's fax to the wrong mailbox."""
    from app.routing import envelope as envelopes
    monkeypatch.setattr(ami, '_database', lambda: engine)

    def missing(database, job_id):
        raise sa.exc.ProgrammingError('SELECT', {}, Exception('relation "fax_job_rule_decisions" does not exist'))
    monkeypatch.setattr(envelopes, 'load', missing)
    with pytest.raises(sa.exc.ProgrammingError):
        ami.rule_subaddress('job1')
