"""One page for a fax server's renewal (N20): what it costs, the channels it really needed, what Faxbot handled beside
it, and what is left to move.

The decisive date for an enterprise whose fax server "just works" is its maintenance renewal. You enter the renewal
(system, product, date, amount, licensed channels, where the figure came from) and the numbers Faxbot runs in
parallel with it, and import the server's call records (``channel_peak``) and its number-to-user routing. Ninety days
before the date the page says, in order: the renewal amount and date; the channels used at peak against the licence,
and what the unneeded channels are worth at the renewal's average price per channel; what Faxbot received on the
parallel numbers and sent since the run began; and the numbers, with their users, still routed by the old server.

**Number routing** is imported as a CSV with the columns ``number`` (the fax number or DID), ``user``, ``email`` and
``cover sheet``. OpenText documents a user import for RightFax (ImpUser) in its Administrative Utilities Guide, which
is available only to verified customers, and no routing export for XM Fax; export your routing to these columns.

Published figures for comparison (``REFERENCE``) carry their sources and read dates; none is used as your price.
Advice only: nothing here cancels a renewal, changes a licence or contacts a vendor.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from uuid import uuid4

import sqlalchemy as sa

from .workbook import WorkbookError, normal, table


REVIEW_DAYS = 90
ROUTE_COLUMNS = {
    'number': ('number', 'fax number', 'did', 'routing code', 'phone number'),
    'user': ('user', 'user name', 'name', 'user id'),
    'email': ('email', 'e-mail', 'email address'),
    'cover_sheet': ('cover sheet', 'cover', 'cover page'),
}
ROUTES_HELP = ('A CSV with the columns number (the fax number or DID), user, email and cover sheet; only the number '
               'is required.')
ADVICE_ONLY = 'Faxbot only advises: it never cancels a renewal, changes a licence or contacts a vendor.'
# Published figures, for comparison only. Each says where it was read and when; none is your price.
REFERENCE = (
    {'id': 'cuyahoga-rightfax', 'sentence': ('Cuyahoga County, Ohio approved up to $26,756.71 for one more year of '
                                             'RightFax Enterprise Fax Manager maintenance (to 31 May 2026).'),
     'source': ('https://cuyahogacms.blob.core.windows.net/home/docs/default-source/agendas/04282025-bocagenda.pdf'
                '?sfvrsn=d0c686f7_1'), 'label': 'Cuyahoga County Board of Control agenda, 28 April 2025',
     'read_on': '2026-10-08', 'products': ('rightfax',)},
    {'id': 'rightfax-support', 'sentence': ('RightFax 21.2 left sustaining maintenance in August 2026, 22.2 leaves it '
                                            'in June 2027 and 23.4 in December 2028; after that a version gets no '
                                            'fixes, and upgrading needs an active support contract.'),
     'source': 'https://mypaperlessfax.com/rightfax-sustaining-maintenance/',
     'label': "A RightFax reseller's table of OpenText's support dates", 'read_on': '2026-10-08',
     'products': ('rightfax',)},
    {'id': 'sr140-cdw', 'sentence': ('Brooktrout SR140 (R3) channel licences listed at CDW: 4 channels $2,173.99, '
                                     '8 channels $4,286.25, 30 channels $16,266.22.'),
     'source': 'https://www.cdw.com/product/brooktrout-sr140-v.-r3-license-8-channels/2109558',
     'label': 'CDW listings, seen in search results (pages not opened)', 'read_on': '2026-10-10',
     'products': ('rightfax', 'sr140', 'brooktrout', 'xm fax', 'xmedius')},
)


class RenewalError(ValueError):
    """A renewal or routing entry Faxbot cannot record; the message is one plain sentence."""


def _tables(engine):
    from .database import reflect
    return reflect(engine, ('fax_server_renewals', 'incumbent_routes'))


def _system(name):
    text = ' '.join(str(name or '').split())[:100]
    if not text:
        raise RenewalError('Name the fax server, such as RightFax at HQ.')
    return text


def _day(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.date() if isinstance(value, datetime) else value


def _when(day):
    return f'{day.day} {day:%B %Y}'


def _cents(micros, currency):
    """Money for a page a person reads: "$26,756.71", "1,250.00 EUR"."""
    text = f'{micros / 1_000_000:,.2f}'
    return f'${text}' if currency == 'USD' else f'{text} {currency}'


def _money(amount, currency):
    from .costs import InvalidRateCard, parse_amount
    try:
        return _cents(parse_amount(amount, whole_digits=9), currency)
    except InvalidRateCard:
        return None


def _numbers(values, default_country):
    from .numbers import AmbiguousNumber, InvalidNumber, normalize_number
    found = []
    for item in values or ():
        text = str(item or '').strip()
        if not text:
            continue
        try:
            found.append(normalize_number(text, country=default_country or 'US'))
        except (AmbiguousNumber, InvalidNumber, ValueError):
            raise RenewalError(f'{text[:40]} is not a fax number Faxbot can read.') from None
    return list(dict.fromkeys(found))


# -- the renewal ---------------------------------------------------------------------------------------------------

def record_renewal(engine, system, *, renews_on, amount, currency='USD', product=None, licensed_channels=None,
                   source_url=None, note=None, parallel_numbers=None, parallel_since=None, default_country='US',
                   actor=None, now=None):
    """Record what you know about a system's renewal; an earlier entry stays as history."""
    from .costs import InvalidRateCard, parse_amount
    from .database import utcnow, write_transaction
    system = _system(system)
    if not isinstance(renews_on, date):
        raise RenewalError('Enter the renewal date, such as 2027-05-31.')
    amount = str(amount or '').strip().lstrip('$').replace(',', '')
    try:
        parse_amount(amount, whole_digits=9)
    except InvalidRateCard:
        raise RenewalError('Enter the renewal amount as a number, such as 26756.71.') from None
    currency = str(currency or '').strip().upper()
    if re.fullmatch(r'[A-Z]{3}', currency) is None:
        raise RenewalError('Enter the currency as three letters, such as USD.')
    if licensed_channels is not None and not 0 < int(licensed_channels) <= 10_000:
        raise RenewalError('Enter the licensed channels as a whole number from 1 to 10,000.')
    if source_url and re.fullmatch(r'https?://\S+', source_url) is None:
        raise RenewalError('The source must be a web address.')
    numbers = _numbers(parallel_numbers, default_country)
    actor = actor or {}
    renewals = _tables(engine)['fax_server_renewals']
    with write_transaction(engine) as connection:
        connection.execute(renewals.insert().values(
            id=uuid4().hex, system=system, product=(product or '').strip()[:100] or None,
            renews_on=datetime(renews_on.year, renews_on.month, renews_on.day), amount=amount, currency=currency,
            licensed_channels=licensed_channels, source_url=source_url or None, note=(note or '').strip() or None,
            parallel_numbers=json.dumps(numbers) if numbers else None,
            parallel_since=datetime(parallel_since.year, parallel_since.month, parallel_since.day)
            if parallel_since else None, state='active', recorded_by=actor.get('id'),
            recorded_by_name=actor.get('name'), created_at=_after_newest(connection, renewals, system,
                                                                         now or utcnow())))
    return renewals_by_system(engine).get(system)


