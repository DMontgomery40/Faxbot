"""Where each of your fax numbers should live (M24, B6 and B13): the cheapest of your accounts to receive on.

Every number one of your accounts receives on (a trunk's numbers, the HumbleFax,
eFax and SignalWire numbers, an extra account's numbers) is placed at the
account that carries it today. For the last ``WINDOW_DAYS`` days Faxbot prices
the faxes that number received, and its own monthly price, at that account and
at each of your other accounts that receive faxes:

- a received fax costs what the account's receiving price says: by the minute
  (the call's time, else the usual estimate of 30 seconds plus 30 a page), by
  the page, or nothing within a flat plan (``carrier_compare``, the same
  shipped, dated prices every comparison uses);
- the number itself costs the account's published monthly price for one
  number; a plan that includes one number costs nothing more for the number it
  already carries, and an unpublished price for a second number stays unknown;
- toll-free numbers stay at accounts that take toll-free numbers, and an
  account that charges for every page received is never suggested to receive
  on (``reply_number``'s rule).

Unknown stays unknown: an account with no receiving price is never called the
cheapest. When another account is cheaper, the advice names it with the steps
to move (port) the number there, read from each provider's published porting
process (``config/number_porting.json``, with sources and dates): what the
current provider must give the new one, and how the new one takes the number,
its fee and its usual lead time. Porting keeps the number, so letterhead,
forms and your NPI record stay right; a number still printed on your NPI
record is never suggested for release.

Advice only: Faxbot never ports, releases or cancels anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
import json
import math

from .costs import attempt_cost, money_text, parse_amount
from .database import utcnow


WINDOW_DAYS = 30
NO_MOVES = 'Every number already lives at the cheapest of your accounts that can receive it.'
ADVICE_ONLY = 'Faxbot only advises: it never moves, releases or cancels a number or an account.'


# -- what each provider publishes about porting ----------------------------------------------------------------------

@lru_cache(maxsize=2)
def porting(path=None):
    """{provider or carrier preset: its porting facts} from ``config/number_porting.json``; {} when unreadable."""
    from ..config_paths import bundled_config_dir
    try:
        document = json.loads((path or bundled_config_dir() / 'number_porting.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    providers = document.get('providers') if isinstance(document, dict) else None
    read_on = document.get('read_on') if isinstance(document, dict) else None
    return {key: {**value, 'read_on': value.get('read_on') or read_on}
            for key, value in (providers or {}).items() if isinstance(value, dict)}


def _sources(facts, side):
    return [{'label': item.get('label'), 'url': item.get('url'), 'read_on': facts.get('read_on'),
             'secondary': bool(item.get('secondary'))}
            for item in ((facts.get(side) or {}).get('sources') or ()) if isinstance(item, dict) and item.get('url')]


def porting_steps(losing, gaining, *, number=None, path=None):
    """The steps to move a number from ``losing`` to ``gaining`` (porting keys such as 'humblefax', 'telnyx').

    {'from', 'to', 'steps': [sentence, ...], 'fee', 'lead_time', 'restriction', 'sources'}; facts a provider
    does not publish are left out, never guessed.
    """
    facts = porting(path)
    out_facts, in_facts = facts.get(losing) or {}, facts.get(gaining) or {}
    out_side, in_side = out_facts.get('port_out') or {}, in_facts.get('port_in') or {}
    old = out_facts.get('label') or losing
    new = in_facts.get('label') or gaining
    what = number or 'the number'
    steps = []
    if out_side.get('needs'):
        steps.append(f"At {old}: {out_side['needs']}")
    else:
        steps.append(f'At {old}: ask for the account number and port-out PIN the new provider will need.')
    how = in_side.get('how') or f'Ask {new} to port in {what}.'
    documents = in_side.get('documents')
    steps.append(f'At {new}: {how}' + (f' {new} asks for {documents}.' if documents else ''))
    steps.append(f'Keep {what} and your {old} account active until {new} confirms the date the move completes; '
                 'never cancel first, or the number can be lost.')
    steps.append(f'When it completes, add {what} to your {new} account in Faxbot under Providers and remove it from '
                 f'{old}; the rule under Numbers that sends its faxes to a mailbox stays as it is.')
    fee = in_side.get('fee_text') or (f"{money_text(parse_amount(in_side['fee']), 'USD')} a number."
                                      if in_side.get('fee') not in (None, '') else None)
    out_fee = out_side.get('fee_text')
    return {'from': old, 'to': new, 'steps': steps,
            'fee': ' '.join(part for part in (f'{new}: {fee}' if fee else f'{new} publishes no porting fee.',
                                              f'{old}: {out_fee}' if out_fee else None) if part),
            'lead_time': in_side.get('lead_time') or f'{new} publishes no usual time for a move.',
            'restriction': out_side.get('restriction'),
            'sources': _sources(out_facts, 'port_out') + _sources(in_facts, 'port_in')}


def takes(gaining, kind, country, *, path=None):
    """True/False when ``gaining`` publishes that it takes numbers of this kind and country; None when unknown."""
    facts = (porting(path).get(gaining) or {}).get('port_in') or {}
    if not facts:
        return None
    if kind == 'toll_free' and 'toll_free' not in (facts.get('number_types') or ()):
        return False
    countries = facts.get('countries')
    return None if not countries else country in countries


# -- your accounts and their numbers ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Host:
    key: str            # the account key ('sip', 'humblefax', 'trunk-2')
    provider: str       # 'sip', 'humblefax', 'efax', ...
    identity: str       # the price identity: 'sip-telnyx', 'humblefax'
    porting_key: str    # the porting facts key: the carrier preset for a trunk, else the provider
    name: str           # what people read: 'Telnyx', 'HumbleFax'
    receives: bool      # Faxbot can receive faxes through this account


def hosts(values) -> list:
    """Every account that carries numbers or receives faxes, as a place a number could live."""
    from ..accounts import account_values, all_accounts, supports_inbound
    from .carriers import carrier_label
    from ..provider_labels import provider_label
    found = []
    for account in all_accounts(values):
        if account.provider == 'sip':
            preset = (getattr(account_values(values, account.key), 'sip_trunk_preset', '') or '').strip()
            if not preset:
                continue  # a trunk with no carrier chosen has no published prices to compare
            name = carrier_label(preset)
            if not account.primary:
                name = account.label or name
            found.append(Host(account.key, 'sip', f'sip-{preset}', preset, name, True))
        else:
            found.append(Host(account.key, account.provider, account.provider, account.provider,
                              provider_label(account.provider) if account.primary else account.label,
                              supports_inbound(account.provider)))
    return found


def placed_numbers(values) -> list:
    """[(number, Host)] for every number an account of yours receives on, send-only numbers left out."""
    from ..accounts import all_accounts
    from .numbers import stored_number
    from .own_numbers import PROVIDER_NUMBERS
    from .send_only import numbers as send_only_numbers
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    by_key = {host.key: host for host in hosts(values)}
    skip = set(send_only_numbers(values))
    found, seen = [], set()
    for account in all_accounts(values):
        host = by_key.get(account.key)
        if host is None:
            continue
        listed = list(account.numbers)
        if account.primary:
            listed += [getattr(values, field, '') or '' for field in PROVIDER_NUMBERS.get(account.provider, ())]
        for number in listed:
            number = stored_number(number, country=country) if number else ''
            if number.startswith('+') and number not in seen and number not in skip:
                seen.add(number)
                found.append((number, host))
    return found


# -- prices ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Price:
    usage: int | None       # what the number's received faxes cost there over the window; None when unknown
    number_fee: int | None  # the number's own monthly price there; None when unknown
    currency: str | None
    per_page: bool = False
    reason: str | None = None   # why it can't be compared, when it can't

    @property
    def total(self):
        return None if self.usage is None or self.number_fee is None else self.usage + self.number_fee


def _published(path=None):
    """{identity: carrier_compare.Carrier} from the shipped prices."""
    from .carrier_compare import carriers
    found, _ = carriers(path)
    return {carrier.id: carrier for carrier in found}


def _receiving_card(host, kind, published, routes):
    """(card, monthly number price or None, includes a number) for receiving on this host."""
    from .receiving import carrier_prices
    carrier = published.get(host.identity)
    card, rental, includes = None, None, False
    if host.provider == 'sip':
        prices = carrier_prices(host.porting_key)
        card = prices.per_minute.get(kind if kind in ('local', 'toll_free') else 'local')
        rental = prices.rental.get(kind if kind in ('local', 'toll_free') else 'local')
        if card is None and kind == 'local':
            card = carrier.receiving if carrier is not None else None
    elif carrier is not None:
        card = carrier.receiving if kind == 'local' else None
        includes = carrier.includes_number
        rental = carrier.rental_micros
    if card is None and routes is not None and kind == 'local':
        card = routes.card_for(host.identity, 'inbound') or (
            routes.card_for(host.identity, 'outbound') if host.provider == 'humblefax' else None)
        if card is not None and card.flat_plan:
            includes = True
    if rental is None and carrier is not None and host.provider == 'sip':
        rental = carrier.rental_micros
    return card, rental, includes


def price_at(host, kind, faxes, *, current, published, routes=None):
    """What this number's ``faxes`` and the number itself cost at ``host`` over the window."""
    from .carrier_compare import _seconds
    card, rental, includes = _receiving_card(host, kind, published, routes)
    if card is None:
        return Price(None, None, None, reason=f'Faxbot has no price for receiving at {host.name}.')
    usage = 0
    if not card.flat_plan:
        for fax in faxes:
            cost = attempt_cost(card, seconds=_seconds(fax), pages=fax.pages, delivered=True)
            if cost is None:
                return Price(None, None, card.currency, reason=f'Faxbot has no price for receiving at {host.name}.')
            usage += cost
    if includes and (current or card.flat_plan):
        fee = 0 if current else None   # the plan's one number is the one it carries; a second is not published
    else:
        fee = rental
    reason = None
    if fee is None:
        reason = (f'{host.name} does not publish the price of another number.' if includes
                  else f'Faxbot has no monthly number price for {host.name}.')
    return Price(usage, fee, card.currency, per_page=bool(card.per_page_micros), reason=reason)


