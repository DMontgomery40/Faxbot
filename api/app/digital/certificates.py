"""Direct certificates: trust bundles, checking a certificate against them, and finding a recipient's certificate.

Trust (Applicability Statement for Secure Health Transport; DirectTrust):
- A trust bundle is a PKCS #7 file (``.p7b``) or PEM text of trust anchors.
  The administrator names the bundles their HISP's trust community publishes
  (DirectTrust's are listed at directtrust.org/trust-bundles). Bundles are kept
  in Faxbot's database (``digital_trust_bundles``), never in the configuration.
- ``check`` builds a path from a certificate to an anchor, each link verified
  with the issuer's key; every certificate on the path must be in its validity
  period, and every issuer a CA. The certificate must fit the purpose
  (``encrypt``: key encipherment; ``sign``: digital signature) and be bound to
  the address: an address-bound certificate names the address (subject
  alternative name rfc822Name, or the legacy emailAddress), a domain-bound one
  names the domain (dNSName, or the domain in rfc822Name/emailAddress).
- Revocation: when a certificate on the path publishes a CRL address, Faxbot
  reads the CRL (``crl_fetch``) and refuses a revoked certificate. A CRL that
  cannot be read is noted on the result and does not refuse the certificate,
  as the Direct reference implementation does by default; integration should
  confirm the HISP's policy.

Discovery (``discover``), as the Direct reference implementation does it:
DNS CERT records first (RFC 4398; the address with ``@`` replaced by ``.``,
then the domain; PKIX certificates and IPKIX links), then LDAP (SRV
``_ldap._tcp.<domain>``, anonymous, ``(mail=<address>)``, ``userCertificate``).
Only certificates that pass ``check`` for encryption are used.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID


MAX_PATH = 8
MAX_BUNDLE_BYTES = 4 * 1024 * 1024


class CertificateRefused(ValueError):
    """The certificate cannot be used; one plain sentence for the administrator."""


@dataclass(frozen=True)
class Checked:
    certificate: x509.Certificate
    path: tuple            # leaf first, anchor last
    bound: str             # 'address' or 'domain'
    notes: tuple = ()      # plain sentences, such as a CRL that could not be read

    @property
    def fingerprint(self):
        return fingerprint(self.certificate)


def fingerprint(certificate):
    return certificate.fingerprint(hashes.SHA256()).hex()


def load_certificates(data):
    """Certificates from PEM text, one DER certificate, or a PKCS #7 bundle (DER or PEM). Raises CertificateRefused."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise CertificateRefused('The certificate file is empty.')
    if len(data) > MAX_BUNDLE_BYTES:
        raise CertificateRefused('The certificate file is larger than 4 MB.')
    data = bytes(data)
    found = []
    if b'-----BEGIN PKCS7-----' in data:
        try:
            return list(pkcs7.load_pem_pkcs7_certificates(data))
        except ValueError:
            raise CertificateRefused('The trust bundle could not be read.') from None
    if b'-----BEGIN CERTIFICATE-----' in data:
        try:
            found = x509.load_pem_x509_certificates(data)
        except ValueError:
            raise CertificateRefused('A certificate in the file could not be read.') from None
        return list(found)
    try:
        return [x509.load_der_x509_certificate(data)]
    except ValueError:
        pass
    try:
        return list(pkcs7.load_der_pkcs7_certificates(data))
    except ValueError:
        raise CertificateRefused('The file is not a certificate or a trust bundle Faxbot can read.') from None


def pem(certificates):
    return b''.join(certificate.public_bytes(serialization.Encoding.PEM) for certificate in certificates)


def load_private_key(data, password=None):
    try:
        return serialization.load_pem_private_key(data if isinstance(data, bytes) else data.encode('ascii'),
                                                  password=password)
    except (ValueError, TypeError):
        raise CertificateRefused('The private key could not be read. Paste it as PEM text, without a password.') \
            from None


def _now(now):
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _extension(certificate, kind):
    try:
        return certificate.extensions.get_extension_for_class(kind).value
    except x509.ExtensionNotFound:
        return None


