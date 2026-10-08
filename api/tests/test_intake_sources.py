"""Intake connectors end to end: the real application, access policy and import path, a TLS IMAP stand-in and folders.

The rule every test checks: the same message or file twice files or sends once.
Synthetic mailboxes (example.com), people and 555 numbers only.
"""
import asyncio
from email.message import EmailMessage
import os
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.intake.sources import text
from app.intake.sources.folder import Watch
from app.intake.sources.poller import Poller
from app.intake.sources.store import SourceSecrets, SourceStore
from app.main import app
from api.tests.imap_fake import FakeImap, client_context
from api.tests.test_access_management_http import B, ORIGIN, _environment, create_user, policy_version, ready_user
from api.tests.test_work_http import hold_worker, mailbox, pdf


GMAIL_PASS = ('mx.google.com; dkim=pass header.d=example.com header.s=s1; spf=pass '
              '(google.com: domain of x designates 192.0.2.1 as permitted sender) smtp.mailfrom=example.com; '
              'dmarc=pass (p=REJECT) header.from=example.com')


def idle(*args, **kwargs):
    return asyncio.sleep(3600)


@pytest.fixture(params=['sqlite', 'postgresql'])
def database_url(request, tmp_path):
    if request.param == 'sqlite':
        yield None
        return
    url = os.environ.get('FAXBOT_SCHEMA_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set FAXBOT_SCHEMA_TEST_POSTGRES_URL to run the PostgreSQL connector tests')
    from app.db import create_database_engine
    admin = create_database_engine(url)
    namespace = 'faxbot_connectors_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA {namespace}')
    scoped = sa.engine.make_url(url).update_query_dict({'options': '-csearch_path=' + namespace})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA {namespace} CASCADE')
        admin.dispose()


@pytest.fixture
def client(monkeypatch, tmp_path, database_url):
    _environment(monkeypatch, tmp_path)
    if database_url:
        monkeypatch.setenv('DATABASE_URL', database_url)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    # The lifespan's own connector check waits; each test drives one with its stand-in servers.
    monkeypatch.setattr('app.intake.sources.http.repeat', idle)
    hold_worker(monkeypatch)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


@pytest.fixture
def imap(tmp_path):
    server = FakeImap(tmp_path, password='synthetic-mailbox-password')
    yield server
    server.close()


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        self.host = host

    def ehlo(self):
        return 250, b'ok'

    def login(self, user, password):
        assert password == 'synthetic-mailbox-password'

    def mail(self, sender):
        self.sender = sender
        return 250, b'ok'

    def rcpt(self, recipient):
        self.recipient = recipient
        return 250, b'ok'

    def data(self, payload):
        from email import policy
        from email.parser import BytesParser
        parsed = BytesParser(policy=policy.default).parsebytes(payload)
        headers = ''.join(f'{name}: {value}\n' for name, value in parsed.items())
        FakeSMTP.sent.append((self.sender, self.recipient, headers + parsed.get_content()))
        return 250, b'ok'

    def quit(self):
        pass

    def close(self):
        pass


def engine():
    return app.state.configuration_runtime.manager.store.engine


def store():
    return SourceStore(engine(), SourceSecrets(app.state.configuration_runtime.manager.store))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def poller(server=None, clock=None):
    FakeSMTP.sent = []
    return Poller(store(), lambda: app.state.access_runtime, app.state.configuration_runtime,
                  ssl_context=client_context(server.cert) if server else None,
                  smtp_connect=lambda host, port, timeout: FakeSMTP(host, port, timeout),
                  watch=Watch(clock=clock or Clock()))


def check(runner, source_id):
    source, lease = runner.store.claim(source_id)
    return runner.run(source, lease)


def count(table):
    with engine().connect() as connection:
        return connection.execute(sa.text(f'SELECT COUNT(*) FROM {table}')).scalar()


