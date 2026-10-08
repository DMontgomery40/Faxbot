"""Synthetic Direct certificates for tests, made at test time; no key is ever committed.

``pki()`` gives a trust anchor, an intermediate authority and certificates for
two synthetic Direct addresses (``example.org`` and ``example.net`` are
reserved for documentation, RFC 2606), plus an untrusted authority that issues
a look-alike certificate.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


SENDER = 'faxes@direct.clinic.example.org'
RECIPIENT = 'records@direct.hospital.example.net'


def _name(common):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Faxbot synthetic test')])


def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def certificate(subject, subject_key, issuer, issuer_key, *, ca=False, email=None, dns=None, days=365,
                not_before=None, usage='both', crl=None, ca_issuers=None):
    now = not_before or datetime.now(timezone.utc) - timedelta(days=1)
    builder = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(issuer)
               .public_key(subject_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now).not_valid_after(now + timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()), critical=False))
    if ca:
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False),
            critical=True)
    else:
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=usage in ('both', 'sign'), content_commitment=False,
            key_encipherment=usage in ('both', 'encrypt'), data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
    names = [x509.RFC822Name(email)] if email else []
    names += [x509.DNSName(dns)] if dns else []
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    if ca_issuers:
        from cryptography.x509.oid import AuthorityInformationAccessOID
        builder = builder.add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(
            AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(ca_issuers))]), critical=False)
    if crl:
        builder = builder.add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
            full_name=[x509.UniformResourceIdentifier(crl)], relative_name=None, reasons=None, crl_issuer=None)]),
            critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


@dataclass(frozen=True)
class Pki:
    anchor: x509.Certificate
    anchor_key: object
    intermediate: x509.Certificate
    intermediate_key: object
    sender: x509.Certificate
    sender_key: object
    recipient: x509.Certificate
    recipient_key: object
    domain_bound: x509.Certificate
    domain_key: object
    rogue_anchor: x509.Certificate
    rogue: x509.Certificate
    rogue_key: object


@lru_cache(maxsize=1)
def pki():
    anchor_key = key()
    anchor = certificate('Synthetic Direct Anchor', anchor_key, _name('Synthetic Direct Anchor'), anchor_key, ca=True,
                         days=3650)
    intermediate_key = key()
    intermediate = certificate('Synthetic HISP CA', intermediate_key, anchor.subject, anchor_key, ca=True,
                               days=1825)
    sender_key, recipient_key, domain_key, rogue_key = key(), key(), key(), key()
    sender = certificate('Clinic faxes', sender_key, intermediate.subject, intermediate_key, email=SENDER)
    recipient = certificate('Hospital records', recipient_key, intermediate.subject, intermediate_key,
                            email=RECIPIENT)
    domain_bound = certificate('Hospital domain', domain_key, intermediate.subject, intermediate_key,
                               dns='direct.hospital.example.net')
    rogue_anchor_key = key()
    rogue_anchor = certificate('Rogue Anchor', rogue_anchor_key, _name('Rogue Anchor'), rogue_anchor_key, ca=True)
    rogue = certificate('Hospital records', rogue_key, rogue_anchor.subject, rogue_anchor_key, email=RECIPIENT)
    return Pki(anchor, anchor_key, intermediate, intermediate_key, sender, sender_key, recipient, recipient_key,
               domain_bound, domain_key, rogue_anchor, rogue, rogue_key)


def pem_key(private_key):
    return private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode('ascii')


def pem_cert(*certificates):
    return b''.join(item.public_bytes(serialization.Encoding.PEM) for item in certificates).decode('ascii')


# A fake HISP: SMTP submission with STARTTLS and sign-in (aiosmtpd), and MDNs from the recipient's side ------------

class _Recorder:
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
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class FakeHisp:
    """The HISP's sending server: STARTTLS required, then sign-in with the synthetic password, then SMTP."""

    USER, PASSWORD = SENDER, 'synthetic-hisp-password'

    def __init__(self, directory, *, cert=None, key=None):
        """``cert`` and ``key``: reuse another fake server's certificate, so one trusted certificate covers both
        (two self-signed certificates with the same name confuse OpenSSL's lookup)."""
        import ssl
        from aiosmtpd.controller import Controller
        from aiosmtpd.smtp import AuthResult
        from api.tests.imap_fake import certificate as server_certificate
        directory.mkdir(parents=True, exist_ok=True)
        if cert is not None:
            self.cert, server_key = cert, key
        else:
            self.cert, server_key = server_certificate(directory)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(server_key))
        self.recorder = _Recorder()
        self.logins = []

        def authenticator(server, session, envelope, mechanism, auth_data):
            self.logins.append(auth_data.login)
            return AuthResult(success=(auth_data.login.decode() == self.USER
                                       and auth_data.password.decode() == self.PASSWORD), handled=False)
        self.controller = Controller(self.recorder, hostname='127.0.0.1', port=free_port(), tls_context=context,
                                     require_starttls=True, auth_required=True, auth_require_tls=True,
                                     authenticator=authenticator)
        self.controller.start()
        self.port = self.controller.port

    @property
    def messages(self):
        return self.recorder.messages

    def close(self):
        self.controller.stop()


