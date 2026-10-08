"""Facts for the pre-dial predictor: the price for a number's class, what recorded calls showed, plan use.

``predict.predict`` reads its facts here, and the pure ``predict.predict_from``
does the arithmetic. Without an installation database the facts are the
shipped published prices (``config/rate_cards.json``) and the predictor's
cited defaults; with one they are the rate cards saved in Faxbot, the
negotiation records of earlier calls (``fax_engine_calls``, migration 0023)
and this month's use of each plan. Reading only: nothing here places a call
or writes a record.

Prices by number class live in ``config/rate_cards.json`` beside the cards:

- ``cards``, ``providers`` and ``plans``: calls to local numbers (as saved in
  Faxbot, which take precedence).
- ``toll_free`` (Builder AE's list) and ``international``: one entry per route
  with ``route`` (the provider identity, ``sip-<preset>`` for a SIP trunk),
  ``pricing`` ('own' with the prices in the entry, 'same_as_card' for the
  route's own card, or 'not_published'), the card fields, ``source_url`` and
  ``advertised_on``. An ``international`` entry also lists ``prefixes``
  ("+44"); the longest match wins. Any entry may carry ``page_time_seconds``
  (the greater-of rule) and ``max_pages_per_fax``.
- ``reference_plans``: a plan's ``included_pages`` and ``overage_per_page``,
  matched to a saved plan card by provider and monthly fee.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import statistics
import weakref

import sqlalchemy as sa

from .costs import InvalidRateCard, RateCard, RateTerms, parse_amount
from .destinations import CLASS_TEXT, INTERNATIONAL, LOCAL, PREMIUM, TOLL_FREE, classify
from .predict import AUDIO_RATE, CODINGS, MIN_CALLS, TYPICAL_RATE, Link, RouteFacts
from .seed import _date, _increment, default_path, load_cards


WINDOW_DAYS = 90
# Recorded calls read for one prediction: the number's newest, and the route's newest for its usual speed.
CALLS_READ = 200
NO_CALL_ROUTES = ('local', 'direct')


# Shipped prices ---------------------------------------------------------------------

def _document(path=None):
    path = Path(path) if path is not None else default_path()
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _int(value):
    return value if type(value) is int and value > 0 else None


def _class_entry(entry, kind):
    """(route identity, pricing, RateTerms or None) for one ``toll_free`` or ``international`` entry; None if bad."""
    route = str(entry.get('route') or '').strip().lower()
    if not route:
        return None
    pricing = entry.get('pricing') if entry.get('pricing') in ('own', 'same_as_card', 'not_published') else 'not_published'
    prefixes = tuple(prefix for prefix in entry.get('prefixes') or () if isinstance(prefix, str))
    terms = None
    if pricing == 'own':
        try:
            card = RateCard(None, route, 'outbound', str(entry.get('label') or route)[:100],
                            str(entry.get('currency') or 'USD').upper(),
                            parse_amount(str(entry.get('per_minute', '0'))), parse_amount(str(entry.get('per_page', '0'))),
                            parse_amount(str(entry.get('per_call', '0'))), _increment(entry),
                            int(entry.get('minimum_seconds', 0)), entry.get('source_url') or None,
                            _date(entry.get('advertised_on')))
            terms = RateTerms(card, kind, prefixes, _int(entry.get('page_time_seconds')),
                              max_pages_per_fax=_int(entry.get('max_pages_per_fax')), published=True)
        except (InvalidRateCard, ValueError, TypeError):
            pricing, terms = 'not_published', None
    return route, pricing, terms, prefixes, entry


@lru_cache(maxsize=4)
def shipped(path=None):
    """Everything the predictor reads from the shipped file, parsed once."""
    document = _document(path)
    classes = {TOLL_FREE: [], INTERNATIONAL: []}
    for kind in classes:
        listed = document.get(kind)
        for entry in listed if isinstance(listed, list) else []:
            found = _class_entry(entry, kind) if isinstance(entry, dict) else None
            if found is not None:
                classes[kind].append(found)
    plans = [plan for plan in document.get('reference_plans') or () if isinstance(plan, dict)]
    return {'cards': tuple(load_cards(path)), 'classes': classes, 'plans': plans}


def _card_for(cards, identity):
    return next((card for card in cards if card.provider_id == identity and card.direction == 'outbound'), None)


def _allowance(card, plans):
    """(included pages, overage micros) of the published plan a saved plan card matches, else (None, None)."""
    if card is None or not card.flat_plan:
        return None, None
    for plan in plans:
        try:
            fee = parse_amount(str(plan.get('monthly_fee')), whole_digits=4) if plan.get('monthly_fee') else None
        except InvalidRateCard:
            continue
        if plan.get('provider_id') == card.provider_id and fee == card.monthly_fee_micros and plan.get('included_pages'):
            overage = plan.get('overage_per_page')
            try:
                return _int(plan.get('included_pages')), (parse_amount(str(overage)) if overage else None)
            except InvalidRateCard:
                return _int(plan.get('included_pages')), None
    return None, None


def _score(destination, prefixes, entry):
    """How closely an international entry fits a number: its longest matching prefix ("+1867" beats "+44"),
    then its countries (``regions``, "CA"), then an entry for every other country; None when it does not fit."""
    regions = tuple(region for region in entry.get('regions') or () if isinstance(region, str))
    match = destination.matches(prefixes) if prefixes else None
    if match is not None:
        return len(match)
    if regions and destination.region in regions:
        return 2.5  # above a bare country code such as "+1", below any longer prefix
    if not prefixes and not regions:
        return 0
    return None


def terms_for(identity, destination, card, data, *, label=None):
    """(RateTerms or None, refusal sentence or None): the price of a call to ``destination`` on this route.

    Local numbers use the route's card. Toll-free and international numbers
    use the route's entry for that class: its own price, its card's price, or
    nothing when the route publishes none (unknown stays unknown).
    """
    if destination.kind == LOCAL:
        if card is None:
            return None, None
        included, overage = _allowance(card, data['plans'])
        return RateTerms(card, LOCAL, included_pages=included, overage_page_micros=overage,
                         published=card.id is None), None
    if destination.kind == PREMIUM:
        return None, None
    best = None
    for route, pricing, terms, prefixes, entry in data['classes'].get(destination.kind, ()):
        if route != identity:
            continue
        score = _score(destination, prefixes, entry) if destination.kind == INTERNATIONAL else 0
        if score is None:
            continue
        if entry.get('reaches') == 'no':
            return None, (f'{label or identity} does not call {CLASS_TEXT[destination.kind]}, so this fax cannot go '
                          'this way')
        if best is None or score > best[0]:
            best = (score, pricing, terms)
    if best is None:
        return None, None
    _, pricing, terms = best
    if pricing == 'own':
        return terms, None
    if pricing == 'same_as_card' and card is not None:
        return RateTerms(card, destination.kind, published=card.id is None), None
    return None, None


# Configuration ------------------------------------------------------------------------

def _values():
    try:
        from ..config import managed_configuration_values
        return managed_configuration_values()
    except Exception:
        return None


def _engine():
    """The installation database, when this process has one; never opens a connection itself."""
    try:
        from .. import config
        source = getattr(config, '_source', None)
        return getattr(source, 'engine', None)
    except Exception:
        return None


def _typical_rate(values, destination, recipient=None):
    """The highest speed the trunk would offer this number now, with its own limit when one is set (Recipients)."""
    if values is None:
        return TYPICAL_RATE
    try:
        from ..hylafax_engine import call_settings
        return int(call_settings(values, destination, recipient=recipient).max_rate) or TYPICAL_RATE
    except Exception:
        return AUDIO_RATE if getattr(values, 'sip_t38_enabled', True) is False else TYPICAL_RATE


def route_label(route_key, preset=''):
    """The route's name in a sentence: the SIP trunk is named after its carrier."""
    if route_key == 'sip':
        from ..provider_labels import trunk_name
        return trunk_name(preset or None)
    from .plan import route_label as label
    return label(route_key)