# -- the advice ---------------------------------------------------------------------------------------------------------

def _money(micros, currency):
    return [] if micros is None or currency is None else [{'currency': currency, 'amount': _amount(micros)}]


def _amount(micros):
    from .costs import format_amount
    return format_amount(micros)


def _about(micros, currency):
    from .receiving import about
    return about(micros, currency)


def _received(engine, values, now, days):
    """{number: [carrier_compare.Fax]} received in the window, by the number that received them."""
    from .carrier_compare import recorded_faxes
    found = {}
    for fax in recorded_faxes(engine, values, now - timedelta(days=days), now):
        if fax.direction == 'received' and fax.number:
            found.setdefault(fax.number, []).append(fax)
    return found


def _notes(values, number, reply):
    notes = []
    if number == (getattr(values, 'sip_trunk_caller_id', '') or ''):
        notes.append('It is also your trunk\'s caller ID: change the caller ID under Providers once it moves.')
    if reply and number == reply:
        notes.append('It is your reply number, printed on every fax you send; porting keeps it, so that stays right.')
    return notes


def placement(engine, values, *, routes=None, now=None, days=WINDOW_DAYS, path=None, published=None):
    """Where each number should live, with porting steps where another account is cheaper; never applied."""
    from .receiving import number_kind, shown_number
    from .nppes import npi_evidence, published_numbers
    now = (now or utcnow()).replace(microsecond=0)
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    places = placed_numbers(values)
    if not places:
        return {'days': days, 'estimate': True, 'state': 'no_numbers', 'numbers': [], 'accounts': [],
                'sentence': 'Faxbot knows none of your fax numbers yet, so there is nothing to place.',
                'note': ADVICE_ONLY}
    receiving_hosts = [host for host in hosts(values) if host.receives]
    priced = _published(path)
    faxes = _received(engine, values, now, days)
    npi = published_numbers(engine) if published is None else published
    reply = (getattr(values, 'fax_reply_number', '') or '') or None
    rows, moves = [], 0
    for number, current in places:
        kind = number_kind(number, country)
        region = _region(number)
        received = faxes.get(number, [])
        here = price_at(current, kind, received, current=True, published=priced, routes=routes)
        options, skipped = [], []
        for host in receiving_hosts:
            if host.identity == current.identity:
                continue
            if takes(host.porting_key, kind, region, path=path) is False:
                skipped.append(f"{host.name} does not take {'toll-free' if kind == 'toll_free' else 'these'} numbers.")
                continue
            there = price_at(host, kind, received, current=False, published=priced, routes=routes)
            if there.per_page:
                skipped.append(f'{host.name} charges for every page you receive, so Faxbot never suggests it for '
                               'receiving.')
                continue
            options.append((host, there))
        costs = [_cost_row(current, here, True)] + [_cost_row(host, price, False) for host, price in options]
        comparable = [(host, price) for host, price in options
                      if price.total is not None and here.total is not None and price.currency == here.currency]
        best = min(comparable, key=lambda item: (item[1].total, item[0].name), default=None)
        evidence = npi_evidence(npi, number)
        row = {'number': number, 'display': shown_number(number), 'kind': kind, 'account': current.name,
               'received': len(received), 'pages': sum(fax.pages for fax in received), 'costs': costs,
               'skipped': skipped, 'notes': _notes(values, number, reply), 'npi_record': evidence,
               'cheapest': None, 'saving': [], 'porting': None}
        if here.total is None:
            row['state'] = 'unknown'
            row['sentence'] = (f'Faxbot can\'t compare where {shown_number(number)} should live: '
                               f'{here.reason[0].lower()}{here.reason[1:]}')
        elif best is None or best[1].total >= here.total:
            row['state'] = 'keep'
            unknown = [host.name for host, price in options if price.total is None]
            cost = (f'it is included in your {current.name} plan' if here.total == 0 and here.number_fee == 0
                    else f'it costs about {_about(here.total, here.currency)} a month there (estimate)')
            row['sentence'] = (f'Keep {shown_number(number)} at {current.name}: {cost}, the least of your accounts'
                               + (f' Faxbot can price (it has no price at {_join(unknown)})' if unknown else '')
                               + '.')
        else:
            host, price = best
            saving = here.total - price.total
            moves += 1
            steps = porting_steps(current.porting_key, host.porting_key, number=shown_number(number), path=path)
            row.update(state='move', cheapest=host.name, saving=_money(saving, here.currency), porting=steps)
            row['sentence'] = (f'Move {shown_number(number)} from {current.name} to {host.name}: about '
                               f'{_about(saving, here.currency)} a month less (estimate), from '
                               f'{len(received)} {"fax" if len(received) == 1 else "faxes"} received in the last '
                               f'{days} days.')
            if evidence is not None and evidence['state'] == 'listed':
                row['notes'].append('It is printed on your NPI record; porting keeps the number, so the record stays '
                                    'right.')
        row['_elsewhere'] = (best[0], best[1]) if best is not None else None
        row['_here'] = here
        rows.append(row)
    accounts = _account_worth(rows, places, routes, priced, path)
    for row in rows:
        row.pop('_elsewhere', None)
        row.pop('_here', None)
    if moves:
        sentence = (f"{moves} of your {len(rows)} {'number' if len(rows) == 1 else 'numbers'} would cost less at "
                    'another of your accounts (estimate).')
        state = 'advice'
    elif accounts:
        sentence, state = accounts[0]['sentence'], 'account'
    elif any(row['state'] == 'unknown' for row in rows):
        sentence = 'Faxbot can\'t price every number yet; add the missing prices in Costs → Prices & plans.'
        state = 'unknown'
    else:
        sentence, state = NO_MOVES, 'nothing_to_move'
    if len(receiving_hosts) < 2 and state not in ('unknown', 'account'):
        sentence = ('You receive through one account, so there is nowhere cheaper of your own to move a number; '
                    'Costs → Recommendations → Other carriers compares carriers you don\'t use.')
        state = 'one_account'
    return {'days': days, 'estimate': True, 'state': state, 'sentence': sentence, 'numbers': rows,
            'accounts': accounts, 'note': ADVICE_ONLY,
            'assumptions': [f'Figures are what the faxes each number received in the last {days} days would have '
                            'cost at each account, plus the number\'s own monthly price.',
                            'A fax with no measured call time counts 30 seconds plus 30 a page.',
                            'Fees to move a number are paid once and are shown with the steps, not in the monthly '
                            'figures.']}


