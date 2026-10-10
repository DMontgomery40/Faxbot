"""Take the fax lines out of a POTS-replacement order (N23): a counter-quote from your line inventory.

When copper lines are retired, carriers and resellers sell boxes that keep an analog port alive over LTE or broadband
(Ooma AirDial, Granite EPIK, DataRemote and others), priced by the line or port each month. Alarm, elevator and
emergency lines need such a port: it has to keep working through a power or network outage. A fax line does not: its
number can move to one shared trunk that Faxbot sends and receives on.

From the line inventory (``inventory.py``: each line's use) and a quote you enter (the product, its price per line
a month, the term, the lines quoted, the ports per device and any price per device), this works out what taking the
fax lines out of the order removes from it, what one shared trunk costs for them instead at its carrier's published
prices (``receiving.carrier_prices``: the trunk fee, a number a month, and the price per received minute, with
sources and dates), and which lines must stay. Unknown stays unknown: a trunk fee a carrier does not publish is
said, never counted as zero, and calls are priced by the minute rather than guessed.

Ooma prices AirDial by quote; ``PUBLISHED`` keeps the one per-line price Faxbot could read from a published sheet,
with its source and date, for you to start from. Advice only: nothing here orders, ports or cancels a line.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
from uuid import uuid4

import sqlalchemy as sa


PUBLISHED = (
    {'id': 'ooma-airdial', 'name': 'Ooma AirDial', 'per_line': '39.95', 'currency': 'USD', 'ports_per_device': 4,
     'sentence': ("Ooma AirDial at $39.95 a line a month, including the device, its wireless data and phone service; "
                  'each device has four analog ports. Ooma itself prices AirDial by quote.'),
     'source': 'https://touchtone.net/services/ooma/Ooma-AirDial-is-Now-Availabe-Through-TouchTone.pdf',
     'label': "TouchTone's Ooma AirDial partner sheet, revision of 25 August 2023", 'source_date': '2023-08-25',
     'read_on': '2026-10-10'},
)
ADVICE_ONLY = 'Faxbot only advises: it never orders, ports or cancels a line.'
KEEP = ('alarm', 'elevator', 'emergency')


class QuoteError(ValueError):
    """A quote Faxbot cannot record; the message is one plain sentence."""


def _table(engine):
    from .database import reflect
    return reflect(engine, ('pots_quotes',))['pots_quotes']


def _cents(micros, currency):
    text = f'{micros / 1_000_000:,.2f}'
    return f'${text}' if currency == 'USD' else f'{text} {currency}'


def _name(value):
    text = ' '.join(str(value or '').split())[:100]
    if not text:
        raise QuoteError('Name the product quoted, such as Ooma AirDial.')
    return text


def _after_newest(connection, table, name, now):
    newest = connection.execute(sa.select(sa.func.max(table.c.created_at)).where(table.c.name == name)).scalar()
    if isinstance(newest, str):
        newest = datetime.fromisoformat(newest)
    return max(now, newest + timedelta(microseconds=1)) if newest is not None else now


def _whole(value, what, limit):
    if value in (None, ''):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise QuoteError(f'Enter the {what} as a whole number.') from None
    if not 1 <= number <= limit:
        raise QuoteError(f'Enter the {what} as a whole number from 1 to {limit:,}.')
    return number


def _amount(value, what):
    from .costs import InvalidRateCard, parse_amount
    text = str(value or '').strip().lstrip('$').replace(',', '')
    if not text:
        return None
    try:
        parse_amount(text, whole_digits=9)
    except InvalidRateCard:
        raise QuoteError(f'Enter the {what} as a number, such as 39.95.') from None
    return text


def record_quote(engine, name, *, per_line, currency='USD', term_months=None, lines_quoted=None,
                 ports_per_device=None, device_price=None, source_url=None, source_date=None, note=None, actor=None,
                 now=None):
    """Record a POTS-replacement quote; an earlier entry for the same product stays as history."""
    from .database import utcnow, write_transaction
    name = _name(name)
    per_line = _amount(per_line, 'price per line a month')
    if per_line is None:
        raise QuoteError('Enter the price per line a month, such as 39.95.')
    currency = str(currency or '').strip().upper()
    if re.fullmatch(r'[A-Z]{3}', currency) is None:
        raise QuoteError('Enter the currency as three letters, such as USD.')
    if source_url and re.fullmatch(r'https?://\S+', source_url) is None:
        raise QuoteError('The source must be a web address.')
    if source_date is not None and not isinstance(source_date, date):
        raise QuoteError("Enter the quote's date, such as 2026-10-01.")
    table = _table(engine)
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(table.insert().values(
            id=uuid4().hex, name=name, per_line=per_line, currency=currency,
            term_months=_whole(term_months, 'term in months', 120), lines_quoted=_whole(lines_quoted, 'lines quoted',
                                                                                         100_000),
            ports_per_device=_whole(ports_per_device, 'ports per device', 64),
            device_price=_amount(device_price, 'price per device'), source_url=source_url or None,
            source_date=datetime(source_date.year, source_date.month, source_date.day) if source_date else None,
            note=(note or '').strip() or None, state='active', recorded_by=actor.get('id'),
            recorded_by_name=actor.get('name'), created_at=_after_newest(connection, table, name, now or utcnow())))
    return quotes(engine).get(name)


def record_published(engine, published_id, *, lines_quoted=None, term_months=None, actor=None, now=None):
    """Record a quote from one of the published prices Faxbot ships, with its source and date."""
    found = next((item for item in PUBLISHED if item['id'] == published_id), None)
    if found is None:
        raise QuoteError('Faxbot has no published price by that name; enter the quote yourself.')
    return record_quote(engine, found['name'], per_line=found['per_line'], currency=found['currency'],
                        ports_per_device=found['ports_per_device'], source_url=found['source'],
                        source_date=date.fromisoformat(found['source_date']), note=found['label'],
                        lines_quoted=lines_quoted, term_months=term_months, actor=actor, now=now)


def remove_quote(engine, name, *, actor=None, now=None):
    from .database import utcnow, write_transaction
    name = _name(name)
    if name not in quotes(engine):
        raise QuoteError('There is no quote recorded for that product.')
    table = _table(engine)
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(table.insert().values(
            id=uuid4().hex, name=name, state='removed', recorded_by=actor.get('id'),
            recorded_by_name=actor.get('name'), created_at=_after_newest(connection, table, name, now or utcnow())))


def quotes(engine):
    """{product: the active quote}: the newest row per product, unless it withdrew the quote."""
    from .database import read_connection
    table = _table(engine)
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(table).order_by(table.c.created_at, table.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['name']] = dict(row)
    return {name: row for name, row in newest.items() if row['state'] == 'active'}


# -- the counter-quote ---------------------------------------------------------------------------------------------

def _trunk_carriers(values):
    """[(preset, trunk account key or None)] to price the shared trunk at, and whether they are your own trunks:
    your trunks' presets, else every carrier with a published number price."""
    from .. import sip_trunk
    from .receiving import load_receiving_prices
    trunks = [(getattr(trunk.values, 'sip_trunk_preset', ''), trunk.key)
              for trunk in sip_trunk.trunk_accounts(values)] if values is not None else []
    trunks = [(preset, key) for preset, key in trunks if preset]
    if trunks:
        return list(dict.fromkeys(trunks)), True
    entries, cards = load_receiving_prices()
    found = {entry.get('carrier') for entry in entries if entry.get('kind') == 'number_rental'}
    found |= {str(card.get('provider_id'))[4:] for card in cards
              if str(card.get('provider_id', '')).startswith('sip-') and card.get('number_rental_monthly')}
    return [(item, None) for item in sorted(item for item in found if item)], False


