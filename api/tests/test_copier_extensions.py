"""Copiers as extensions of Faxbot (N22, migration 0085): the groundwork that is built so far.

Built and tested here: the extension's credentials (shown once, only pjsip's MD5 digest kept), the pjsip.conf
sections Faxbot writes for each copier (registration only from private addresses), the dialplan context that
receives a copier's fax and hands it to Faxbot with the number it dialled, and the hand-over script's two new
fields. Not built yet: turning that hand-over into an outbound fax from the copier's mailbox, forwarding received
faxes to the copier's extension, the console and command line, and the loopback proof with a second Asterisk
registering as the copier. Every name and number is synthetic; nothing has run against a real copier.
"""
import hashlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.app import copier_extensions, schema, schema_copier_extensions, sip_trunk
from api.tests.test_digital_schema import _downgrade, _tables
from api.tests.test_inbound_handover import _Faxbot, needs_shell, notify
from api.tests.test_schema import database  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]


def test_a_copier_gets_a_password_once_and_only_its_digest_is_kept(database):  # noqa: F811
    schema.upgrade_schema(database)
    copier, password = copier_extensions.add(database, name='Front desk Ricoh', extension='601',
                                             numbers=['+13035550160'], actor={'name': 'Ada'})
    assert copier.username == 'copier-601' and copier.endpoint == 'copier-601' and len(password) >= 20
    assert copier.md5_cred == hashlib.md5(f'copier-601:faxbot:{password}'.encode()).hexdigest()
    with database.connect() as connection:
        stored = json.dumps([dict(row) for row in connection.execute(
            sa.text('SELECT * FROM copier_extensions')).mappings()], default=str)
    assert password not in stored
    with pytest.raises(copier_extensions.CopierError, match='already a copier'):
        copier_extensions.add(database, name='Second', extension='601')
    with pytest.raises(copier_extensions.CopierError, match='3 to 6 digits'):
        copier_extensions.add(database, name='Bad', extension='06')
    copier_extensions.remove(database, copier.id)
    assert copier_extensions.copiers(database) == []


def test_pjsip_takes_a_copier_only_from_private_addresses_and_never_holds_its_password():
    copier = copier_extensions.Copier('c' * 32, 'Front desk Ricoh', '601', 'copier-601', 'a' * 32, None, (),
                                      'copier', 'active')
    text = copier_extensions.render([copier])
    assert text.count('[copier-601]') == 2 and text.count('[copier-601-auth]') == 1  # one AOR, one endpoint
    assert 'auth_type=md5' in text and 'md5_cred=' + 'a' * 32 in text and 'password=' not in text
    assert 'context=faxbot-copier' in text and f"set_var=FAXBOT_COPIER={'c' * 32}" in text
    assert 'deny=0.0.0.0/0.0.0.0' in text and 'permit=192.168.0.0/255.255.0.0' in text
    assert 'contact_deny=0.0.0.0/0.0.0.0' in text and 'contact_permit=10.0.0.0/255.0.0.0' in text
    assert 'max_contacts=1' in text and 't38_udptl=yes' in text
    assert copier_extensions.render([]) == ''
    # The file Faxbot writes for Asterisk carries the copier after the trunk and any partners.
    from api.app.config_values import ConfigurationValues
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx',
                                                   'SIP_TRUNK_USERNAME': 'faxbotuser',
                                                   'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1'})
    rendered = sip_trunk.rendered_configuration(values, peers=[], copiers=[copier])
    assert rendered.index('[copier-601-auth]') > rendered.index('type=transport')
    assert rendered.count('[copier-601-auth]') == 1


def test_the_copier_dialplan_receives_and_hands_over_the_dialled_number_and_never_dials_out():
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions-copier.conf').read_text()
    main = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    assert main.count('#include extensions-copier.conf') == 1
    context = text.split('[faxbot-copier]', 1)[1].split('\n[', 1)[0]
    assert 'ReceiveFAX(${FAXBOT_FILE},f)' in context and 'Dial(' not in context
    assert 'Set(FAXBOT_DIALED=${FILTER(0123456789+,${EXTEN})})' in context
    assert 'PJSIPHangup(403)' in context  # no copier ID: refused unanswered
    done = text.split('[faxbot-copier-done]', 1)[1].split('\n[', 1)[0]
    assert 'copier=${FILTER(0123456789abcdef,${FAXBOT_COPIER})}' in done
    assert 'dialed=${FILTER(0123456789+,${FAXBOT_DIALED})}' in done
    assert 'Dial(' not in text  # store and forward: a copier call is never bridged to the trunk


@needs_shell
def test_the_hand_over_script_passes_the_copier_and_the_dialled_number_and_nothing_else(tmp_path):
    faxbot = _Faxbot(200)
    try:
        sent = notify(tmp_path / 'one', faxbot.url, extra=['copier=' + 'c' * 32, 'dialed=+13035550199'])
        junk = notify(tmp_path / 'two', faxbot.url, extra=['copier=zz"},{"x', 'dialed=12;rm'])
        none = notify(tmp_path / 'three', faxbot.url)
    finally:
        faxbot.close()
    assert [result.stdout for result in (sent, junk, none)] == ['ok\n'] * 3
    bodies = [request['body'] for request in faxbot.requests]
    assert (bodies[0]['copier'], bodies[0]['dialed']) == ('c' * 32, '+13035550199')
    assert (bodies[1]['copier'], bodies[1]['dialed']) == (None, '12')
    assert (bodies[2]['copier'], bodies[2]['dialed']) == (None, None)


