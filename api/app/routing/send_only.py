"""Send-only numbers (B9): a number you show on faxes you send but never receive on here.

An installation that only sends faxes does not need a fax number of its own:
the caller ID and station ID can be a number the organization already has,
such as its main office number or the fax number of its existing fax machine,
when the carrier lets you show it (each carrier's published rule is in
``reply_number.CALLER_ID_RULES``; Telnyx, for one, shows a number only when it
is on your account or verified in its portal). Such a number is a *send-only
number* (``fax_send_only_numbers``):

- it never counts as one of your own numbers: a fax to it places a real call
  (it is not delivered inside Faxbot), it is never a reply number Faxbot picks,
  and a test fax to it is not a test between your own numbers;
- it cannot also be a number one of your accounts receives on: saving refuses
  that, because Faxbot could not tell whether a fax to it should stay inside;
- each trunk shows it as caller ID when it is that trunk's caller ID setting,
  and faxes show it as the station ID when it is the station ID setting.

Advice (``advice``) names a number you rent but use only to send from: one of a
trunk's numbers with no fax received in ``QUIET_DAYS`` days and no rule under
Numbers sending its faxes to a mailbox, while it is the trunk's caller ID or
the station ID. Showing a send-only number instead and releasing the rented one
saves its monthly rental. The reply number (``reply_number``) is never named:
it must receive. Advice only: Faxbot never releases a number.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re

import sqlalchemy as sa

from .numbers import InvalidNumber, normalize_number


SETTING = 'fax_send_only_numbers'
QUIET_DAYS = 90
LIMIT = 50


class SendOnlyRefused(ValueError):
    """A number that cannot be a send-only number; the message is one plain sentence."""


def numbers(values) -> tuple:
    """The send-only numbers, in E.164, in the order you entered them."""
    text = getattr(values, SETTING, '') or ''
    return tuple(dict.fromkeys(part.strip() for part in str(text).split(',') if part.strip()))


def without(values, found) -> set:
    """``found`` without the send-only numbers: what own-number rules use."""
    excluded = set(numbers(values))
    return {number for number in found if number not in excluded} if excluded else set(found)


def encode(found) -> str:
    return ','.join(found)


def _receiving_elsewhere(values, number):
    """The account name that receives on ``number`` (send-only numbers set aside), or None."""
    try:
        plain = values.with_patch({SETTING: ''})
    except Exception:
        plain = values
    from .own_numbers import receiving_numbers
    try:
        from ..accounts import all_accounts
        for account in all_accounts(plain):
            if number in account.numbers and account.receives:
                return account.label
    except Exception:
        pass
    if number in set(getattr(plain, 'sip_trunk_did_list', ())) or number in receiving_numbers(plain):
        from ..provider_labels import trunk_name
        return trunk_name(getattr(plain, 'sip_trunk_preset', None) or None)
    return None


def checked(values, texts) -> list:
    """The numbers as they will be saved (E.164, no repeats). Raises SendOnlyRefused with one sentence."""
    if not isinstance(texts, (list, tuple)) or len(texts) > LIMIT:
        raise SendOnlyRefused(f'List at most {LIMIT} send-only numbers.')
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    found = []
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            number = normalize_number(text.strip(), country=country)
        except (InvalidNumber, ValueError):
            raise SendOnlyRefused(f'{str(text)[:40]} is not a fax number Faxbot can read. Write it with its country '
                                  'code, such as +13035550100.') from None
        owner = _receiving_elsewhere(values, number)
        if owner:
            raise SendOnlyRefused(f'{number} receives faxes on {owner}, so it cannot be send-only. Take it off that '
                                  'account first, or keep it as a number that receives.')
        if number not in found:
            found.append(number)
    return found


def _trunks_showing(values, number):
    from .. import sip_trunk
    return [trunk for trunk in sip_trunk.trunk_accounts(values)
            if (getattr(trunk.values, 'sip_trunk_caller_id', '') or '') == number]


def view(values) -> list:
    """Each send-only number: where it shows, and what its carriers say about showing it."""
    from .reply_number import CALLER_ID_RULES
    found = []
    for number in numbers(values):
        trunks = _trunks_showing(values, number)
        station = (getattr(values, 'fax_station_id', '') or '') == number
        parts = []
        if trunks:
            parts.append('Shown as caller ID on ' + ', '.join(trunk.label for trunk in trunks))
        if station:
            parts.append('sent as the station ID')
        sentence = ('; '.join(parts) + '. ' if parts else
                    'Not shown yet: set it as a trunk\'s caller ID or as the station ID. ')
        sentence += 'Faxbot never receives on it and never treats it as one of your fax numbers.'
        rules = []
        for trunk in trunks:
            preset = getattr(trunk.values, 'sip_trunk_preset', '') or ''
            rule = CALLER_ID_RULES.get(preset)
            dids = set(getattr(trunk.values, 'sip_trunk_did_list', ()))
            if rule and number not in dids:
                rules.append({'trunk': trunk.label, 'sentence': rule['other'][0].upper() + rule['other'][1:] + '.',
                              'source_url': rule['source_url'], 'read_on': rule['read_on']})
        found.append({'number': number, 'caller_id_on': [trunk.key for trunk in trunks], 'station_id': station,
                      'sentence': sentence, 'carrier_rules': rules})
    return found


# -- advice: a number you rent only to send from -------------------------------------------------------------------

def _rental(values, preset):
    """(micros, currency, source) of one number's monthly rental at this trunk's carrier, or None."""
    from .costs import InvalidRateCard, parse_amount
    from .predict_facts import _document
    document = _document()
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    for entry in document.get('receiving_prices') or ():
        if (isinstance(entry, dict) and entry.get('kind') == 'number_rental' and entry.get('carrier') == preset
                and entry.get('number_type', 'local') == 'local' and entry.get('country', country) == country):
            try:
                return parse_amount(str(entry['monthly_fee']), whole_digits=4), entry.get('currency'), \
                    entry.get('source_url')
            except (InvalidRateCard, KeyError):
                return None
    for entry in document.get('cards') or ():
        if (isinstance(entry, dict) and entry.get('provider_id') == f'sip-{preset}'
                and entry.get('direction') == 'outbound' and entry.get('number_rental_monthly')):
            try:
                return parse_amount(str(entry['number_rental_monthly']), whole_digits=4), entry.get('currency'), \
                    entry.get('source_url')
            except InvalidRateCard:
                return None
    return None


def _received(engine, number, since):
    if engine is None:
        return 0
    try:
        table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            return int(connection.scalar(sa.select(sa.func.count()).select_from(table).where(
                table.c.direction == 'inbound', sa.or_(table.c.did == number, table.c.called == number),
                table.c.started_at >= since)) or 0)
    except sa.exc.SQLAlchemyError:
        return 0


def advice(values, engine=None, *, now=None) -> list:
    """Numbers a trunk rents that serve only to send from: they could be replaced by a send-only number."""
    from .. import sip_trunk
    from .costs import money_text
    from .reply_number import mailbox_routes
    now = now or datetime.utcnow()
    since = now - timedelta(days=QUIET_DAYS)
    try:
        routed = {route.number for route in mailbox_routes(engine, values)} if engine is not None else set()
    except Exception:
        routed = set()
    reply = getattr(values, 'fax_reply_number', '') or ''
    station = getattr(values, 'fax_station_id', '') or ''
    found = []
    for trunk in sip_trunk.trunk_accounts(values):
        caller = getattr(trunk.values, 'sip_trunk_caller_id', '') or ''
        for number in getattr(trunk.values, 'sip_trunk_did_list', ()):
            if number not in (caller, station) or number == reply or number in routed:
                continue
            if _received(engine, number, since):
                continue
            preset = getattr(trunk.values, 'sip_trunk_preset', '') or ''
            rental = _rental(values, preset)
            cost = f' for {money_text(rental[0], rental[1] or "USD")} a month' if rental else ''
            sentence = (f'You rent {number} on {trunk.label}{cost} and use it only to show on faxes you send: no fax '
                        f'arrived on it in {QUIET_DAYS} days and no rule under Numbers sends its faxes to a mailbox. '
                        'You could show a number you already have instead (add it as a send-only number; your '
                        'carrier may need to verify it first) and release this one'
                        + (f' to save {money_text(rental[0], rental[1] or "USD")} a month.' if rental else '.'))
            found.append({'number': number, 'trunk': trunk.key, 'trunk_label': trunk.label,
                          'saving_per_month': ({'micros': rental[0], 'currency': rental[1],
                                                'text': money_text(rental[0], rental[1] or 'USD')} if rental else None),
                          'source_url': rental[2] if rental else None, 'sentence': sentence})
    return found


def shown(number):
    """+13035550100 as +1 303-555-0100."""
    import phonenumbers
    try:
        return phonenumbers.format_number(phonenumbers.parse(number), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return number if re.fullmatch(r'\+[0-9]{7,15}', str(number or '')) else ''
