"""Intake queue delivery over a real local SMTP server; never a duplicate delivery."""
from datetime import datetime, timedelta
from email import message_from_bytes, policy
import socket
from uuid import uuid4

import pytest
import sqlalchemy as sa
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

from api.tests.test_outbound_store import installation
from api.tests.test_schema import database
from api.app.intake.email import AmbiguousFailure, DefiniteFailure, send
from api.app.intake.store import IntakeConflict, IntakeInputError, IntakeStore
from api.app.intake.worker import ConnectorSecrets, IntakeWorker


class Recorder:
    def __init__(self):
        self.messages = []
        self.rcpt_reply = None
        self.data_reply = None

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.rcpt_reply:
            return self.rcpt_reply
        envelope.rcpt_tos.append(address)
        return '250 OK'

    async def handle_DATA(self, server, session, envelope):
        if self.data_reply:
            return self.data_reply
        self.messages.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.content))
        return '250 Message accepted for delivery'


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture
def smtp():
    recorder = Recorder()
    controller = Controller(recorder, hostname='127.0.0.1', port=free_port())
    controller.start()
    try:
        yield recorder, controller.port
    finally:
        controller.stop()


@pytest.fixture
def intake(installation, tmp_path):
    configuration, _, snapshot = installation
    store = IntakeStore(configuration.engine, ConnectorSecrets(configuration))
    return store, configuration, snapshot.active.values, tmp_path


def email_settings(port, **changes):
    return {'host': '127.0.0.1', 'port': port, 'security': 'none', 'from_address': 'fax@clinic.example',
            'recipients': ['frontdesk@clinic.example'], 'subject_template': 'Fax from {from_number}', **changes}


def write_pdf(path):
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, 'Synthetic referral page')
    pdf.showPage()
    pdf.save()
    return path


def inbound(store, tmp_path, *, received_at=None, to='+15550100001'):
    identity = uuid4().hex
    path = write_pdf(tmp_path / f'{identity}.pdf')
    now = received_at or datetime.utcnow()
    with store.engine.begin() as connection:
        connection.execute(store.inbound.insert().values(id=identity, from_number='+15550109999', to_number=to,
            status='received', backend='sip', pages=1, pdf_path=str(path), created_at=now, received_at=now,
            updated_at=now))
    return identity


def worker(store, values):
    return IntakeWorker(store, values=lambda: values)


def test_inbound_fax_is_emailed_once_with_the_original_pdf(intake, smtp):
    store, _, values, tmp_path = intake
    recorder, port = smtp
    connector = store.create_connector(name='Front desk', settings=email_settings(port))
    fax = inbound(store, tmp_path)
    assert worker(store, values).step() is True
    (item,) = store.list_items()
    assert item['inbound_fax_id'] == fax and item['state'] == 'delivered' and item['attempts'] == 1
    assert item['delivery_reference'] == f"<intake-{item['id']}@clinic.example>"
    assert item['connector_id'] == connector.id
    (sender, recipients, content), = recorder.messages
    assert sender == 'fax@clinic.example' and recipients == ['frontdesk@clinic.example']
    message = message_from_bytes(content, policy=policy.default)
    assert message['Subject'] == 'Fax from +15550109999'
    body = message.get_body(('plain',)).get_content().strip()
    assert body.startswith('Fax from +15550109999 to +15550100001, 1 page, received ') and body.endswith('UTC.')
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_content_type() == 'application/pdf'
    assert attachment.get_content() == (tmp_path / f'{fax}.pdf').read_bytes()
    # Nothing else is due; the delivered item is never sent again.
    assert worker(store, values).step() is False
    assert len(recorder.messages) == 1
    with pytest.raises(IntakeConflict):
        store.schedule_retry(item['id'])


def test_temporary_refusal_backs_off_then_delivers(intake, smtp):
    store, _, values, tmp_path = intake
    recorder, port = smtp
    store.create_connector(name='Front desk', settings=email_settings(port))
    inbound(store, tmp_path)
    recorder.rcpt_reply = '451 Try again later'
    worker(store, values).step()
    (item,) = store.list_items()
    assert item['state'] == 'received' and item['attempts'] == 1
    assert item['last_error'] == 'The email server refused a recipient address.'
    assert item['next_attempt_at'] > datetime.utcnow()
    assert worker(store, values).step() is False  # Not due yet.
    recorder.rcpt_reply = None
    due = store.claim(now=item['next_attempt_at'] + timedelta(seconds=1))
    worker(store, values).deliver(due)
    assert store.get_item(item['id'])['state'] == 'delivered' and len(recorder.messages) == 1


def test_permanent_refusal_waits_for_a_person(intake, smtp):
    store, _, values, tmp_path = intake
    recorder, port = smtp
    store.create_connector(name='Front desk', settings=email_settings(port))
    inbound(store, tmp_path)
    recorder.data_reply = '554 Message rejected'
    worker(store, values).step()
    (item,) = store.list_items()
    assert item['state'] == 'failed' and item['last_error'] == 'The email server refused the message.'
    assert worker(store, values).step() is False
    recorder.data_reply = None
    store.schedule_retry(item['id'])
    worker(store, values).step()
    assert store.get_item(item['id'])['state'] == 'delivered' and len(recorder.messages) == 1