def _region(number):
    import phonenumbers
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(number, None))
    except phonenumbers.NumberParseException:
        return None


def _join(names):
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]


def _cost_row(host, price, current):
    return {'account': host.name, 'current': current, 'monthly': _money(price.total, price.currency),
            'faxes': _money(price.usage, price.currency), 'number_fee': _money(price.number_fee, price.currency),
            'reason': price.reason}


def _account_worth(rows, places, routes, published, path=None):
    """An account with a monthly fee whose numbers would cost less elsewhere, fee included (M24's worth check).

    Keeping it costs its fee plus its numbers there; ending it costs each number at your cheapest other account
    (plus a one-time move). Its other uses, such as sending, are not counted, so the sentence says to check them.
    """
    by_account = {}
    for row, (_, host) in zip(rows, places):
        by_account.setdefault(host.key, (host, []))[1].append(row)
    found = []
    for host, items in by_account.values():
        fee, currency = _fixed_fee(host, routes, published)
        if not fee:
            continue
        here = [item['_here'] for item in items]
        elsewhere = [item['_elsewhere'] for item in items]
        if any(price.total is None or price.currency != currency for price in here) or any(
                option is None or option[1].currency != currency for option in elsewhere):
            continue
        keep = fee + sum(price.total for price in here)
        moved = sum(option[1].total for option in elsewhere)
        if moved >= keep:
            continue
        targets = _join([option[0].name for option in elsewhere])
        count = len(items)
        found.append({
            'account': host.name, 'monthly_fee': _money(fee, currency), 'saving': _money(keep - moved, currency),
            'sentence': (f'{host.name} costs {_about(fee, currency)} a month; '
                         f"{'its number' if count == 1 else f'its {count} numbers'} would cost about "
                         f'{_about(moved, currency)} a month at {targets}. If nothing else needs {host.name}, moving '
                         f"{'it' if count == 1 else 'them'} and ending {host.name} saves about "
                         f'{_about(keep - moved, currency)} a month (estimate); check first that it sends no faxes '
                         'you need.'),
            'porting': [porting_steps(host.porting_key, option[0].porting_key, number=item['display'], path=path)
                        for item, option in zip(items, elsewhere)]})
    return found


