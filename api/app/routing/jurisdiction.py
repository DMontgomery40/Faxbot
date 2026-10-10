"""US prices by jurisdiction: calls within one state against calls between states, by where a call really starts.

Some US carriers price a call by whether it stays within one state
(intrastate) or crosses a state line (interstate). AnveoDirect publishes both
prices for every area code and exchange in its rate file
(https://www.anveo.com/anveodirect.standard.csv, columns ``rate_inter`` and
``rate_intra``, read 2026-10-08: they differ on 99,117 of 208,237 US rows, and
the intrastate price is the higher one on 32,321 of those). Telnyx publishes
one US price ("Starting at $0.005 per minute",
https://telnyx.com/pricing/elastic-sip, read 2026-10-08), so for a Telnyx trunk
where a call starts makes no difference.

A call's jurisdiction is set by where it really starts and ends. Faxbot reads
where it starts from the site of the trunk it goes over (the site's ``state``
in your organization's rules) and never changes caller ID to change a call's
rate: the FCC treats caller ID used to avoid intercarrier charges as a
violation (Truth in Caller ID order, DA 11-1089; 47 CFR 64.1601). So:

- ``import_rows`` keeps a carrier's prices by jurisdiction (a newer import
  supersedes the older one; rows are never changed);
- ``origin_row`` prices a US call from a site in a known state at the right
  one of the two prices, for quotes, caps and the predictor
  (``origin_rates.rated_terms``);
- ``site_advice`` reads your sent faxes and says when another site's trunk,
  where the call would really start, costs less for numbers in a state:
  "Faxes from Denver to Utah numbers would cost about $4.10 less a month from
  your Salt Lake City trunk." The change is a sending rule (``site_accounts``)
  you add; nothing is applied.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
import io
import re
from uuid import uuid4

import sqlalchemy as sa

from .costs import InvalidRateCard, parse_amount


WINDOW_DAYS = 30
MAX_ROWS = 400_000
INTRASTATE, INTERSTATE = 'intrastate', 'interstate'
FLAT = '{carrier} publishes one price for US calls, so where a call starts makes no difference.'
NO_CALLER_ID = 'Faxbot never changes caller ID to lower call charges (FCC, Truth in Caller ID; 47 CFR 64.1601).'
_INCREMENT = re.compile(r'([0-9]{1,4})-([0-9]{1,4})')


class JurisdictionInputError(ValueError):
    """A price file Faxbot cannot read; the message is one plain sentence."""


@dataclass(frozen=True)
class Rate:
    route: str
    prefix: str                 # E.164 digits without the plus: '1303555'
    currency: str
    interstate_micros: int
    intrastate_micros: int
    billing_increment_seconds: int
    minimum_seconds: int
    source_url: str | None = None
    captured_on: datetime | None = None

    def micros(self, jurisdiction):
        return self.intrastate_micros if jurisdiction == INTRASTATE else self.interstate_micros


# -- reading a carrier's file --------------------------------------------------------------------------------------

_COLUMNS = {'prefix': ('prefix', 'destination_prefix', 'npanxx', 'npa_nxx'),
            'inter': ('rate_inter', 'interstate', 'inter', 'interstate_rate'),
            'intra': ('rate_intra', 'intrastate', 'intra', 'intrastate_rate'),
            'billing': ('billing', 'increment'),
            'destination': ('destination', 'country', 'name')}


def parse_rows(text, route, *, currency='USD', source_url=None, captured_on=None):
    """Rates from a CSV with a prefix, an interstate and an intrastate price a minute (AnveoDirect's columns
    ``destination,prefix,rate_inter,rate_intra,billing`` or ``prefix,interstate,intrastate``). Only US numbers
    (prefix 1 and a US area code) are kept; billing such as ``1-1`` is the minimum seconds, then the increment.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        raise JurisdictionInputError('The file is empty.')
    names = [column.strip().lower() for column in header]

    def column(kind):
        return next((names.index(name) for name in _COLUMNS[kind] if name in names), None)
    prefix_at, inter_at, intra_at = column('prefix'), column('inter'), column('intra')
    billing_at, where_at = column('billing'), column('destination')
    if None in (prefix_at, inter_at, intra_at):
        raise JurisdictionInputError('The file needs a prefix column and a price for calls between states and '
                                     'within one state (rate_inter and rate_intra, or interstate and intrastate).')
    found, seen = [], set()
    for line in reader:
        if len(line) <= max(prefix_at, inter_at, intra_at):
            continue
        prefix = re.sub(r'[^0-9]', '', line[prefix_at])
        if not prefix.startswith('1') or len(prefix) < 4 or len(prefix) > 15 or prefix in seen:
            continue
        if where_at is not None and len(line) > where_at and line[where_at].strip().upper() not in ('', 'USA', 'US',
                                                                                                    'UNITED STATES'):
            continue
        try:
            inter, intra = parse_amount(line[inter_at].strip()), parse_amount(line[intra_at].strip())
        except InvalidRateCard:
            continue
        minimum, increment = 60, 60
        match = _INCREMENT.fullmatch(line[billing_at].strip()) if billing_at is not None and len(line) > billing_at \
            else None
        if match:
            minimum, increment = int(match[1]), max(1, int(match[2]))
        seen.add(prefix)
        found.append(Rate(route, prefix, currency, inter, intra, increment, minimum, source_url, captured_on))
        if len(found) > MAX_ROWS:
            raise JurisdictionInputError(f'The file has more than {MAX_ROWS:,} US rows.')
    if not found:
        raise JurisdictionInputError('The file has no US prices Faxbot can read.')
    return found