def test_ambiguous_send_and_stopped_worker_are_never_resent_automatically(intake, smtp):
    store, _, values, tmp_path = intake
    _, port = smtp
    store.create_connector(name='Front desk', settings=email_settings(port))
    inbound(store, tmp_path)
    inbound(store, tmp_path)
    sent = []

    def lost(settings, password, message):
        sent.append(message['Message-ID'])
        raise AmbiguousFailure('The email server stopped responding after the fax was sent.')
    IntakeWorker(store, values=lambda: values, sender=lost).step()
    failed = [item for item in store.list_items() if item['state'] == 'failed']
    assert len(failed) == 1 and failed[0]['last_error'].endswith('Check the inbox before sending it again.')
    # A worker that stops mid-send leaves the item for a person too.
    claimed = store.claim()
    assert store.recover_expired(now=datetime.utcnow() + timedelta(minutes=3)) == 1
    stopped = store.get_item(claimed['id'])
    assert stopped['state'] == 'failed' and 'check the inbox' in stopped['last_error']
    assert IntakeWorker(store, values=lambda: values, sender=lost).step() is False
    assert len(sent) == 1


def test_faxes_from_before_a_connector_existed_wait_for_a_person(intake, smtp):
    store, _, values, tmp_path = intake
    recorder, port = smtp
    inbound(store, tmp_path, received_at=datetime.utcnow() - timedelta(days=2))
    worker(store, values).step()
    (item,) = store.list_items()
    assert item['state'] == 'received' and item['next_attempt_at'] is None
    assert item['last_error'] == 'No email delivery is set up for this number yet.'
    store.create_connector(name='Front desk', settings=email_settings(port))
    assert worker(store, values).step() is False and recorder.messages == []
    store.schedule_retry(item['id'])
    worker(store, values).step()
    assert store.get_item(item['id'])['state'] == 'delivered'


def test_connector_matching_and_sealed_password(intake, smtp):
    store, configuration, _, _ = intake
    _, port = smtp
    general = store.create_connector(name='Everything', settings=email_settings(port), password='synthetic-secret')
    clinic = store.create_connector(name='Clinic line', settings=email_settings(port), match_number='(555) 010-0002')
    assert clinic.match_number == '+15550100002'
    assert store.connector_for('+1 555 010 0002').id == clinic.id
    assert store.connector_for('+15550100001').id == general.id
    with store.engine.connect() as connection:
        envelope = connection.scalar(sa.select(store.connectors.c.secret_envelope).where(
            store.connectors.c.id == general.id))
    assert 'synthetic-secret' not in envelope and store.password(general.id) == 'synthetic-secret'
    kept = store.update_connector(general.id, version=general.version, password=None)
    assert kept.has_password and store.password(general.id) == 'synthetic-secret'
    cleared = store.update_connector(general.id, version=kept.version, password='')
    assert not cleared.has_password
    with pytest.raises(IntakeConflict):
        store.update_connector(general.id, version=1, name='stale')
    with pytest.raises(IntakeInputError, match='recipient'):
        store.create_connector(name='Bad', settings=email_settings(port, recipients=['not-an-address']))
    with pytest.raises(IntakeInputError, match='subject'):
        store.create_connector(name='Bad', settings=email_settings(port, subject_template='{secret}'))


def test_installation_settings_define_a_managed_connector(intake, smtp):
    store, _, values, _ = intake
    _, port = smtp
    configured = values.with_patch({'intake_email_enabled': True, 'intake_smtp_host': '127.0.0.1',
                                    'intake_smtp_port': port, 'intake_smtp_security': 'none',
                                    'intake_email_from': 'fax@clinic.example',
                                    'intake_email_to': 'a@clinic.example, b@clinic.example',
                                    'intake_smtp_password': 'synthetic-password'})
    managed = store.sync_managed(configured)
    assert managed.managed and managed.settings.recipients == ('a@clinic.example', 'b@clinic.example')
    assert store.sync_managed(configured).version == managed.version
    with pytest.raises(IntakeConflict):
        store.update_connector(managed.id, version=managed.version, name='Edited')
    with pytest.raises(IntakeConflict):
        store.delete_connector(managed.id)
    assert store.sync_managed(configured.with_patch({'intake_email_enabled': False})) is None
    assert not store.get_connector(managed.id).enabled


def test_send_authenticates_and_classifies_refusals(smtp):
    recorder, port = smtp
    from api.app.intake.store import EmailSettings
    from api.app.intake.email import test_message
    settings = EmailSettings.validate(email_settings(port))
    delivery = send(settings, '', test_message(settings, 'connector-1'))
    assert delivery.reference.startswith('<intake-test-connector-1@') and len(recorder.messages) == 1
    unreachable = EmailSettings.validate(email_settings(free_port()))
    with pytest.raises(DefiniteFailure) as error:
        send(unreachable, '', test_message(unreachable, 'connector-1'), timeout=2)
    assert error.value.temporary


@pytest.mark.filterwarnings('ignore:Requiring AUTH while not requiring TLS')
def test_login_is_used_when_a_user_name_is_set():
    recorder = Recorder()
    seen = []

    def authenticator(server, session, envelope, mechanism, auth_data):
        seen.append((auth_data.login, auth_data.password))
        return AuthResult(success=auth_data.password == b'right', handled=False)
    controller = Controller(recorder, hostname='127.0.0.1', port=free_port(), auth_required=True,
                            auth_require_tls=False, authenticator=authenticator)
    controller.start()
    try:
        from api.app.intake.store import EmailSettings
        from api.app.intake.email import test_message
        settings = EmailSettings.validate(email_settings(controller.port, username='fax'))
        with pytest.raises(DefiniteFailure, match='user name or password') as error:
            send(settings, 'wrong', test_message(settings, 'c'))
        assert not error.value.temporary
        send(settings, 'right', test_message(settings, 'c'))
        assert seen[-1] == (b'fax', b'right') and len(recorder.messages) == 1
    finally:
        controller.stop()