# Recorded calls --------------------------------------------------------------------------

def _rate(row):
    if row.get('sslfax') == 1:
        return None
    return row.get('rate_lowest') or row.get('rate_last_page') or None


def learn(rows, number, *, typical_rate=TYPICAL_RATE):
    """A ``Link`` from recorded calls on the trunk route: successful, at least one page, engine-reported.

    ``rows`` join ``fax_engine_calls`` and ``sip_call_records``. A row whose
    engine reported no negotiation is skipped: its speed and coding were what
    Faxbot asked for, not what the call reached (a failed T.38 call on
    2026-10-05 recorded 14,400 bit/s JBIG it never used).
    """
    usable = [row for row in rows if row.get('negotiation_by') and row.get('fax_status') == 'SUCCESS'
              and (row.get('pages') or 0) > 0]
    mine = [row for row in usable if row.get('number') == number]
    rates = [rate for rate in (_rate(row) for row in mine) if rate]
    scope, rate, rate_calls = None, None, 0
    if rates:
        scope, rate, rate_calls = 'number', statistics.median_low(rates), len(rates)
    else:
        route_rates = [rate for rate in (_rate(row) for row in usable) if rate]
        if len(route_rates) >= MIN_CALLS:
            scope, rate, rate_calls = 'route', statistics.median_low(route_rates), len(route_rates)
    newest = sorted(mine, key=lambda row: row.get('created_at') or datetime.min)
    coding = next((row['compression'] for row in reversed(newest) if row.get('compression') in CODINGS), None)
    per_page, setups = [], []
    for row in mine:
        pages, connected, transfer = row['pages'], row.get('connected_seconds'), row.get('transfer_seconds')
        if transfer is not None:
            per_page.append(transfer / pages)
            if connected is not None and connected >= transfer:
                setups.append(float(connected - transfer))
        elif connected is not None:
            from .predict import SETUP_SECONDS
            per_page.append(max(0.0, connected - SETUP_SECONDS) / pages)
    jbig = any(row.get('compression') == 'JBIG' and (row.get('engine') == 'hylafax' or row.get('engine_ref'))
               for row in mine)
    return Link(rate=rate, rate_calls=rate_calls, rate_scope=scope, coding=coding,
                seconds_per_page=statistics.median(per_page) if per_page else None, page_calls=len(per_page),
                setup_seconds=statistics.median(setups) if setups else None, setup_calls=len(setups),
                typical_rate=typical_rate, jbig=jbig)