def client_context(*cert_paths):
    import ssl
    context = ssl.create_default_context()
    for path in cert_paths:
        context.load_verify_locations(cafile=str(path))
    return context


class Party:
    """A synthetic Direct party (its own security agent) for building messages Faxbot receives."""

    def __init__(self, address, certificate, private_key, chain=()):
        self.address, self.certificate, self.key, self.chain = address, certificate, private_key, tuple(chain)

    def setting(self, name):
        return {'direct_address': self.address}.get(name)


def notice(party, original_message_id, faxbot_certificate, disposition):
    """An MDN from ``party`` about a message Faxbot sent, signed by it and encrypted for Faxbot."""
    from api.app.digital.direct_message import notice_message, secure_message
    headers, body = notice_message(account=party, original_message_id=original_message_id, recipient=SENDER,
                                   disposition=disposition)
    return secure_message(headers, body, own_certificate=party.certificate, own_chain=party.chain,
                          own_key=party.key, recipients=[faxbot_certificate])


def direct_message_from(party, faxbot_certificate, document, *,
                        message_id='<synthetic.1@direct.hospital.example.net>', ask_delivery=True):
    """A Direct message with an XDM package, from ``party`` to Faxbot's address, signed and encrypted."""
    from api.app.digital.direct_message import build_message, secure_message
    headers, body = build_message(sender=party.address, recipient=SENDER, message_id=message_id, document=document,
                                  pages=1, organization='Synthetic Hospital', request_delivery=ask_delivery)
    return secure_message(headers, body, own_certificate=party.certificate, own_chain=party.chain,
                          own_key=party.key, recipients=[faxbot_certificate])


def synthetic_pdf(text='Synthetic referral for a test patient'):
    from io import BytesIO
    from reportlab.pdfgen import canvas
    output = BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 720, text)
    document.showPage()
    document.save()
    return output.getvalue()


def hisp_settings(hisp=None, imap=None, *, security='faxbot', receives=False, mailbox_id=None, **extra):
    """(settings, credentials) of a synthetic HISP account on the fake HISP and fake mailbox."""
    world = pki()
    settings = {'direct_address': SENDER, 'smtp_host': '127.0.0.1', 'smtp_port': hisp.port if hisp else 587,
                'security': security, 'imap_host': '127.0.0.1', 'imap_port': imap.port if imap else 993,
                'request_delivery': True, 'wait_minutes': 60, 'receives': receives,
                'certificate': pem_cert(world.sender, world.intermediate), 'currency': 'USD',
                'monthly_fee': '16.58', 'price_per_message': '0', 'price_source': 'https://hdirect.inpriva.com/',
                'price_date': '2026-10-08', **extra}
    if mailbox_id:
        settings['mailbox_id'] = mailbox_id
    credentials = {'password': FakeHisp.PASSWORD, 'private_key': pem_key(world.sender_key)}
    return settings, credentials