# -- stored rows -----------------------------------------------------------------------------------------------------

# The 0055 table as a light construct (no reflection), so a quote that asks costs one indexed read.
TABLE = sa.table('jurisdiction_rates', sa.column('id', sa.String()), sa.column('route', sa.String()),
                 sa.column('prefix', sa.String()), sa.column('currency', sa.String()),
                 sa.column('interstate_micros', sa.Integer()), sa.column('intrastate_micros', sa.Integer()),
                 sa.column('billing_increment_seconds', sa.Integer()), sa.column('minimum_seconds', sa.Integer()),
                 sa.column('source_url', sa.String()), sa.column('captured_on', sa.DateTime()),
                 sa.column('superseded_at', sa.DateTime()), sa.column('created_at', sa.DateTime()))


def _table(engine):
    return TABLE


def has_rows(engine, routes):
    """True when any of ``routes`` has current prices by jurisdiction (one indexed read)."""
    from .database import read_connection
    routes = [route for route in routes if route]
    if engine is None or not routes:
        return False
    with read_connection(engine) as connection:
        return connection.execute(sa.select(TABLE.c.id).where(
            TABLE.c.route.in_(routes), TABLE.c.superseded_at.is_(None)).limit(1)).first() is not None


def import_rows(engine, route, rates, *, now=None):
    """Make ``rates`` the route's prices by jurisdiction; earlier rows are superseded, never changed."""
    from .database import write_transaction
    table = _table(engine)
    now = now or datetime.utcnow().replace(microsecond=0)
    with write_transaction(engine) as connection:
        connection.execute(table.update().where(table.c.route == route, table.c.superseded_at.is_(None))
                           .values(superseded_at=now))
        batch = []
        for rate in rates:
            batch.append({'id': uuid4().hex, 'route': route, 'prefix': rate.prefix, 'currency': rate.currency,
                          'interstate_micros': rate.interstate_micros, 'intrastate_micros': rate.intrastate_micros,
                          'billing_increment_seconds': rate.billing_increment_seconds,
                          'minimum_seconds': rate.minimum_seconds, 'source_url': rate.source_url,
                          'captured_on': rate.captured_on or now, 'superseded_at': None, 'created_at': now})
            if len(batch) >= 5_000:
                connection.execute(table.insert(), batch)
                batch = []
        if batch:
            connection.execute(table.insert(), batch)
    return summary(engine)


def rate_for(engine, routes, number):
    """The current row with the longest prefix matching ``number`` for the first of ``routes`` that has one."""
    from .database import read_connection
    digits = re.sub(r'[^0-9]', '', str(number or ''))
    if not digits.startswith('1'):
        return None
    prefixes = [digits[:length] for length in range(min(len(digits), 15), 3, -1)]
    table = _table(engine)
    with read_connection(engine) as connection:
        for route in [route for route in routes if route]:
            row = connection.execute(sa.select(table).where(
                table.c.route == route, table.c.superseded_at.is_(None), table.c.prefix.in_(prefixes))
                .order_by(sa.func.length(table.c.prefix).desc(), table.c.created_at.desc()).limit(1)).mappings().first()
            if row is not None:
                return Rate(row['route'], row['prefix'], row['currency'], row['interstate_micros'],
                            row['intrastate_micros'], row['billing_increment_seconds'], row['minimum_seconds'],
                            row['source_url'], row['captured_on'])
    return None