_TABLES = weakref.WeakKeyDictionary()
_NAMES = ('provider_rate_cards', 'fax_engine_calls', 'sip_call_records', 'delivery_attempt_costs')


def _tables(engine):
    """The tables the predictor reads, reflected once per database (the schema only changes at startup)."""
    found = _TABLES.get(engine)
    if found is None:
        from .database import reflect
        found = reflect(engine, _NAMES)
        _TABLES[engine] = found
    return found


def stored_card(engine, route_key, preset=''):
    """The route's current sending card as saved in Faxbot, found as ``RouteStore.card_for`` finds it; None if none."""
    from .store import RouteStore
    cards = _tables(engine)['provider_rate_cards']
    identities = ((f'sip-{preset}', 'sip') if preset else ('sip',)) if route_key == 'sip' else (route_key,)
    with engine.connect() as connection:
        rows = {row['provider_id']: row for row in connection.execute(sa.select(cards).where(
            cards.c.superseded_at.is_(None), cards.c.direction == 'outbound',
            cards.c.provider_id.in_(identities))).mappings()}
    return next((RouteStore._card(rows[identity]) for identity in identities if identity in rows), None)


def recorded_calls(engine, *, since, number=None, limit=CALLS_READ):
    """Successful, engine-reported outbound trunk calls since ``since``, with their call records.

    The newest ``limit`` calls to ``number`` and the newest ``limit`` to other
    numbers (for the route's usual speed); [] when the tables are missing.
    """
    try:
        tables = _tables(engine)
    except Exception:
        return []
    calls, records = tables['fax_engine_calls'], tables['sip_call_records']
    if 'negotiation_by' not in calls.c:
        return []
    base = sa.select(
        calls.c.number, calls.c.engine, calls.c.engine_ref, calls.c.sslfax, calls.c.negotiation_by,
        calls.c.rate_lowest, calls.c.rate_last_page, calls.c.compression, calls.c.ecm, calls.c.transfer_seconds,
        calls.c.created_at, records.c.fax_status, records.c.pages, records.c.connected_seconds,
    ).join(records, sa.and_(records.c.direction == calls.c.direction, records.c.call_id == calls.c.call_key)).where(
        calls.c.direction == 'outbound', calls.c.created_at >= since, calls.c.negotiation_by.is_not(None),
        records.c.fax_status == 'SUCCESS', records.c.pages > 0)
    newest = (calls.c.created_at.desc(), calls.c.id.desc())
    queries = [base.order_by(*newest).limit(limit)] if number is None else [
        base.where(calls.c.number == number).order_by(*newest).limit(limit),
        base.where(sa.or_(calls.c.number.is_(None), calls.c.number != number)).order_by(*newest).limit(limit)]
    with engine.connect() as connection:
        return [dict(row) for query in queries for row in connection.execute(query).mappings()]