def email(*, sender='jane@example.com', subject='Referral', message_id='<referral-1@example.com>',
          documents=(('referral.pdf', None),), results=GMAIL_PASS, headers=()):
    built = EmailMessage()
    if results:
        built['Authentication-Results'] = results
    for name, value in headers:
        built[name] = value
    built['From'] = sender
    built['To'] = 'fax@example.com'
    built['Subject'] = subject
    if message_id:
        built['Message-ID'] = message_id
    built.set_content('Synthetic message.')
    for name, data in documents:
        built.add_attachment(data or pdf('Synthetic ' + name), maintype='application', subtype='pdf', filename=name)
    return built.as_bytes(policy=built.policy.clone(linesep='\r\n'))


def mail_settings(server, **extra):
    return {'provider': 'other', 'address': 'fax@example.com', 'imap_host': 'localhost', 'imap_port': server.port,
            'sign_in': 'password', 'check_seconds': 60, **extra}


def add(client, body):
    response = client.post('/intake/sources', headers=B, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def items(client, name=None):
    response = client.get('/intake/sources/items', headers=B, params={'connector': name} if name else None)
    assert response.status_code == 200, response.text
    return response.json()['items']


# -- receiving from a mailbox ----------------------------------------------------------------

def test_a_message_polled_again_is_filed_once_even_after_a_crash_before_the_move(client, imap):
    box = mailbox(client, 'Front Desk', '+15550100001')
    source = add(client, {'name': 'Scans mailbox', 'kind': 'email', 'direction': 'receive',
                          'settings': mail_settings(imap, mailbox_id=box['id']),
                          'secret': {'password': 'synthetic-mailbox-password'}})
    assert source['status'] == text.NOT_CHECKED and 'synthetic-mailbox-password' not in str(source)
    raw = email(documents=(('first.pdf', None), ('second.pdf', None)), results=None)
    imap.add(raw)
    runner = poller(imap)

    # The import is recorded, then moving the message fails: it stays in the inbox.
    imap.refuse_moves = True
    ok, result = check(runner, source['id'])
    assert not ok and result == text.MOVE_FAILED.format(folder='Faxbot processed')
    assert count('inbound_imports') == 2 and imap.count('INBOX') == 1

    # The next check reads it again and files nothing new; then it moves.
    imap.refuse_moves = False
    ok, result = check(runner, source['id'])
    assert ok and result == '1 message handled in the last check.'
    assert count('inbound_imports') == 2 and imap.count('INBOX') == 0 and imap.count('Faxbot processed') == 1

    # The same message delivered again is still one document per attachment.
    imap.add(raw)
    check(runner, source['id'])
    assert count('inbound_imports') == 2
    listed = items(client, 'Scans mailbox')
    assert [(item['state'], item['duplicates']) for item in listed] == [('imported', 2), ('imported', 2)]
    assert listed[0]['status'] == 'Filed in Front Desk. Seen again twice; it was never filed twice.'
    with engine().connect() as connection:
        rows = connection.execute(sa.text("SELECT mailbox_label, status, backend FROM inbound_faxes")).all()
        accounts = connection.execute(sa.text('SELECT DISTINCT account, source FROM inbound_imports')).all()
    assert sorted(rows) == [('Front Desk', 'received', 'import')] * 2
    assert accounts == [('import:connector:' + source['id'], 'import')]
    listed_sources = client.get('/intake/sources', headers=B).json()['connectors']
    assert listed_sources[0]['counts'] == {'items': 2, 'duplicates': 4, 'refused': 0, 'failed': 0}


def test_faxbot_own_email_is_not_brought_in_again(client, imap):
    box = mailbox(client, 'Front Desk', '+15550100001')
    source = add(client, {'name': 'Scans mailbox', 'kind': 'email', 'direction': 'receive',
                          'settings': mail_settings(imap, mailbox_id=box['id']),
                          'secret': {'password': 'synthetic-mailbox-password'}})
    imap.add(email(message_id='<intake-0123abcd@example.com>', results=None))
    check(poller(imap), source['id'])
    assert count('inbound_imports') == 0
    assert items(client)[0]['status'] == text.OWN_MAIL
    # A scanner mailing scans from the mailbox's own address is filed.
    imap.add(email(sender='fax@example.com', message_id='<scan-77@scanner.example.com>', results=None))
    check(poller(imap), source['id'])
    assert count('inbound_imports') == 1


def test_sign_in_problems_are_one_sentence_and_nothing_is_read(client, imap):
    source = add(client, {'name': 'Scans mailbox', 'kind': 'email', 'direction': 'receive',
                          'settings': mail_settings(imap), 'secret': {'password': 'wrong-password'}})
    imap.add(email(results=None))
    runner = poller(imap)
    assert check(runner, source['id']) == (False, text.SIGN_IN_REFUSED)
    assert runner.test(runner.store.get(source['id'])) == (False, text.SIGN_IN_REFUSED)
    assert imap.count('INBOX') == 1 and count('inbound_imports') == 0
    listed = client.get('/intake/sources', headers=B).json()['connectors'][0]
    assert listed['status'] == text.SIGN_IN_REFUSED and listed['ok'] is False


# -- receiving from a folder ------------------------------------------------------------------

def _old(path, seconds=120):
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def test_folder_waits_for_a_file_to_settle_and_files_bytes_once(client, tmp_path):
    box = mailbox(client, 'Front Desk', '+15550100001')
    folder = tmp_path / 'scans'
    folder.mkdir()
    source = add(client, {'name': 'Scanner share', 'kind': 'folder', 'direction': 'receive',
                          'settings': {'path': str(folder), 'mailbox_id': box['id'], 'settle_seconds': 10}})
    clock = Clock()
    runner = poller(clock=clock)
    document = pdf('Synthetic scan one')

    # Half-written: the size keeps changing, so it is never read.
    scan = folder / 'scan.pdf'
    scan.write_bytes(document[:200])
    _old(scan)
    check(runner, source['id'])
    clock.now += 11
    scan.write_bytes(document[:400])
    _old(scan)
    check(runner, source['id'])
    assert count('inbound_imports') == 0 and scan.exists()
    # Still being written right now (a fresh modification time) is not settled either.
    scan.write_bytes(document)
    check(runner, source['id'])
    clock.now += 11
    check(runner, source['id'])
    assert count('inbound_imports') == 0
    _old(scan)
    check(runner, source['id'])
    clock.now += 11
    ok, result = check(runner, source['id'])
    assert ok and result == '1 file handled in the last check.'
    assert count('inbound_imports') == 1 and (folder / 'done' / 'scan.pdf').exists() and not scan.exists()

    # The same bytes under a new name are a duplicate.
    copy = folder / 'scan-copy.pdf'
    copy.write_bytes(document)
    _old(copy)
    check(runner, source['id'])
    clock.now += 11
    check(runner, source['id'])
    assert count('inbound_imports') == 1 and (folder / 'done' / 'scan-copy.pdf').exists()

    # A changed file under the old name is a new document; a name already in done/ gets a number.
    scan.write_bytes(pdf('Synthetic scan two'))
    _old(scan)
    check(runner, source['id'])
    clock.now += 11
    check(runner, source['id'])
    assert count('inbound_imports') == 2 and (folder / 'done' / 'scan (2).pdf').exists()

    # Something that is not a PDF goes to failed/ with its reason beside it.
    broken = folder / 'broken.pdf'
    broken.write_bytes(b'not a pdf at all')
    _old(broken)
    check(runner, source['id'])
    clock.now += 11
    check(runner, source['id'])
    assert (folder / 'failed' / 'broken.pdf').exists()
    assert (folder / 'failed' / 'broken.pdf.reason.txt').read_text().strip() == \
        text.UNREADABLE_RECEIVE.format(name='broken.pdf')
    states = [(item['what'], item['state'], item['duplicates']) for item in items(client)]
    assert sorted(states) == [('broken.pdf', 'failed', 0), ('scan.pdf', 'imported', 0), ('scan.pdf', 'imported', 1)]


def test_a_mailbox_with_no_fax_number_receives_a_connectors_documents(client, tmp_path):
    created = client.post('/access/mailboxes', headers=B, json={'label': 'Scans only', 'enabled': True,
                                                                'expected_policy_version': policy_version(client)})
    assert created.status_code == 200, created.text
    box = created.json()['mailbox']
    folder = tmp_path / 'scans'
    folder.mkdir()
    source = add(client, {'name': 'Scanner share', 'kind': 'folder', 'direction': 'receive',
                          'settings': {'path': str(folder), 'mailbox_id': box['id'], 'settle_seconds': 10}})
    clock = Clock()
    runner = poller(clock=clock)
    scan = folder / 'scan.pdf'
    scan.write_bytes(pdf('Synthetic scan for a mailbox with no number'))
    _old(scan)
    check(runner, source['id'])
    clock.now += 11
    ok, _ = check(runner, source['id'])
    assert ok and (folder / 'done' / 'scan.pdf').exists()
    (item,) = items(client, 'Scanner share')
    assert item['state'] == 'imported' and item['status'] == text.FILED.format(mailbox='Scans only')
    with engine().connect() as connection:
        placed = connection.execute(sa.text('SELECT mailbox_label, to_number FROM inbound_faxes')).all()
    assert [(row.mailbox_label, row.to_number) for row in placed] == [('Scans only', None)]


def test_a_connector_files_only_into_a_mailbox_its_administrator_can_see(client, tmp_path):
    """Faxbot files a connector's documents as itself, so adding one, or moving it to another mailbox, needs the
    person setting it up to see faxes in that mailbox; a mailbox that does not exist is one sentence."""
    from api.tests.test_access_management_http import assign
    front = mailbox(client, 'Front Desk', '+15550100001')
    billing = mailbox(client, 'Billing', '+15550100002')
    role = client.post('/access/roles', headers=B, json={
        'name': 'Connector setup', 'description': 'Sets up scanners', 'permissions': ['settings:read', 'settings:write'],
        'enabled': True, 'expected_policy_version': policy_version(client)})
    assert role.status_code == 200, role.text
    sam, person = ready_user(client, 'sam', role.json()['role']['id'])
    assign(client, person['id'], 'role_fax_viewer', resource_id=front['resource_id'])
    folder = tmp_path / 'scans'
    folder.mkdir()

    def body(box_id):
        return {'name': 'Scanner share', 'kind': 'folder', 'direction': 'receive',
                'settings': {'path': str(folder), 'mailbox_id': box_id, 'settle_seconds': 10}}
    refused = sam.post('/intake/sources', body(billing['id']))
    assert refused.status_code == 403
    assert refused.json()['detail'] == text.MAILBOX_NOT_YOURS.format(mailbox='Billing')
    unknown = sam.post('/intake/sources', body('0' * 32))
    assert unknown.status_code == 400 and unknown.json()['detail'] == text.MAILBOX_UNKNOWN
    added = sam.post('/intake/sources', body(front['id']))
    assert added.status_code == 201, added.text
    source = added.json()
    moved = sam.client.put(f"/intake/sources/{source['id']}", headers=sam.headers(csrf=True), json={
        'version': source['version'], 'settings': {'mailbox_id': billing['id']}})
    assert moved.status_code == 403
    renamed = sam.client.put(f"/intake/sources/{source['id']}", headers=sam.headers(csrf=True), json={
        'version': source['version'], 'name': 'Front desk scanner'})
    assert renamed.status_code == 200, renamed.text
    # Someone who sees every mailbox may move it; Faxbot then files there as itself.
    moved = client.put(f"/intake/sources/{source['id']}", headers=B, json={
        'version': renamed.json()['version'], 'settings': {'mailbox_id': billing['id']}})
    assert moved.status_code == 200, moved.text
    (listed,) = [item for item in client.get('/intake/sources', headers=B).json()['connectors']
                 if item['id'] == source['id']]
    assert listed['mailbox']['label'] == 'Billing'


def test_a_sidecar_names_the_number_and_a_missing_folder_is_one_sentence(client, tmp_path):
    mailbox(client, 'Billing', '+15550100002')
    folder = tmp_path / 'inbox'
    folder.mkdir()
    source = add(client, {'name': 'Billing drop', 'kind': 'folder', 'direction': 'receive',
                          'settings': {'path': str(folder), 'settle_seconds': 2}})
    clock = Clock()
    runner = poller(clock=clock)
    (folder / 'bill.pdf').write_bytes(pdf('Synthetic bill'))
    (folder / 'bill.json').write_text('{"to_number": "+15550100002", "from_number": "+15550100009"}')
    (folder / 'stray.pdf').write_bytes(pdf('Synthetic stray'))
    for name in ('bill.pdf', 'bill.json', 'stray.pdf'):
        _old(folder / name)
    check(runner, source['id'])
    clock.now += 3
    check(runner, source['id'])
    with engine().connect() as connection:
        placed = connection.execute(sa.text('SELECT mailbox_label, from_number FROM inbound_faxes')).all()
    assert placed == [('Billing', '+15550100009')]
    assert (folder / 'done' / 'bill.json').exists()
    assert (folder / 'failed' / 'stray.pdf.reason.txt').read_text().strip() == text.NO_MAILBOX
    missing = client.post('/intake/sources', headers=B, json={
        'name': 'Nowhere', 'kind': 'folder', 'direction': 'receive', 'settings': {'path': str(tmp_path / 'absent')}})
    assert missing.status_code == 400
    assert missing.json()['detail'] == text.FOLDER_MISSING.format(path=str(tmp_path / 'absent'))
    own = os.environ['FAX_DATA_DIR']
    refused = client.post('/intake/sources', headers=B, json={
        'name': 'Own files', 'kind': 'folder', 'direction': 'receive', 'settings': {'path': own}})
    assert refused.status_code == 400 and refused.json()['detail'] == text.FOLDER_IS_FAXBOT.format(path=own)
    system = client.post('/intake/sources', headers=B, json={
        'name': 'System', 'kind': 'folder', 'direction': 'receive', 'settings': {'path': '/etc'}})
    assert system.status_code == 400 and system.json()['detail'] == text.FOLDER_IS_FAXBOT.format(path='/etc')


# -- email to fax -----------------------------------------------------------------------------

def _people(client):
    _, jane = ready_user(client, 'jane', 'role_fax_operator')
    _, nora = ready_user(client, 'nora', 'role_fax_viewer')
    return jane, nora


def _email_to_fax(client, imap, jane, nora):
    return add(client, {'name': 'Email to fax', 'kind': 'email', 'direction': 'send',
                        'settings': mail_settings(imap, checked_by='mx.google.com', smtp_host='localhost',
                                                  smtp_port=2525),
                        'secret': {'password': 'synthetic-mailbox-password'},
                        'senders': [{'address': 'jane@example.com', 'principal_id': jane['id']},
                                    {'address': 'nora@example.com', 'principal_id': nora['id']}]})


def test_email_to_fax_sends_once_even_across_pause_and_resume(client, imap):
    jane, nora = _people(client)
    source = _email_to_fax(client, imap, jane, nora)
    keys = client.get('/access/keys', headers=B).json()['items']
    own = [key for key in keys if key['id'] == source['sending_key_id']]
    assert own and own[0]['name'] == 'Email to fax'
    raw = email(subject='Referral for +1 303 555 0100')
    imap.add(raw)
    runner = poller(imap)
    assert check(runner, source['id'])[0]
    assert count('fax_jobs') == 1, [(i['state'], i['status']) for i in items(client)]
    sent = items(client)[0]
    assert sent['state'] == 'sent' and sent['to_number'] == '+13035550100' and sent['sender_name'] == 'Jane'
    who = client.get(f"/intake/sources/faxes/{sent['fax_id']}", headers=B)
    assert who.json()['sentence'] == 'Sent by email from Jane (jane@example.com) through Email to fax.'

    # Polled again: the existing fax, never a second one.
    imap.add(raw)
    check(runner, source['id'])
    assert count('fax_jobs') == 1

    # Pausing revokes the connector's key; resuming issues a new one. The same message still sends nothing.
    paused = client.post(f"/intake/sources/{source['id']}/pause", headers=B)
    assert paused.status_code == 200 and paused.json()['status'].startswith('Paused by ')
    revoked = [key for key in client.get('/access/keys', headers=B).json()['items']
               if key['id'] == source['sending_key_id']][0]
    assert revoked['revoked_at'] is not None
    resumed = client.post(f"/intake/sources/{source['id']}/resume", headers=B).json()
    assert resumed['sending_key_id'] and resumed['sending_key_id'] != source['sending_key_id']
    imap.add(raw)
    check(runner, source['id'])
    assert count('fax_jobs') == 1
    assert items(client)[0]['duplicates'] == 2

    # A new message from Jane is a new fax under the new key.
    imap.add(email(subject='+13035550111', message_id='<referral-2@example.com>'))
    check(runner, source['id'])
    assert count('fax_jobs') == 2

    # When the fax is done, Jane hears once.
    with engine().begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'success' WHERE id = :id"),
                           {'id': sent['fax_id']})
    runner.reply_step()
    runner.reply_step()
    replies = [(to, payload) for _, to, payload in FakeSMTP.sent if 'Fax sent to +13035550100' in payload]
    assert len(replies) == 1 and replies[0][0] == 'jane@example.com'
    assert 'Auto-Submitted: auto-replied' in replies[0][1] and 'In-Reply-To: <referral-1@example.com>' in replies[0][1]


