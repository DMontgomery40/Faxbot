"""Whether Telnyx accepts fax over IP (T.38) on the trunk's numbers, and the one fix Faxbot offers.

Telnyx switches a call to T.38 only when the number's voice settings have
``media_features.t38_fax_gateway_enabled`` on, and its connection's
``outbound.t38_reinvite_source`` says who starts the switch. With the gateway
off, every fax received on that number stays an audio fax (found live on the
owner's account on 2026-10-05 and fixed by the lead through the API).

Faxbot checks both with read-only GETs, using the read-only Telnyx key the
trunk page already uses for call charges (``telnyx_api_key``):

- ``GET /v2/phone_numbers?filter[phone_number]=<digits>`` finds the number's id
  and connection. The filter takes digits only: Telnyx's OpenAPI description
  (team-telnyx/openapi spec3.json, read 2026-10-05) says "Non-numerical
  characters will result in no values being returned", so the + is left out and
  the returned ``phone_number`` is compared with the full E.164 number;
- ``GET /v2/phone_numbers/{id}/voice`` reads ``media_features``;
- ``GET /v2/credential_connections/{id}`` (or ``/ip_connections/{id}`` and
  ``/fqdn_connections/{id}``) reads ``user_name`` and
  ``outbound.t38_reinvite_source``.

The voice read also gives ``caller_id_name_enabled``, which ``telnyx_numbers``
reports as caller-name lookup. It reads nothing else from the account and never
changes it by itself. The only change here, made when a person selects the fix, is ``PATCH
/v2/phone_numbers/{id}/voice`` with the number's full ``media_features`` and
only ``t38_fax_gateway_enabled`` set to true, followed by a fresh read. Fields
left out of a PATCH stay as they are (Telnyx API reference, "Update a phone
number with voice settings", read 2026-10-05).

Portal steps, from Telnyx's "Fax service with Telnyx via T.38 or G711" article
(https://support.telnyx.com/en/articles/1130672-fax-service-with-telnyx-via-t-38-or-g711,
read 2026-10-05), are given whenever the key cannot read or change a number.

The key is sent only to api.telnyx.com and never logged; response bodies are
never logged. The last check is kept in ``<FAX_DATA_DIR>/asterisk/telnyx-t38``.

With several trunks (provider-rules design §3.6) the check runs once per Telnyx
trunk account, with that account's own API key and numbers (``check_all``); a
trunk after the first keeps its check in ``telnyx-t38-<key>``. Every function
takes ``key``, the trunk account; None is the first trunk.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time

import httpx

from . import sip_trunk

API = 'https://api.telnyx.com/v2'
TIMEOUT_SECONDS = 10.0
SOURCE = 'https://support.telnyx.com/en/articles/1130672-fax-service-with-telnyx-via-t-38-or-g711'
# Tests point Faxbot at a fake Telnyx with an httpx transport; without one, test runs never reach Telnyx.
TRANSPORT = None
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}', re.ASCII)
ON, OFF, NOT_FOUND, UNREADABLE, UNAVAILABLE = 'on', 'off', 'not_found', 'unreadable', 'unavailable'


class Refused(Exception):
    """Telnyx answered 401 or 403: the key may not read or change this."""


class Missing(Exception):
    """Telnyx answered 404."""


class Unavailable(Exception):
    """Telnyx could not be reached, or answered something unusable."""


def _test_mode():
    return os.environ.get('FAXBOT_TEST_MODE', '').lower() in {'1', 'true', 'yes'}


def applies(values) -> bool:
    """A Telnyx trunk with numbers and a Telnyx API key."""
    return bool(values.sip_trunk_preset == 'telnyx' and values.telnyx_api_key and values.sip_trunk_did_list)


class Telnyx:
    """The few Telnyx calls Faxbot makes; ``transport`` replaces the network in tests."""

    def __init__(self, key, *, transport=None):
        self.key = key
        self.transport = transport

    def __enter__(self):
        options = {'timeout': TIMEOUT_SECONDS, 'base_url': API,
                   'headers': {'Authorization': f'Bearer {self.key}', 'Accept': 'application/json'}}
        if self.transport is not None:
            options['transport'] = self.transport
        self.client = httpx.Client(**options)
        return self

    def __exit__(self, *exc):
        self.client.close()
        return False

    def _call(self, method, path, **options):
        try:
            response = self.client.request(method, path, **options)
        except httpx.HTTPError:
            raise Unavailable() from None
        if response.status_code in (401, 403):
            raise Refused()
        if response.status_code == 404:
            raise Missing()
        if response.status_code != 200:
            raise Unavailable()
        try:
            body = response.json()
        except ValueError:
            raise Unavailable() from None
        data = body.get('data') if isinstance(body, dict) else None
        if data is None:
            raise Unavailable()
        return data

    def find_number(self, number):
        """(id, connection id) of an E.164 number on this account, or None when the account has no such number."""
        digits = re.sub(r'\D', '', number)
        data = self._call('GET', '/phone_numbers', params={'filter[phone_number]': digits, 'page[size]': 25})
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get('phone_number') == number and _ID.fullmatch(str(item.get('id'))):
                connection = item.get('connection_id')
                return str(item['id']), str(connection) if connection and _ID.fullmatch(str(connection)) else None
        return None

    def voice(self, number_id):
        data = self._call('GET', f'/phone_numbers/{number_id}/voice')
        if not isinstance(data, dict) or not isinstance(data.get('media_features'), dict):
            raise Unavailable()
        return data

    def connection(self, connection_id):
        """(user name or None, T.38 re-invite source or None) of the number's connection."""
        for kind in ('credential_connections', 'ip_connections', 'fqdn_connections'):
            try:
                data = self._call('GET', f'/{kind}/{connection_id}')
            except Missing:
                continue
            outbound = data.get('outbound') if isinstance(data, dict) else None
            source = outbound.get('t38_reinvite_source') if isinstance(outbound, dict) else None
            user = data.get('user_name') if isinstance(data, dict) else None
            return (user if isinstance(user, str) else None), (source if isinstance(source, str) else None)
        raise Missing()

    def enable_gateway(self, number_id):
        """Turn on the T.38 gateway for one number, keeping every other media setting; returns the new setting."""
        features = dict(self.voice(number_id)['media_features'])
        features['t38_fax_gateway_enabled'] = True
        self._call('PATCH', f'/phone_numbers/{number_id}/voice', json={'media_features': features})
        return self.voice(number_id)['media_features'].get('t38_fax_gateway_enabled')


