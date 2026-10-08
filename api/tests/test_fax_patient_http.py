"""The patient given with a fax, over the real application: ``POST /fax`` and ``faxbot send`` keep the details beside
the fax's document and nowhere else, never repeat them, refuse a replay with other details, and leave nothing
behind for a fax that was not accepted (digital/patient.py). Synthetic numbers and details only."""
import json
import logging
import stat
from pathlib import Path

import sqlalchemy as sa

import app.main as main_module
from api.tests.test_cli import BOOTSTRAP, Cli, server  # noqa: F401 - server is a fixture

DETAILS = ('--patient-record-number', 'MRN-5550199', '--patient-record-system', '2.16.840.1.113883.19.5',
           '--patient-family-name', 'Zyxwvut', '--patient-given-name', 'Quillon', '--patient-birth-date', '1961-07-23')
SECRETS = ('MRN-5550199', 'Zyxwvut', 'Quillon', '1961-07-23')
FORM = {'patient_record_number': 'MRN-5550199', 'patient_family_name': 'Zyxwvut', 'patient_given_name': 'Quillon',
        'patient_birth_date': '1961-07-23'}


def _data_dir():
    return Path(main_module.app.state.configuration_runtime.manager.store.read().active.values.fax_data_dir)


def _stored():
    engine = main_module.app.state.configuration_runtime.manager.store.engine
    rows = []
    with engine.connect() as connection:
        for name in sa.inspect(connection).get_table_names():
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            rows.extend(repr(tuple(row)) for row in connection.execute(sa.select(table)))
    return '\n'.join(rows)


def _note(tmp_path):
    note = tmp_path / 'referral.txt'
    note.write_text('Synthetic referral letter\n')
    return note


def test_send_keeps_the_patient_beside_the_document_and_never_repeats_it(server, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cli = Cli(server)
    human = cli('send', '+15551230001', _note(tmp_path), '--queue', *DETAILS)
    assert human.exit_code == 0 and 'Fax accepted.' in human.stdout
    shown = cli('--json', 'send', '+15551230002', _note(tmp_path), '--queue', *DETAILS)
    assert shown.exit_code == 0
    job = json.loads(shown.stdout)['id']
    kept = _data_dir() / f'{job}.patient.json'
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert json.loads(kept.read_text()) == {
        'record_number': 'MRN-5550199', 'record_system': 'urn:oid:2.16.840.1.113883.19.5', 'family_name': 'Zyxwvut',
        'given_name': 'Quillon', 'birth_date': '1961-07-23'}
    assert (_data_dir() / f'{job}.pdf').is_file()
    headers = {'X-API-Key': BOOTSTRAP}
    public, detail = server.get(f'/fax/{job}', headers=headers), server.get(f'/admin/fax-jobs/{job}', headers=headers)
    assert public.status_code == detail.status_code == 200
    shown_human, shown_json = cli('sent', 'show', job), cli('--json', 'sent', 'show', job)
    assert shown_human.exit_code == shown_json.exit_code == 0 and job in shown_json.stdout
    answers = [public.text, detail.text, shown_human.stdout, shown_json.stdout]
    for value in SECRETS:
        for text in (human.stdout, human.stderr, shown.stdout, *answers, _stored(), caplog.text):
            assert value not in text


def test_a_replay_with_other_patient_details_is_refused(server, tmp_path):
    headers = {'X-API-Key': BOOTSTRAP, 'Idempotency-Key': 'patient-replay-1'}

    def post(**fields):
        with _note(tmp_path).open('rb') as handle:
            return server.post('/fax', headers=headers, data={'to': '+15551230003', 'queue_only': 'true', **fields},
                               files={'file': ('referral.txt', handle, 'text/plain')})
    first = post(**FORM)
    assert first.status_code == 202
    assert post(**FORM).json()['id'] == first.json()['id']
    other = post(**{**FORM, 'patient_record_number': 'MRN-5550200'})
    assert other.status_code == 409 and 'MRN' not in other.text
    assert post().status_code == 409  # the same request without the patient is another request too


def test_details_that_cannot_be_used_are_refused_without_repeating_them(server, tmp_path):
    before = set(_data_dir().iterdir()) if _data_dir().exists() else set()
    with _note(tmp_path).open('rb') as handle:
        refused = server.post('/fax', headers={'X-API-Key': BOOTSTRAP},
                              data={'to': '+15551230004', 'queue_only': 'true', 'patient_family_name': 'Zyxwvut',
                                    'patient_birth_date': '23/07/1961'},
                              files={'file': ('referral.txt', handle, 'text/plain')})
    assert refused.status_code == 400
    assert refused.json()['detail'] == "Give the patient's medical record number with the other patient details."
    assert 'Zyxwvut' not in refused.text and '1961' not in refused.text
    after = set(_data_dir().iterdir()) if _data_dir().exists() else set()
    assert after == before


def test_retention_removes_the_patient_details_with_the_document(server, tmp_path):
    """The scheduled cleanup removes a finished fax's files after the retention period: its patient file too."""
    from datetime import datetime, timedelta
    cli = Cli(server)
    kept = json.loads(cli('--json', 'send', '+15551230006', _note(tmp_path), '--queue').stdout)['id']
    old = json.loads(cli('--json', 'send', '+15551230007', _note(tmp_path), '--queue', *DETAILS).stdout)['id']
    recent = json.loads(cli('--json', 'send', '+15551230008', _note(tmp_path), '--queue', *DETAILS).stdout)['id']
    engine = main_module.app.state.configuration_runtime.manager.store.engine
    long_ago = datetime.utcnow() - timedelta(days=60)
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'failed', updated_at = :at "
                                   'WHERE id IN (:old, :kept)'), {'at': long_ago, 'old': old, 'kept': kept})
    folder = _data_dir()
    assert (folder / f'{old}.pdf').exists() and (folder / f'{old}.patient.json').exists()
    main_module._cleanup_outbound_documents(datetime.utcnow() - timedelta(days=30))
    assert not (folder / f'{old}.pdf').exists() and not (folder / f'{old}.patient.json').exists()
    assert not (folder / f'{kept}.pdf').exists()
    # A fax still within the period, or not finished, keeps both.
    assert (folder / f'{recent}.pdf').exists() and (folder / f'{recent}.patient.json').exists()


def test_a_fax_that_is_not_accepted_leaves_no_patient_details_behind(server, tmp_path, monkeypatch):
    from app.routing import rules_acceptance

    def unreadable(*args, **kwargs):
        raise RuntimeError('synthetic: the rules cannot be read')
    monkeypatch.setattr(rules_acceptance, 'prepare', unreadable)
    with _note(tmp_path).open('rb') as handle:
        refused = server.post('/fax', headers={'X-API-Key': BOOTSTRAP},
                              data={'to': '+15551230005', 'queue_only': 'true', **FORM},
                              files={'file': ('referral.txt', handle, 'text/plain')})
    assert refused.status_code == 503
    assert not list(_data_dir().glob('*.patient.json'))
