"""Origin-rated prices: what a call costs from where it starts to the number it calls (design §3.7, B10).

A rate card has one flat price for calls to local numbers, and the shipped file
lists prices for other number classes (``predict_facts``). Rate rows add two
things: where the call starts (``origin``) and the start of the number it calls
(``destination_prefix``). The price for one account and one number is the row

1. for the account's own site (its key, from the organization's rules
   document), else for the site's country, or the installation's country
   when the account names no site (``country:GB``), else for anywhere (``any``);
2. with the longest destination prefix that matches the number (FreeSWITCH
   ``mod_lcr``'s longest-prefix digits), within the first origin that has one;

and the card's own price when no row matches. A row never prices a number
class its carrier refuses, and an unknown price stays unknown, never zero.

Rows come from two places, read the same way:

- the shipped file's ``origin_rates`` (``config/rate_cards.json``): prices
  carriers publish per destination, each with its source and the date it was
  read; a carrier that publishes none has no row;
- ``provider_rate_rows`` (migration 0033): prices you enter for one of your
  cards, such as a contract rate from your Leeds office to UK numbers. A saved
  row is never changed: a new price supersedes it.

Nothing here contacts a carrier. Reading only, apart from ``save_rows``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import re
from uuid import uuid4

import sqlalchemy as sa

from .costs import InvalidRateCard, RateCard, RateTerms, parse_amount


ANY = 'any'
_ORIGIN = re.compile(r'any|country:[A-Z]{2}|[a-z0-9][a-z0-9_-]{0,63}')
_PREFIX = re.compile(r'[0-9]{0,15}')


@dataclass(frozen=True)
class OriginRate:
    """One row: an origin, a destination prefix and the price of a call there, in ``currency``."""
    route: str                      # the card's identity: sip-<preset>, a provider id, or an account key
    origin: str                     # 'any', a site key, or 'country:GB'
    destination_prefix: str         # E.164 digits without '+'; '' matches every number
    currency: str
    per_minute_micros: int
    per_page_micros: int
    per_call_micros: int
    billing_increment_seconds: int
    minimum_seconds: int
    source_url: str | None = None
    captured_on: datetime | None = None
    label: str | None = None        # what the shipped file calls it ("AnveoDirect, calls to UK mobiles")
    published: bool = False         # shipped with Faxbot (a carrier's published price); False: saved by you
    id: str | None = None
    card_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.origin, str) or _ORIGIN.fullmatch(self.origin) is None:
            raise InvalidRateCard('Where calls start is a site, a country such as country:GB, or any.')
        if not isinstance(self.destination_prefix, str) or _PREFIX.fullmatch(self.destination_prefix) is None:
            raise InvalidRateCard('Write the number prefix as digits after the country code sign, such as 44113.')
        RateCard(None, self.route, 'outbound', 'Rate row', self.currency, self.per_minute_micros,
                 self.per_page_micros, self.per_call_micros, self.billing_increment_seconds, self.minimum_seconds,
                 self.source_url, self.captured_on or datetime(2026, 1, 1))

    def card(self, label=None):
        """This row as a rate card, for the predictor's arithmetic (``costs.terms_cost``)."""
        return RateCard(self.card_id, self.route, 'outbound', (label or self.label or self.route)[:100], self.currency,
                        self.per_minute_micros, self.per_page_micros, self.per_call_micros,
                        self.billing_increment_seconds, self.minimum_seconds, self.source_url,
                        self.captured_on or datetime(2026, 1, 1))


# -- the shipped rows -------------------------------------------------------------------------------------------

def _increment(text):
    """('60-60' style billing, as AnveoDirect writes it) or explicit fields → (minimum, increment)."""
    match = re.fullmatch(r'([0-9]{1,4})-([0-9]{1,4})', str(text or ''))
    return (int(match[1]), int(match[2])) if match else None