def test_unconfirmed_senders_get_no_reply_and_unlisted_ones_hear_why(client, imap):
    jane, nora = _people(client)
    source = _email_to_fax(client, imap, jane, nora)
    runner = poller(imap)
    # No passing result from the administrator's mail server: refused, listed, no reply, nothing sent.
    imap.add(email(results=None, subject='+13035550100'))
    imap.add(email(results='mx.attacker.example; dmarc=pass header.from=example.com', subject='+13035550100',
                   message_id='<forged@example.com>'))
    # Confirmed, but not on the connector's list: refused with one reply.
    imap.add(email(sender='bob@example.com', subject='+13035550100', message_id='<bob@example.com>'))
    check(runner, source['id'])
    runner.reply_step()
    assert count('fax_jobs') == 0
    listed = {item['what']: item for item in items(client)}
    assert listed['+13035550100']['status'] == text.NOT_AUTHENTICATED
    refused = [item for item in items(client) if item['refused']]
    assert len(refused) == 3
    assert [to for _, to, _ in FakeSMTP.sent] == ['bob@example.com']
    assert 'Your address is not allowed to send faxes through fax@example.com.' in FakeSMTP.sent[0][2]
    assert 'Nothing was sent.' in FakeSMTP.sent[0][2]
    assert client.get('/intake/sources', headers=B).json()['connectors'][0]['counts']['refused'] == 3
    # Polled again: still one reply.
    imap.add(email(sender='bob@example.com', subject='+13035550100', message_id='<bob@example.com>'))
    check(runner, source['id'])
    runner.reply_step()
    assert len(FakeSMTP.sent) == 1


