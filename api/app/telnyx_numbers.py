"""Caller-name lookup on the trunk's Telnyx numbers: whether it is on, what it costs, and one audited switch.

Telnyx can look up the caller's name for each call received on a number (its
portal calls this "CNAM Caller ID Lookup"; the API field is the number's voice
setting ``caller_id_name_enabled``, "Controls whether the caller ID name is
enabled for this phone number", Telnyx API reference "Update a phone number
with voice settings", read 2026-10-07). Telnyx bills it monthly for each number
where it is on: "Inbound CNAM: $0.40 per number/month" in Telnyx's "Your number
lookup guide" (``PRICE_SOURCE``, read 2026-10-07). Listing your own name for
the people you call (outbound CNAM listing) is a separate, free setting that
Faxbot never reads or changes.

Faxbot never shows a caller's name: its fax engine replaces the name on every
received call with its own routing token (``extensions.conf``,
``faxbot-engine-in``). So the lookup buys nothing in Faxbot.

The setting is read by the T.38 check's voice read (``telnyx_t38.check``) and
kept in the same record, so checking costs no extra Telnyx call. The one
change, made only when a person selects "Turn off name lookup", is
``PATCH /v2/phone_numbers/{id}/voice`` with ``{"caller_id_name_enabled": false}``
and nothing else, followed by a fresh read. Portal steps, from Telnyx's "Set up
Inbound Caller ID Name (incoming)" article (``PORTAL_SOURCE``, read
2026-10-07), are given when the key cannot change the number. This has not been
run against the live account; tests use a fake Telnyx.
"""
from __future__ import annotations

import time

from . import telnyx_t38
from .routing.costs import format_amount, money_text
from .telnyx_t38 import Missing, Refused, Unavailable, shown

PRICE_MICROS = 400_000
PRICE_CURRENCY = 'USD'
PRICE_TEXT = '$0.40 a month for each number'
PRICE_SOURCE = 'https://support.telnyx.com/en/articles/4366901-your-number-lookup-guide'
PORTAL_SOURCE = 'https://support.telnyx.com/en/articles/1130656-set-up-inbound-caller-id-name-incoming'
API_SOURCE = 'https://developers.telnyx.com/api-reference/phone-number-configurations/update-a-phone-number-with-voice-settings'
READ_ON = '2026-10-07'
PORTAL_STEPS = ('In the Telnyx portal, open Numbers → My Numbers, select the business card icon next to {number}, '
                'turn off CNAM Caller ID Lookup and save.')
PORTAL_CHECK = ('In the Telnyx portal, open Numbers → My Numbers and select the business card icon next to {number} '
                'to see whether CNAM Caller ID Lookup is on.')
NOT_USED ='Faxbot never shows callers\' names, so turning it off changes nothing in Faxbot.'
OUTCOMES = {'off': 'turned_off', 'still_on': 'still_on', 'refused': 'refused', 'not_found': 'not_found',
            'unavailable': 'unreachable'}


def _turn_off(telnyx, number_id):
    telnyx._call('PATCH', f'/phone_numbers/{number_id}/voice', json={'caller_id_name_enabled': False})
    return telnyx.voice(number_id).get('caller_id_name_enabled')


def turn_off(values, number, *, now=time.time):
    """Turn off caller-name lookup for one trunk number and read it back; returns (outcome, record).

    outcome is 'off' (read back as off), 'still_on' (Telnyx accepted the change but reads back on),
    'refused' (the key may not change it), 'not_found' or 'unavailable'.
    """
    if telnyx_t38.TRANSPORT is None and telnyx_t38._test_mode():
        return 'unavailable', telnyx_t38.read(values)
    outcome = 'unavailable'
    with telnyx_t38._client(values) as telnyx:
        try:
            found = telnyx.find_number(number)
            if found is None:
                outcome = 'not_found'
            else:
                outcome = 'still_on' if _turn_off(telnyx, found[0]) is True else 'off'
        except Refused:
            outcome = 'refused'
        except (Missing, Unavailable):
            outcome = 'unavailable'
    record = telnyx_t38.read(values) or {'checked_at': None, 'numbers': [], 'connections': {}}
    if outcome in ('off', 'still_on'):
        for entry in record['numbers']:
            if entry.get('number') == number:
                entry['name_lookup'] = outcome == 'still_on'
        record['checked_at'] = round(now(), 3)
    return outcome, telnyx_t38._write(values, record)