@lru_cache(maxsize=4)
def shipped(path=None) -> tuple:
    """Every published row in ``config/rate_cards.json`` → ``origin_rates`` that can be read; the rest are left out."""
    from .seed import default_path
    try:
        document = json.loads(Path(path or default_path()).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ()
    found = []
    for entry in document.get('origin_rates') or () if isinstance(document, dict) else ():
        if not isinstance(entry, dict) or entry.get('pricing', 'own') != 'own':
            continue
        try:
            billing = _increment(entry.get('billing'))
            minimum = billing[0] if billing else int(entry.get('minimum_seconds', 0))
            increment = billing[1] if billing else int(entry.get('billing_increment_seconds', 60))
            for prefix in entry.get('destination_prefixes') or [entry.get('destination_prefix', '')]:
                found.append(OriginRate(
                    str(entry['route']).strip().lower(), str(entry.get('origin') or ANY), str(prefix),
                    str(entry.get('currency') or 'USD').upper(), parse_amount(str(entry.get('per_minute', '0'))),
                    parse_amount(str(entry.get('per_page', '0'))), parse_amount(str(entry.get('per_call', '0'))),
                    increment, minimum, entry.get('source_url') or None,
                    datetime.strptime(str(entry.get('advertised_on'))[:10], '%Y-%m-%d'),
                    str(entry.get('label') or '')[:100] or None, True))
        except (InvalidRateCard, ValueError, TypeError, KeyError):
            continue
    return tuple(found)


# -- the saved rows -----------------------------------------------------------------------------------------------

def _tables(engine):
    from .database import reflect
    try:
        return reflect(engine, ('provider_rate_rows', 'provider_rate_cards'))
    except Exception:
        return None


def saved(engine, identities=None) -> tuple:
    """The current saved rows ({route} of their card), for ``identities`` (card provider ids) or every card."""
    tables = _tables(engine) if engine is not None else None
    if not tables or 'provider_rate_rows' not in tables:
        return ()
    rows, cards = tables['provider_rate_rows'], tables['provider_rate_cards']
    query = (sa.select(rows, cards.c.provider_id, cards.c.currency, cards.c.label.label('card_label'))
             .join(cards, cards.c.id == rows.c.card_id).where(rows.c.superseded_at.is_(None)))
    if identities is not None:
        query = query.where(cards.c.provider_id.in_(list(identities)))
    try:
        with engine.connect() as connection:
            found = connection.execute(query.order_by(rows.c.created_at, rows.c.id)).mappings().all()
    except sa.exc.SQLAlchemyError:
        return ()
    result = []
    for row in found:
        try:
            result.append(OriginRate(row['provider_id'], row['origin'], row['destination_prefix'], row['currency'],
                                     row['per_minute_micros'], row['per_page_micros'], row['per_call_micros'],
                                     row['billing_increment_seconds'], row['minimum_seconds'], row['source_url'],
                                     row['captured_on'], row['card_label'], False, row['id'], row['card_id']))
        except InvalidRateCard:
            continue
    return tuple(result)


def save_rows(engine, card_id, rows, *, now=None):
    """Replace the saved rows of one card: earlier rows are superseded, never changed. Returns the new rows."""
    tables = _tables(engine)
    if not tables or 'provider_rate_rows' not in tables:
        raise InvalidRateCard('Prices by where calls start need the latest database upgrade.')
    table, cards = tables['provider_rate_rows'], tables['provider_rate_cards']
    now = now or datetime.utcnow().replace(microsecond=0)
    with engine.begin() as connection:
        card = connection.execute(sa.select(cards.c.id, cards.c.provider_id).where(cards.c.id == card_id)).first()
        if card is None:
            raise InvalidRateCard('Faxbot has no rate card with this ID.')
        connection.execute(table.update().where(table.c.card_id == card_id, table.c.superseded_at.is_(None))
                           .values(superseded_at=now))
        for row in rows:
            connection.execute(table.insert().values(
                id=uuid4().hex, card_id=card_id, origin=row.origin, destination_prefix=row.destination_prefix,
                per_minute_micros=row.per_minute_micros, per_page_micros=row.per_page_micros,
                per_call_micros=row.per_call_micros, billing_increment_seconds=row.billing_increment_seconds,
                minimum_seconds=row.minimum_seconds, source_url=row.source_url,
                captured_on=row.captured_on or now, superseded_at=None, created_at=now))
    return saved(engine, [card.provider_id])


# -- where a call starts --------------------------------------------------------------------------------------------

def organization_sites(engine) -> dict:
    """{site key: Site} from the organization's active rules document; {} without one."""
    if engine is None:
        return {}
    from ..rules.store import RuleStore
    try:
        active = RuleStore(engine).active('organization', '')
    except Exception:
        return {}
    document = (active or {}).get('document') if isinstance(active, dict) else None
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError:
            document = None
    found = {}
    for item in (document or {}).get('sites') or () if isinstance(document, dict) else ():
        if isinstance(item, dict) and isinstance(item.get('key'), str):
            found[item['key']] = item
    return found


def account_site(values, account_key, sites=None):
    """The site an account's calls start from: the account's own ``site``, else the site that lists it."""
    from ..accounts import account_named
    try:
        account = account_named(values, account_key)
    except Exception:
        account = None
    if account is not None and account.site:
        return account.site
    for key, site in (sites or {}).items():
        if account_key in (site.get('accounts') or ()):
            return key
    return None


def origins(values, account_key, *, sites=None, site=None) -> list:
    """Where calls by ``account_key`` start, most specific first: its site, the site's (or the installation's)
    country, then anywhere. ``site`` overrides the account's own (a quote "from the Leeds office")."""
    site = site or account_site(values, account_key, sites)
    found = []
    country = None
    if site:
        found.append(site)
        country = ((sites or {}).get(site) or {}).get('country')
    country = str(country or getattr(values, 'fax_default_country', '') or '').upper()
    if re.fullmatch(r'[A-Z]{2}', country):
        found.append(f'country:{country}')
    found.append(ANY)
    return found


def best(rows, origin_list, destination) -> OriginRate | None:
    """The row for the first origin that has one matching ``destination``, with the longest prefix; else None."""
    digits = re.sub(r'[^0-9]', '', str(destination or ''))
    if not digits:
        return None
    for origin in origin_list:
        matching = [row for row in rows if row.origin == origin and digits.startswith(row.destination_prefix)]
        if matching:
            # Longest prefix; a row you saved wins over a shipped one with the same prefix.
            return max(matching, key=lambda row: (len(row.destination_prefix), not row.published))
    return None


def rows_for(identities, engine=None) -> list:
    """Every row (saved and shipped) of the cards ``identities`` names, in that order of preference."""
    identities = [identity for identity in identities if identity]
    found = list(saved(engine, identities)) if engine is not None else []
    found += [row for row in shipped() if row.route in identities]
    return found


def rated_terms(identities, destination, where, *, values, account_key, engine=None, sites=None, site=None):
    """(RateTerms, the row) for an origin row that prices this call, or (None, None).

    ``identities`` are the card identities to read rows for, the account's own first (``sinch-uk``, then
    ``sinch``; ``sip-gamma`` for a Gamma trunk). ``where`` is the number's class (``destinations.classify``):
    a premium-rate number is never priced by a row, and a toll-free number keeps its own class's price
    (``predict_facts``: who pays, and whether the route reaches it at all).
    """
    from .destinations import PREMIUM, TOLL_FREE, UNKNOWN
    if getattr(where, 'kind', None) in (PREMIUM, TOLL_FREE, UNKNOWN):
        return None, None
    rows = rows_for(identities, engine)
    # A carrier deck priced by the caller ID the call presents (origin_classes, N15). A row you saved for the
    # account's own site still wins below: your contract price from that office.
    from .origin_classes import class_terms
    from .database import DeliveryStoreError
    try:
        classed, quote = class_terms(identities, destination, where, values=values, account_key=account_key,
                                     engine=engine)
    except DeliveryStoreError as error:
        # The decks could not be read: the prices below, and the cause logged. Anything else raises.
        import logging
        logging.getLogger(__name__).warning('Prices by caller ID could not be read: %s', error)
        classed, quote = None, None
    from .jurisdiction import has_rows
    if not rows and not has_rows(engine, identities):
        return (classed, quote) if classed is not None else (None, None)
    if sites is None:
        sites = organization_sites(engine)
    where_from = origins(values, account_key, sites=sites, site=site)
    row = best(rows, where_from, destination) if rows else None
    if classed is not None and (row is None or row.published or row.origin == ANY or row.origin.startswith('country:')):
        return classed, quote
    if row is None or row.origin == ANY or row.origin.startswith('country:'):
        # A carrier's US price by jurisdiction, from the state of the site the call really starts at
        # (routing/jurisdiction.py); a row for the site itself still wins.
        from .jurisdiction import origin_row
        state = ((sites or {}).get(where_from[0]) or {}).get('state') if len(where_from) > 2 else None
        row = origin_row(engine, identities, state, destination) or row
    if row is None:
        return None, None
    prefixes = (f'+{row.destination_prefix}',) if row.destination_prefix and len(row.destination_prefix) <= 7 else ()
    kind = getattr(where, 'kind', 'local')
    try:
        return RateTerms(row.card(), kind, prefixes, published=row.published), row
    except InvalidRateCard:
        return None, None


# -- what people read -------------------------------------------------------------------------------------------------

def origin_label(origin, sites=None) -> str:
    """'Leeds office', 'United Kingdom' or 'Anywhere' for a row's origin."""
    if not origin or origin == ANY:
        return 'Anywhere'
    from .origin_classes import ORIGIN_TEXT
    if origin in ORIGIN_TEXT:
        return ORIGIN_TEXT[origin]  # priced by the caller ID the call presents (origin_classes)
    from .jurisdiction import label
    if label(origin):
        return label(origin)
    if origin.startswith('country:'):
        code = origin.split(':', 1)[1]
        try:
            import pycountry
            found = pycountry.countries.get(alpha_2=code)
            if found is not None:
                return getattr(found, 'common_name', None) or found.name
        except Exception:
            pass
        return {'GB': 'United Kingdom', 'US': 'United States', 'AU': 'Australia', 'CA': 'Canada',
                'IE': 'Ireland'}.get(code, code)
    site = (sites or {}).get(origin) or {}
    return str(site.get('name') or origin)


def row_view(row, sites=None) -> dict:
    """One row as the console and command line show it (``OriginRateRow``)."""
    from .costs import format_amount
    return {'origin': row.origin, 'origin_label': origin_label(row.origin, sites),
            'destination_prefix': f'+{row.destination_prefix}' if row.destination_prefix else 'Any number',
            'currency': row.currency, 'per_minute': format_amount(row.per_minute_micros),
            'per_page': format_amount(row.per_page_micros), 'per_call': format_amount(row.per_call_micros),
            'billing_increment_seconds': row.billing_increment_seconds, 'minimum_seconds': row.minimum_seconds,
            'source_url': row.source_url,
            'captured_on': row.captured_on.date().isoformat() if row.captured_on else None,
            'published': row.published, 'label': row.label}


def card_rows(card_identity, engine=None, sites=None) -> list:
    """The rows of one card for Costs → Prices & plans, shortest prefix first."""
    found = rows_for([card_identity], engine)
    found.sort(key=lambda row: (row.origin != ANY, row.origin, len(row.destination_prefix), row.destination_prefix))
    return [row_view(row, sites) for row in found]