def test_a_person_without_fax_send_is_refused_although_the_connector_key_could_send(client, imap):
    jane, nora = _people(client)
    source = _email_to_fax(client, imap, jane, nora)
    # Tom holds fax:send through his role but has not replaced his temporary password: no authority yet.
    tom = create_user(client, 'tom', 'Tom')['user']
    from api.tests.test_access_management_http import assign
    assign(client, tom['id'], 'role_fax_operator')
    current = client.get('/intake/sources', headers=B).json()['connectors'][0]
    changed = client.put(f"/intake/sources/{source['id']}", headers=B, json={
        'version': current['version'], 'senders': [
            {'address': 'nora@example.com', 'principal_id': nora['id']},
            {'address': 'tom@example.com', 'principal_id': tom['id']}]})
    assert changed.status_code == 200, changed.text
    runner = poller(imap)
    imap.add(email(sender='nora@example.com', subject='+13035550100', message_id='<nora-1@example.com>'))
    imap.add(email(sender='tom@example.com', subject='+13035550100', message_id='<tom-1@example.com>'))
    check(runner, source['id'])
    runner.reply_step()
    assert count('fax_jobs') == 0
    found = {item['sender']: item for item in items(client)}
    assert found['nora@example.com']['state'] == 'refused'
    assert found['nora@example.com']['status'] == text.NO_SEND_PERMISSION.format(person='Nora')
    assert found['tom@example.com']['status'] == text.NO_SEND_PASSWORD.format(person='Tom')
    replies = {to: body for _, to, body in FakeSMTP.sent}
    assert sorted(replies) == ['nora@example.com', 'tom@example.com']
    assert 'Your Faxbot account is not allowed to send faxes.' in replies['nora@example.com']
    assert text.REPLY_NO_PASSWORD in replies['tom@example.com']


