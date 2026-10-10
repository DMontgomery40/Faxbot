"""The caller-verification stamp on received faxes (research N25): what the network asserted about who called.

Some receivers act on a fax because of where it came from: a pharmacy on a faxed
Schedule II order for a hospice patient (21 CFR 1306.11), a bank on an instruction
from a registered number. Caller ID can be spoofed; STIR/SHAKEN lets the network
say whether it verified the caller number. On a trunk whose carrier passes that
result, Faxbot keeps it with each received fax and stamps it:

- **verified and registered**: the network verified the caller number, and it is
  on your list of registered senders for received faxes
  (``RECEIVED_REGISTERED_SENDERS``, kept in the audited configuration; not the
  sending side's registered-sender pins, ``routing/sender_pins.py``);
- **verified but unregistered**: verified, and not on that list;
- **unverified**: the network did not verify it, or said nothing.

The network's word comes from either of two places. The carrier's own check, the
``verstat`` parameter on the caller's P-Asserted-Identity or From
(``TN-Validation-Passed``, ``TN-Validation-Failed``, ``No-TN-Validation``; 3GPP TS
24.229 and ATIS-1000074). Or the SHAKEN PASSporT in the Identity header (RFC 8225,
RFC 8588), whose ``attest`` is A (the carrier knows the customer and the number), B
or C: Faxbot checks its signature exactly as it checks a forwarded call's
(``diversion.check_passport``) against the certificate authorities you trust for
forwarded calls (``inbound/trust.py``), and counts only attestation A with a
signature that chains to one of them. The stamp reports what the network asserted
about the call, never that the document is genuine.

The headers come from the file Asterisk keeps for a call that carries an Identity
header or a verstat ([faxbot-sip-headers]), which the hand-over removes once read.
Not yet run against a real carrier's STIR/SHAKEN result.
"""
from __future__ import annotations

import json
import re

import sqlalchemy as sa

SETTING = 'received_registered_senders'
VERIFIED_REGISTERED, VERIFIED_UNREGISTERED, UNVERIFIED = 'verified_registered', 'verified_unregistered', 'unverified'
PASSED, FAILED, NOT_CHECKED = 'TN-Validation-Passed', 'TN-Validation-Failed', 'No-TN-Validation'
GENUINE = 'This shows who placed the call, not that the document is genuine.'
_VERSTAT = re.compile(r'verstat=([A-Za-z-]+)', re.IGNORECASE)


def registered(values):
    """The registered senders for received faxes, E.164 as stored, in the order you listed them."""
    text = str(getattr(values, SETTING, '') or '')
    return [part.strip() for part in text.split(',') if part.strip()]


def encode(numbers):
    return ','.join(dict.fromkeys(numbers))


def verstat(headers):
    """The carrier's verification result (``verstat``) on P-Asserted-Identity or From, or None."""
    for name in ('P-Asserted-Identity', 'From'):
        for value in headers.get(name) or ():
            found = _VERSTAT.search(value or '')
            if found:
                return found.group(1)
    return None


def shaken(headers):
    """The SHAKEN PASSporT among the call's Identity headers (ppt absent or shaken, with attest), or None."""
    from .diversion import parse_identity
    for value in headers.get('Identity') or ():
        passport = parse_identity(value)
        if passport is not None and passport.header.get('ppt') in (None, 'shaken') and \
                passport.claims.get('attest') in ('A', 'B', 'C'):
            return passport
    return None


def check(headers, *, caller, did, at, trusted=(), numbers=(), fetch=None):
    """The stamp for one received call: {'stamp', 'attest', 'verstat', 'signature', 'registered', 'sentence'}, or
    None when the network asserted nothing and you keep no registered senders (no stamp then)."""
    from . import diversion
    status = verstat(headers or {})
    passport = shaken(headers or {})
    attest, signature, why = None, None, None
    if passport is not None:
        attest = passport.claims.get('attest')
        if trusted:
            try:
                signature, why = diversion.check_passport(passport, did=did, at=at, fetch=fetch, trusted=trusted)
            except ValueError as error:  # an unusable certificate or key (cryptography's documented error)
                signature, why = diversion.UNCHECKED, f'its certificate could not be read ({error})'
        else:
            signature, why = diversion.UNANCHORED, 'you trust no certificate authority for forwarded calls yet'
    if status is None and passport is None and not numbers:
        return None
    by_carrier = status == PASSED
    by_faxbot = attest == 'A' and signature == diversion.SIGNED
    on_list = bool(caller) and caller in set(numbers)
    shown = caller or 'the caller number'
    if by_carrier or by_faxbot:
        how = ("the carrier's STIR/SHAKEN check passed" if by_carrier
               else 'STIR/SHAKEN attestation A, signed and checked by Faxbot')
        stamp = VERIFIED_REGISTERED if on_list else VERIFIED_UNREGISTERED
        sentence = (f'The network verified the caller number {shown} ({how}), and it is on your registered senders. '
                    + GENUINE) if on_list else (
            f'The network verified the caller number {shown} ({how}), but it is not on your registered senders.')
    else:
        stamp = UNVERIFIED
        if status == FAILED:
            reason = "the carrier's STIR/SHAKEN check failed"
        elif attest in ('B', 'C'):
            reason = f'the carrier vouched only with attestation {attest}, not for this number'
        elif attest == 'A':
            reason = f'its signature could not be confirmed: {why}' if why else 'its signature could not be confirmed'
        elif status == NOT_CHECKED:
            reason = 'the carrier did not check it'
        else:
            reason = 'the network said nothing about it'
        sentence = (f'The caller number {shown} is not verified ({reason}); treat it as what the sender claims'
                    + (', even though it is on your registered senders.' if on_list else '.'))
    return {'stamp': stamp, 'attest': attest, 'verstat': status, 'signature': signature, 'registered': on_list,
            'sentence': sentence}


def received(payload, call, *, received_at=None):
    """The stamp for a call handed over by the built-in engine (``inbound/http.receive_handover``), from the headers
    Asterisk kept for it; also whether a header file was there to remove."""
    from . import diversion, trust
    from ..config import configuration_values, settings
    values = configuration_values()
    headers = diversion.read_headers(settings.fax_data_dir, payload.get('uniqueid'))
    moment = diversion.call_time(call, received_at or diversion.utcnow())
    stamp = check(headers, caller=payload.get('from_number'), did=payload.get('to_number'), at=moment,
                  trusted=trust.certificates(values) if headers else (), numbers=registered(values))
    return stamp, bool(headers)


def for_fax(engine, inbound_id):
    """The stamp kept with one received fax's import report, or None."""
    imports = sa.table('inbound_imports', sa.column('inbound_fax_id'), sa.column('report'),
                       sa.column('imported_at', sa.DateTime()))
    with engine.connect() as connection:
        text = connection.execute(sa.select(imports.c.report).where(imports.c.inbound_fax_id == inbound_id)
                                  .order_by(imports.c.imported_at.desc()).limit(1)).scalar()
    try:
        report = json.loads(text) if text else {}
    except ValueError:
        return None
    found = report.get('caller_check') if isinstance(report, dict) else None
    return found if isinstance(found, dict) and found.get('sentence') else None
