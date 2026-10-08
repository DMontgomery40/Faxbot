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
                not_before=None, usage='both', crl=None):
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
