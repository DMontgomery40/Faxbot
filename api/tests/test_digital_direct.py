"""A fax delivered as a Direct message: the real delivery worker, planner, routed transport and delivery store,
against a fake HISP (SMTP with STARTTLS and sign-in) and a fake HISP mailbox (IMAP over TLS) with MDNs.

Every address is synthetic (example.org and example.net, RFC 2606), every number a 555 number, every
certificate made at test time. Nothing contacts a real HISP.
"""
import asyncio
from datetime import datetime, timedelta
import smtplib
from types import SimpleNamespace
from uuid import uuid4
import zipfile
import io

import pytest
import sqlalchemy as sa

from api.app.config_profiles import ConfigurationDocument, ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.digital import accounts as digital_accounts
from api.app.digital import certificates, direct_message, smime, xdm
from api.app.digital.routes import DigitalRoute
from api.app.digital.store import DigitalStore
from api.app.digital.worker import DigitalWorker
from api.app.outbound_store import OutboundStore
from api.app.outbound_worker import OutboundWorker
from api.app.routing.transport import RoutedTransport
from api.app.schema import create_database_engine, upgrade_schema
from api.tests.digital_fixtures import (RECIPIENT, SENDER, FakeHisp, Party, client_context, hisp_settings, notice,
                                        pki, synthetic_pdf)
from api.tests.imap_fake import FakeImap
from api.tests.test_direct_delivery import Conventional
from api.tests.test_peer_fax import _namespaces


DEST = '+13035550142'


def _rows(engine, query, **params):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(query), params).mappings()]


@pytest.fixture(params=['sqlite', 'postgresql'])
def world(request, tmp_path):
    scoped = _namespaces(request, 1)
    url = scoped[0].render_as_string(hide_password=False) if scoped else 'sqlite:///' + str(tmp_path / 'clinic.db')
    engine = create_database_engine(url)
    upgrade_schema(engine)
    data = tmp_path / 'data'
    data.mkdir()
    configuration = ConfigurationStore(engine, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(data),
        'PUBLIC_API_URL': 'https://clinic.example.org', 'DIRECT_ORGANIZATION': 'County Clinic',
        'FAX_DEFAULT_COUNTRY': 'US'})
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'k', 'api_secret': 's'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    imap = FakeImap(tmp_path, password=FakeHisp.PASSWORD, user=SENDER)
    hisp = FakeHisp(tmp_path / 'hisp', cert=imap.cert, key=tmp_path / 'imap-key.pem')
    settings, credentials = hisp_settings(hisp, imap)
    documents = digital_accounts.added(snapshot.desired.values, {'key': 'hisp', 'provider': 'hisp',
                                                                 'settings': settings, 'credentials': credentials})
    configuration.apply(snapshot, snapshot.desired.values, restart_required=False, actor='test',
                        accounts=ConfigurationDocument(documents))
    store = DigitalStore(engine)
    world = pki()
    store.add_bundle('hisp', certificates.pem([world.anchor]).decode('ascii'), anchors=1)
    address = store.add_address(number=DEST, kind='direct', address=RECIPIENT, source='entered', action='confirmed')
    # The recipient's directory publishes its certificate and the HISP authority that issued it.
    published = {'records': [world.recipient, world.intermediate]}
    context = client_context(imap.cert)

    def dns_lookup(name):
        if name == RECIPIENT.replace('@', '.'):
            return [certificate.public_bytes(certificates.serialization.Encoding.DER)
                    for certificate in published['records']]
        return []
    transport = direct_message.Transport(ssl_context=context, dns_lookup=dns_lookup, ldap_lookup=lambda address: [],
                                         crl_fetch=lambda url: (_ for _ in ()).throw(LookupError('none')))
    delivery = OutboundStore(configuration)
    try:
        yield SimpleNamespace(engine=engine, configuration=configuration, data=data, store=store, hisp=hisp,
                              imap=imap, transport=transport, delivery=delivery, address=address, pki=world,
                              published=published,
                              values=lambda: configuration.read().active.values)
    finally:
        hisp.close()
        imap.close()
        engine.dispose()


def queue(world, *, number=DEST, pages=1):
    job = uuid4().hex
    (world.data / (job + '.pdf')).write_bytes(synthetic_pdf())
    now = datetime.utcnow()
    world.configuration.accept_outbound(world.configuration.read().active, {
        'id': job, 'to_number': number, 'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued',
        'pages': pages, 'created_at': now, 'updated_at': now})
    return job


async def send(world):
    conventional = Conventional(world.delivery)
    transport = RoutedTransport(conventional, direct=None, relay=None,
                                digital=DigitalRoute(world.engine, values=world.values, transport=world.transport))
    assert await OutboundWorker(world.delivery, transport).step() is True
    return conventional