INTERNAL_HEADERS = (('X-MS-Exchange-Organization-AuthAs', 'Internal'),
                    ('X-MS-Exchange-Organization-AuthSource', 'SN6PR04MB4224.namprd04.prod.outlook.com'),
                    ('X-MS-Exchange-Organization-AuthMechanism', '04'))
INTERNAL_RESULTS = ('dkim=none (message not signed) header.d=none;dmarc=none action=none header.from=example.com;'
                    'compauth=pass reason=115')


def test_microsoft_365_internal_mail_sends_and_forged_internal_marks_do_not(client, tmp_path):
    import imaplib
    from app.intake.sources.oauth import Tokens
    jane, nora = _people(client)
    server = FakeImap(tmp_path, password='synthetic-mailbox-password', token='synthetic-access-token')
    try:
        tenant = add(client, {'name': 'Microsoft fax', 'kind': 'email', 'direction': 'send',
                              'settings': {'provider': 'microsoft365', 'address': 'fax@example.com',
                                           'tenant_id': 'example.onmicrosoft.com', 'client_id': 'client-1'},
                              'secret': {'client_secret': 'synthetic-client-secret'},
                              'senders': [{'address': 'jane@example.com', 'principal_id': jane['id']}]})
        generic = _email_to_fax(client, server, jane, nora)
        runner = poller(server)
        # The Microsoft 365 connector reaches Exchange Online; here that is the stand-in on this machine.
        runner.connect = lambda host, port, context, timeout: imaplib.IMAP4_SSL('localhost', server.port,
                                                                               ssl_context=context, timeout=timeout)
        runner.tokens = Tokens(post=lambda url, data: (200, {'access_token': 'synthetic-access-token',
                                                             'expires_in': 3600}))
        # Mail from inside the organization: no DKIM or SPF pass, but Exchange's Internal mark.
        server.add(email(results=INTERNAL_RESULTS, headers=INTERNAL_HEADERS, subject='+13035550100',
                         message_id='<internal-1@example.com>'))
        assert check(runner, tenant['id'])[0]
        assert count('fax_jobs') == 1
        sent = items(client, 'Microsoft fax')[0]
        assert sent['state'] == 'sent'
        assert sent['status'] == 'Sent on to be faxed to +13035550100. ' + text.ACCEPTED_INTERNAL
        # The same marks from outside reach a generic connector: refused, listed, no reply.
        server.add(email(results=None, headers=INTERNAL_HEADERS, subject='+13035550100',
                         message_id='<forged-1@example.com>'))
        runner.connect = None
        check(runner, generic['id'])
        runner.reply_step()
        assert count('fax_jobs') == 1
        forged = items(client, 'Email to fax')[0]
        assert forged['state'] == 'refused' and forged['status'] == text.NOT_AUTHENTICATED
        assert FakeSMTP.sent == []
    finally:
        server.close()