def _after_newest(connection, renewals, system, now):
    newest = connection.execute(sa.select(sa.func.max(renewals.c.created_at)).where(renewals.c.system == system)) \
        .scalar()
    if isinstance(newest, str):
        newest = datetime.fromisoformat(newest)
    return max(now, newest + timedelta(microseconds=1)) if newest is not None else now


def remove_renewal(engine, system, *, actor=None, now=None):
    from .database import utcnow, write_transaction
    system = _system(system)
    if system not in renewals_by_system(engine):
        raise RenewalError('There is no renewal recorded for that system.')
    actor = actor or {}
    renewals = _tables(engine)['fax_server_renewals']
    with write_transaction(engine) as connection:
        connection.execute(renewals.insert().values(
            id=uuid4().hex, system=system, state='removed', recorded_by=actor.get('id'),
            recorded_by_name=actor.get('name'), created_at=_after_newest(connection, renewals, system,
                                                                         now or utcnow())))


def renewals_by_system(engine):
    """{system: the active renewal}: the newest row per system, unless it withdrew the renewal."""
    from .database import read_connection
    renewals = _tables(engine)['fax_server_renewals']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(renewals).order_by(renewals.c.created_at, renewals.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['system']] = dict(row)
    return {system: row for system, row in newest.items() if row['state'] == 'active'}