def summary(engine):
    """Each route with current prices by jurisdiction: rows, how many differ, where they came from and when."""
    from .database import read_connection
    table = _table(engine)
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(
            table.c.route, sa.func.count().label('rows'),
            sa.func.sum(sa.case((table.c.interstate_micros != table.c.intrastate_micros, 1), else_=0)).label('differ'),
            sa.func.max(table.c.source_url).label('source_url'), sa.func.max(table.c.captured_on).label('read'),
            sa.func.max(table.c.created_at).label('imported'))
            .where(table.c.superseded_at.is_(None)).group_by(table.c.route).order_by(table.c.route)).all()
    return [{'route': row.route, 'carrier': _carrier(row.route), 'rows': row.rows, 'differ': int(row.differ or 0),
             'source_url': row.source_url, 'read_on': row.read.date().isoformat() if row.read else None,
             'imported_at': row.imported} for row in rows]


def _carrier(route):
    from .carriers import carrier_label
    return carrier_label(route[len('sip-'):]) if route.startswith('sip-') else route


# -- pricing by where a call really starts ------------------------------------------------------------------------------

def jurisdiction(site_state, number):
    """'intrastate', 'interstate', or None when either end's state is unknown (or the number is not a US one)."""
    from .nppes import us_state
    there = us_state(number)
    if not site_state or not there:
        return None
    return INTRASTATE if site_state.upper() == there else INTERSTATE


def origin_row(engine, identities, site_state, number):
    """An origin-rated row (``origin_rates.OriginRate``) at this call's jurisdiction price, or None.

    ``site_state`` is the state of the site the call starts from. None when the site's state, the number's
    state or a price by jurisdiction for these card identities is unknown.
    """
    if engine is None or not site_state:
        return None
    which = jurisdiction(site_state, number)
    if which is None:
        return None
    rate = rate_for(engine, identities, number)
    if rate is None:
        return None
    from .origin_rates import OriginRate
    return OriginRate(rate.route, which, rate.prefix, rate.currency, rate.micros(which), 0, 0,
                      rate.billing_increment_seconds, rate.minimum_seconds, rate.source_url,
                      rate.captured_on, f'{_carrier(rate.route)}, calls {_words(which)}', False)


def _words(which):
    return 'within one state' if which == INTRASTATE else 'between states'


def label(origin):
    """'Within one state' or 'Between states' for a jurisdiction origin, else None."""
    return {INTRASTATE: 'Within one state', INTERSTATE: 'Between states'}.get(origin)


# -- which site's trunk a call would cost less from --------------------------------------------------------------------

def _trunks(values, sites):
    """[(account key, site key, site name, state, card identities)] for every trunk at a site with a US state."""
    from ..accounts import account_values, all_accounts
    from .origin_rates import account_site
    found = []
    for account in all_accounts(values):
        if account.provider != 'sip':
            continue
        site = account_site(values, account.key, sites)
        state = str(((sites or {}).get(site) or {}).get('state') or '').upper() if site else ''
        preset = (getattr(account_values(values, account.key), 'sip_trunk_preset', '') or '').strip()
        identities = list(dict.fromkeys([account.key if account.key != 'sip' else None,
                                         f'sip-{preset}' if preset else None]))
        found.append((account.key, site, ((sites or {}).get(site) or {}).get('name') or site, state or None,
                      [identity for identity in identities if identity], preset))
    return found


def _sent(engine, start, end):
    """[(trunk key, destination, billed seconds or None)] for each delivered trunk fax in the window."""
    from .database import read_connection, reflect
    tables = reflect(engine, ('delivery_attempt_costs', 'sip_call_records'))
    costs, calls = tables['delivery_attempt_costs'], tables['sip_call_records']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(costs.c.route, costs.c.destination, costs.c.billed_seconds,
                                            calls.c.connected_seconds, calls.c.trunk_key).select_from(
            costs.outerjoin(calls, sa.and_(calls.c.attempt_id == costs.c.id, calls.c.direction == 'outbound')))
            .where(costs.c.outcome == 'success', costs.c.created_at >= start, costs.c.created_at < end,
                   costs.c.provider_id == 'sip')).all()
    return [(row.trunk_key or 'sip', row.destination, row.billed_seconds if row.billed_seconds is not None
             else row.connected_seconds) for row in rows]