OUTSIDE = '+13035550199'


@pytest.fixture(params=['receiving', 'send_only'])
def sip_client(request, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    for name, value in {'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_BACKEND': 'sip', 'FAX_INBOUND_BACKEND': 'sip',
                        'INBOUND_ENABLED': 'true' if request.param == 'receiving' else 'false',
                        'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                        'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_CALLER_ID': '+13035550160',
                        'SIP_TRUNK_DIDS': '+13035550160,+13035550161', 'AMI_PASSWORD': 'synthetic-ami-pass',
                        'ASTERISK_INBOUND_SECRET': 'sekret', 'FAX_HEADER': 'Example Clinic'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def _b():
    from api.tests.test_access_management_http import B
    return B


def _copier(client, **extra):
    from api.tests.test_work_http import mailbox
    box = mailbox(client, 'Front desk', '+13035550161')
    created = client.post('/admin/sip/copiers/extensions', headers=_b(), json={
        'name': 'Front desk Ricoh', 'extension': '601', 'mailbox_id': box['id'], 'numbers': ['+13035550161'],
        **extra})
    assert created.status_code == 201, created.text
    return created.json(), box


def _copier_tiff(client, name='copier.tiff', pages=2):
    import os
    from PIL import Image
    folder = Path(os.environ['FAX_DATA_DIR']) / 'inbound'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    frames = [Image.new('1', (1728, 200), 1) for _ in range(pages)]
    frames[0].save(path, format='TIFF', save_all=True, append_images=frames[1:], compression='group4',
                   dpi=(204, 196))
    return path


def _engine():
    from app.main import app
    return app.state.configuration_runtime.manager.store.engine


def test_a_copier_fax_is_queued_from_its_mailbox_to_the_number_it_dialled(sip_client):
    from api.app.routing import envelope as envelopes
    created, box = _copier(sip_client)
    assert created['password'] and created['username'] == 'copier-601' and created['mailbox'] == 'Front desk'
    assert created['password_text'].startswith('Enter this password on the copier now')
    listed = sip_client.get('/admin/sip/copiers', headers=_b()).json()['extensions']
    assert [item['extension'] for item in listed] == ['601'] and 'password' not in listed[0]
    tiff = _copier_tiff(sip_client)
    payload = {'tiff_path': str(tiff), 'copier': created['id'], 'dialed': '3035550199', 'faxstatus': 'SUCCESS',
               'faxpages': 2, 'uniqueid': '1791100000.7'}
    handed = sip_client.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'sekret'}, json=payload)
    assert handed.status_code == 200, handed.text
    job = handed.json()['queued']
    assert not tiff.exists()  # the queued fax keeps its own copy; nothing is brought in again as received
    with _engine().connect() as connection:
        row = connection.execute(sa.text('SELECT to_number, pages FROM fax_jobs WHERE id = :id'), {'id': job}).one()
        received = connection.execute(sa.text('SELECT count(*) FROM inbound_faxes')).scalar()
    assert tuple(row) == (OUTSIDE, 2) and received == 0
    pinned = envelopes.load(_engine(), job)
    assert pinned.facts.mailbox_id == box['id']  # its mailbox's sending rules and reply number apply
    # The same call handed over again (the script repeats when Faxbot was slow) is the same fax.
    again = sip_client.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'sekret'},
                            json={**payload, 'tiff_path': str(_copier_tiff(sip_client, 'again.tiff'))})
    assert again.json()['queued'] == job
    assert created['server_text'].startswith('On the copier, set the SIP server (registrar) to')
    import os
    if os.environ.get('INBOUND_ENABLED') != 'true':
        return  # a send-only installation queued the copier's fax above; filing a refused one needs receiving on
    # A call that ended before every page arrived is never sent on: its pages are filed in Received, with why.
    partial = sip_client.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'sekret'}, json={
        **payload, 'faxstatus': 'FAILED', 'uniqueid': '1791100005.2',
        'tiff_path': str(_copier_tiff(sip_client, 'partial.tiff'))})
    assert partial.status_code == 200 and 'queued' not in partial.json()
    with _engine().connect() as connection:
        report = connection.execute(sa.text('SELECT report FROM inbound_imports ORDER BY created_at DESC')).scalar()
    assert 'ended before every page arrived, so nothing was sent on' in report
    # Removing the copier revokes its key: a later fax from it is filed as received, with why, never lost.
    assert sip_client.post(f"/admin/sip/copiers/extensions/{created['id']}/remove", headers=_b()).status_code == 200
    lost = sip_client.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'sekret'}, json={
        **payload, 'uniqueid': '1791100009.1', 'tiff_path': str(_copier_tiff(sip_client, 'late.tiff'))})
    assert lost.status_code == 200 and 'queued' not in lost.json()
    with _engine().connect() as connection:
        report = connection.execute(sa.text('SELECT report FROM inbound_imports ORDER BY created_at DESC')).scalar()
    assert 'No copier extension has that ID any more.' in report