# -- the old server's routing -------------------------------------------------------------------------------------

def parse_routes(data, *, default_country='US'):
    """([route dicts], skipped sentences) from the documented routing CSV."""
    from .numbers import AmbiguousNumber, InvalidNumber, normalize_number
    try:
        header, rows, _, first = table(data, [ROUTE_COLUMNS['number']])
    except WorkbookError as error:
        raise RenewalError(f'{error} The routing file needs a column named number.') from None
    at = {key: next((header.index(name) for name in names if name in header), None)
          for key, names in ROUTE_COLUMNS.items()}

    def cell(row, key, limit):
        index = at[key]
        value = row[index] if index is not None and index < len(row) else None
        return ' '.join(str(value).split())[:limit] or None if value is not None else None
    routes, skipped, seen = [], [], set()
    for position, row in enumerate(rows, start=first):
        raw = cell(row, 'number', 40)
        if raw is None:
            continue
        try:
            number = normalize_number(raw, country=default_country or 'US')
        except (AmbiguousNumber, InvalidNumber, ValueError):
            skipped.append(f'Row {position}: {raw} is not a fax number Faxbot can read.')
            continue
        if number in seen:
            skipped.append(f'Row {position}: {number} is listed twice; the first row counts.')
            continue
        seen.add(number)
        routes.append({'number': number, 'user_name': cell(row, 'user', 200), 'user_email': cell(row, 'email', 320),
                       'cover_sheet': cell(row, 'cover_sheet', 200)})
    if not routes:
        raise RenewalError('The routing file has no number Faxbot can read.')
    return routes, skipped


def import_routes(engine, system, routes, *, file_name=None, actor=None, now=None):
    """Make ``routes`` the system's current routing; its earlier routing is superseded, never changed."""
    from .database import utcnow, write_transaction
    system = _system(system)
    now = (now or utcnow()).replace(microsecond=0)
    actor = actor or {}
    table_ = _tables(engine)['incumbent_routes']
    import_id = uuid4().hex
    with write_transaction(engine) as connection:
        connection.execute(table_.update().where(table_.c.system == system, table_.c.superseded_at.is_(None))
                           .values(superseded_at=now))
        if routes:
            connection.execute(table_.insert(), [
                {'id': uuid4().hex, 'import_id': import_id, 'system': system, **route,
                 'file_name': (file_name or '')[:200] or None, 'superseded_at': None, 'imported_by': actor.get('id'),
                 'imported_by_name': actor.get('name'), 'created_at': now} for route in routes])
    return len(routes)


def routes_for(engine, system):
    from .database import read_connection
    table_ = _tables(engine)['incumbent_routes']
    with read_connection(engine) as connection:
        return [dict(row) for row in connection.execute(sa.select(table_).where(
            table_.c.system == system, table_.c.superseded_at.is_(None)).order_by(table_.c.number)).mappings()]


# -- the page ------------------------------------------------------------------------------------------------------

