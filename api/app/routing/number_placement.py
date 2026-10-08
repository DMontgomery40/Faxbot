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