def delivery_state(world, job):
    (row,) = _rows(world.engine, 'SELECT state, attempt_id FROM outbound_deliveries WHERE id = :id', id=job)
    (attempt,) = _rows(world.engine, 'SELECT phase, error_category FROM outbound_attempts WHERE id = :id',
                       id=row['attempt_id'])
    return row['state'], attempt['phase'], attempt['error_category']


def worker(world):
    return DigitalWorker(world.engine, values=world.values, delivery=lambda: world.delivery,
                         transport=world.transport)


def opened(world, raw):
    """What the recipient's security agent reads: decrypted, signature checked, unwrapped."""
    headers, body = smime.split_headers(raw)
    plain = smime.decrypt(smime.transfer_decode(headers, body), world.pki.recipient, world.pki.recipient_key)
    content, signature, opaque = smime.unwrap_signed(plain)
    signer = smime.verify_detached(content, signature, opaque=opaque)
    certificates.check(signer.certificate, anchors=[world.pki.anchor], intermediates=signer.certificates,
                       address=SENDER, purpose='sign')
    return headers, signer.content


def test_a_fax_goes_as_a_direct_message_then_processed_then_dispatched_settle_it(world):
    job = queue(world)
    conventional = asyncio.run(send(world))
    assert conventional.submissions == 0 and len(world.hisp.messages) == 1
    assert world.hisp.logins == [SENDER.encode()]
    mail_from, recipients, raw = world.hisp.messages[0]
    assert mail_from == SENDER and recipients == [RECIPIENT]
    outer, inner = opened(world, raw)
    assert 'smime-type=enveloped-data' in outer['content-type']
    wrapped_headers, wrapped = smime.split_headers(inner)
    assert smime.media_type(wrapped_headers['content-type']) == 'message/rfc822'
    message_headers, _ = smime.split_headers(wrapped)
    assert message_headers['disposition-notification-options'] == direct_message.OPTIONS
    assert message_headers['disposition-notification-to'] == SENDER
    documents = direct_message._documents_in(wrapped)
    assert [(name, kind) for name, kind, _ in documents] == [('DOC0001.PDF', 'application/pdf')]
    assert documents[0][2] == (world.data / (job + '.pdf')).read_bytes()
    (message,) = world.store.for_job(job)
    assert message['state'] == 'submitted' and message['security'] == 'faxbot'
    assert message['certificate_sha256'] == certificates.fingerprint(world.pki.recipient)
    assert delivery_state(world, job)[0] == 'in_progress'
    choice = _rows(world.engine, 'SELECT route, provider_id FROM delivery_route_costs WHERE job_id = :id', id=job) \
        if 'delivery_route_costs' in sa.inspect(world.engine).get_table_names() else None
    assert choice is None or choice[0]['route'] == 'dsm.' + world.address['id']

    party = Party(RECIPIENT, world.pki.recipient, world.pki.recipient_key, chain=(world.pki.intermediate,))
    world.imap.add(notice(party, message['message_id'], world.pki.sender, 'processed'))
    worker(world).step()
    assert world.store.message(message['id'])['state'] == 'processed'
    assert delivery_state(world, job)[0] == 'in_progress'
    world.imap.add(notice(party, message['message_id'], world.pki.sender, 'dispatched'))
    worker(world).step()
    assert world.store.message(message['id'])['state'] == 'dispatched'
    assert delivery_state(world, job)[0] == 'success'
    # Both notices were moved out of the inbox, and a repeat changes nothing.
    assert world.imap.count('INBOX') == 0 and world.imap.count('Faxbot filed') == 2
    world.imap.add(notice(party, message['message_id'], world.pki.sender, 'dispatched'))
    worker(world).step()
    assert [event['kind'] for event in world.store.events_for(message['id'])].count('dispatched') == 1


def test_no_notice_within_the_wait_is_uncertain_and_a_late_one_still_settles_it(world):
    job = queue(world)
    asyncio.run(send(world))
    (message,) = world.store.for_job(job)
    worker(world).step(now=datetime.utcnow() + timedelta(minutes=30))
    assert world.store.message(message['id'])['state'] == 'submitted'
    worker(world).step(now=datetime.utcnow() + timedelta(minutes=61))
    assert world.store.message(message['id'])['state'] == 'uncertain'
    # Exactly what AV's uncertain item reads: waiting for reconciliation, the attempt uncertain; never failed.
    assert delivery_state(world, job) == ('reconciliation_required', 'uncertain', 'notice_missing')
    assert len(world.hisp.messages) == 1
    party = Party(RECIPIENT, world.pki.recipient, world.pki.recipient_key, chain=(world.pki.intermediate,))
    world.imap.add(notice(party, message['message_id'], world.pki.sender, 'dispatched'))
    worker(world).step()
    assert world.store.message(message['id'])['state'] == 'dispatched'
    assert delivery_state(world, job)[0] == 'success'