def test_email_to_fax_needs_one_number_and_a_document(client, imap):
    jane, nora = _people(client)
    source = _email_to_fax(client, imap, jane, nora)
    runner = poller(imap)
    imap.add(email(subject='No number here', message_id='<a@example.com>'))
    imap.add(email(subject='+13035550100 or +13035550111', message_id='<b@example.com>'))
    imap.add(email(subject='+13035550100', message_id='<c@example.com>', documents=()))
    imap.add(email(subject='Auto: +13035550100', message_id='<d@example.com>',
                   headers=[('Auto-Submitted', 'auto-replied')]))
    check(runner, source['id'])
    runner.reply_step()
    assert count('fax_jobs') == 0
    statuses = sorted(item['status'] for item in items(client))
    assert statuses == sorted([text.NO_NUMBER.format(example='fax+13035550100@example.com'), text.TWO_NUMBERS,
                               text.NO_DOCUMENT_SEND, text.AUTOMATIC])
    assert len(FakeSMTP.sent) == 3  # no reply to automatic mail


# -- folder to fax ----------------------------------------------------------------------------

def test_folder_to_fax_sends_a_file_to_a_number_once(client, tmp_path):
    folder = tmp_path / 'outbox'
    folder.mkdir()
    source = add(client, {'name': 'Outbox', 'kind': 'folder', 'direction': 'send',
                          'settings': {'path': str(folder), 'settle_seconds': 2, 'sidecar_minutes': 1}})
    assert source['has_sending_key']
    clock = Clock()
    runner = poller(clock=clock)
    document = pdf('Synthetic letter')

    def drop(name, number):
        (folder / f'{name}.pdf').write_bytes(document)
        (folder / f'{name}.json').write_text('{"to": "%s"}' % number)
        _old(folder / f'{name}.pdf')
        _old(folder / f'{name}.json')
        check(runner, source['id'])
        clock.now += 3
        check(runner, source['id'])
    drop('letter', '+13035550100')
    assert count('fax_jobs') == 1 and (folder / 'done' / 'letter.pdf').exists()
    drop('letter-again', '+13035550100')
    assert count('fax_jobs') == 1
    drop('letter-elsewhere', '+13035550111')
    assert count('fax_jobs') == 2
    # A number Faxbot cannot dial goes to failed/ and is never sent.
    (folder / 'short.pdf').write_bytes(pdf('Synthetic short'))
    (folder / 'short.json').write_text('{"to": "+1555"}')
    _old(folder / 'short.pdf')
    _old(folder / 'short.json')
    check(runner, source['id'])
    clock.now += 3
    check(runner, source['id'])
    assert count('fax_jobs') == 2
    assert (folder / 'failed' / 'short.pdf.reason.txt').read_text().strip() == text.BAD_NUMBER.format(text='+1555')
    # A file without its sidecar waits, then fails with the reason.
    (folder / 'orphan.pdf').write_bytes(pdf('Synthetic orphan'))
    _old(folder / 'orphan.pdf')
    check(runner, source['id'])
    clock.now += 3
    check(runner, source['id'])
    assert (folder / 'orphan.pdf').exists()
    clock.now += 61
    check(runner, source['id'])
    assert (folder / 'failed' / 'orphan.pdf.reason.txt').read_text().strip() == \
        text.NO_SIDECAR_SEND.format(name='orphan.json', minutes=1)
    who = client.get(f"/intake/sources/faxes/{items(client, 'Outbox')[-1]['fax_id']}", headers=B).json()
    assert who['sentence'] == 'Sent from the folder connector Outbox.'


# -- routes -----------------------------------------------------------------------------------

def test_routes_need_settings_permissions_and_list_choices(client):
    viewer, _ = ready_user(client, 'viewer', 'role_fax_viewer')
    assert viewer.get('/intake/sources').status_code == 403
    assert viewer.post('/intake/sources', {'name': 'x', 'kind': 'folder', 'direction': 'receive',
                                           'settings': {'path': '/tmp'}}).status_code == 403
    box = mailbox(client, 'Front Desk', '+15550100001')
    choices = client.get('/intake/sources/choices', headers=B).json()
    assert {'id': box['id'], 'label': 'Front Desk', 'number': '+15550100001'} in choices['mailboxes']
    assert [provider['id'] for provider in choices['providers']] == ['microsoft365', 'google', 'other']
    assert choices['providers'][0]['checks_senders_with'] == 'Microsoft 365'