def _saved_cards(routes, carrier, account_key):
    """(outbound card with a monthly fee, inbound card) you saved for the trunk account or its carrier, or None."""
    if routes is None:
        return None, None
    identities = [item for item in dict.fromkeys([account_key, f'sip-{carrier}']) if item]
    cards = routes.current_cards()
    monthly = next((card for identity in identities for card in cards if card.provider_id == identity
                    and card.direction == 'outbound' and card.monthly_fee_micros is not None), None)
    inbound = next((card for identity in identities for card in cards if card.provider_id == identity
                    and card.direction == 'inbound'), None)
    return monthly, inbound


def _trunk_option(carrier, fax_lines, currency, *, routes=None, account_key=None):
    """What one shared trunk at ``carrier`` costs for the fax lines a month, or None when no number price is known in
    ``currency``. A rate card you saved for the trunk (Costs) gives its monthly fee and its price per received minute
    first, as trunk advice reads it; the carrier's published prices give the rest."""
    from .costs import money_text
    from .carriers import carrier_label
    from .receiving import carrier_prices
    prices = carrier_prices(carrier)
    rental = prices.rental.get('local')
    if rental is None or (prices.currency or 'USD') != currency:
        return None
    saved_monthly, saved_inbound = _saved_cards(routes, carrier, account_key)
    trunk_fee = prices.trunk_fee
    if saved_monthly is not None and saved_monthly.currency == currency:
        trunk_fee = saved_monthly.monthly_fee_micros
    per_minute = prices.per_minute.get('local')
    if saved_inbound is not None and saved_inbound.currency == currency:
        per_minute = saved_inbound
    numbers = rental * fax_lines
    fixed = numbers + (trunk_fee or 0)
    label = carrier_label(carrier)
    parts = [f"{fax_lines} {'number' if fax_lines == 1 else 'numbers'} at {money_text(rental, currency)} a month"]
    if trunk_fee is None:
        parts.append(f'its trunk fee, which {label} does not publish')
    elif trunk_fee == 0:
        parts.append('no trunk fee')
    else:
        parts.append(f'a trunk fee of {money_text(trunk_fee, currency)} a month')
    where = (f"at your rate card for {label} and its published number price" if saved_monthly or saved_inbound
             else f"at {label}'s published prices")
    sentence = (f"Faxbot can send and receive them on one shared trunk instead: {where}, {' and '.join(parts)}"
                + (f", so {_cents(fixed, currency)} a month" if trunk_fee is not None else '')
                + (f", plus {money_text(per_minute.per_minute_micros, currency)} a minute for each received call"
                   if per_minute is not None else ', plus the calls themselves') + '.')
    sources = list(prices.sources)
    for card in (saved_monthly, saved_inbound):
        if card is not None:
            sources.insert(0, {'label': f'Your rate card: {card.label}', 'source_url': card.source_url,
                               'read_on': card.captured_on.date().isoformat() if card.captured_on else None})
    return {'carrier': carrier, 'label': label, 'monthly_micros': fixed if trunk_fee is not None else None,
            'monthly': _cents(fixed, currency) if trunk_fee is not None else None,
            'trunk_fee_known': trunk_fee is not None, 'rate_card': bool(saved_monthly or saved_inbound),
            'sentence': sentence, 'sources': sources}