def number_sentence(entry):
    number, lookup, state = shown(entry['number']), entry.get('name_lookup'), entry.get('state')
    if lookup is True:
        return (f'Telnyx looks up callers\' names on {number}, at {PRICE_TEXT}. {NOT_USED}')
    if lookup is False:
        return f'Caller-name lookup is off for {number}.'
    if state == telnyx_t38.UNREADABLE:
        return (f'Faxbot\'s Telnyx API key cannot read {number}, so Faxbot cannot check its caller-name lookup. '
                + PORTAL_CHECK.format(number=number))
    if state == telnyx_t38.NOT_FOUND:
        return f'{number} is not a number on this Telnyx account, so Faxbot cannot check its caller-name lookup.'
    if state in (telnyx_t38.ON, telnyx_t38.OFF):
        return f'Telnyx did not say whether caller-name lookup is on for {number}.'
    return f'Faxbot could not reach Telnyx to check caller-name lookup for {number}.'


def outcome_sentence(outcome, number):
    number_text = shown(number)
    if outcome == 'off':
        return f'Telnyx now has caller-name lookup off for {number_text}.'
    if outcome == 'still_on':
        return (f'Telnyx accepted the change but still shows caller-name lookup on for {number_text}. '
                + PORTAL_STEPS.format(number=number_text))
    if outcome == 'refused':
        return (f'Telnyx did not let Faxbot change {number_text}, because the API key may not change numbers. '
                + PORTAL_STEPS.format(number=number_text))
    if outcome == 'not_found':
        return f'{number_text} is not a number on this Telnyx account, so Faxbot changed nothing.'
    return f'Faxbot could not reach Telnyx, so nothing changed for {number_text}; try again.'


def report(values):
    """What the trunk page and the command line show about caller-name lookup on each trunk number."""
    price = {'text': PRICE_TEXT, 'monthly': {'currency': PRICE_CURRENCY, 'amount': format_amount(PRICE_MICROS)},
             'source_url': PRICE_SOURCE, 'read_on': READ_ON}
    if not telnyx_t38.applies(values):
        return {'applies': False, 'numbers': [], 'text': None, 'price': price}
    record = telnyx_t38.read(values)
    if record is None:
        return {'applies': True, 'checked_at': None, 'numbers': [], 'price': price, 'monthly_total': None,
                'text': 'Faxbot has not checked caller-name lookup on your Telnyx numbers yet.'}
    trunk = set(values.sip_trunk_did_list)
    numbers = [{'number': entry['number'], 'display': shown(entry['number']), 'lookup': entry.get('name_lookup'),
                'text': number_sentence(entry), 'can_turn_off': entry.get('name_lookup') is True}
               for entry in record['numbers'] if entry.get('number') in trunk]
    on = [entry for entry in numbers if entry['lookup'] is True]
    total = PRICE_MICROS * len(on)
    if len(on) == 1:
        text = on[0]['text']
    elif on:
        text = (f'Telnyx looks up callers\' names on {len(on)} of your numbers, about {money_text(total, PRICE_CURRENCY)} '
                f'a month together ({PRICE_TEXT}). {NOT_USED}')
    elif numbers and all(entry['lookup'] is False for entry in numbers):
        text = 'Caller-name lookup is off for all your trunk numbers, so Telnyx charges nothing for it.'
    else:
        text = next((entry['text'] for entry in numbers if entry['lookup'] is None),
                    'Faxbot has not checked caller-name lookup on your Telnyx numbers yet.')
    return {'applies': True, 'checked_at': telnyx_t38._iso(record.get('checked_at')), 'numbers': numbers,
            'price': price, 'text': text,
            'monthly_total': {'currency': PRICE_CURRENCY, 'amount': format_amount(total)} if on else None}