def _parallel(engine, numbers, since, now):
    """What Faxbot received on the parallel numbers, and sent, since the run began."""
    from .database import read_connection, reflect
    tables = reflect(engine, ('inbound_faxes', 'fax_jobs'))
    faxes, jobs = tables['inbound_faxes'], tables['fax_jobs']
    start = datetime(since.year, since.month, since.day) if since else None
    with read_connection(engine) as connection:
        received = connection.execute(
            sa.select(faxes.c.status, sa.func.count()).where(
                faxes.c.to_number.in_(numbers),
                sa.func.coalesce(faxes.c.received_at, faxes.c.created_at) >= start if start else sa.true())
            .group_by(faxes.c.status)).all() if numbers else []
        sent = connection.execute(
            sa.select(jobs.c.status, sa.func.count()).where(jobs.c.created_at >= start if start else sa.true(),
                                                          jobs.c.created_at <= now)
            .group_by(jobs.c.status)).all()
    by_status = {status: count for status, count in received}
    sent_by = {status: count for status, count in sent}
    return {'received': sum(by_status.values()), 'received_ok': by_status.get('received', 0),
            'sent': sum(sent_by.values()), 'sent_delivered': sent_by.get('SUCCESS', 0)}


def page(engine, values, system, *, today=None, now=None, loaded=None):
    """The renewal page for one system, in the order the administrator reads it."""
    from .channel_peak import report, system_calls
    from .database import utcnow
    from .inventory import inventory_rows
    from .number_placement import placed_numbers
    from .receiving import shown_number
    now = now or utcnow()
    today = today or now.date()
    renewal = renewals_by_system(engine).get(system)
    zone = getattr(values, 'time_zone', '') if values is not None else ''
    calls, licensed_imported, _, found_imports = loaded or system_calls(engine, system)
    licensed = (renewal or {}).get('licensed_channels') or licensed_imported
    sentences, result = [], {'system': system, 'renewal': None, 'channels': None, 'parallel': None, 'left': None}
    if renewal:
        renews = _day(renewal['renews_on'])
        review = renews - timedelta(days=REVIEW_DAYS)
        amount = _money(renewal['amount'], renewal['currency'])
        left = (renews - today).days
        state = 'passed' if left < 0 else 'review' if left <= REVIEW_DAYS else 'early'
        product = renewal['product'] or system
        headline = {
            'passed': f'{product} renewed on {_when(renews)} for {amount}.',
            'review': f'{product} renews on {_when(renews)} for {amount}: {left:,} days left to decide.',
            'early': (f'{product} renews on {_when(renews)} for {amount}. This page is for {_when(review)}, ninety '
                      'days before.'),
        }[state]
        sentences.append(headline)
        result['renewal'] = {'product': renewal['product'], 'renews_on': renews.isoformat(),
                             'review_on': review.isoformat(), 'days_left': left, 'state': state, 'amount': amount,
                             'source_url': renewal['source_url'], 'note': renewal['note'],
                             'licensed_channels': renewal['licensed_channels'],
                             'recorded_by': renewal['recorded_by_name']}
    if calls:
        found = report(calls, licensed=licensed, time_zone=zone)
        channels = {**found, 'per_channel': None, 'unneeded_value': None}
        sentence = found['sentence']
        if renewal and licensed and found.get('never_used'):
            from .costs import parse_amount
            total = parse_amount(renewal['amount'], whole_digits=9)
            channels['per_channel'] = _cents(total / licensed, renewal['currency'])
            channels['unneeded_value'] = _cents(total * found['never_used'] / licensed, renewal['currency'])
            sentence += (f" At this renewal's average of {channels['per_channel']} a channel, those "
                         f"{found['never_used']} channels are {channels['unneeded_value']} of it, if it is priced by "
                         'channel.')
        sentences.append(sentence)
        result['channels'] = channels
    else:
        sentences.append('No call records from this system yet: import them to see the channels it really needed.')
    parallel_numbers = json.loads(renewal['parallel_numbers']) if renewal and renewal['parallel_numbers'] else []
    since = _day(renewal['parallel_since']) if renewal else None
    on_faxbot = {number for number, _ in placed_numbers(values)} if values is not None else set()
    if parallel_numbers:
        handled = _parallel(engine, parallel_numbers, since, now)
        start = f' since {_when(since)}' if since else ''
        # Sent faxes are counted for the whole installation: a sent fax does not record which number it went from.
        sentence = (f"Faxbot runs beside it on {len(parallel_numbers)} "
                    f"{'number' if len(parallel_numbers) == 1 else 'numbers'}: it received {handled['received']:,} "
                    f"{'fax' if handled['received'] == 1 else 'faxes'} on them{start} "
                    f"({handled['received_ok']:,} complete). Faxbot sent {handled['sent']:,} "
                    f"{'fax' if handled['sent'] == 1 else 'faxes'} in all{start} ({handled['sent_delivered']:,} "
                    'delivered).')
        missing = [number for number in parallel_numbers if number not in on_faxbot]
        if missing:
            sentence += (f" {len(missing)} of these numbers {'is' if len(missing) == 1 else 'are'} on no Faxbot "
                         f"account yet, so faxes to {'it' if len(missing) == 1 else 'them'} still reach only the old "
                         'server.')
        sentences.append(sentence)
        result['parallel'] = {'numbers': [{'number': number, 'display': shown_number(number),
                                           'on_faxbot': number in on_faxbot} for number in parallel_numbers],
                              'since': since.isoformat() if since else None, **handled, 'sentence': sentence}
    else:
        sentences.append('No numbers run in parallel yet: choose a few of its numbers for Faxbot to receive on '
                         'first.')
    routes = routes_for(engine, system)
    basis = 'routing'
    if not routes:
        routes = [{'number': row['number'], 'user_name': None, 'user_email': None, 'cover_sheet': None}
                  for row in inventory_rows(engine) if row['line_use'] == 'fax']
        basis = 'inventory' if routes else None
    if basis:
        left = [route for route in routes if route['number'] not in on_faxbot]
        users = {route['user_email'] or route['user_name'] for route in left if route['user_email']
                 or route['user_name']}
        if left:
            sentence = (f"{len(left):,} of its {len(routes):,} {'number is' if len(routes) == 1 else 'numbers are'} "
                        'not on Faxbot yet' + (f", for {len(users):,} {'user' if len(users) == 1 else 'users'}"
                                               if users else '') + '.')
        else:
            sentence = f"All {len(routes):,} of its numbers are on Faxbot."
        if basis == 'inventory':
            sentence += ' (From your line inventory\'s fax lines; import its number routing for users.)'
        sentences.append(sentence)
        result['left'] = {'basis': basis, 'count': len(left), 'total': len(routes), 'users': len(users),
                          'numbers': [{**{key: route[key] for key in ('number', 'user_name', 'user_email',
                                                                       'cover_sheet')},
                                       'display': shown_number(route['number'])} for route in left[:500]],
                          'sentence': sentence}
    else:
        sentences.append('Faxbot does not know its numbers yet: import its number routing or your line inventory.')
    product = normal((renewal or {}).get('product') or system)
    result['reference'] = [item for item in REFERENCE if any(word in product for word in item['products'])]
    result['imports'] = [{'file_name': row['file_name'], 'calls': row['calls'], 'format': row['source_format']}
                         for row in found_imports]
    result['sentences'] = sentences
    return result


def view(engine, values, *, today=None, now=None):
    """A page for every system with a renewal or imported call records, and the channel report beside them; each
    system's calls are read once."""
    from .channel_peak import imports, system_calls, view as channel_view
    renewals = renewals_by_system(engine)
    names = list(dict.fromkeys([*renewals, *(row['system'] for row in imports(engine))]))
    loaded = {name: system_calls(engine, name) for name in names}
    licensed_for = {name: row['licensed_channels'] for name, row in renewals.items() if row['licensed_channels']}
    return {'pages': [page(engine, values, name, today=today, now=now, loaded=loaded[name]) for name in names],
            'channels': channel_view(engine, values, now=now, loaded=loaded, licensed_for=licensed_for),
            'sentence': None if names else ('No fax server renewal yet. Enter the renewal of the fax server Faxbot '
                                            'could replace, and import its call records.'),
            'help': {'routes': ROUTES_HELP}, 'reference': list(REFERENCE), 'note': ADVICE_ONLY,
            'review_days': REVIEW_DAYS}