def _cost(rate, which, seconds):
    from .costs import RateCard, attempt_cost
    card = RateCard(None, rate.route, 'outbound', 'jurisdiction', rate.currency, rate.micros(which), 0, 0,
                    rate.billing_increment_seconds, rate.minimum_seconds, rate.source_url,
                    rate.captured_on or datetime(2026, 1, 1))
    return attempt_cost(card, seconds=seconds, pages=1, delivered=True)


def site_advice(engine, values, *, now=None, days=WINDOW_DAYS, sites=None):
    """Per trunk carrier: flat or by jurisdiction; and where another site's trunk would cost less (estimates)."""
    from .nppes import STATE_NAMES, us_state
    from .origin_rates import organization_sites
    from .receiving import about
    now = (now or datetime.utcnow()).replace(microsecond=0)
    sites = organization_sites(engine) if sites is None else sites
    trunks = _trunks(values, sites)
    priced = {route['route'] for route in summary(engine)}
    carriers = []
    for key, _, _, _, identities, preset in trunks:
        name = _carrier(f'sip-{preset}') if preset else 'Your trunk'
        by_state = any(identity in priced for identity in identities)
        carriers.append({'account': key, 'carrier': name, 'by_jurisdiction': by_state,
                         'sentence': (f'{name} prices US calls by whether they stay within one state; Faxbot prices '
                                      'each call from the state of its trunk\'s site.' if by_state
                                      else FLAT.format(carrier=name))})
    located = [trunk for trunk in trunks if trunk[3]]
    items = []
    if len({trunk[3] for trunk in located}) >= 2:
        by_key = {trunk[0]: trunk for trunk in located}
        totals = {}
        for trunk_key, destination, seconds in _sent(engine, now - timedelta(days=days), now):
            source = by_key.get(trunk_key)
            there = us_state(destination)
            if source is None or there is None or seconds is None:
                continue
            here_rate = rate_for(engine, source[4], destination)
            if here_rate is None:
                continue
            here = _cost(here_rate, jurisdiction(source[3], destination), seconds)
            for other in located:
                if other[0] == source[0] or other[3] == source[3]:
                    continue
                other_rate = rate_for(engine, other[4], destination)
                if other_rate is None or other_rate.currency != here_rate.currency:
                    continue
                there_cost = _cost(other_rate, jurisdiction(other[3], destination), seconds)
                if here is None or there_cost is None:
                    continue
                entry = totals.setdefault((source[0], other[0], there), [0, 0, here_rate.currency])
                entry[0] += here - there_cost
                entry[1] += 1
        for (source_key, other_key, there), (saving, faxes, currency) in sorted(
                totals.items(), key=lambda item: -item[1][0]):
            if saving <= 0:
                continue
            source, other = by_key[source_key], by_key[other_key]
            monthly = saving * 30 // max(1, days)
            items.append({'from_site': source[2], 'to_site': other[2], 'state': there,
                          'state_name': STATE_NAMES.get(there, there), 'faxes': faxes,
                          'saving': [{'currency': currency, 'amount': _format(monthly)}],
                          'sentence': (f'Faxes from {source[2]} to {STATE_NAMES.get(there, there)} numbers would '
                                       f'cost about {about(monthly, currency)} less a month from your {other[2]} '
                                       'trunk (estimate).'),
                          'action': (f'To send them from there, add a sending rule for numbers in '
                                     f'{STATE_NAMES.get(there, there)} that sends from the {other[2]} site '
                                     '(Delivery setup → Routing rules).')})
    if items:
        sentence = items[0]['sentence']
    elif len({trunk[3] for trunk in located}) >= 2:
        sentence = 'None of your sites\' trunks would have sent your recent faxes for less from another site.'
    elif any(carrier['by_jurisdiction'] for carrier in carriers):
        sentence = ('Give each site its state under Delivery setup → Routing rules → Sites so Faxbot can price calls from where '
                    'they start; with trunks at sites in two states it also says which site costs less.')
    else:
        sentence = carriers[0]['sentence'] if carriers else 'Faxbot has no trunk, so this does not apply.'
    return {'days': days, 'estimate': True, 'sentence': sentence, 'carriers': carriers, 'items': items,
            'prices': summary(engine), 'caller_id': NO_CALLER_ID}


def _format(micros):
    from .costs import format_amount
    return format_amount(micros)