def _is_ca(certificate):
    constraints = _extension(certificate, x509.BasicConstraints)
    return bool(constraints is not None and constraints.ca)


def _issued_by(child, issuer):
    if child.issuer != issuer.subject:
        return False
    try:
        child.verify_directly_issued_by(issuer)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _names(certificate):
    """(rfc822 names, dns names), lower case, including the legacy emailAddress attribute."""
    emails, dns = set(), set()
    alternative = _extension(certificate, x509.SubjectAlternativeName)
    if alternative is not None:
        emails |= {name.lower() for name in alternative.get_values_for_type(x509.RFC822Name)}
        dns |= {name.lower().rstrip('.') for name in alternative.get_values_for_type(x509.DNSName)}
    for attribute in certificate.subject.get_attributes_for_oid(NameOID.EMAIL_ADDRESS):
        emails.add(str(attribute.value).lower())
    return emails, dns


def binding(certificate, address):
    """'address' when the certificate names ``address``, 'domain' when it names its domain, else None."""
    address = (address or '').strip().lower()
    domain = address.rpartition('@')[2]
    emails, dns = _names(certificate)
    if address and address in emails:
        return 'address'
    if domain and (domain in dns or domain in emails):
        return 'domain'
    return None


def _usage_ok(certificate, purpose):
    usage = _extension(certificate, x509.KeyUsage)
    if usage is None:
        return True
    if purpose == 'encrypt':
        return bool(usage.key_encipherment or usage.key_agreement)
    return bool(usage.digital_signature)


def _path(certificate, anchors, intermediates):
    """Leaf-to-anchor path, each link verified; None when no anchor is reached."""
    anchor_prints = {fingerprint(anchor): anchor for anchor in anchors}
    if fingerprint(certificate) in anchor_prints:
        return (certificate,)
    path = [certificate]
    current = certificate
    pool = list(intermediates)
    for _ in range(MAX_PATH):
        anchor = next((item for item in anchors if _issued_by(current, item)), None)
        if anchor is not None:
            return tuple(path + [anchor])
        issuer = next((item for item in pool if item not in path and _is_ca(item) and _issued_by(current, item)),
                      None)
        if issuer is None:
            return None
        path.append(issuer)
        current = issuer
    return None


def check(certificate, *, anchors, intermediates=(), address=None, purpose='encrypt', now=None, crl_fetch=None):
    """``Checked`` when ``certificate`` chains to a trust anchor, is current, fits ``purpose`` and is bound to
    ``address``; raises CertificateRefused with one sentence otherwise."""
    if not anchors:
        raise CertificateRefused('No trust bundle is loaded, so no certificate can be trusted yet.')
    moment = _now(now)
    path = _path(certificate, list(anchors), list(intermediates))
    if path is None:
        raise CertificateRefused('The certificate is not issued by any authority in your trust bundles.')
    for index, item in enumerate(path):
        if item.not_valid_before_utc > moment or item.not_valid_after_utc < moment:
            raise CertificateRefused('The certificate has expired or is not valid yet.' if index == 0 else
                                     'A certificate that vouches for it has expired or is not valid yet.')
        if index > 0 and not _is_ca(item) and fingerprint(item) not in {fingerprint(a) for a in anchors}:
            raise CertificateRefused('The certificate is vouched for by a certificate that is not an authority.')
    if not _usage_ok(certificate, purpose):
        raise CertificateRefused('The certificate is not meant for encrypting messages.' if purpose == 'encrypt'
                                 else 'The certificate is not meant for signing messages.')
    bound = 'address'
    if address is not None:
        bound = binding(certificate, address)
        if bound is None:
            raise CertificateRefused(f'The certificate is not issued for {address} or its domain.')
    notes = []
    if crl_fetch is not None:
        # ``crl_fetch(url) -> bytes`` raises LookupError when the list cannot be read.
        for child, issuer in zip(path, path[1:]):
            state = revocation(child, issuer, crl_fetch, now=moment)
            if state == 'revoked':
                raise CertificateRefused('The certificate has been revoked by its authority.')
            if state == 'unknown':
                notes.append('Faxbot could not read the authority\'s revocation list for a certificate on the path.')
    return Checked(certificate, path, bound, tuple(dict.fromkeys(notes)))