def _fixed_fee(host, routes, published):
    from .receiving import carrier_prices
    if host.provider == 'sip':
        prices = carrier_prices(host.porting_key)
        if prices.trunk_fee:
            return prices.trunk_fee, prices.currency
    carrier = published.get(host.identity)
    card = carrier.sending if carrier is not None else None
    if card is None and routes is not None:
        card = routes.card_for(host.identity, 'outbound')
    if card is not None and card.monthly_fee_micros:
        return card.monthly_fee_micros, card.currency
    return None, None


# -- whether a line can go (CE5): keep, move the termination, investigate or can likely go -------------------------------

QUIET_DAYS = 90
YEAR_DAYS = 365
# One-sided 95% upper bound on a steady arrival rate after zero arrivals in T days: -ln(0.05) / T, about 3 / T.
POISSON_95 = -math.log(0.05)
VERDICTS = {'keep': 'Keep', 'move_termination': 'Move the termination', 'investigate': 'Investigate',
            'can_likely_go': 'Can likely go', 'not_in_faxbot': 'Not in Faxbot yet'}
# Lines with a date someone else set (a carrier's letter or list, a contract end) come first: passed, then soon.
DATE_ORDER = {'passed': 0, 'soon': 1, 'later': 2}
QUIET_LIMIT = ('This says nothing about yearly, seasonal or emergency use: a number used once a year, in one season or '
               'only in an emergency can be quiet this long and still be needed, so "can likely go" also needs your '
               'answers about it.')