# -- the check, kept between runs ----------------------------------------------------------------------------

def record_path(values, key=None) -> Path:
    name = 'telnyx-t38' if not key or key == sip_trunk.PRIMARY else f'telnyx-t38-{key}'
    if key and sip_trunk.TRUNK_KEY.fullmatch(key) is None:
        raise ValueError('Unsupported trunk key')
    return Path(values.fax_data_dir) / 'asterisk' / name


def read(values, key=None):
    """The last check, or None."""
    try:
        record = json.loads(record_path(values, key).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get('numbers'), list) else None


def _write(values, record, key=None):
    path = record_path(values, key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sip_trunk._write_private(path, json.dumps(record) + '\n')
    return record


def _client(values):
    return Telnyx(values.telnyx_api_key, transport=TRANSPORT)


def check_all(values, *, now=time.time):
    """Check every Telnyx trunk account with its own key and numbers: {trunk key: record or None}."""
    found = {}
    for trunk in sip_trunk.trunk_accounts(values):
        found[trunk.key] = check(trunk.values, now=now, key=None if trunk.primary else trunk.key)
    return found


def check(values, *, now=time.time, key=None):
    """Read each trunk number's T.38 gateway and its connection's re-invite source, store and return it.

    ``values`` are the trunk's own (``sip_trunk.trunk_for``); ``key`` names a trunk after the first.
    Returns None (and keeps no record) when it does not apply, or in a test run without a fake Telnyx.
    """
    if not applies(values):
        try:
            record_path(values, key).unlink()
        except OSError:
            pass
        return None
    if TRANSPORT is None and _test_mode():
        return None
    numbers, connections, stopped = [], {}, None
    with _client(values) as telnyx:
        for number in values.sip_trunk_did_list:
            entry = {'number': number, 'state': UNAVAILABLE, 'number_id': None, 'connection_id': None}
            if stopped:
                # Telnyx was unreachable or refused this key: the other numbers would wait or fail the same way.
                entry['state'] = stopped
                numbers.append(entry)
                continue
            try:
                found = telnyx.find_number(number)
                if found is None:
                    entry['state'] = NOT_FOUND
                else:
                    entry['number_id'], entry['connection_id'] = found
                    voice = telnyx.voice(found[0])
                    features = voice['media_features']
                    entry['state'] = ON if features.get('t38_fax_gateway_enabled') is True else OFF
                    # The same read says whether Telnyx looks up callers' names on this number (telnyx_numbers).
                    lookup = voice.get('caller_id_name_enabled')
                    entry['name_lookup'] = lookup if isinstance(lookup, bool) else None
            except Refused:
                entry['state'] = stopped = UNREADABLE
            except Missing:
                entry['state'] = UNAVAILABLE
            except Unavailable:
                entry['state'] = stopped = UNAVAILABLE
            numbers.append(entry)
        for connection_id in sorted({entry['connection_id'] for entry in numbers if entry['connection_id']}):
            try:
                user, source = telnyx.connection(connection_id)
                connections[connection_id] = {'user_name_matches': (user == values.sip_trunk_username) if user else None,
                                              'reinvite_source': source, 'state': 'read'}
            except Refused:
                connections[connection_id] = {'state': UNREADABLE}
            except (Missing, Unavailable):
                connections[connection_id] = {'state': UNAVAILABLE}
    return _write(values, {'checked_at': round(now(), 3), 'numbers': numbers, 'connections': connections}, key)


def enable(values, number, *, now=time.time, key=None):
    """Turn on the T.38 gateway for one trunk number; returns (outcome, record).

    outcome is 'on' (read back as on), 'not_on' (Telnyx accepted the change but reads back off),
    'refused' (the key may not change it), 'not_found' or 'unavailable'.
    """
    if TRANSPORT is None and _test_mode():
        return 'unavailable', read(values, key)
    outcome = 'unavailable'
    with _client(values) as telnyx:
        try:
            found = telnyx.find_number(number)
            if found is None:
                outcome = NOT_FOUND
            else:
                outcome = 'on' if telnyx.enable_gateway(found[0]) is True else 'not_on'
        except Refused:
            outcome = 'refused'
        except (Missing, Unavailable):
            outcome = 'unavailable'
    record = read(values, key) or {'checked_at': None, 'numbers': [], 'connections': {}}
    for entry in record['numbers']:
        if entry.get('number') == number and outcome in ('on', 'not_on'):
            entry['state'] = ON if outcome == 'on' else OFF
    record['checked_at'] = round(now(), 3) if outcome in ('on', 'not_on') else record.get('checked_at')
    return outcome, _write(values, record, key)


# -- sentences ------------------------------------------------------------------------------------------------

GATEWAY_STEPS = ('In the Telnyx portal, open Numbers → My Numbers, select the gear next to {number}, open Expert '
                 'Configuration and tick Enable T.38 Fax Gateway.')
REINVITE_STEPS = ('In the Telnyx portal, edit the SIP connection, open Outbound and set T.38 Re-invite Initiated By '
                  'to Telnyx.')


def shown(number):
    """+17208565062 as +1 720-856-5062."""
    import phonenumbers
    try:
        return phonenumbers.format_number(phonenumbers.parse(number), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return number


def number_sentence(entry):
    number, state = shown(entry['number']), entry.get('state')
    if state == ON:
        return f'Telnyx has fax over IP (T.38) turned on for {number}.'
    if state == OFF:
        return f'Telnyx has fax over IP (T.38) turned off for {number}, so received faxes there arrive as audio.'
    if state == NOT_FOUND:
        return f'{number} is not a number on this Telnyx account, so Faxbot cannot check its fax over IP (T.38).'
    if state == UNREADABLE:
        return (f'Faxbot\'s Telnyx API key cannot read {number}, so Faxbot cannot check its fax over IP (T.38). '
                + GATEWAY_STEPS.format(number=number))
    return f'Faxbot could not reach Telnyx to check fax over IP (T.38) for {number}; select Check again to retry.'


def connection_sentence(connection):
    if connection.get('state') == UNREADABLE:
        return ('Faxbot\'s Telnyx API key cannot read the SIP connection, so Faxbot cannot check who starts fax over '
                'IP (T.38). ' + REINVITE_STEPS)
    source = connection.get('reinvite_source')
    if connection.get('state') == 'read' and source and source != 'telnyx':
        return (f'Telnyx\'s SIP connection has T.38 Re-invite Initiated By set to "{source}", so Telnyx does not '
                'switch calls to fax over IP (T.38) itself. ' + REINVITE_STEPS)
    if connection.get('user_name_matches') is False:
        return ('This number is on a different Telnyx SIP connection than the one Faxbot signs in with, so calls to '
                'it may not reach Faxbot.')
    return None


def outcome_sentence(outcome, number):
    number_text = shown(number)
    if outcome == 'on':
        return f'Telnyx now has fax over IP (T.38) turned on for {number_text}.'
    if outcome == 'not_on':
        return (f'Telnyx accepted the change but still shows fax over IP (T.38) off for {number_text}. '
                + GATEWAY_STEPS.format(number=number_text))
    if outcome == 'refused':
        return (f'Telnyx did not let Faxbot change {number_text}, because the API key may not change numbers. '
                + GATEWAY_STEPS.format(number=number_text))
    if outcome == NOT_FOUND:
        return f'{number_text} is not a number on this Telnyx account, so Faxbot changed nothing.'
    return f'Faxbot could not reach Telnyx, so nothing changed for {number_text}; try again.'


def _iso(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'


def report(values, key=None):
    """What the trunk page, Diagnostics and the command line show; ``applies`` False when there is nothing to say."""
    if not applies(values):
        return {'applies': False, 'numbers': [], 'connection_texts': [], 'text': None}
    record = read(values, key)
    if record is None:
        return {'applies': True, 'checked_at': None, 'numbers': [], 'connection_texts': [],
                'text': 'Faxbot has not checked fax over IP (T.38) on your Telnyx numbers yet.'}
    trunk = set(values.sip_trunk_did_list)
    numbers = [{'number': entry['number'], 'display': shown(entry['number']), 'state': entry.get('state'),
                'text': number_sentence(entry), 'fixable': entry.get('state') == OFF}
               for entry in record['numbers'] if entry.get('number') in trunk]
    connections = [text for text in map(connection_sentence, (record.get('connections') or {}).values()) if text]
    off = [entry for entry in numbers if entry['state'] == OFF]
    problems = [entry for entry in numbers if entry['state'] != ON]
    text = (off[0]['text'] if len(off) == 1 else
            f'Telnyx has fax over IP (T.38) turned off for {len(off)} of your numbers, so received faxes there arrive '
            'as audio.' if off else problems[0]['text'] if problems else connections[0] if connections else
            'Telnyx has fax over IP (T.38) turned on for all your trunk numbers.')
    return {'applies': True, 'checked_at': _iso(record.get('checked_at')), 'numbers': numbers,
            'connection_texts': connections, 'text': text, 'ready': not problems and not connections}