def counter_quote(engine, values, quote, *, routes=None):
    """The counter-quote for one recorded quote, as sentences in reading order with the figures behind them."""
    from .costs import parse_amount
    from .inventory import inventory_rows
    lines = inventory_rows(engine)
    fax = [line for line in lines if line['line_use'] == 'fax']
    keep = [line for line in lines if line['line_use'] in KEEP]
    unsure = [line for line in lines if line['line_use'] in ('other', 'unknown')]
    currency = quote['currency']
    per_line = parse_amount(quote['per_line'], whole_digits=9)
    quoted = quote['lines_quoted'] or len(lines)
    result = {'name': quote['name'], 'per_line': _cents(per_line, currency), 'currency': currency,
              'term_months': quote['term_months'], 'lines_quoted': quoted, 'source_url': quote['source_url'],
              'source_date': quote['source_date'].date().isoformat() if isinstance(quote['source_date'], datetime)
              else quote['source_date'], 'note': quote['note'],
              'counts': {'lines': len(lines), 'fax': len(fax), 'keep': len(keep), 'unsure': len(unsure)},
              'removed_monthly': None, 'removed_term': None, 'devices_saved': None, 'trunk': None, 'net_monthly': None}
    sentences = []
    if not lines:
        sentences.append('Import your line inventory first, with each line\'s use (fax, alarm, elevator, emergency or '
                         'other), so Faxbot knows which lines are fax.')
        result['sentences'] = sentences
        return result
    share = f' Fax lines are {len(fax) * 100 / quoted:.0f}% of the {quoted:,} lines quoted.' if quoted else ''
    sentences.append(f"Your inventory has {len(lines):,} {'line' if len(lines) == 1 else 'lines'}: {len(fax):,} fax, "
                     f"{len(keep):,} alarm, elevator or emergency, and {len(unsure):,} other or not known.{share}")
    if not fax:
        sentences.append('No line in your inventory is marked as fax, so there is nothing to take out of this order. '
                         'Mark the fax lines with the use fax in the inventory.')
    else:
        removed = per_line * len(fax)
        result['removed_monthly'] = _cents(removed, currency)
        sentence = (f"Take the {len(fax):,} fax {'line' if len(fax) == 1 else 'lines'} out of the {quote['name']} "
                    f"order: at {_cents(per_line, currency)} a line a month, that is {_cents(removed, currency)} a "
                    'month')
        if quote['term_months']:
            result['removed_term'] = _cents(removed * quote['term_months'], currency)
            sentence += f" ({result['removed_term']} over the {quote['term_months']}-month term)"
        sentence += '.'
        ports = quote['ports_per_device']
        if ports:
            saved = math.ceil(quoted / ports) - math.ceil(max(0, quoted - len(fax)) / ports)
            result['devices_saved'] = saved
            if saved:
                sentence += (f" With {ports} ports to a device, that is {saved} fewer "
                             f"{'device' if saved == 1 else 'devices'}")
                if quote['device_price']:
                    sentence += (f", and {_cents(parse_amount(quote['device_price'], whole_digits=9) * saved, currency)}"
                                 ' less for them')
                sentence += '.'
        sentences.append(sentence)
        carriers, own = _trunk_carriers(values)
        options = [option for option in (_trunk_option(carrier, len(fax), currency, routes=routes, account_key=key)
                                         for carrier, key in carriers) if option is not None]
        if options:
            option = options[0] if own else min(options, key=lambda item: (
                item['monthly_micros'] is None, item['monthly_micros'] or 0))
            result['trunk'] = option
            sentences.append(option['sentence'])
            if option['monthly_micros'] is not None:
                net = removed - option['monthly_micros']
                result['net_monthly'] = _cents(net, currency)
                sentences.append(f"That is {_cents(net, currency)} a month less than keeping them in the order, before "
                                 'the calls.' if net > 0 else
                                 f"At these prices the trunk costs {_cents(-net, currency)} a month more than keeping "
                                 'them in the order, before the calls.')
        else:
            sentences.append(f'No carrier Faxbot knows publishes a price for a number in {currency}, so the shared '
                             'trunk cannot be priced here.')
        sentences.append('Move each fax number to the trunk with its move plan (Delivery setup, Number moves) before the '
                         'copper line goes.')
    if keep:
        sentences.append(f"Keep the {len(keep):,} alarm, elevator and emergency "
                         f"{'line' if len(keep) == 1 else 'lines'} in the order: they need an analog port that keeps "
                         'working through a power or network outage.')
    if unsure:
        sentences.append(f"{len(unsure):,} {'line has' if len(unsure) == 1 else 'lines have'} no use recorded; mark "
                         'them in the inventory to know whether they can come out too.')
    result['sentences'] = sentences
    return result


def view(engine, values, *, routes=None):
    """Every recorded quote with its counter-quote, and the published prices to start from."""
    found = [counter_quote(engine, values, quote, routes=routes) for quote in quotes(engine).values()]
    return {'quotes': found, 'published': list(PUBLISHED), 'note': ADVICE_ONLY,
            'sentence': None if found else ('No POTS-replacement quote yet. Enter the price per line from the quote, '
                                            'or start from a published price.')}