def test_a_failed_notice_lets_the_fax_take_its_next_route(world):
    job = queue(world)
    asyncio.run(send(world))
    (message,) = world.store.for_job(job)
    party = Party(RECIPIENT, world.pki.recipient, world.pki.recipient_key, chain=(world.pki.intermediate,))
    world.imap.add(notice(party, message['message_id'], world.pki.sender, 'failed'))
    worker(world).step()
    assert world.store.message(message['id'])['state'] == 'failed'
    state = delivery_state(world, job)[0]
    assert state in ('ready', 'failed')   # back in the queue for the fax route, or failed when none remains


def test_an_untrusted_certificate_is_refused_and_the_fax_goes_by_its_own_route_in_the_same_attempt(world):
    world.published['records'] = [world.pki.rogue]
    job = queue(world)
    conventional = asyncio.run(send(world))
    assert world.hisp.messages == [] and conventional.submissions == 1
    (message,) = world.store.for_job(job)
    assert message['state'] == 'refused' and 'no trusted certificate' in message['detail'].lower() \
        or 'not issued by any authority' in message['detail']
    assert delivery_state(world, job)[0] == 'in_progress'


def test_a_refused_recipient_address_falls_back_and_a_lost_answer_is_uncertain(world):
    world.hisp.recorder.rcpt_reply = '550 No such Direct address'
    job = queue(world)
    conventional = asyncio.run(send(world))
    assert conventional.submissions == 1
    assert world.store.for_job(job)[0]['state'] == 'refused'

    class LostAfterData:
        """An SMTP session that takes the message and then drops the connection before answering."""
        def __init__(self, *args):
            self.sent = []

        def ehlo(self):
            return 250, b'ok'

        def has_extn(self, name):
            return True

        def starttls(self, context=None):
            return 220, b'go'

        def login(self, user, password):
            return 235, b'ok'

        def mail(self, sender):
            return 250, b'ok'

        def rcpt(self, recipient):
            return 250, b'ok'

        def docmd(self, command):
            return 354, b'go ahead'

        def send(self, data):
            self.sent.append(data)

        def getreply(self):
            raise smtplib.SMTPServerDisconnected('gone')

        def quit(self):
            raise smtplib.SMTPServerDisconnected('gone')

        def close(self):
            pass
    world.hisp.recorder.rcpt_reply = None
    world.transport.smtp_connect = lambda host, port, timeout: LostAfterData()
    second = queue(world, number='+13035550143')
    world.store.add_address(number='+13035550143', kind='direct', address=RECIPIENT, source='entered',
                            action='confirmed')
    conventional = asyncio.run(send(world))
    assert conventional.submissions == 0
    assert world.store.for_job(second)[0]['state'] == 'uncertain'
    assert delivery_state(world, second)[:2] == ('reconciliation_required', 'uncertain')


def bounce(message_id, *, action='failed'):
    """A mail server's delivery status report (RFC 3464) about a message Faxbot sent; plain, as servers send them."""
    lines = [
        'From: MAILER-DAEMON@smtp.hisp.example.org', f'To: {SENDER}', 'Subject: Undelivered Mail Returned to Sender',
        'Message-ID: <bounce.1@smtp.hisp.example.org>', 'MIME-Version: 1.0',
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b1"', '',
        '--b1', 'Content-Type: text/plain', '', 'Your message could not be delivered.',
        '--b1', 'Content-Type: message/delivery-status', '', 'Reporting-MTA: dns; smtp.hisp.example.org', '',
        f'Final-Recipient: rfc822; {RECIPIENT}', f'Action: {action}', 'Status: 5.1.1', '',
        '--b1', 'Content-Type: text/rfc822-headers', '', f'Message-ID: {message_id}', f'To: {RECIPIENT}', '',
        '--b1--', '']
    return '\r\n'.join(lines).encode()


