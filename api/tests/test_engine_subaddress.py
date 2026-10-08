"""The built-in engine asks for a subaddress (M17, patch 0005): the Originate field, where it comes from, and the
record of it as requested and, only when the far end takes one, carried.

Synthetic numbers and frames only. The DIS frames are the receiving engine's as the replay proof
(test_t38_terminal_replay.py) gets them from spandsp 0.0.6: with patch 0005 (bit 49 set) and without it.
"""
import importlib.util
import logging
import sys
import types
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

def test_without_notice_faxes_on_this_installation_no_notice_id_is_asked_for(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, ami._NOTICE_MODULE, None)  # the module is not part of this installation
    monkeypatch.setattr(ami, '_notice_missing_logged', False)
    with caplog.at_level(logging.INFO, logger=ami.__name__):
        assert ami.notice_subaddress('job1') is None
        assert ami.notice_subaddress('job2') is None
    assert sum('No notice faxes on this installation' in record.message for record in caplog.records) == 1


@pytest.mark.skipif(importlib.util.find_spec(ami._NOTICE_MODULE) is not None,
                    reason='Notice faxes are part of this installation.')
def test_the_real_notice_module_missing_reads_as_no_notice_fax():
    assert ami.notice_subaddress('job1') is None


def test_a_broken_notice_module_is_not_hidden(monkeypatch):
    """Only the notice module's own absence means "no notice fax"; an import failure inside it surfaces."""
    broken = types.ModuleType(ami._NOTICE_MODULE)  # present, but without subaddress_for
    monkeypatch.setitem(sys.modules, ami._NOTICE_MODULE, broken)
    with pytest.raises(ImportError):
        ami.notice_subaddress('job1')

    class Inner(types.ModuleType):
        def __getattr__(self, name):
            raise ModuleNotFoundError("No module named 'barcode_reader'", name='barcode_reader')
    monkeypatch.setitem(sys.modules, ami._NOTICE_MODULE, Inner(ami._NOTICE_MODULE))
    with pytest.raises(ModuleNotFoundError):
        ami.notice_subaddress('job1')


def test_a_notice_fax_asks_for_its_notice_id_on_the_built_in_engine(monkeypatch, engine):
    asked = []
    notice = types.ModuleType(ami._NOTICE_MODULE)

    def subaddress_for(database, job_id):
        asked.append((database, job_id))
        return NOTICE_ID if job_id == 'notice1' else None
    notice.subaddress_for = subaddress_for
    monkeypatch.setitem(sys.modules, ami._NOTICE_MODULE, notice)
    monkeypatch.setattr(ami, '_database', lambda: engine)
    trunk = values()
    fields = ami.originate_fields_for(trunk, 'notice1', NUMBER, '/faxdata/notice1.tiff',
                                      choice=ami.reply_choice(trunk))
    assert f'FAXBOT_TX_SUB={NOTICE_ID}' in fields['Variable'].split(',')
    other = ami.originate_fields_for(trunk, 'job1', NUMBER, '/faxdata/job1.tiff', choice=ami.reply_choice(trunk))
    assert 'FAXBOT_TX_SUB' not in other['Variable']
    assert asked == [(engine, 'notice1'), (engine, 'job1')]


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
    pinned = types.SimpleNamespace(envelope=_decide({'automatic': True, 'subaddress': '2001'}).envelope)
    monkeypatch.setattr(envelopes, 'load', lambda database, job_id: pinned if job_id != 'old' else None)
    monkeypatch.setattr(ami, '_database', lambda: engine)
    monkeypatch.setitem(sys.modules, ami._NOTICE_MODULE, None)
    trunk = values()
    fields = ami.originate_fields_for(trunk, 'job1', NUMBER, '/faxdata/job1.tiff', choice=ami.reply_choice(trunk))
    assert ami.requested_subaddress(fields) == '2001'
    assert ami.rule_subaddress('old') is None
    notice = types.ModuleType(ami._NOTICE_MODULE)
    notice.subaddress_for = lambda database, job_id: NOTICE_ID
    monkeypatch.setitem(sys.modules, ami._NOTICE_MODULE, notice)
    assert ami.fax_subaddress('job1') == NOTICE_ID

    def unreadable(database, job_id):
        raise envelopes.UnreadableDecision('The stored routing decision for this fax cannot be read.')
    monkeypatch.setattr(envelopes, 'load', unreadable)
    assert ami.rule_subaddress('job1') is None