def plan_terms(route_key, terms, card, values):
    """A monthly plan's allowance and extra-page price from its budget (``plan_budget``) first.

    The budget is what you set in ``plan_budgets``, else the published plan the
    card matches, so an allowance or extra-page price you set prices the fax
    here too. Terms that are not the route's own plan card are unchanged.
    """
    if terms is None or card is None or terms.card is not card:
        return terms
    try:
        from .plan_budget import budget_for
        budget = budget_for(route_key, card, values)
    except Exception:
        return terms
    if budget is None:
        return terms
    # A minute allowance (a trunk bundle) prices any card's fax; minutes past it cost its per-minute price.
    minutes = budget.included_minutes if budget.included_minutes and card.per_minute_micros else None
    if not card.monthly_fee_micros:
        return replace(terms, included_minutes=minutes) if minutes else terms
    included = budget.included_pages
    return replace(terms, included_pages=included, included_minutes=minutes,
                   overage_page_micros=budget.page_overage_micros if included else None)


def plan_use(engine, route_key, *, now, values=None):
    """Pages and faxes the plan carried this billing period, with its page budget (``plan_budget``); None if unreadable."""
    try:
        from .plan_budget import plan_use as budget_use
        return budget_use(engine, route_key, now=now, values=values)
    except Exception:
        return None


# Putting them together -------------------------------------------------------------------

def _extra_trunk(values, account):
    """Settings as an extra trunk account sees them (its own carrier and numbers), or None for anything else."""
    if not account or account == 'sip' or values is None:
        return None
    from ..sip_trunk import trunk_for
    try:
        found = trunk_for(values, account)
    except Exception:
        return None
    return found.values if found is not None else None


def facts_for(route_key, destination, *, now=None, engine=None, values=None, data=None, account=None, site=None):
    """The ``RouteFacts`` for one route and number, from the installation when it has a database.

    ``account`` is the account the call would use (its key; ``route_key`` when not given): an extra trunk is
    priced by its own carrier's card, and an origin-rated row for where its calls start (``origin_rates``)
    prices the call when one matches. ``site`` prices it as if it started from that site instead.
    """
    values = _values() if values is None else values
    engine = _engine() if engine is None else engine
    data = shipped() if data is None else data
    account = account or route_key
    own = _extra_trunk(values, account)
    if own is not None:
        values, route_key = own, 'sip'
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    where = classify(destination, country)
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    label = route_label(route_key, preset)
    if route_key in NO_CALL_ROUTES:
        return RouteFacts(route_key, label, where, None)
    identity = (f'sip-{preset}' if preset else 'sip') if route_key == 'sip' else route_key
    number = where.number or destination
    card, saved = None, False
    if engine is not None:
        try:
            # The saved cards are authoritative: a card the administrator removed is not brought back here.
            card, saved = stored_card(engine, route_key, preset), True
        except Exception:
            card = None
    if card is None and not saved:
        card = _card_for(data['cards'], identity) or (_card_for(data['cards'], 'sip') if route_key == 'sip' else None)
    terms, refusal = terms_for(identity, where, card, data, label=label)
    origin = None
    if refusal is None and (card is None or not card.flat_plan):
        # Prices by where the call starts (design §3.7): the account's site or country, the longest prefix.
        from .database import DeliveryStoreError
        from .origin_rates import rated_terms
        try:
            rated, row = rated_terms(list(dict.fromkeys([account, identity])), number, where, values=values,
                                     account_key=account, engine=engine, site=site)
        except DeliveryStoreError as error:
            # Saved rows or prices by state could not be read: the card's own price, and the cause logged.
            # Anything else is a bug and raises.
            import logging
            logging.getLogger(__name__).warning('Prices by where calls start could not be read: %s', error)
            rated, row = None, None
        if rated is not None:
            terms, origin = rated, row.origin
            card = card or rated.card
    terms = plan_terms(route_key, terms, card, values)
    missing = refusal
    if terms is None and where.kind == LOCAL and card is None:
        missing = f'{label} has no rate card'
    moment = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    plan, recipient = None, None
    if engine is not None and route_key == 'sip':
        from ..hylafax_engine import recipient_limits
        recipient = recipient_limits(engine, number)
    link = Link(typical_rate=_typical_rate(values, number, recipient))
    if engine is not None:
        if route_key == 'sip':
            try:
                rows = recorded_calls(engine, since=moment - timedelta(days=WINDOW_DAYS), number=number)
            except Exception:
                rows = []
            link = learn(rows, number, typical_rate=link.typical_rate)
            cap = (recipient or {}).get('max_rate')
            if cap and link.rate and link.rate > cap:
                # A speed limit set for this number (Recipients) holds whatever earlier calls reached.
                link = replace(link, rate=cap)
        if terms is not None and (terms.card.flat_plan or terms.included_pages or terms.included_minutes):
            plan = plan_use(engine, route_key, now=moment, values=values)
    currency = card.currency if card is not None else 'USD'
    return RouteFacts(route_key, label, where, terms, link, plan, currency, missing, refused=refusal is not None,
                      origin=origin)
