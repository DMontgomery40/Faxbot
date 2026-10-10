"""Encrypted audio fax trunks (N18), and the internet access a trunk is reached over.

Some business trunks carry fax only as encrypted audio:

- **Swisscom Smart Business Connect.** Innovaphone's provider profile
  (wiki.innovaphone.com/?i=14782, test results of 3 December 2024, marked
  deprecated because the vendor no longer tests it; read 2026-10-09) records
  that "the provider supports only TLS as transport protocol", supports SRTP
  for incoming, outgoing and on-net calls, that G.711 fax to and from the PSTN
  worked while "transport of faxes using T.38 failed to PSTN and onnet
  destinations", that the remote RTP endpoint may not change during a call,
  and that "registration of two SIP-interfaces on the same SIP-account is not
  supported". Its 2021 profile (?i=12001) and Enreach's guide reported T.38
  working and no SRTP, so the newest evidence decides: TLS, SRTP, G.711 audio
  fax. If T.38 now completes on your trunk, the preset is what to change.
- **Telekom CompanyFlex.** On another provider's internet access, "the
  signalling and call data of the telephony must be encrypted" and
  "unencrypted operation is not possible" (hilfe.companyflex.de, use of
  another provider's IP access, read 2026-10-09; Telekom's guideline 1TR119
  defines the encryption). Its fax page (read 2026-10-09) recommends T.38 on
  Telekom's own IP lines, 9600 bit/s and error correction (ECM), and says
  "with encrypted transmission only fax with T.30 is supported". Telekom asks
  for SRTP whenever signalling uses TLS (Yeastar's CompanyFlex TLS guide), so
  on Telekom's own line T.38 needs unencrypted signalling over TCP.

So a CompanyFlex trunk's media depends on the access it is reached over. The
network check (``sip_network``) records Faxbot's internet address; you list
the addresses (or address ranges) of your Telekom line
(``sip_trunk_own_access``). On any other access, and whenever the access is
not known yet, encryption is required: Faxbot signs in over TLS, encrypts the
audio (SRTP, SDES keys in the TLS-protected SDP) and sends audio fax (T.30),
whatever the trunk's connection setting says, and turns T.38 off for new calls
with the reason. Back on the Telekom line, it turns T.38 on again only when
that was its own earlier decision and the trunk signs in over TCP. Required
encryption is never dropped; a trunk whose encryption cannot be written into
Asterisk's file is not loaded, so its faxes wait in Sent.

The access record lives next to the trunk files in
``<FAX_DATA_DIR>/asterisk/trunk-access``. Nothing here opens a port or
contacts a carrier.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path


OWN, OTHER, UNKNOWN = 'own', 'other', 'unknown'
TELEKOM = 'telekom'
ACCESS_TEXT = {OWN: 'your Telekom line', OTHER: 'another internet access', UNKNOWN: 'an access Faxbot has not '
               'checked yet'}


def _preset(values):
    from . import sip_trunk
    return sip_trunk.PRESETS.get(getattr(values, 'sip_trunk_preset', '') or '')


def own_networks(values):
    """The addresses and ranges you listed as your own line's internet access; unreadable entries are left out."""
    found = []
    for item in str(getattr(values, 'sip_trunk_own_access', '') or '').replace(';', ',').split(','):
        item = item.strip()
        if not item:
            continue
        try:
            found.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return found


def access(values, check=None):
    """'own', 'other' or 'unknown': whether the last network check found Faxbot on the access you listed."""
    if check is None:
        from .sip_network import read_check
        check = read_check(values)
    address = (check or {}).get('internet_address')
    if not address:
        return UNKNOWN
    try:
        found = ipaddress.ip_address(address)
    except ValueError:
        return UNKNOWN
    networks = own_networks(values)
    if not networks:
        return UNKNOWN
    return OWN if any(found in network for network in networks) else OTHER


def encryption_required(values, preset=None, *, check=None):
    """Whether this trunk's calls must be encrypted now: always for an encrypted-audio-only trunk; for a trunk with
    an access rule (CompanyFlex), unless the last check found Faxbot on your own line."""
    preset = preset or _preset(values)
    if preset is None:
        return False
    if preset.encrypted_audio_only:
        return True
    if preset.access_rule == TELEKOM:
        return access(values, check) != OWN
    return False


def record_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'trunk-access'


def read_record(values):
    try:
        found = json.loads(record_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and found.get('access') in (OWN, OTHER, UNKNOWN) else None


def requalify(values, check):
    """After a network check: record the trunk's access and return {'from', 'to', 'required'} when it changed (the
    trunk's media must be written again), else None. Only for a trunk with an access rule."""
    preset = _preset(values)
    if preset is None or preset.access_rule != TELEKOM:
        return None
    now = access(values, check)
    before = read_record(values)
    if before is not None and before.get('access') == now:
        return None
    from . import sip_trunk
    record = {'access': now, 'address': (check or {}).get('internet_address'),
              'at': datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z',
              'from': before.get('access') if before else None}
    path = record_path(values)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sip_trunk._write_private(path, json.dumps(record) + '\n')
    return {'from': record['from'], 'to': now, 'required': now != OWN}


def sentence(values):
    """One sentence beside the trunk's connection setting: how its calls are encrypted now, and why; None for a
    trunk without encrypted audio."""
    preset = _preset(values)
    if preset is None or not (preset.encrypted_audio_only or preset.access_rule):
        return None
    if preset.encrypted_audio_only:
        return (f'{preset.label} accepts only encrypted sign-in (TLS), so Faxbot encrypts the audio too (SRTP) and '
                'sends fax as audio: fax over IP (T.38) cannot be encrypted.')
    where = access(values)
    if where == OWN:
        transport = getattr(values, 'sip_trunk_transport', '') or preset.transport
        if transport == 'tls':
            return ('Faxbot is on your Telekom line. Calls are encrypted (TLS and SRTP), so fax goes as audio; choose '
                    'TCP to allow fax over IP (T.38) on this line.')
        return ('Faxbot is on your Telekom line, so CompanyFlex allows unencrypted calls and fax over IP (T.38). On '
                'any other internet access Faxbot encrypts the calls and sends audio fax by itself.')
    if where == OTHER:
        return ('Faxbot is not on the Telekom line you listed, so CompanyFlex requires encrypted calls: Faxbot signs '
                'in over TLS, encrypts the audio (SRTP) and sends audio fax, since encrypted fax works only as T.30.')
    return ('Faxbot has not seen your Telekom line yet (list its internet address below), so it treats this as '
            'another internet access: encrypted calls (TLS and SRTP) and audio fax.')


def view(values):
    """What the trunk screen shows about encryption and access."""
    preset = _preset(values)
    if preset is None or not (preset.encrypted_audio_only or preset.access_rule):
        return {'media_encryption': None, 'access': None, 'encryption_sentence': None}
    required = encryption_required(values, preset)
    return {'media_encryption': 'sdes' if required or (getattr(values, 'sip_trunk_transport', '') or preset.transport)
            == 'tls' else None,
            'access': access(values) if preset.access_rule else None,
            'encryption_sentence': sentence(values)}