LINE_ADVICE_ONLY = 'Faxbot only advises: it never ports, cancels or releases a number or a line.'


def carrier_facts(number, *, broadband=False, about=None, path=None):
    """Carrier rules about moving or keeping a line in the number's country (``carrier_facts``), with sources."""
    from ..config_paths import bundled_config_dir
    try:
        document = json.loads((path or bundled_config_dir() / 'number_porting.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    region = _region(number)
    wanted = {'all', 'broadband', 'other_lines'} if about is None else {about}
    if broadband:
        wanted = {'broadband'}
    return [{'id': fact.get('id'), 'sentence': fact.get('sentence'), 'about': fact.get('about'),
             'source': (fact.get('source') or {}).get('url'), 'label': (fact.get('source') or {}).get('label'),
             'read_on': fact.get('read_on')}
            for fact in (document.get('carrier_facts') or ()) if isinstance(fact, dict)
            and region in (fact.get('countries') or ()) and fact.get('about') in wanted]


def quiet_bound(days):
    """The one-sided 95% upper bound on a steady daily rate after ``days`` days with no arrival, with its limit."""
    if not days or days <= 0:
        return None
    per_day = POISSON_95 / days
    every = 1 / per_day
    return {'per_day': round(per_day, 6), 'days': days,
            'sentence': (f'No fax arrived in the {days:,} days Faxbot has watched it. Had faxes come at a steady rate '
                         f'of more than about one every {every:,.0f} days, at least one would very likely (95%) have '
                         'arrived by now.'),
            'limit': QUIET_LIMIT}


def _records_start(engine):
    """When Faxbot's record of received faxes starts; None when it holds none."""
    import sqlalchemy as sa
    from .database import read_connection, reflect
    faxes = reflect(engine, ('inbound_faxes',))['inbound_faxes']
    with read_connection(engine) as connection:
        return connection.execute(sa.select(sa.func.min(sa.func.coalesce(faxes.c.received_at,
                                                                        faxes.c.created_at)))).scalar()


def _month_label(year, month):
    from datetime import date
    return f'{date(year, month, 1):%B %Y}'


def _months(now, found):
    """Arrivals in each of the last 12 calendar months, oldest first."""
    year, month = now.year, now.month
    keys = []
    for _ in range(12):
        keys.append((year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    counts = {key: 0 for key in keys}
    for at, *_ in found:
        key = (at.year, at.month)
        if key in counts:
            counts[key] += 1
    return [{'month': f'{year:04d}-{month:02d}', 'label': _month_label(year, month), 'arrivals': counts[(year, month)]}
            for year, month in reversed(keys)]


def _removed(row):
    """(micros or None, currency, sentence): the monthly expense that giving the number up would remove."""
    current = next((cost for cost in row['costs'] if cost['current']), None) if row else None
    fee = (current or {}).get('number_fee') or []
    if not fee:
        return None, None, 'Faxbot does not know what this number costs you a month on its own.'
    amount = parse_amount(fee[0]['amount'], whole_digits=6)
    if amount == 0:
        return 0, fee[0]['currency'], (f"It is included in your {row['account']} plan, so giving it up removes no "
                                       'monthly expense unless the plan ends too.')
    return amount, fee[0]['currency'], f"Giving it up removes about {_about(amount, fee[0]['currency'])} a month."


def line_advice(engine, values, *, routes=None, now=None, days=QUIET_DAYS, path=None, published=None):
    """Per number: keep, move the termination, investigate or can likely go, with the evidence; never applied."""
    from .number_moves import DependencyStore, MoveStore, QUESTIONS, arrivals, dependency_rows, outcome
    from .nppes import npi_evidence, published_numbers
    from .receiving import shown_number
    now = (now or utcnow()).replace(microsecond=0)
    places = placed_numbers(values)
    questions = [{'question': key, 'label': label, 'help': text} for key, (label, text) in QUESTIONS.items()]
    if not places:
        rows = []
        _dated_first(engine, rows, set(), DependencyStore(engine), now, path)
        if rows:
            return {'state': 'advice', 'sentence': (f"No Faxbot account receives faxes yet; {len(rows)} "
                                                    f"{'line' if len(rows) == 1 else 'lines'} in your inventory "
                                                    f"{'has a date' if len(rows) == 1 else 'have dates'} set by a "
                                                    'carrier or a contract.'),
                    'numbers': rows, 'questions': questions, 'note': LINE_ADVICE_ONLY}
        return {'state': 'no_numbers', 'sentence': 'Faxbot knows none of your fax numbers yet, so there is nothing to '
                'advise on.', 'numbers': [], 'questions': questions, 'note': LINE_ADVICE_ONLY}
    npi = published_numbers(engine) if published is None else published
    placed = {row['number']: row for row in placement(engine, values, routes=routes, now=now, path=path,
                                                       published=npi)['numbers']}
    start = _records_start(engine)
    covered = max(0, (now - start).days) if start is not None else 0
    answers_store, moves = DependencyStore(engine), MoveStore(engine)
    rows = []
    for number, host in places:
        found = arrivals(engine, number, now - timedelta(days=YEAR_DAYS))
        last = found[-1][0] if found else None
        if last is None and start is not None:
            older = arrivals(engine, number, start)
            last = older[-1][0] if older else None
        recent = [item for item in found if item[0] >= now - timedelta(days=days)]
        senders = len({item[3] for item in recent if item[3]})
        quiet_since = max(item for item in (last, start) if item is not None) if (last or start) else None
        quiet_days = max(0, (now - quiet_since).days) if quiet_since is not None else 0
        evidence_npi = npi_evidence(npi, number)
        answers = answers_store.answers(number)
        removed, currency, removed_sentence = _removed(placed.get(number))
        placed_state = (placed.get(number) or {}).get('state')
        reasons = []
        if recent:
            verdict = 'move_termination' if placed_state == 'move' else 'keep'
        else:
            if covered < YEAR_DAYS:
                reasons.append(f"Faxbot's record of received faxes covers only {covered:,} days, less than a year, "
                               'so yearly or seasonal use cannot be ruled out.')
            if found:
                months = sorted({(at.year, at.month) for at, *_ in found})
                reasons.append(f"It received faxes in {_join([_month_label(*key) for key in months])} and none "
                               'since: that may be seasonal or yearly use.')
            if evidence_npi is not None and evidence_npi.get('state') == 'listed':
                reasons.append(evidence_npi['sentence'])
            for question, (label, _) in QUESTIONS.items():
                row = answers.get(question)
                if row is None or row['answer'] == 'unknown':
                    reasons.append(f'Not answered yet: {label}')
                elif row['answer'] == 'yes':
                    reasons.append(f'You answered yes to: {label}' + (f" ({row['note']})" if row['note'] else ''))
            if not removed:
                reasons.append(removed_sentence)
            if not reasons:
                verdict = 'can_likely_go'
            elif placed_state == 'move':
                verdict = 'move_termination'
            else:
                verdict = 'investigate'
        display = shown_number(number)
        sentence = {
            'keep': (f"Keep {display}: {len(recent)} {'fax' if len(recent) == 1 else 'faxes'} from {senders} "
                     f"{'sender' if senders == 1 else 'senders'} arrived in the last {days} days."),
            'move_termination': (f"Keep {display} but move it to {(placed.get(number) or {}).get('cheapest')}: the "
                                 'number stays the same for everyone who has it, and its line costs less there.'),
            'investigate': f'Find out more before giving up {display}.',
            'can_likely_go': (f'{display} can likely go: nothing arrived in over a year, nothing you answered needs '
                              f'it, and {removed_sentence[0].lower()}{removed_sentence[1:]}'),
        }[verdict]
        move, events = moves.current(number)
        move_state = None
        if move is not None:
            ended = outcome(events)
            move_state = {'state': ended or 'open',
                          'sentence': {'finished': 'Its move is finished.', 'abandoned': 'Its last move was abandoned.'}
                          .get(ended, 'A move is in progress.')}
        rows.append({
            'number': number, 'display': display, 'account': host.name, 'verdict': verdict,
            'verdict_label': VERDICTS[verdict], 'sentence': sentence, 'reasons': reasons,
            'evidence': {
                'last_arrival': last.isoformat() if last else None, 'last_arrival_text': _last_text(last, now),
                'senders': senders, 'window_days': days, 'covered_days': covered, 'months': _months(now, found),
                'npi_record': evidence_npi, 'quiet_bound': quiet_bound(quiet_days) if not recent else None,
                'removed': _money(removed, currency), 'removed_sentence': removed_sentence},
            'dependencies': dependency_rows(answers),
            'carrier_facts': carrier_facts(number, path=path),
            'move': move_state})
    _dated_first(engine, rows, {number for number, _ in places}, answers_store, now, path)
    counts = {verdict: sum(1 for row in rows if row['verdict'] == verdict) for verdict in VERDICTS}
    parts = [f'{counts[key]} {VERDICTS[key].lower()}' for key in VERDICTS if counts[key]]
    dated = sum(1 for row in rows if row['dates'])
    return {'state': 'advice', 'sentence': f"Your {len(rows)} {'number' if len(rows) == 1 else 'numbers'}: "
            + ', '.join(parts) + '.' + (f" {dated} {'has a date' if dated == 1 else 'have dates'} set by a carrier "
                                        'or a contract, listed first.' if dated else ''),
            'numbers': rows, 'questions': questions, 'note': LINE_ADVICE_ONLY}


def _dated_first(engine, rows, placed, answers_store, now, path):
    """Give every row its dates (``inventory.line_dates``), add the inventory's dated fax lines that no account
    receives on yet, and put dated rows first: passed, then soon, then later, earliest first."""
    from .inventory import plan_rows
    from .number_moves import dependency_rows
    from .receiving import shown_number
    dates, outside = plan_rows(engine, placed, today=now.date())
    for row in rows:
        row['dates'] = dates.get(row['number'], [])
    for line in outside:
        display = shown_number(line['number'])
        where = ', '.join(part for part in (line.get('street'), line.get('city'), line.get('region')) if part)
        rows.append({
            'number': line['number'], 'display': display, 'account': None, 'verdict': 'not_in_faxbot',
            'verdict_label': VERDICTS['not_in_faxbot'],
            'sentence': (f"{display} is a line in your inventory{' at ' + where if where else ''}"
                         f"{' with ' + line['carrier'] if line.get('carrier') else ''} that no Faxbot account "
                         'receives on yet.'),
            'reasons': [],
            'evidence': {'last_arrival': None, 'last_arrival_text': 'Faxbot does not receive faxes on this line yet.',
                         'senders': 0, 'window_days': None, 'covered_days': None, 'months': [], 'npi_record': None,
                         'quiet_bound': None, 'removed': None,
                         'removed_sentence': (f"Your inventory says it costs {line['monthly']} a month."
                                              if line.get('monthly') else
                                              'Your inventory gives no monthly price for this line.')},
            'dependencies': dependency_rows(answers_store.answers(line['number'])),
            'carrier_facts': carrier_facts(line['number'], path=path), 'move': None,
            'dates': dates.get(line['number'], [])})
    order = {id(row): index for index, row in enumerate(rows)}
    rows.sort(key=lambda row: (0, DATE_ORDER[row['dates'][0]['state']], row['dates'][0]['date'])
              if row['dates'] else (1, order[id(row)], ''))


def _last_text(last, now):
    if last is None:
        return 'No fax has arrived on it in the time Faxbot has records for.'
    from ..people_time import date_and_time
    return f'Its last fax arrived {(now - last).days:,} days ago, on {date_and_time(last)}.'