class FakeAmi:
    def __init__(self):
        self.calls, self.listeners = [], {}

    async def originate_copier(self, forward_id, endpoint, tiff_path):
        self.calls.append((forward_id, endpoint, tiff_path))


@pytest.mark.asyncio
async def test_a_received_fax_goes_on_to_the_copier_or_stays_in_received_with_why(database):  # noqa: F811
    from datetime import datetime
    schema.upgrade_schema(database)
    copier, _ = copier_extensions.add(database, name='Front desk Ricoh', extension='601', numbers=['+13035550161'])
    now = datetime.utcnow()
    with database.begin() as connection:
        for fax in ('in-1', 'in-2'):
            connection.execute(sa.text(
                "INSERT INTO inbound_faxes (id, to_number, from_number, status, backend, tiff_path, created_at, updated_at, "
                "received_at) VALUES (:id, '+13035550161', '+13035550100', 'received', 'sip', :tiff, :now, :now, :now)"),
                {'id': fax, 'tiff': f'/faxdata/inbound/{fax}.tiff', 'now': now})
    ami = FakeAmi()
    values = type('Values', (), {'fax_data_dir': '/faxdata'})()
    forwarder = copier_extensions.Forwarder(database, ami, lambda: values)
    import os
    real = os.path.isfile
    os.path.isfile = lambda path: path.startswith('/faxdata/inbound/') or real(path)
    try:
        await forwarder.step()
    finally:
        os.path.isfile = real
    assert [(endpoint, path) for _, endpoint, path in ami.calls] == [
        ('copier-601', '/faxdata/inbound/in-1.tiff'), ('copier-601', '/faxdata/inbound/in-2.tiff')]
    assert copier_extensions.due(database) == []  # both are being called: never called twice at once
    first, second = (forward_id for forward_id, _, _ in ami.calls)
    forwarder.on_sent({'Event': 'UserEvent', 'UserEvent': 'FaxCopierSent', 'Job': first, 'Status': 'SUCCESS',
                       'Pages': '2'})
    forwarder.on_originate({'Event': 'OriginateResponse', 'ActionID': f'copier-{second}', 'Response': 'Failure'})
    assert copier_extensions.fax_view(database, 'in-1') == 'Sent on to Front desk Ricoh at extension 601, 2 pages.'
    assert copier_extensions.fax_view(database, 'in-2') == "Front desk Ricoh didn't answer, so this fax stays in Received."
    # The unanswered one is tried once more; the sent one never again; after two tries it stays in Received.
    assert [row['id'] for row, _ in copier_extensions.due(database)] == ['in-2']
    copier_extensions.record_forward(database, 'in-2', copier.id, 'calling')
    copier_extensions.record_forward(database, 'in-2', copier.id, 'no_answer', reason='Again.')
    assert copier_extensions.due(database) == []


@pytest.mark.asyncio
async def test_the_fax_engine_is_asked_to_call_only_the_copiers_own_extension():
    from api.app import ami as ami_module
    client = ami_module.AMIClient.__new__(ami_module.AMIClient)
    sent = []

    async def capture(fields):
        sent.append(fields)
    client._send_action = capture
    await client.originate_copier('f' * 32, 'copier-601', '/faxdata/inbound/1791100000.7.tiff')
    assert sent == [{'Action': 'Originate', 'ActionID': 'copier-' + 'f' * 32, 'Channel': 'PJSIP/copier-601',
                     'Context': 'faxbot-copier-send', 'Exten': 's', 'Priority': '1', 'Async': 'true',
                     'Timeout': '45000', 'CallerID': 'Faxbot',
                     'Variable': 'FAXFILE=/faxdata/inbound/1791100000.7.tiff,FAXBOT_JOB=' + 'f' * 32}]
    for endpoint, path in (('trunk-endpoint', '/faxdata/a.tiff'), ('copier-601', '/faxdata/../etc/passwd.tiff'),
                           ('PJSIP/copier-601', '/faxdata/a.tiff')):
        with pytest.raises(ValueError):
            await client.originate_copier('f' * 32, endpoint, path)


def test_0085_is_the_head_and_its_downgrade_keeps_copiers(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_copier_extensions.REVISION == '0085_copier_extensions' == schema.HEAD
    assert schema.PARTNER_ADMISSION == '0084_partner_admission'
    assert schema_copier_extensions.TABLES <= schema.STRICT_TABLES and schema_copier_extensions.TABLES <= _tables(database)
    _downgrade(database, '0084_partner_admission')
    assert not schema_copier_extensions.TABLES & _tables(database)
    schema.upgrade_schema(database)
    copier_extensions.add(database, name='Ricoh', extension='602')
    with pytest.raises(schema.SchemaUpgradeError, match='cannot be undone'):
        _downgrade(database, '0084_partner_admission')
