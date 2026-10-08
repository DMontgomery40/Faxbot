"""Certificate authorities you trust for forwarded calls (X4): STIR/SHAKEN trust anchors.

A diversion PASSporT is signed with a certificate the PASSporT itself names, so
a signature that checks proves only which certificate signed. It is verified
when that certificate chains to a certificate authority you trust (RFC 8224
section 6.2.2; ATIS-1000074 and ATIS-1000080): in the US, the STI-CAs the STI-PA
(iconectiv) approved. The STI-PA gives its list of trusted STI-CAs to registered
service providers only, through its API with their account (STI-PA Service
Provider Guidelines, Issue 6, section 7.4); iconectiv also published a dated
"STI-CA Root Certificates" list (May 2021). So you supply the anchors yourself:
paste their certificates (PEM), or give the address of a list you can reach (a
PEM bundle, or JSON that holds PEM certificates), which Faxbot reads once when
you add it.

They are kept in the configuration (``STIR_TRUST_ANCHORS``, a JSON list of
``{"pem", "source", "added_on"}``), so every change is an audited configuration
change. Only certificate authorities are kept (basic constraints: CA), each
once, by SHA-256 fingerprint.

With anchors, ``verify_chain`` checks the PASSporT's certificate (and any
intermediate certificates served with it) up to one of them, valid at the time
of the call, with the usual CA rules and no web-server requirements on the
signing certificate (a STIR certificate names no host).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
from urllib.parse import urlsplit

SETTING = 'stir_trust_anchors'
MAX_ANCHORS = 200
LIST_BYTES = 512 * 1024
FETCH_SECONDS = 10.0
_PEM = re.compile(rb'-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s\\n]+?-----END CERTIFICATE-----')


class TrustRefused(ValueError):
    """One plain sentence about trust anchors you tried to add or remove."""


@dataclass(frozen=True)
class Anchor:
    fingerprint: str
    name: str
    valid_until: datetime
    source: str
    added_on: str
    pem: str


def _certificates(data: bytes):
    """Every X.509 certificate in ``data``: PEM blocks (also inside JSON, with escaped newlines), else one DER."""
    from cryptography import x509
    found = []
    text = data.replace(b'\\n', b'\n').replace(b'\\/', b'/')
    for block in _PEM.findall(text):
        try:
            found.append(x509.load_pem_x509_certificate(block))
        except ValueError:
            continue
    if not found and data[:1] == b'\x30':
        try:
            found.append(x509.load_der_x509_certificate(data))
        except ValueError:
            pass
    return found


def _is_authority(certificate):
    from cryptography import x509
    try:
        return certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        return False


def _fingerprint(certificate):
    from cryptography.hazmat.primitives import hashes
    return certificate.fingerprint(hashes.SHA256()).hex()


def _name(certificate):
    from cryptography.x509.oid import NameOID
    names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME) or \
        certificate.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    return str(names[0].value)[:120] if names else certificate.subject.rfc4514_string()[:120]


def _stored(values):
    text = getattr(values, SETTING, '') or ''
    try:
        items = json.loads(text) if text else []
    except ValueError:
        logging.getLogger(__name__).warning('The trusted certificate authorities for forwarded calls are unreadable.')
        return []
    return [item for item in items if isinstance(item, dict) and isinstance(item.get('pem'), str)]


def anchors(values) -> list:
    """The certificate authorities you trust, as Anchor rows, in the order you added them."""
    from cryptography import x509
    found = []
    for item in _stored(values):
        try:
            certificate = x509.load_pem_x509_certificate(item['pem'].encode('ascii'))
        except ValueError:
            continue
        found.append(Anchor(_fingerprint(certificate), _name(certificate),
                            certificate.not_valid_after_utc.replace(tzinfo=None), str(item.get('source') or '')[:300],
                            str(item.get('added_on') or '')[:10], item['pem']))
    return found


def certificates(values):
    from cryptography import x509
    return [x509.load_pem_x509_certificate(anchor.pem.encode('ascii')) for anchor in anchors(values)]


def fetch_list(url, *, get=None):
    """The certificates at ``url`` (a list you can reach): HTTPS only, no redirects, at most LIST_BYTES."""
    parts = urlsplit(url or '')
    if parts.scheme != 'https' or not parts.hostname or parts.username:
        raise TrustRefused('Give the list as an https:// address.')
    import httpx
    try:
        fetch = get or (lambda target: httpx.get(target, timeout=FETCH_SECONDS, follow_redirects=False))
        response = fetch(url)
    except (OSError, httpx.HTTPError):
        raise TrustRefused('Faxbot could not read that address; check it from the computer Faxbot runs on.') from None
    if response.status_code != 200:
        raise TrustRefused(f'That address answered {response.status_code}; Faxbot needs the list itself (200).')
    if len(response.content) > LIST_BYTES:
        raise TrustRefused('That list is larger than Faxbot reads (512 KB).')
    return response.content


def added(values, *, pem=None, url=None, get=None, today=None) -> str:
    """The setting's new value with the certificate authorities in ``pem`` or at ``url`` added (each once).

    Raises TrustRefused with one sentence when nothing usable is given."""
    if bool(pem) == bool(url):
        raise TrustRefused('Paste the certificates, or give the address of the list, but not both.')
    data = pem.encode('utf-8', 'replace') if pem else fetch_list(url, get=get)
    found = _certificates(data)
    if not found:
        raise TrustRefused('No certificate was found there. Paste PEM certificates that start with '
                           '"-----BEGIN CERTIFICATE-----".')
    authorities = [certificate for certificate in found if _is_authority(certificate)]
    if not authorities:
        raise TrustRefused('Those are not certificate authorities. Add the STI-CA root (or intermediate) '
                           "certificates, not a carrier's signing certificate.")
    from cryptography.hazmat.primitives.serialization import Encoding
    items = _stored(values)
    known = {anchor.fingerprint for anchor in anchors(values)}
    day = (today or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
    for certificate in authorities:
        if _fingerprint(certificate) in known:
            continue
        known.add(_fingerprint(certificate))
        items.append({'pem': certificate.public_bytes(Encoding.PEM).decode('ascii'),
                      'source': url or 'pasted', 'added_on': day})
    if len(items) > MAX_ANCHORS:
        raise TrustRefused(f'Faxbot keeps at most {MAX_ANCHORS} certificate authorities.')
    return json.dumps(items, separators=(',', ':'), sort_keys=True)


def removed(values, fingerprint) -> str:
    """The setting's new value without the anchor whose fingerprint starts with ``fingerprint`` (at least 8 hex)."""
    wanted = re.sub(r'[^0-9a-f]', '', str(fingerprint or '').lower())
    matches = [anchor for anchor in anchors(values) if len(wanted) >= 8 and anchor.fingerprint.startswith(wanted)]
    if len(matches) != 1:
        raise TrustRefused('Give the start of one fingerprint from the list (at least 8 characters).')
    gone = matches[0].pem
    return json.dumps([item for item in _stored(values) if item['pem'] != gone], separators=(',', ':'),
                      sort_keys=True)


def verify_chain(certificate, intermediates, trusted, at) -> bool:
    """Whether ``certificate`` chains to one of ``trusted`` (through ``intermediates``), valid at ``at``."""
    from cryptography.x509.verification import ExtensionPolicy, PolicyBuilder, Store, VerificationError
    if not trusted:
        return False
    moment = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    verifier = (PolicyBuilder().store(Store(list(trusted))).time(moment)
                .extension_policies(ca_policy=ExtensionPolicy.webpki_defaults_ca(),
                                    ee_policy=ExtensionPolicy.permit_all())
                .build_client_verifier())
    try:
        verifier.verify(certificate, list(intermediates))
    except VerificationError:
        return False
    return True


def view(anchor) -> dict:
    return {'fingerprint': anchor.fingerprint, 'short': anchor.fingerprint[:16], 'name': anchor.name,
            'valid_until': anchor.valid_until.isoformat() + 'Z', 'source': anchor.source, 'added_on': anchor.added_on}