def test_a_plain_bounce_fails_a_waiting_message_and_reports_are_never_received_messages(world):
    job = queue(world)
    asyncio.run(send(world))
    (message,) = world.store.for_job(job)
    report = bounce(message['message_id'])
    headers, body = smime.split_headers(report)
    parsed = direct_message.read_notice(report)
    assert (parsed.kind, parsed.original_message_id, parsed.detail['status']) == ('failed', message['message_id'],
                                                                                   '5.1.1')
    # An unencrypted delivery notice is never trusted when Faxbot is the security agent.
    party = Party(RECIPIENT, world.pki.recipient, world.pki.recipient_key)
    from api.app.digital.direct_message import notice_message, plain_message
    plain_mdn = plain_message(*notice_message(account=party, original_message_id=message['message_id'],
                                              recipient=SENDER, disposition='dispatched'))
    world.imap.add(plain_mdn)
    world.imap.add(report)
    worker(world).step()
    assert world.store.message(message['id'])['state'] == 'failed'
    assert world.store.recent(direction='in') == []
    assert world.imap.count('INBOX') == 0


def test_with_receiving_off_only_notices_are_read_and_other_messages_stay_in_the_mailbox(world):
    from api.tests.digital_fixtures import direct_message_from
    job = queue(world)
    asyncio.run(send(world))
    (message,) = world.store.for_job(job)
    party = Party(RECIPIENT, world.pki.recipient, world.pki.recipient_key, chain=(world.pki.intermediate,))
    world.imap.add(direct_message_from(party, world.pki.sender, synthetic_pdf()))
    world.imap.add(notice(party, message['message_id'], world.pki.sender, 'processed'))
    for _ in range(2):
        worker(world).step()
    assert world.store.message(message['id'])['state'] == 'processed'
    assert world.store.recent(direction='in') == [] and len(world.hisp.messages) == 1
    assert world.imap.count('INBOX') == 1 and world.imap.count('Faxbot filed') == 1


def test_hisp_security_hands_over_the_plain_message_for_the_hisp_to_sign(world):
    snapshot = world.configuration.read()
    documents = digital_accounts.patched(snapshot.desired.values, 'hisp', {'settings': {'security': 'hisp'}})
    world.configuration.apply(snapshot, snapshot.desired.values, restart_required=False, actor='test',
                              accounts=ConfigurationDocument(documents))
    job = queue(world)
    asyncio.run(send(world))
    (_, _, raw) = world.hisp.messages[0]
    headers, _ = smime.split_headers(raw)
    assert smime.media_type(headers['content-type']) == 'multipart/mixed'
    assert world.store.for_job(job)[0]['security'] == 'hisp'


def test_xdm_package_structure_and_metadata():
    document = synthetic_pdf()
    package = xdm.build([('referral.pdf', 'application/pdf', document)], sender=SENDER, recipient=RECIPIENT,
                        organization='County Clinic')
    names = set(zipfile.ZipFile(io.BytesIO(package)).namelist())
    assert names == {'README.TXT', 'INDEX.HTM', 'IHE_XDM/SUBSET01/METADATA.XML', 'IHE_XDM/SUBSET01/DOC0001.PDF'}
    metadata = zipfile.ZipFile(io.BytesIO(package)).read('IHE_XDM/SUBSET01/METADATA.XML').decode()
    import hashlib
    assert hashlib.sha1(document).hexdigest() in metadata and f'<rim:Value>{len(document)}</rim:Value>' in metadata
    assert 'urn:ihe:iti:xds:2017:mimeTypeSufficient' in metadata and f'^^Internet^{RECIPIENT}' in metadata
    (found,) = xdm.read(package)
    assert found.data == document and found.media_type == 'application/pdf'
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, 'w') as archive:
        archive.writestr('../outside.pdf', b'%PDF-1.4')
    with pytest.raises(xdm.XdmError, match='outside'):
        xdm.read(evil.getvalue())


def test_certificate_discovery_tries_dns_then_ldap_and_keeps_only_a_trusted_one():
    world = pki()
    der = lambda certificate: certificate.public_bytes(certificates.serialization.Encoding.DER)  # noqa: E731
    asked = []

    def dns(name):
        asked.append(('dns', name))
        return [der(world.rogue)] if name == RECIPIENT.replace('@', '.') else []

    def ldap(address):
        asked.append(('ldap', address))
        return [der(world.recipient)]
    found = certificates.discover(RECIPIENT, anchors=[world.anchor], intermediates=[world.intermediate],
                                  dns_lookup=dns, ldap_lookup=ldap)
    assert found.certificates == [world.recipient]
    assert asked == [('dns', 'records.direct.hospital.example.net'), ('dns', 'direct.hospital.example.net'),
                     ('ldap', RECIPIENT)]
    assert any('not issued by any authority' in reason for reason in found.refused)

    def unreachable(name):
        raise LookupError('DNS could not be reached.')
    nothing = certificates.discover(RECIPIENT, anchors=[world.anchor], dns_lookup=unreachable,
                                    ldap_lookup=lambda address: [])
    assert nothing.certificates == [] and nothing.tried[0][2] == 'DNS could not be reached.'