_CRLS = {}


def revocation(certificate, issuer, crl_fetch, *, now=None):
    """'good', 'revoked', 'unknown' (a published CRL could not be read) or 'none' (no CRL published)."""
    points = _extension(certificate, x509.CRLDistributionPoints)
    if points is None:
        return 'none'
    urls = [name.value for point in points for name in (point.full_name or ())
            if isinstance(name, x509.UniformResourceIdentifier) and re.match(r'https?://', name.value)]
    if not urls:
        return 'none'
    moment = _now(now)
    for url in urls[:2]:
        crl = _CRLS.get(url)
        if crl is None or (crl.next_update_utc is not None and crl.next_update_utc < moment):
            try:
                data = crl_fetch(url)
                crl = x509.load_der_x509_crl(data) if not data.lstrip().startswith(b'-----') else \
                    x509.load_pem_x509_crl(data)
            except (LookupError, ValueError):
                # ``crl_fetch`` raises LookupError when the list cannot be read; ValueError is an unreadable list.
                continue
            if crl.issuer != issuer.subject or not crl.is_signature_valid(issuer.public_key()):
                continue
            if len(_CRLS) > 256:
                _CRLS.clear()
            _CRLS[url] = crl
        return 'revoked' if crl.get_revoked_certificate_by_serial_number(certificate.serial_number) else 'good'
    return 'unknown'


def describe(certificate):
    """One line for a person: who it names and until when."""
    emails, dns = _names(certificate)
    names = sorted(emails | dns)
    who = names[0] if names else certificate.subject.rfc4514_string()[:120]
    return f'{who}, valid until {certificate.not_valid_after_utc:%d %B %Y}'


def digest(data):
    return hashlib.sha256(data).hexdigest()


# Discovery --------------------------------------------------------------------------------------------------------

@dataclass
class Discovery:
    """What a lookup found: the usable certificates and what was tried, for the message's record."""
    certificates: list = field(default_factory=list)
    checked: list = field(default_factory=list)
    tried: list = field(default_factory=list)      # ('dns'|'ldap', name, outcome) tuples
    refused: list = field(default_factory=list)    # sentences for certificates found but not usable


def discover(address, *, anchors, intermediates=(), dns_lookup=None, ldap_lookup=None, now=None, crl_fetch=None):
    """The recipient's usable encryption certificates: DNS CERT (address, then domain), then LDAP.

    ``dns_lookup(name) -> [bytes]`` returns DER certificates published at a name (``dns_cert.certificates``);
    ``ldap_lookup(address) -> [bytes]`` the certificates an LDAP directory publishes for an address or its domain
    (``ldap.certificates``). Either may raise ``LookupError`` when its service cannot be asked; that is recorded
    and the next source is tried.
    """
    address = address.strip().lower()
    local, _, domain = address.partition('@')
    result = Discovery()
    sources = []
    if dns_lookup is not None:
        sources += [('dns', f'{local}.{domain}', lambda: dns_lookup(f'{local}.{domain}')),
                    ('dns', domain, lambda: dns_lookup(domain))]
    if ldap_lookup is not None:
        sources.append(('ldap', domain, lambda: ldap_lookup(address)))
    for kind, name, lookup in sources:
        try:
            found = lookup()
        except LookupError as error:
            result.tried.append((kind, name, str(error) or 'could not be asked'))
            continue
        usable = []
        for blob in found:
            try:
                certificate = x509.load_der_x509_certificate(blob)
            except ValueError:
                result.refused.append('A published certificate could not be read.')
                continue
            try:
                checked = check(certificate, anchors=anchors, intermediates=intermediates, address=address,
                                purpose='encrypt', now=now, crl_fetch=crl_fetch)
            except CertificateRefused as refusal:
                result.refused.append(str(refusal))
                continue
            usable.append(checked)
        result.tried.append((kind, name, f'{len(usable)} usable' if usable else ('none usable' if found else 'none')))
        if usable:
            result.checked = usable
            result.certificates = [item.certificate for item in usable]
            return result
    return result
